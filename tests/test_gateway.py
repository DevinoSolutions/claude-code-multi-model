"""The gateway: two lines out, and what each one may carry. The outbound
hop is an httpx MockTransport answering for both api.anthropic.com and the
upstream."""

import json
import logging
import os
import socket
import subprocess
import sys
import time

import httpx
import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from claude_code_hybrid.gateway import create_gateway_app, strip_metadata

UPSTREAM = "https://upstream.example"
UPSTREAM_KEY = "upstream_key_123"
OAUTH = "sk-ant-oat01-owner-subscription-token"
METADATA = {"user_id": json.dumps({"device_id": "dev-123", "account_uuid": "acct-456",
                                   "session_id": "s-1"})}
BASE = {
    "Authorization": f"Bearer {OAUTH}",
    "anthropic-beta": "claude-code-20250219,oauth-2025-04-20",
    "anthropic-version": "2023-06-01",
    "anthropic-dangerous-direct-browser-access": "true",
    "x-app": "cli",
    "User-Agent": "claude-cli/2.1.280 (external, cli)",
    "x-claude-code-session-id": "sess-1",
    "content-type": "application/json",
    "accept": "application/json",
    "cookie": "a=b",
}
SUBAGENT = {**BASE, "x-claude-code-agent-id": "agent-7", "x-claude-code-agent-type": "custom",
            "x-claude-code-request-class": "subagent", "x-claude-code-parent-agent-id": "p"}
SSE = b'event: message_stop\ndata: {"type":"message_stop"}\n\n'


def _body(model: str) -> bytes:
    return json.dumps({"model": model, "max_tokens": 32, "stream": True,
                       "metadata": METADATA,
                       "messages": [{"role": "user", "content": "hi"}]},
                      indent=1).encode()


class Recorder:
    def __init__(self, upstream_headers=None, upstream_body=SSE):
        self.requests: list[httpx.Request] = []
        self.upstream_headers = upstream_headers or {}
        self.upstream_body = upstream_body

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.url.host == "api.anthropic.com":
            return httpx.Response(200, content=SSE, headers={
                "content-type": "text/event-stream", "request-id": "req_1"})
        return httpx.Response(200, content=self.upstream_body, headers={
            "content-type": "text/event-stream", **self.upstream_headers})


@pytest_asyncio.fixture
async def gw():
    rec = Recorder()
    client = httpx.AsyncClient(transport=httpx.MockTransport(rec.handler))
    app = create_gateway_app(UPSTREAM, UPSTREAM_KEY, client=client)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://127.0.0.1") as c:
        c.rec = rec
        yield c
    await client.aclose()


@pytest.mark.asyncio
async def test_main_claude_turn_takes_the_anthropic_line_untouched(gw):
    body = _body("claude-opus-5-5")
    r = await gw.post("/v1/messages?beta=true", content=body, headers=BASE)
    assert r.status_code == 200 and r.content == SSE
    assert r.headers["x-hybrid-provider"] == "anthropic"
    (sent,) = gw.rec.requests
    assert str(sent.url) == "https://api.anthropic.com/v1/messages?beta=true"
    assert sent.content == body                      # metadata and all
    assert sent.headers["authorization"] == f"Bearer {OAUTH}"
    assert sent.headers["anthropic-beta"] == BASE["anthropic-beta"]
    assert sent.headers["x-claude-code-session-id"] == "sess-1"
    assert "cookie" not in sent.headers
    assert UPSTREAM_KEY.encode() not in repr(sent.headers.multi_items()).encode()


@pytest.mark.asyncio
async def test_subagent_turn_takes_the_upstream_line_with_our_key_only(gw):
    """The upstream-line guarantees, pinned together: (a) the client's
    Authorization value never goes out, (b) no x-claude-code-* header
    does, (c) the body's metadata is gone, while (d) the Anthropic line
    keeps it."""
    r = await gw.post("/v1/messages?beta=true", content=_body("kimi-k3"), headers=SUBAGENT)
    assert r.status_code == 200
    (sent,) = gw.rec.requests
    assert str(sent.url) == f"{UPSTREAM}/v1/messages?beta=true"
    wire = repr(sent.headers.multi_items()).encode() + sent.content
    assert OAUTH.encode() not in wire                                            # (a)
    assert sent.headers["authorization"] == f"Bearer {UPSTREAM_KEY}"
    assert sent.headers["x-api-key"] == UPSTREAM_KEY
    assert not [n for n in sent.headers if n.lower().startswith("x-claude-code-")]  # (b)
    assert "cookie" not in sent.headers
    # The client's four, our two credentials, and what httpx itself adds
    # for the hop (host, length, its own user-agent, connection, encoding).
    assert set(k.lower() for k in sent.headers) <= {
        "content-type", "anthropic-version", "anthropic-beta", "accept", "authorization",
        "x-api-key", "host", "content-length", "user-agent", "connection", "accept-encoding"}
    assert sent.headers["user-agent"].startswith("python-httpx")
    sent_body = json.loads(sent.content)
    assert "metadata" not in sent_body                                           # (c)
    assert b"acct-456" not in sent.content and b"dev-123" not in sent.content
    assert sent_body["model"] == "kimi-k3" and sent_body["messages"][0]["content"] == "hi"

    gw.rec.requests.clear()
    await gw.post("/v1/messages", content=_body("claude-haiku-4-5-20251001"), headers=BASE)
    assert json.loads(gw.rec.requests[0].content)["metadata"] == METADATA        # (d)


@pytest.mark.asyncio
async def test_upstream_answer_is_relayed_with_its_disclosure_headers():
    rec = Recorder(upstream_headers={"x-hybrid-model": "moonshotai/kimi-k3",
                                     "x-hybrid-provider": "nim", "content-length": "999",
                                     "cf-ray": "edge"})
    client = httpx.AsyncClient(transport=httpx.MockTransport(rec.handler))
    app = create_gateway_app(UPSTREAM, UPSTREAM_KEY, client=client)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://127.0.0.1") as c:
        r = await c.post("/v1/messages", content=_body("kimi-k3"), headers=SUBAGENT)
    await client.aclose()
    assert r.content == SSE
    assert r.headers["x-hybrid-provider"] == "nim"
    assert r.headers["x-hybrid-model"] == "moonshotai/kimi-k3"
    assert "cf-ray" not in r.headers


@pytest.mark.asyncio
@pytest.mark.parametrize("method,path", [
    ("GET", "/v1/models"), ("POST", "/api/v1/chat/completions"),
    ("POST", "/openai/v1/responses"), ("POST", "/anthropic/v1/messages"),
])
async def test_every_other_path_goes_upstream_verbatim(gw, method, path):
    body = b'{"model":"glm-5.3","messages":[]}' if method == "POST" else None
    r = await gw.request(method, path + "?x=1", content=body, headers=BASE)
    assert r.status_code == 200
    (sent,) = gw.rec.requests
    assert str(sent.url) == f"{UPSTREAM}{path}?x=1"
    assert sent.method == method
    assert sent.headers["authorization"] == f"Bearer {UPSTREAM_KEY}"
    if body:
        assert sent.content == body


@pytest.mark.asyncio
async def test_a_client_holding_the_upstream_key_gets_the_upstream_line(gw):
    """A client whose only credential is the upstream key: claude-* is
    whatever the upstream calls that, not Anthropic."""
    r = await gw.post("/v1/messages", content=_body("claude-opus-5-5"),
                      headers={"x-api-key": UPSTREAM_KEY, "content-type": "application/json"})
    assert r.status_code == 200
    assert gw.rec.requests[0].url.host == "upstream.example"


@pytest.mark.asyncio
async def test_committed_stream_disclosure_lands_in_the_log(caplog):
    body = b": ORF x-orf-model: moonshotai/kimi-k3\n: ORF x-orf-provider: nim\n\n" + SSE
    rec = Recorder(upstream_body=body)
    client = httpx.AsyncClient(transport=httpx.MockTransport(rec.handler))
    app = create_gateway_app(UPSTREAM, UPSTREAM_KEY, client=client)
    caplog.set_level(logging.INFO, logger="claude_code_hybrid.gateway")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://127.0.0.1") as c:
        await c.post("/v1/messages?beta=true", content=_body("kimi-k3"), headers=SUBAGENT)
        await c.post("/v1/messages?beta=true", content=_body("claude-opus-5-5"), headers=BASE)
    await client.aclose()
    text = caplog.text
    assert "class=subagent line=upstream POST /v1/messages model=kimi-k3 " \
           "agent-type=custom request-class=subagent -> done status=200 " \
           "served=moonshotai/kimi-k3 provider=nim" in text
    assert "class=main line=anthropic POST /v1/messages model=claude-opus-5-5 -> done " \
           "status=200 served=claude-opus-5-5 provider=anthropic" in text
    for secret in (OAUTH, UPSTREAM_KEY, "acct-456"):
        assert secret not in text


@pytest.mark.asyncio
async def test_health_probe_and_unrouted_paths(gw):
    h = await gw.get("/health")
    assert h.json() == {"ok": True, "mode": "gateway", "upstream": "upstream.example",
                        "anthropic": "api.anthropic.com"}
    assert (await gw.head("/api/hello")).status_code == 200
    assert (await gw.get("/api/admin/health")).status_code == 404
    assert gw.rec.requests == []


@pytest.mark.asyncio
async def test_unreachable_upstream_is_a_502():
    def boom(request):
        raise httpx.ConnectError("down", request=request)
    client = httpx.AsyncClient(transport=httpx.MockTransport(boom))
    app = create_gateway_app(UPSTREAM, UPSTREAM_KEY, client=client)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://127.0.0.1") as c:
        r = await c.post("/v1/messages", content=_body("kimi-k3"), headers=SUBAGENT)
    await client.aclose()
    assert r.status_code == 502 and r.json()["type"] == "error"


def test_strip_metadata_leaves_other_bodies_alone():
    assert strip_metadata(b"not json") == b"not json"
    assert strip_metadata(b'{"model":"x"}') == b'{"model":"x"}'
    assert strip_metadata(b'["metadata"]') == b'["metadata"]'
    assert json.loads(strip_metadata(b'{"metadata":{"a":1},"model":"x"}')) == {"model": "x"}


def test_gateway_boots_with_nothing_but_its_two_variables(tmp_path, tmp_path_factory):
    """No config file, no other keys: the gateway still starts, binds
    loopback, answers /health, writes nothing where it runs, and logs to
    HYBRID_LOG_FILE without the key in it."""
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    log_file = tmp_path_factory.mktemp("log") / "gateway.log"
    env = {k: v for k, v in os.environ.items()
           if k in ("SYSTEMROOT", "PATH", "TEMP", "TMP", "HOME", "USERPROFILE")}
    env.update(HYBRID_UPSTREAM_URL=UPSTREAM, HYBRID_UPSTREAM_KEY=UPSTREAM_KEY,
               HYBRID_PORT=str(port), PYTHONPATH=repo, HYBRID_LOG_FILE=str(log_file))
    proc = subprocess.Popen([sys.executable, "-m", "claude_code_hybrid.gateway"], cwd=tmp_path,
                            env=env, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    try:
        deadline = time.monotonic() + 30
        while True:
            try:
                r = httpx.get(f"http://127.0.0.1:{port}/health", timeout=1)
                break
            except httpx.HTTPError:
                if proc.poll() is not None or time.monotonic() > deadline:
                    raise AssertionError(proc.stderr.read().decode(errors="replace")[-2000:])
                time.sleep(0.2)
        assert r.json()["mode"] == "gateway"
        assert list(tmp_path.iterdir()) == []
    finally:
        proc.terminate()
        proc.wait(timeout=10)
    text = log_file.read_text()
    assert "gateway on 127.0.0.1" in text and "Uvicorn running" in text
    assert UPSTREAM_KEY not in text
