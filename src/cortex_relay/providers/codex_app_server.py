from __future__ import annotations

import json
import queue
import subprocess
import threading
import time
import uuid

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from cortex_relay.runtime.process import prepare_process_argv


class CodexAppServerError(RuntimeError):
    pass


@dataclass(frozen=True)
class CodexTurnResult:
    thread_id: str
    turn_id: str
    final_text: str
    usage: dict[str, Any]
    events: tuple[dict[str, Any], ...]
    stderr: str


class CodexAppServerClient:
    """Minimal stdlib JSON-RPC client for `codex app-server`.

    A fresh app-server process is used for each Cortex turn, but the Codex
    thread itself is durable and is resumed by thread ID. This keeps the
    provider session first-class without coupling CortexRelay to Codex's
    process lifetime.
    """

    def __init__(
        self,
        *,
        binary: str,
        cwd: Path,
        timeout_seconds: int,
        on_event: Callable[[str], None] | None = None,
    ) -> None:
        self.binary = binary
        self.cwd = cwd
        self.timeout_seconds = timeout_seconds
        self.on_event = on_event
        self._process: subprocess.Popen[str] | None = None
        self._stdout_queue: queue.Queue[str | BaseException | None] = queue.Queue()
        self._stderr: list[str] = []
        self._events: list[dict[str, Any]] = []
        self._reader: threading.Thread | None = None
        self._stderr_reader: threading.Thread | None = None

    def run_turn(
        self,
        *,
        prompt: str,
        thread_id: str | None,
        model: str | None,
        reasoning: str | None,
        sandbox: str,
        output_schema: dict[str, Any],
    ) -> CodexTurnResult:
        deadline = time.monotonic() + self.timeout_seconds
        self._start()
        try:
            self._request(
                "initialize",
                {
                    "clientInfo": {
                        "name": "cortex-relay",
                        "title": "CortexRelay",
                        "version": "1",
                    },
                    "capabilities": {"experimentalApi": True},
                },
                deadline,
            )
            self._notify("initialized", None)

            if thread_id:
                resumed = self._request(
                    "thread/resume",
                    {"threadId": thread_id},
                    deadline,
                )
                provider_thread = _thread_id(resumed) or thread_id
            else:
                started = self._request(
                    "thread/start",
                    _without_none(
                        {
                            "cwd": str(self.cwd),
                            "model": model,
                            "approvalPolicy": "never",
                            "sandbox": sandbox,
                            "threadSource": "appServer",
                        }
                    ),
                    deadline,
                )
                provider_thread = _thread_id(started)
                if not provider_thread:
                    raise CodexAppServerError(
                        "thread/start response did not contain a thread id"
                    )

            turn_params: dict[str, Any] = {
                "threadId": provider_thread,
                "input": [{"type": "text", "text": prompt}],
                "cwd": str(self.cwd),
                "approvalPolicy": "never",
                "outputSchema": output_schema,
            }
            if model:
                turn_params["model"] = model
            if reasoning and reasoning != "default":
                turn_params["effort"] = reasoning

            started_turn = self._request("turn/start", turn_params, deadline)
            turn_id = _turn_id(started_turn)
            if not turn_id:
                raise CodexAppServerError(
                    "turn/start response did not contain a turn id"
                )

            final_text = ""
            last_agent_text = ""
            usage: dict[str, Any] = {}
            failed: str | None = None

            while True:
                message = self._next_message(deadline)
                if not isinstance(message, dict):
                    continue

                method = message.get("method")
                if not isinstance(method, str):
                    # Late request responses are irrelevant after turn/start.
                    continue
                params = message.get("params")
                params = params if isinstance(params, dict) else {}
                self._record_notification(message)

                if method == "item/completed":
                    if params.get("turnId") != turn_id:
                        continue
                    item = params.get("item")
                    if isinstance(item, dict) and item.get("type") == "agentMessage":
                        text = item.get("text")
                        if isinstance(text, str):
                            last_agent_text = text
                            if item.get("phase") == "final_answer":
                                final_text = text

                elif method == "turn/completed":
                    turn = params.get("turn")
                    if isinstance(turn, dict):
                        if str(turn.get("id") or "") != turn_id:
                            continue
                        raw_usage = turn.get("usage")
                        if isinstance(raw_usage, dict):
                            usage = raw_usage
                        status = str(turn.get("status") or "").lower()
                        if status in {"failed", "error", "cancelled", "canceled"}:
                            err = turn.get("error")
                            failed = _message(err) or f"Codex turn ended with status {status}"
                    break

                elif method == "error":
                    failed = _message(params.get("error")) or _message(params) or "Codex app-server error"

            if failed:
                raise CodexAppServerError(failed)
            final_text = final_text or last_agent_text
            if not final_text:
                raise CodexAppServerError(
                    "Codex turn completed without an assistant final message"
                )
            return CodexTurnResult(
                thread_id=provider_thread,
                turn_id=turn_id,
                final_text=final_text,
                usage=usage,
                events=tuple(self._events),
                stderr="".join(self._stderr).strip(),
            )
        finally:
            self.close()

    def _start(self) -> None:
        if self._process is not None:
            return
        argv = prepare_process_argv(
            [self.binary, "app-server", "--listen", "stdio://"]
        )
        self._process = subprocess.Popen(
            argv,
            cwd=self.cwd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
        )
        self._reader = threading.Thread(target=self._read_stdout, daemon=True)
        self._stderr_reader = threading.Thread(target=self._read_stderr, daemon=True)
        self._reader.start()
        self._stderr_reader.start()

    def close(self) -> None:
        process = self._process
        self._process = None
        if process is None:
            return
        try:
            if process.stdin:
                process.stdin.close()
        except OSError:
            pass
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=2)
        if self._reader is not None:
            self._reader.join(timeout=0.2)
        if self._stderr_reader is not None:
            self._stderr_reader.join(timeout=0.2)

    def _read_stdout(self) -> None:
        assert self._process is not None and self._process.stdout is not None
        try:
            for line in self._process.stdout:
                self._stdout_queue.put(line)
        except BaseException as exc:
            self._stdout_queue.put(exc)
        finally:
            self._stdout_queue.put(None)

    def _read_stderr(self) -> None:
        assert self._process is not None and self._process.stderr is not None
        try:
            for line in self._process.stderr:
                self._stderr.append(line)
        except Exception:
            return

    def _request(
        self,
        method: str,
        params: dict[str, Any] | None,
        deadline: float,
    ) -> dict[str, Any]:
        request_id = str(uuid.uuid4())
        self._write(
            {
                "id": request_id,
                "method": method,
                **({"params": params} if params is not None else {}),
            }
        )
        while True:
            message = self._next_message(deadline)
            if not isinstance(message, dict):
                continue
            if message.get("id") == request_id:
                error = message.get("error")
                if error is not None:
                    raise CodexAppServerError(
                        f"{method} failed: {_message(error) or error}"
                    )
                result = message.get("result")
                if not isinstance(result, dict):
                    raise CodexAppServerError(
                        f"{method} returned a non-object result"
                    )
                return result
            if isinstance(message.get("method"), str):
                self._record_notification(message)

    def _notify(self, method: str, params: dict[str, Any] | None) -> None:
        self._write(
            {
                "method": method,
                **({"params": params} if params is not None else {}),
            }
        )

    def _write(self, payload: dict[str, Any]) -> None:
        if self._process is None or self._process.stdin is None:
            raise CodexAppServerError("Codex app-server is not running")
        self._process.stdin.write(
            json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n"
        )
        self._process.stdin.flush()

    def _next_message(self, deadline: float) -> dict[str, Any]:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise subprocess.TimeoutExpired(
                [self.binary, "app-server"], self.timeout_seconds
            )
        try:
            item = self._stdout_queue.get(timeout=remaining)
        except queue.Empty as exc:
            raise subprocess.TimeoutExpired(
                [self.binary, "app-server"], self.timeout_seconds
            ) from exc
        if item is None:
            stderr = "".join(self._stderr).strip()
            raise CodexAppServerError(
                stderr or "Codex app-server closed its stdout unexpectedly"
            )
        if isinstance(item, BaseException):
            raise CodexAppServerError(str(item))
        line = item.strip()
        if not line:
            return {}
        try:
            value = json.loads(line)
        except json.JSONDecodeError as exc:
            raise CodexAppServerError(
                f"Codex app-server emitted invalid JSON: {line[:200]}"
            ) from exc
        return value if isinstance(value, dict) else {}

    def _record_notification(self, message: dict[str, Any]) -> None:
        self._events.append(message)
        if self.on_event is not None:
            try:
                self.on_event(
                    json.dumps(
                        message,
                        ensure_ascii=False,
                        separators=(",", ":"),
                    )
                )
            except Exception:
                pass


def _thread_id(response: dict[str, Any]) -> str | None:
    thread = response.get("thread")
    if not isinstance(thread, dict):
        return None
    value = thread.get("id")
    return value.strip() if isinstance(value, str) and value.strip() else None


def _turn_id(response: dict[str, Any]) -> str | None:
    turn = response.get("turn")
    if not isinstance(turn, dict):
        return None
    value = turn.get("id")
    return value.strip() if isinstance(value, str) and value.strip() else None


def _without_none(value: dict[str, Any]) -> dict[str, Any]:
    return {key: item for key, item in value.items() if item is not None}


def _message(value: Any) -> str | None:
    if isinstance(value, str):
        text = value.strip()
        return text or None
    if isinstance(value, dict):
        for key in ("message", "error", "details"):
            text = _message(value.get(key))
            if text:
                return text
    return None
