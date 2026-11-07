import asyncio
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from openai import APIConnectionError, APIStatusError

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))
from agents.llm_caller import LLMCaller, _AsyncFailoverClient


def connection_error():
    return APIConnectionError(request=httpx.Request("POST", "https://example.invalid"))


def make_caller(outcomes, calls):
    class FakeCaller:
        def _endpoint_chain(self):
            return [("key", "primary", "model-a"), ("key", "backup", "model-b")]

        def _sdk_client(self, key, url, sync):
            async def create(**kwargs):
                calls.append((url, kwargs["model"]))
                value = outcomes[url].pop(0)
                if isinstance(value, Exception):
                    raise value
                return value

            return SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))

    return FakeCaller()


def test_dead_primary_fails_over_without_losing_response():
    calls = []
    caller = make_caller({"primary": [connection_error()], "backup": ["speech"]}, calls)
    assert asyncio.run(_AsyncFailoverClient(caller).create(model="ignored")) == "speech"
    assert calls == [("primary", "model-a"), ("backup", "model-b")]


def test_temporarily_unavailable_endpoints_are_retried(monkeypatch):
    calls, delays = [], []
    caller = make_caller({"primary": [connection_error(), "recovered"], "backup": [connection_error()]}, calls)

    async def fake_sleep(delay):
        delays.append(delay)

    monkeypatch.setattr("agents.llm_caller.asyncio.sleep", fake_sleep)
    assert asyncio.run(_AsyncFailoverClient(caller).create()) == "recovered"
    assert [url for url, _ in calls] == ["primary", "backup", "primary"]
    assert delays == [5]


def test_permanent_errors_do_not_retry_forever(monkeypatch):
    request = httpx.Request("POST", "https://example.invalid")
    response = httpx.Response(401, request=request)
    error = APIStatusError("unauthorized", response=response, body=None)
    calls = []
    caller = make_caller({"primary": [error], "backup": [error]}, calls)
    with pytest.raises(APIStatusError):
        asyncio.run(_AsyncFailoverClient(caller).create())
    assert len(calls) == 2


def test_transient_failures_stop_after_three_rounds(monkeypatch):
    calls, delays = [], []
    caller = make_caller({"primary": [connection_error() for _ in range(3)],
                          "backup": [connection_error() for _ in range(3)]}, calls)

    async def fake_sleep(delay):
        delays.append(delay)

    monkeypatch.setattr("agents.llm_caller.asyncio.sleep", fake_sleep)
    with pytest.raises(APIConnectionError):
        asyncio.run(_AsyncFailoverClient(caller).create())
    assert len(calls) == 6
    assert delays == [5, 10]


def test_async_sdk_connect_timeout_is_short_but_read_timeout_is_preserved():
    client = LLMCaller()._sdk_client("test-key", "https://example.invalid/v1", sync=False)
    assert client.max_retries == 0
    assert client.timeout.connect == 8.0
    assert client.timeout.read == 120.0


def test_sdk_uses_backup_after_connection_failure(monkeypatch):
    async def run():
        async def respond(reader, writer):
            headers = await reader.readuntil(b"\r\n\r\n")
            length = next(int(line.split(b":", 1)[1]) for line in headers.split(b"\r\n")
                          if line.lower().startswith(b"content-length:"))
            await reader.readexactly(length)
            body = json.dumps({"id": "test", "object": "chat.completion", "created": 1,
                               "model": "backup-model", "choices": [{"index": 0,
                               "finish_reason": "stop", "message": {"role": "assistant", "content": "有效发言"}}]}).encode()
            writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: "
                         + str(len(body)).encode() + b"\r\nConnection: close\r\n\r\n" + body)
            await writer.drain()
            writer.close()
            await writer.wait_closed()

        server = await asyncio.start_server(respond, "127.0.0.1", 0)
        backup_port = server.sockets[0].getsockname()[1]
        closed = await asyncio.start_server(lambda *_: None, "127.0.0.1", 0)
        dead_port = closed.sockets[0].getsockname()[1]
        closed.close()
        await closed.wait_closed()
        caller = LLMCaller()
        monkeypatch.setattr(caller, "_endpoint_chain", lambda: [
            ("key", f"http://127.0.0.1:{dead_port}/v1", "primary-model"),
            ("key", f"http://127.0.0.1:{backup_port}/v1", "backup-model"),
        ])
        try:
            result = await caller.async_client.chat.completions.create(
                messages=[{"role": "user", "content": "test"}], model="ignored"
            )
            assert result.choices[0].message.content == "有效发言"
            assert result.model == "backup-model"
        finally:
            server.close()
            await server.wait_closed()
            for client in caller._clients.values():
                await client.close()

    asyncio.run(run())
