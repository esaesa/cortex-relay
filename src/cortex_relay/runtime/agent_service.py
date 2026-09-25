from __future__ import annotations

from pathlib import Path
from typing import Any

from cortex_relay.core.models import TaskSpec
from cortex_relay.core.registry import ProviderRegistry
from cortex_relay.runtime.agent_events import normalize_agent_events


class AgentService:
    """Provider-neutral control surface for live or resumable agent sessions."""

    def __init__(self, registry: ProviderRegistry) -> None:
        self.registry = registry
        self.store = registry.agent_store

    def start(
        self,
        *,
        objective: str,
        role: str = "reviewer",
        profile: str | None = None,
        preset: str | None = None,
        provider: str = "auto",
        workspace: str | Path = ".",
        access: str = "read_only",
        reasoning: str = "high",
        model: str | None = None,
        timeout_seconds: int = 300,
        parent_session_id: str | None = None,
    ) -> dict[str, Any]:
        """Start a direct persistent provider-backed agent session.

        Session-first starts intentionally bypass task/DAG/worktree orchestration.
        Use delegate/delegate_async when those workflow guarantees are required.
        """
        if not objective.strip():
            raise ValueError("objective must not be empty")
        if access not in {"read_only", "workspace_write"}:
            raise ValueError("access must be read_only or workspace_write")
        if timeout_seconds < 1:
            raise ValueError("timeout_seconds must be positive")

        resolved_workspace = Path(workspace).expanduser().resolve()
        metadata: dict[str, Any] = {}
        if parent_session_id is not None:
            parent = self.store.get(parent_session_id)
            if parent.workspace.resolve() != resolved_workspace:
                raise ValueError(
                    "parent agent session must use the same workspace"
                )
            metadata["_parent_agent_session_id"] = parent_session_id

        task = TaskSpec(
            objective=objective,
            role=role,
            profile=profile,
            preset=preset,
            provider=provider,
            workspace=resolved_workspace,
            access=access,  # type: ignore[arg-type]
            reasoning=reasoning,
            model=model,
            timeout_seconds=timeout_seconds,
            isolate_write=False,
            metadata=metadata,
        )
        result = self.registry.start_agent(task)
        payload = result.to_dict()
        session_id = result.metadata.get("agent_session_id")
        if isinstance(session_id, str):
            payload["agent_session_id"] = session_id
            payload["session"] = self.store.get(session_id).to_dict()
        return payload

    def get(self, session_id: str) -> dict[str, Any]:
        return self.store.get(session_id).to_dict()

    def list(
        self,
        *,
        parent_session_id: str | None = None,
        root_session_id: str | None = None,
    ) -> list[dict[str, Any]]:
        return self.store.list(
            parent_session_id=parent_session_id,
            root_session_id=root_session_id,
        )

    def children(self, session_id: str) -> list[dict[str, Any]]:
        return self.store.children(session_id)

    def events(
        self,
        session_id: str,
        *,
        after_sequence: int = 0,
        limit: int = 50,
    ) -> dict[str, Any]:
        return self.store.events(
            session_id,
            after_sequence=after_sequence,
            limit=limit,
        )

    def messages(
        self,
        session_id: str,
        *,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        return self.store.messages(session_id, limit=limit)

    def send(
        self,
        session_id: str,
        message: str,
        *,
        timeout_seconds: int = 300,
    ) -> dict[str, Any]:
        if not message.strip():
            raise ValueError("message must not be empty")
        session = self.store.get(session_id)
        provider = self.registry.provider(session.provider)
        capabilities = provider.capabilities()
        if not capabilities.persistent_sessions:
            raise ValueError(
                f"{session.provider} session is closed-end and cannot be continued"
            )
        if session.parent_session_id and not capabilities.child_messaging:
            raise ValueError(
                f"{session.provider} does not expose direct messaging to native child sessions"
            )
        if not session.provider_session_id:
            raise ValueError(
                f"agent session {session_id} has no provider session handle yet"
            )

        self.store.add_message(
            session_id,
            direction="host_to_agent",
            content=message,
            recipient_session_id=session_id,
        )
        self.store.update(session_id, state="running")

        def progress_line(
            provider_name: str,
            line: str,
            stream: str = "stdout",
        ) -> None:
            for event in normalize_agent_events(
                provider_name,
                line,
                session_id,
                stream,
            ):
                try:
                    self.registry.record_agent_event(provider_name, event)
                except (OSError, ValueError):
                    pass

        task = TaskSpec(
            objective=message,
            role=session.role,
            provider=session.provider,
            workspace=session.workspace,
            access=session.access,  # type: ignore[arg-type]
            reasoning=session.reasoning,
            model=session.model,
            timeout_seconds=timeout_seconds,
            isolate_write=False,
            metadata={
                "_agent_session_id": session_id,
                "_progress_line": progress_line,
                "_resume_provider_session_id": session.provider_session_id,
            },
        )
        try:
            result = provider.continue_session(
                task,
                session.provider_session_id,
            )
        except Exception:
            self.store.update(session_id, state="failed")
            raise

        if result.conversation_id:
            self.store.bind_provider_session(
                session_id,
                result.conversation_id,
            )
        self.store.update(
            session_id,
            state="idle" if result.status == "success" else "failed",
        )
        if result.final_text:
            self.store.add_message(
                session_id,
                direction="agent_to_host",
                content=result.final_text,
                sender_session_id=session_id,
                metadata={"final": True, "status": result.status},
            )
        payload = result.to_dict()
        payload["agent_session_id"] = session_id
        return payload

    def close(self, session_id: str) -> dict[str, Any]:
        return self.store.close(session_id).to_dict()
