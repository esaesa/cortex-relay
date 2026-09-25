"""Provider-neutral runtime contracts and routing primitives."""

from .models import Evidence, TaskResult, TaskSpec
from .policy import RoutingPolicy

__all__ = [
    "Evidence",
    "TaskResult",
    "TaskSpec",
    "RoutingPolicy",
    "ProviderRegistry",
    "default_registry",
]


def __getattr__(name: str):
    """Lazy registry exports avoid core-package import cycles.

    observability and runtime stores depend on core models. Importing registry
    eagerly from core.__init__ would make those modules recurse back into
    observability while it is only partially initialized.
    """
    if name in {"ProviderRegistry", "default_registry"}:
        from .registry import ProviderRegistry, default_registry

        return {
            "ProviderRegistry": ProviderRegistry,
            "default_registry": default_registry,
        }[name]
    raise AttributeError(name)
