"""Real Claude, relayed: the Anthropic line of a multi-model session.

A multi-model session is Claude Code on its owner's own Anthropic login (the
subscription's OAuth token, or an Anthropic API key) with the gateway as its
``ANTHROPIC_BASE_URL``. One Claude Code process talks to exactly one base
URL and its subagents inherit it, so the only way for a Claude main loop to
spawn a ``model: kimi-k3`` subagent natively is for that one endpoint to
serve both: the other models, routed to your upstream, and real Claude,
relayed here to Anthropic untouched.

What this module promises, and the tests hold it to:

* The body goes out byte for byte, and the answer comes back as it came,
  streamed as it arrives -- status, SSE events and Anthropic's own headers
  (``request-id``, ``anthropic-ratelimit-*``, ``retry-after``) included.
* The client's own credential goes out with it. The gateway holds no
  Anthropic credential and needs none; it forwards what the caller sent
  and keeps nothing.
* Hop-by-hop headers and whatever an edge might have added (``cf-*``,
  ``x-forwarded-*``) do not go out.
* Nothing here logs a header value or a body. The log line names the path,
  the model and the status.
* No deadline of its own: a long Claude turn ends when Anthropic ends it or
  the client walks away. Only the connect is bounded.
"""

from __future__ import annotations

import logging
import time
from typing import Iterable

import httpx
from starlette.background import BackgroundTask
from starlette.responses import JSONResponse, StreamingResponse

log = logging.getLogger("claude_code_multi_model.passthrough")

# What the response's ``x-multimodel-provider`` header says for a relayed request.
PROVIDER = "anthropic"

_HOP_BY_HOP = frozenset({
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "proxy-connection", "te", "trailer", "trailers", "transfer-encoding", "upgrade",
})

# Request headers that stay here. ``host`` and ``content-length`` are
# rebuilt by the client for the new hop; ``accept-encoding`` is left to it
# too, so the body can be decoded and relayed without a content-encoding
# this hop would otherwise have to keep straight. The rest are an edge's.
_DROP_REQUEST = _HOP_BY_HOP | {
    "host", "content-length", "accept-encoding", "cookie",
    "forwarded", "via", "x-real-ip", "true-client-ip", "cdn-loop",
}
_DROP_REQUEST_PREFIXES = ("cf-", "x-forwarded-", "x-multimodel-")

# Response headers that stay at Anthropic's end: this hop frames the body
# itself (decoded, chunked), stamps its own date and server, and cookies
# are Anthropic's edge's and mean nothing to a CLI.
_DROP_RESPONSE = _HOP_BY_HOP | {
    "content-length", "content-encoding", "date", "server", "set-cookie", "alt-svc",
}
_DROP_RESPONSE_PREFIXES = ("cf-",)


def is_claude_model(model) -> bool:
    """A model id that names real Claude: ``claude-opus-5-5``,
    ``claude-haiku-4-5-20251001``. Claude Code resolves its ``opus`` /
    ``sonnet`` / ``haiku`` aliases to these full ids before it sends, so
    the prefix is the whole test. ``anthropic/claude-...`` is an exact id
    some upstream might serve, and is not this."""
    return isinstance(model, str) and model.strip().lower().startswith("claude-")


def outbound_headers(headers: Iterable[tuple[str, str]]) -> list[tuple[str, str]]:
    """The client's headers as they go to Anthropic. A list, not a dict:
    a repeated header (``anthropic-beta`` may be) goes out repeated."""
    out = []
    for name, value in headers:
        low = name.lower()
        if low in _DROP_REQUEST or low.startswith(_DROP_REQUEST_PREFIXES):
            continue
        out.append((name, value))
    return out


def relayable_response_headers(headers: Iterable[tuple[str, str]]) -> dict[str, str]:
    """An upstream's response headers minus what this hop re-frames or
    must not repeat (see ``_DROP_RESPONSE``)."""
    out = {}
    for name, value in headers:
        low = name.lower()
        if low in _DROP_RESPONSE or low.startswith(_DROP_RESPONSE_PREFIXES):
            continue
        out[low] = value
    return out


def inbound_headers(headers: Iterable[tuple[str, str]], model: str) -> dict[str, str]:
    """Anthropic's response headers as they go back to the client, plus the
    two ``x-multimodel-*`` headers that say which line answered."""
    out = relayable_response_headers(headers)
    safe_model = "".join(c if 32 <= ord(c) <= 126 else "?" for c in model)[:160]
    out["x-multimodel-model"] = safe_model
    out["x-multimodel-provider"] = PROVIDER
    return out


def upstream_path(path: str) -> str:
    """``/anthropic/v1/messages`` is a prefix some proxies use for the same
    resource; Anthropic knows only ``/v1/messages``."""
    return path[len("/anthropic"):] if path.startswith("/anthropic/") else path


def make_client() -> httpx.AsyncClient:
    """The shared outbound client. Connect is bounded so an unreachable
    Anthropic fails fast; read, write and pool are not, because a Claude
    turn that thinks for minutes between events is a normal turn, and the
    client (Claude Code's own API timeout) is the one who decides when it
    has waited long enough."""
    return httpx.AsyncClient(
        timeout=httpx.Timeout(connect=30.0, read=None, write=None, pool=None),
        limits=httpx.Limits(max_connections=None, max_keepalive_connections=32),
        follow_redirects=False)


def _error(status: int, message: str) -> JSONResponse:
    return JSONResponse({"type": "error",
                         "error": {"type": "api_error", "message": message}},
                        status_code=status,
                        headers={"x-multimodel-provider": PROVIDER})


async def relay(client: httpx.AsyncClient, base_url: str, path: str, query: str,
                headers: Iterable[tuple[str, str]], body: bytes, model: str):
    """Send the request on to Anthropic and hand back its answer as a
    stream. The upstream response is closed when the stream ends, when the
    client disconnects, or -- if the body is never read -- by the
    response's background task."""
    target = upstream_path(path)
    url = base_url.rstrip("/") + target + (f"?{query}" if query else "")
    started = time.monotonic()
    request = client.build_request("POST", url, headers=outbound_headers(headers),
                                   content=body)
    try:
        upstream = await client.send(request, stream=True)
    except httpx.HTTPError as e:
        # The exception's text can carry the URL, never a header; the type
        # is all the log needs.
        log.warning("anthropic relay %s model=%s failed to connect: %s",
                    target, model, type(e).__name__)
        return _error(502, f"gateway could not reach Anthropic ({type(e).__name__})")
    log.info("anthropic relay %s model=%s status=%d headers_in=%.2fs",
             target, model, upstream.status_code, time.monotonic() - started)

    async def body_iter():
        try:
            async for chunk in upstream.aiter_bytes():
                yield chunk
        except httpx.HTTPError as e:
            log.warning("anthropic relay %s model=%s cut mid-body: %s",
                        target, model, type(e).__name__)
            raise
        finally:
            await upstream.aclose()

    return StreamingResponse(body_iter(), status_code=upstream.status_code,
                             headers=inbound_headers(upstream.headers.multi_items(), model),
                             background=BackgroundTask(upstream.aclose))
