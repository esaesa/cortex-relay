"""Real-socket integration coverage for the A2A transport.

``tests/test_a2a.py`` exercises the FastAPI application through the in-process
ASGI test client. This module binds a loopback uvicorn server on an ephemeral
port and speaks HTTP to it the way a remote A2A client does, so routing,
version headers, SSE framing, and loopback-only binding are all verified over a
real transport.

The module is intentionally *not* an importable package member: ``unittest
discover -s tests`` skips it on base installs without the ``a2a`` extra, and the
dedicated ``a2a-integration`` CI job runs it explicitly.
"""

from __future__ import annotations

import http.client
import json
import socket
import tempfile
import threading
import time
import unittest
import uuid

from pathlib import Path
from typing import Any, Callable

from cortex_relay import __version__
from cortex_relay.core.models import TaskResult
from cortex_relay.transports.a2a import (
    A2AServerPolicy,
    a2a_available,
    create_a2a_app,
    run_a2a,
    validate_bind_host,
)


HOST = "127.0.0.1"
STARTUP_TIMEOUT_SECONDS = 20.0
HTTP_TIMEOUT_SECONDS = 30.0
STARTUP_ATTEMPTS = 3


class RecordingRegistry:
    """Scriptable stand-in for the provider registry behind the executor."""

    def __init__(self, *, block: bool = False) -> None:
        self.calls: list[Any] = []
        self.block = block
        self.started = threading.Event()
        self.cancelled = threading.Event()

    def capabilities(self) -> list[dict[str, Any]]:
        return []

    def execute(self, task: Any) -> TaskResult:
        self.calls.append(task)
        self.started.set()
        if self.block:
            cancel_event = task.metadata.get("_cancel_event")
            if cancel_event is None:
                raise AssertionError("a blocking task must carry a cancel event")
            cancel_event.wait(timeout=HTTP_TIMEOUT_SECONDS)
            self.cancelled.set()
            return TaskResult(
                status="cancelled",
                provider=task.provider,
                summary="cancelled through the transport",
                termination_reason="cancelled",
            )
        return TaskResult(
            status="success",
            provider=task.provider,
            summary="delegated through the transport",
            final_text="done",
            metadata={
                "objective": task.objective,
                "transport": task.metadata.get("transport"),
            },
        )


class LoopbackServer:
    """Run a FastAPI app on a loopback port in a background thread."""

    def __init__(self, app: Any, port: int) -> None:
        import uvicorn

        self._server = uvicorn.Server(
            uvicorn.Config(
                app,
                host=HOST,
                port=port,
                log_level="warning",
                lifespan="off",
            )
        )
        self._thread = threading.Thread(target=self._server.run, daemon=True)
        self.port: int | None = None
        self.bound_address: str | None = None

    @property
    def base_url(self) -> str:
        assert self.port is not None
        return f"http://{HOST}:{self.port}"

    def start(self) -> None:
        self._thread.start()
        deadline = time.monotonic() + STARTUP_TIMEOUT_SECONDS
        while not self._server.started:
            if time.monotonic() > deadline:
                raise AssertionError("loopback A2A server did not start in time")
            if not self._thread.is_alive():
                raise AssertionError("loopback A2A server exited during startup")
            time.sleep(0.05)
        sock = self._server.servers[0].sockets[0]
        self.port = int(sock.getsockname()[1])
        self.bound_address = str(sock.getsockname()[0])

    def stop(self) -> None:
        self._server.should_exit = True
        self._thread.join(timeout=STARTUP_TIMEOUT_SECONDS)

    def abort(self) -> None:
        self._server.should_exit = True


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind((HOST, 0))
        return int(probe.getsockname()[1])


def _start_server(app: Any, port: int) -> LoopbackServer:
    last_error: Exception | None = None
    for _ in range(STARTUP_ATTEMPTS):
        server = LoopbackServer(app, port)
        try:
            server.start()
        except Exception as exc:
            last_error = exc
            server.abort()
            continue
        return server
    raise AssertionError(f"loopback server failed to start: {last_error}")


def _request(
    base_url: str,
    method: str,
    path: str,
    *,
    payload: Any = None,
    headers: dict[str, str] | None = None,
    timeout: float = HTTP_TIMEOUT_SECONDS,
) -> tuple[int, dict[str, str], Any]:
    conn = http.client.HTTPConnection(HOST, _port_of(base_url), timeout=timeout)
    try:
        body = None if payload is None else json.dumps(payload)
        request_headers = dict(headers or {})
        if body is not None:
            request_headers.setdefault("Content-Type", "application/json")
        conn.request(method, path, body=body, headers=request_headers)
        response = conn.getresponse()
        raw = response.read()
        parsed = json.loads(raw) if raw else None
        return response.status, {k.lower(): v for k, v in response.getheaders()}, parsed
    finally:
        conn.close()


def _port_of(base_url: str) -> int:
    return int(base_url.rsplit(":", 1)[1])


def _open_stream(
    base_url: str,
    path: str,
    payload: Any,
    *,
    headers: dict[str, str] | None = None,
    timeout: float = HTTP_TIMEOUT_SECONDS,
) -> tuple[http.client.HTTPConnection, http.client.HTTPResponse]:
    conn = http.client.HTTPConnection(HOST, _port_of(base_url), timeout=timeout)
    request_headers = {"Content-Type": "application/json"}
    request_headers.update(headers or {})
    conn.request("POST", path, body=json.dumps(payload), headers=request_headers)
    response = conn.getresponse()
    if response.status != 200:
        raw = response.read()
        conn.close()
        raise AssertionError(
            f"stream {path} returned {response.status}: {raw.decode('utf-8', 'replace')}"
        )
    return conn, response


def _read_events(
    response: http.client.HTTPResponse,
    *,
    on_event: Callable[[dict[str, Any]], bool] | None = None,
    deadline_seconds: float = HTTP_TIMEOUT_SECONDS,
) -> list[dict[str, Any]]:
    """Collect server-sent ``data:`` frames until the stream closes or stops."""
    events: list[dict[str, Any]] = []
    limit = time.monotonic() + deadline_seconds
    while time.monotonic() < limit:
        try:
            line = response.readline()
        except OSError:
            break
        if not line:
            break
        text = line.decode("utf-8", "replace").strip()
        if not text.startswith("data:"):
            continue
        payload = json.loads(text[len("data:"):].strip())
        events.append(payload)
        if on_event is not None and on_event(payload):
            break
    return events


def _message(text: str) -> dict[str, Any]:
    return {
        "role": "user",
        "messageId": str(uuid.uuid4()),
        "parts": [{"type": "text", "text": text}],
    }


def _jsonrpc(method: str, params: dict[str, Any], request_id: str = "1") -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params}


@unittest.skipUnless(a2a_available(), "A2A optional dependency is not installed")
class A2ATransportIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls._tmp = tempfile.TemporaryDirectory()
        cls.workspace = Path(cls._tmp.name) / "workspace"
        cls.workspace.mkdir(parents=True, exist_ok=True)
        cls.registry = RecordingRegistry()
        cls.port = _free_port()
        cls.base_url = f"http://{HOST}:{cls.port}"
        cls.policy = A2AServerPolicy(
            provider="fake",
            workspace=cls.workspace,
            access="read_only",
        )
        cls.app = create_a2a_app(
            policy=cls.policy,
            registry=cls.registry,
            public_url=cls.base_url,
            name="cortex-relay",
        )
        cls.server = _start_server(cls.app, cls.port)

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.stop()
        cls._tmp.cleanup()

    def test_server_listens_on_loopback_only(self):
        self.assertEqual(self.server.bound_address, HOST)
        self.assertIsNotNone(self.server.port)
        self.assertNotEqual(self.server.port, 0)

        with self.assertRaises(RuntimeError):
            validate_bind_host("0.0.0.0", allow_remote=False)

    def test_health_and_agent_card_are_served_over_http(self):
        status, _, health = _request(self.base_url, "GET", "/healthz")
        self.assertEqual(status, 200)
        self.assertEqual(health["status"], "ok")
        self.assertEqual(health["version"], __version__)
        self.assertEqual(health["provider"], "fake")
        self.assertEqual(health["access"], "read_only")

        status, headers, card = _request(
            self.base_url, "GET", "/.well-known/agent-card.json"
        )
        self.assertEqual(status, 200)
        self.assertTrue(headers["content-type"].startswith("application/json"))
        self.assertEqual(card["name"], "cortex-relay")
        self.assertEqual(card["version"], __version__)

        bindings = {
            (item["protocolBinding"], item["protocolVersion"]): item["url"]
            for item in card["supportedInterfaces"]
        }
        self.assertIn(("JSONRPC", "1.0"), bindings)
        self.assertIn(("HTTP+JSON", "1.0"), bindings)
        self.assertTrue(bindings[("JSONRPC", "1.0")].startswith(self.base_url))
        self.assertTrue(bindings[("HTTP+JSON", "1.0")].startswith(self.base_url))

    def test_jsonrpc_message_send_completes_the_task(self):
        status, _, payload = _request(
            self.base_url,
            "POST",
            "/a2a/jsonrpc",
            payload=_jsonrpc("message/send", {"message": _message("Review auth")}),
        )

        self.assertEqual(status, 200)
        self.assertNotIn("error", payload)
        task = payload["result"]
        self.assertEqual(task["kind"], "task")
        self.assertEqual(task["status"]["state"], "completed")

        artifact = task["artifacts"][0]
        self.assertEqual(artifact["name"], "cortex-relay-result.json")
        result = json.loads(artifact["parts"][0]["text"])
        self.assertEqual(result["status"], "success")
        self.assertEqual(result["metadata"]["objective"], "Review auth")
        self.assertEqual(result["metadata"]["transport"], "a2a")

        executed = self.registry.calls[-1]
        self.assertEqual(executed.objective, "Review auth")
        self.assertEqual(executed.provider, "fake")
        self.assertEqual(executed.workspace, self.workspace)

    def test_jsonrpc_stream_reports_progress_and_result(self):
        conn, response = _open_stream(
            self.base_url,
            "/a2a/jsonrpc",
            _jsonrpc("message/stream", {"message": _message("Stream the review")}),
        )
        try:
            events = _read_events(response)
        finally:
            conn.close()

        results = [event["result"] for event in events if "result" in event]
        self.assertTrue(results, "stream produced no events")

        states = [
            result["status"]["state"]
            for result in results
            if result.get("kind") == "status-update" and "status" in result
        ]
        self.assertIn("working", states)

        artifacts = [result for result in results if result.get("kind") == "artifact-update"]
        self.assertTrue(artifacts, "stream produced no artifact event")
        streamed = json.loads(artifacts[0]["artifact"]["parts"][0]["text"])
        self.assertEqual(streamed["metadata"]["objective"], "Stream the review")

        final = results[-1]
        self.assertTrue(final.get("final"))
        self.assertEqual(final["status"]["state"], "completed")

    def test_rest_v1_message_send_completes_the_task(self):
        body = {
            "message": {
                "messageId": str(uuid.uuid4()),
                "role": "ROLE_USER",
                "parts": [{"text": "Inspect the parser"}],
            }
        }
        status, _, payload = _request(
            self.base_url,
            "POST",
            "/a2a/rest/message:send",
            payload=body,
            headers={"A2A-Version": "1.0"},
        )

        self.assertEqual(status, 200)
        task = payload["task"]
        self.assertEqual(task["status"]["state"], "TASK_STATE_COMPLETED")
        self.assertTrue(task["artifacts"])

        result = json.loads(task["artifacts"][0]["parts"][0]["text"])
        self.assertEqual(result["metadata"]["objective"], "Inspect the parser")
        self.assertEqual(result["metadata"]["transport"], "a2a")

    def test_rest_v1_requires_a_version_header(self):
        body = {
            "message": {
                "messageId": str(uuid.uuid4()),
                "role": "ROLE_USER",
                "parts": [{"text": "No version header"}],
            }
        }
        status, _, payload = _request(
            self.base_url,
            "POST",
            "/a2a/rest/message:send",
            payload=body,
        )
        self.assertEqual(status, 400)
        self.assertEqual(payload["error"]["status"], "FAILED_PRECONDITION")

    def test_empty_text_message_is_rejected_without_execution(self):
        executed = len(self.registry.calls)
        status, _, payload = _request(
            self.base_url,
            "POST",
            "/a2a/jsonrpc",
            payload=_jsonrpc(
                "message/send",
                {"message": _message("   ")},
                request_id="empty",
            ),
        )

        self.assertEqual(status, 200)
        self.assertNotIn("error", payload)
        task = payload["result"]
        self.assertEqual(task["status"]["state"], "rejected")
        self.assertIn(
            "non-empty text task",
            task["status"]["message"]["parts"][0]["text"],
        )
        self.assertEqual(len(self.registry.calls), executed)


@unittest.skipUnless(a2a_available(), "A2A optional dependency is not installed")
class A2ACancellationIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.workspace = Path(self._tmp.name) / "workspace"
        self.workspace.mkdir(parents=True, exist_ok=True)
        self.registry = RecordingRegistry(block=True)
        self.port = _free_port()
        self.base_url = f"http://{HOST}:{self.port}"
        app = create_a2a_app(
            policy=A2AServerPolicy(provider="fake", workspace=self.workspace),
            registry=self.registry,
            public_url=self.base_url,
            name="cortex-relay",
        )
        self.server = _start_server(app, self.port)

    def tearDown(self) -> None:
        self.server.stop()
        self._tmp.cleanup()

    def test_cancelling_a_running_task_reaches_the_provider(self):
        events: list[dict[str, Any]] = []
        task_ids: list[str] = []
        opened = threading.Event()
        reader_error: list[BaseException] = []

        def read() -> None:
            conn = None
            try:
                conn, response = _open_stream(
                    self.base_url,
                    "/a2a/jsonrpc",
                    _jsonrpc("message/stream", {"message": _message("Long review")}),
                )
                opened.set()

                def capture(event: dict[str, Any]) -> bool:
                    events.append(event)
                    result = event.get("result") or {}
                    task_id = result.get("id")
                    if isinstance(task_id, str) and task_id not in task_ids:
                        task_ids.append(task_id)
                    return bool(result.get("final"))

                _read_events(response, on_event=capture)
            except BaseException as exc:  # pragma: no cover - surfaced below
                reader_error.append(exc)
            finally:
                opened.set()
                if conn is not None:
                    conn.close()

        reader = threading.Thread(target=read, daemon=True)
        reader.start()

        deadline = time.monotonic() + STARTUP_TIMEOUT_SECONDS
        while not task_ids and time.monotonic() < deadline:
            if reader_error:
                raise reader_error[0]
            time.sleep(0.05)
        self.assertTrue(task_ids, "stream never reported a task id")
        self.assertTrue(
            self.registry.started.wait(STARTUP_TIMEOUT_SECONDS),
            "provider execution never started",
        )

        status, _, _payload = _request(
            self.base_url,
            "POST",
            "/a2a/jsonrpc",
            payload=_jsonrpc("tasks/cancel", {"id": task_ids[0]}, request_id="cancel"),
        )
        self.assertEqual(status, 200)

        self.assertTrue(
            self.registry.cancelled.wait(STARTUP_TIMEOUT_SECONDS),
            "cancellation never reached the provider call",
        )
        reader.join(timeout=STARTUP_TIMEOUT_SECONDS)
        self.assertFalse(reader.is_alive())
        self.assertEqual(reader_error, [])

        results = [event["result"] for event in events if "result" in event]
        self.assertTrue(results)
        self.assertTrue(results[-1].get("final"))
        self.assertEqual(results[-1]["status"]["state"], "canceled")


@unittest.skipUnless(a2a_available(), "A2A optional dependency is not installed")
class A2ABindPolicyIntegrationTests(unittest.TestCase):
    def test_remote_bind_is_refused_before_listening(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(RuntimeError):
                run_a2a(
                    policy=A2AServerPolicy(provider="fake", workspace=Path(tmp)),
                    host="0.0.0.0",
                    port=_free_port(),
                )


if __name__ == "__main__":
    unittest.main()
