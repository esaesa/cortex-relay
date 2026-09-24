"""Compatibility name for the durable task service."""

from cortex_relay.runtime.task_service import TaskService as AsyncTaskManager

__all__ = ["AsyncTaskManager"]
