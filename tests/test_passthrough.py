"""The Anthropic relay on its own: what leaves, what comes back, and that
a stream is relayed chunk by chunk."""

import logging

import httpx
import pytest

from claude_code_hybrid import passthrough

OAUTH = "sk-ant-oat01-owner-subscription-token"
CLAUDE = "claude-haiku-4-5-20251001"
HYBRID = {
    "Authorization": f"Bearer {OAUTH}",
    "anthropic-beta": "claude-code-20250219,oauth-2025-04-20,interleaved-thinking-2025-05-14",
    "anthropic-version": "2023-06-01",
    "anthropic-dangerous-direct-browser-access": "true",
    "x-app": "cli",
    "User-Agent": "claude-cli/9.9.9 (external, cli)",
    "content-type": "application/json",
    "accept": "application/json",
}
EDGE = {
    "cf-connecting-ip": "203.0.113.9", "cf-ray": "abc-YUL", "x-forwarded-for": "203.0.113.9",
    "x-real-ip": "203.0.113.9", "cdn-loop": "cloudflare", "cookie": "a=b",
    "x-hybrid-anything": "1",
}
SSE = (b'event: message_start\ndata: {"type":"message_start"}\n\n'
       b'event: content_block_delta\ndata: {"type":"content_block_delta","index":0,'
       b'"delta":{"type":"text_delta","text":"hi"}}\n\n'
       b'event: message_stop\ndata: {"type":"message_stop"}\n\n')
BODY = b'{ "stream": true,  "model":"' + CLAUDE.encode() + b'", "max_tokens":64,' \
       b'"messages":[{"role":"user","content":"hi \\u00e9"}] }'


class FakeAnthropic:
    def __init__(self, status=200, body=SSE, headers=None, chunks=None, fail=None):
        self.status, self.body, self.chunks, self.fail = status, body, chunks, fail
        self.headers = {"content-type": "text/event-stream", "request-id": "req_01abc",
                        "anthropic-ratelimit-unified-status": "allowed",
                        "cf-ray": "anthropic-edge-ray", "set-cookie": "__cf_bm=x",
                        **(headers or {})}
        self.requests: list[httpx.Request] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.fail:
            raise self.fail("boom", request=request)
        if self.chunks is not None:
            async def gen():
                for c in self.chunks:
                    yield c
            return httpx.Response(self.status, headers=self.headers, content=gen())
        return httpx.Response(self.status, headers=self.headers, content=self.body)

    def client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(self.handler))


async def _drain(response) -> bytes:
    return b"".join([c async for c in response.body_iterator])


@pytest.mark.asyncio
async def test_request_is_relayed_with_its_own_credential_and_without_the_edge():
    fake = FakeAnthropic()
    async with fake.client() as client:
        r = await passthrough.relay(client, "https://api.anthropic.com", "/v1/messages",
                                    "beta=true", list({**HYBRID, **EDGE}.items()), BODY, CLAUDE)
        assert await _drain(r) == SSE
    (sent,) = fake.requests
    assert str(sent.url) == "https://api.anthropic.com/v1/messages?beta=true"
    assert sent.content == BODY
    for name in ("authorization", "anthropic-beta", "anthropic-version",
                 "anthropic-dangerous-direct-browser-access", "x-app", "user-agent"):
        assert sent.headers[name] == {k.lower(): v for k, v in HYBRID.items()}[name], name
    for name in EDGE:
        assert name not in sent.headers, name
    assert r.status_code == 200
    assert r.headers["request-id"] == "req_01abc"
    assert r.headers["anthropic-ratelimit-unified-status"] == "allowed"
    assert "cf-ray" not in r.headers and "set-cookie" not in r.headers
    assert r.headers["x-hybrid-provider"] == "anthropic"
    assert r.headers["x-hybrid-model"] == CLAUDE


@pytest.mark.asyncio
async def test_stream_is_relayed_chunk_by_chunk():
    chunks = [SSE[:40], SSE[40:200], SSE[200:]]
    fake = FakeAnthropic(chunks=chunks)
    async with fake.client() as client:
        r = await passthrough.relay(client, "https://api.anthropic.com", "/v1/messages",
                                    "", list(HYBRID.items()), BODY, CLAUDE)
        got = [c async for c in r.body_iterator]
    assert got == chunks


@pytest.mark.asyncio
async def test_anthropic_errors_are_relayed_as_sent():
    err = b'{"type":"error","error":{"type":"rate_limit_error","message":"slow down"}}'
    fake = FakeAnthropic(status=429, body=err,
                         headers={"content-type": "application/json", "retry-after": "17"})
    async with fake.client() as client:
        r = await passthrough.relay(client, "https://api.anthropic.com", "/v1/messages",
                                    "", list(HYBRID.items()), BODY, CLAUDE)
        assert await _drain(r) == err
    assert r.status_code == 429 and r.headers["retry-after"] == "17"


@pytest.mark.asyncio
async def test_the_anthropic_prefix_maps_to_anthropics_path():
    fake = FakeAnthropic(body=b'{"input_tokens":42}', headers={"content-type": "application/json"})
    async with fake.client() as client:
        r = await passthrough.relay(client, "https://api.anthropic.com",
                                    "/anthropic/v1/messages/count_tokens", "beta=true",
                                    list(HYBRID.items()), BODY, CLAUDE)
        await _drain(r)
    assert str(fake.requests[0].url) == \
        "https://api.anthropic.com/v1/messages/count_tokens?beta=true"


@pytest.mark.asyncio
async def test_unreachable_anthropic_is_a_502_and_nothing_secret_is_logged(caplog):
    caplog.set_level(logging.DEBUG)
    fake = FakeAnthropic(fail=httpx.ConnectError)
    async with fake.client() as client:
        r = await passthrough.relay(client, "https://api.anthropic.com", "/v1/messages",
                                    "", list(HYBRID.items()), BODY, CLAUDE)
    assert r.status_code == 502
    assert "anthropic relay /v1/messages" in caplog.text
    for secret in (OAUTH, "hi \\u00e9"):
        assert secret not in caplog.text, secret


def test_outbound_headers_keep_repeats_and_drop_the_edge():
    out = passthrough.outbound_headers([
        ("anthropic-beta", "a"), ("anthropic-beta", "b"), ("host", "gateway.example"),
        ("content-length", "9"), ("connection", "keep-alive"), ("x-hybrid-model", "m"),
        ("CF-Visitor", "{}"), ("X-Forwarded-Host", "h"), ("accept-encoding", "gzip"),
        ("x-stainless-retry-count", "0")])
    assert out == [("anthropic-beta", "a"), ("anthropic-beta", "b"),
                   ("x-stainless-retry-count", "0")]


@pytest.mark.parametrize("model,expected", [
    ("claude-opus-5-5", True), ("claude-haiku-4-5-20251001", True), ("Claude-Sonnet-5", True),
    ("kimi-k3", False), ("opus", False), ("anthropic/claude-3", False), (None, False),
])
def test_is_claude_model(model, expected):
    assert passthrough.is_claude_model(model) is expected
