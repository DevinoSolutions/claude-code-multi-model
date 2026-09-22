"""The hybrid-session gateway: a loopback server with two lines out.

Run on the developer's own machine (``python -m claude_code_hybrid.gateway``,
started by the ``claude-hybrid`` launcher) and point Claude Code at it with
``ANTHROPIC_BASE_URL=http://127.0.0.1:<port>`` and *no* credential
variable. Claude Code's gateway guide is explicit that this keeps the
subscription: "Setting only that variable, without a gateway credential,
doesn't replace the subscription. Requests still route through the
gateway, but a saved claude.ai login remains the active credential"
(https://code.claude.com/docs/en/llm-gateway, "Subscriptions and
gateways"). Every request then arrives here, and leaves on one of two
lines:

* **Anthropic line.** A Messages request for a ``claude-*`` model that
  carries its own credential (the subscription's OAuth bearer, or an
  Anthropic API key) goes to ``api.anthropic.com`` as it came: same path,
  query and body bytes, every header but host, hop-by-hop,
  ``accept-encoding`` and ``x-hybrid-*``. In particular ``anthropic-beta``
  goes verbatim: it "carries an OAuth capability that the upstream
  requires, and stripping it fails those requests with ``401``"
  (https://code.claude.com/docs/en/llm-gateway-protocol, "Request
  headers"). The token never leaves this machine for anywhere but
  Anthropic, and nothing here stores or logs it.
* **Upstream line.** Everything else (``kimi-k3``, ``glm-5.3``,
  ``deepseek-chat``, ``/v1/models``, the OpenAI dialects) goes to
  ``HYBRID_UPSTREAM_URL`` with headers rebuilt from nothing:
  ``content-type``, ``anthropic-version``, ``anthropic-beta``, ``accept``
  and your own ``HYBRID_UPSTREAM_KEY`` (as both ``Authorization: Bearer``
  and ``x-api-key``, since Anthropic-compatible endpoints differ on which
  they read). No ``x-claude-code-*`` header, no cookie, never the client's
  own Authorization; and the body's top-level ``metadata`` is removed,
  because Claude Code puts the claude.ai account and device ids there.

This process is deliberately thin: no providers, no catalog, no database,
no model keys beyond the one upstream key. It needs nothing but the two
``HYBRID_UPSTREAM_*`` variables and binds loopback only.

The routing key is the model name, because that is the only thing an
agent definition controls (``model:``); Claude Code never sends a
user-chosen agent name. ``x-claude-code-agent-id``, present "only on
requests from an agent Claude Code spawned inside the session", is logged
as ``class=main|subagent`` so the split is visible, and never routed on.

Prior art for the same split: https://github.com/1morr/subagent-handoff.
"""

from __future__ import annotations

import json
import logging
import os
import re
import secrets
import time
from typing import Iterable
from urllib.parse import urlsplit

import httpx
from fastapi import FastAPI, Request
from starlette.background import BackgroundTask
from starlette.responses import JSONResponse, Response, StreamingResponse

from claude_code_hybrid import passthrough as ap

log = logging.getLogger("claude_code_hybrid.gateway")

DEFAULT_PORT = 8290

# The only client headers the upstream line carries.
UPSTREAM_KEEP = frozenset({"content-type", "anthropic-version", "anthropic-beta", "accept"})

_ROUTED_PREFIXES = ("/v1/", "/api/v1/", "/anthropic/", "/openai/")
_MESSAGES_PATHS = frozenset({"/v1/messages", "/v1/messages/count_tokens",
                             "/anthropic/v1/messages", "/anthropic/v1/messages/count_tokens"})

# An upstream may name what served a request in its response headers
# (``x-hybrid-model`` / ``x-hybrid-provider``, or the ``x-orf-*`` pair some
# proxies use) or, for a stream whose headers went out before routing
# finished, in leading SSE comment lines. Read for the log only.
_DISCLOSURE = re.compile(rb"^: \w+ (x-(?:hybrid|orf)-[a-z-]+): (.*)$", re.MULTILINE)
_SNIFF_LIMIT = 16 * 1024
_SERVED_HEADERS = (("x-hybrid-model", "x-orf-model"), ("x-hybrid-provider", "x-orf-provider"))


def upstream_headers(headers: Iterable[tuple[str, str]], key: str) -> list[tuple[str, str]]:
    """The upstream line's headers, rebuilt from an empty set."""
    out = [(n, v) for n, v in headers if n.lower() in UPSTREAM_KEEP]
    out.append(("authorization", f"Bearer {key}"))
    out.append(("x-api-key", key))
    return out


def strip_metadata(body: bytes) -> bytes:
    """``body`` without its top-level ``metadata``; untouched when it has
    none or is not a JSON object."""
    if b'"metadata"' not in body:
        return body
    try:
        doc = json.loads(body)
    except ValueError:
        return body
    if not isinstance(doc, dict) or "metadata" not in doc:
        return body
    del doc["metadata"]
    return json.dumps(doc, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def request_class(headers) -> str:
    return "subagent" if headers.get("x-claude-code-agent-id") else "main"


def own_credential(headers, upstream_key: str) -> bool:
    """The request carries an Anthropic credential that is not ours: the
    subscription's OAuth bearer or a real Anthropic key. A client that was
    handed the upstream key itself gets the upstream line."""
    def ours(token: str) -> bool:
        return bool(upstream_key) and secrets.compare_digest(
            token.encode("utf-8"), upstream_key.encode("utf-8"))

    api_key = headers.get("x-api-key", "").strip()
    if api_key and not ours(api_key):
        return True
    parts = headers.get("authorization", "").split(None, 1)
    return len(parts) == 2 and parts[0].lower() == "bearer" \
        and bool(parts[1].strip()) and not ours(parts[1].strip())


def _model_of(body: bytes):
    try:
        doc = json.loads(body)
    except ValueError:
        return None
    return doc.get("model") if isinstance(doc, dict) else None


def _error(status: int, message: str) -> JSONResponse:
    return JSONResponse({"type": "error", "error": {"type": "api_error", "message": message}},
                        status_code=status)


def _served(out_headers: dict[str, str]) -> dict[str, str]:
    served = {}
    for canonical, alias in _SERVED_HEADERS:
        value = out_headers.get(canonical) or out_headers.get(alias)
        if value:
            served[canonical] = value
    return served


def create_gateway_app(upstream_url: str, upstream_key: str,
                       anthropic_base_url: str = "https://api.anthropic.com",
                       client: httpx.AsyncClient | None = None) -> FastAPI:
    upstream_url = upstream_url.rstrip("/")
    anthropic_base_url = anthropic_base_url.rstrip("/")
    app = FastAPI(title="claude-code-hybrid gateway", docs_url=None, redoc_url=None,
                  openapi_url=None)
    app.state.client = client

    def http() -> httpx.AsyncClient:
        if app.state.client is None:
            app.state.client = ap.make_client()
        return app.state.client

    @app.get("/health")
    async def health():
        return {"ok": True, "mode": "gateway",
                "upstream": urlsplit(upstream_url).netloc,
                "anthropic": urlsplit(anthropic_base_url).netloc}

    # Claude Code's connection-warming probe (gateway guide, "best-effort
    # startup traffic"); answered here rather than sent anywhere.
    @app.api_route("/api/hello", methods=["GET", "HEAD"])
    async def hello():
        return Response(status_code=200)

    @app.api_route("/{rest:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE"])
    async def route(request: Request, rest: str):
        path = request.url.path
        if not path.startswith(_ROUTED_PREFIXES):
            return _error(404, f"gateway does not route {path}")
        body = await request.body()
        query = request.url.query
        cls = request_class(request.headers)
        model = _model_of(body) if request.method == "POST" and path in _MESSAGES_PATHS \
            else None
        if ap.is_claude_model(model) and own_credential(request.headers, upstream_key):
            line = "anthropic"
            url = anthropic_base_url + ap.upstream_path(path)
            headers = ap.outbound_headers(request.headers.items())
            out_body = body
        else:
            line = "upstream"
            url = upstream_url + path
            headers = upstream_headers(request.headers.items(), upstream_key)
            out_body = strip_metadata(body)
        if query:
            url += f"?{query}"
        hints = " ".join(f"{name[14:]}={request.headers[name]}"
                         for name in ("x-claude-code-agent-type", "x-claude-code-request-class")
                         if name in request.headers)
        tag = f"class={cls} line={line} {request.method} {path} model={model or '-'}" \
              + (f" {hints}" if hints else "")
        return await _send(request.method, url, headers, out_body, line, tag,
                           str(model or ""))

    async def _send(method, url, headers, body, line, tag, model):
        started = time.monotonic()
        client = http()
        outgoing = client.build_request(method, url, headers=headers,
                                        content=body if body else None)
        log.info("gateway %s -> sent", tag)
        try:
            upstream = await client.send(outgoing, stream=True)
        except httpx.HTTPError as e:
            log.warning("gateway %s failed to connect: %s", tag, type(e).__name__)
            where = "Anthropic" if line == "anthropic" else "the upstream"
            return _error(502, f"gateway could not reach {where} ({type(e).__name__})")
        first_headers = time.monotonic() - started
        if line == "anthropic":
            out_headers = ap.inbound_headers(upstream.headers.multi_items(), model)
        else:
            out_headers = ap.relayable_response_headers(upstream.headers.multi_items())
        served = _served(out_headers)

        async def body_iter():
            head = b""
            size = 0
            outcome = "done"
            try:
                async for chunk in upstream.aiter_bytes():
                    size += len(chunk)
                    if len(head) < _SNIFF_LIMIT:
                        head += chunk[:_SNIFF_LIMIT - len(head)]
                    yield chunk
            except httpx.HTTPError as e:
                outcome = f"cut ({type(e).__name__})"
                raise
            finally:
                await upstream.aclose()
                for name, value in _DISCLOSURE.findall(head):
                    key = name.decode("ascii").replace("x-orf-", "x-hybrid-")
                    served.setdefault(key, value.decode("utf-8", "replace").strip())
                log.info("gateway %s -> %s status=%d served=%s provider=%s "
                         "headers_in=%.1fs total=%.1fs bytes=%d", tag, outcome,
                         upstream.status_code, served.get("x-hybrid-model", "-"),
                         served.get("x-hybrid-provider", "-"), first_headers,
                         time.monotonic() - started, size)

        return StreamingResponse(body_iter(), status_code=upstream.status_code,
                                 headers=out_headers,
                                 background=BackgroundTask(upstream.aclose))

    return app


def main() -> None:
    import uvicorn

    # HYBRID_LOG_FILE lets the launcher start this detached without
    # redirecting its output: a redirected child inherits the launcher's
    # own handles, and a caller reading the launcher through a pipe then
    # waits on the gateway forever.
    logging.basicConfig(level=logging.INFO, filename=os.environ.get("HYBRID_LOG_FILE") or None,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    upstream_url = os.environ.get("HYBRID_UPSTREAM_URL", "").strip()
    upstream_key = os.environ.get("HYBRID_UPSTREAM_KEY", "").strip()
    anthropic = os.environ.get("HYBRID_ANTHROPIC_URL", "https://api.anthropic.com").strip()
    for name, value in (("HYBRID_UPSTREAM_URL", upstream_url),
                        ("HYBRID_ANTHROPIC_URL", anthropic)):
        parts = urlsplit(value)
        if parts.scheme not in ("http", "https") or not parts.netloc:
            log.error("%s must be an http(s) URL, got %r", name, value)
            raise SystemExit(2)
    if not upstream_key:
        log.error("HYBRID_UPSTREAM_KEY is not set")
        raise SystemExit(2)
    port = int(os.environ.get("HYBRID_PORT", str(DEFAULT_PORT)))
    app = create_gateway_app(upstream_url, upstream_key, anthropic)
    log.info("gateway on 127.0.0.1:%d: claude-* -> %s, everything else -> %s",
             port, urlsplit(anthropic).netloc, urlsplit(upstream_url).netloc)
    # Loopback only, always: the Anthropic line relays whoever's OAuth
    # token arrives, so nothing off this machine may reach it.
    # log_config=None: uvicorn's records go to the root handler above (the
    # log file, when there is one) instead of its own stderr handlers.
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="info", access_log=False,
                log_config=None)


if __name__ == "__main__":
    main()
