"""One gateway process serves every Claude Code session on the machine.

Fifty sessions, each with subagents, must not mean fifty gateways, and the
one gateway must not serialise their streams."""

import asyncio
import json
import time

import httpx
import pytest
from httpx import ASGITransport, AsyncClient

from claude_code_multi_model import gateway
from claude_code_multi_model.gateway import create_gateway_app

UPSTREAM = "https://upstream.example"
CHUNKS, CHUNK_DELAY = 3, 0.2          # one upstream stream lasts ~0.6 s


async def _slow_sse():
    for _ in range(CHUNKS):
        await asyncio.sleep(CHUNK_DELAY)
        yield b'event: ping\ndata: {"type":"ping"}\n\n'


def _handler(request: httpx.Request) -> httpx.Response:
    return httpx.Response(200, content=_slow_sse(),
                          headers={"content-type": "text/event-stream"})


@pytest.mark.asyncio
async def test_sixty_four_sessions_stream_through_one_gateway_at_once():
    client = httpx.AsyncClient(transport=httpx.MockTransport(_handler))
    app = create_gateway_app(UPSTREAM, "k", client=client)
    body = json.dumps({"model": "kimi-k3", "max_tokens": 8, "stream": True,
                       "messages": [{"role": "user", "content": "hi"}]}).encode()
    headers = {"content-type": "application/json", "x-claude-code-agent-id": "a",
               "x-claude-code-request-class": "subagent"}
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://127.0.0.1") as c:
        t0 = time.monotonic()
        responses = await asyncio.gather(*(
            c.post("/v1/messages", content=body, headers={**headers, "x-claude-code-session-id": f"s{i}"})
            for i in range(64)))
        wall = time.monotonic() - t0
    await client.aclose()
    assert all(r.status_code == 200 for r in responses)
    assert all(r.content.count(b"event: ping") == CHUNKS for r in responses)
    serial = 64 * CHUNKS * CHUNK_DELAY          # ~38 s if the streams queued
    assert wall < serial / 8, f"64 streams took {wall:.1f}s; they are being serialised"


def test_a_second_gateway_sees_the_first_and_stands_down(monkeypatch):
    class Up:
        def json(self):
            return {"ok": True, "mode": "gateway"}
    monkeypatch.setattr(gateway.httpx, "get", lambda url, timeout: Up())
    assert gateway.already_running(8290) is True

    def refused(url, timeout):
        raise httpx.ConnectError("refused")
    monkeypatch.setattr(gateway.httpx, "get", refused)
    assert gateway.already_running(8290) is False

    class Other:                                   # something else on the port
        def json(self):
            return {"status": "fine"}
    monkeypatch.setattr(gateway.httpx, "get", lambda url, timeout: Other())
    assert gateway.already_running(8290) is False


def test_main_exits_without_binding_when_a_gateway_is_up(monkeypatch):
    monkeypatch.setenv("MULTI_MODEL_UPSTREAM_URL", UPSTREAM)
    monkeypatch.setenv("MULTI_MODEL_UPSTREAM_KEY", "k")
    monkeypatch.setattr(gateway, "already_running", lambda port: True)
    monkeypatch.setattr(gateway, "create_gateway_app",
                        lambda *a, **k: pytest.fail("must not build an app"))
    gateway.main()                                 # returns instead of uvicorn.run
