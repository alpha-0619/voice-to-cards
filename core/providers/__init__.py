from .base import ArgsFragment, Completed, Failed, Provider, ProviderEvent
from .scripted import ScriptedProvider

__all__ = [
    "ArgsFragment",
    "Completed",
    "Failed",
    "Provider",
    "ProviderEvent",
    "ScriptedProvider",
    "build_provider",
]


def build_provider(name: str = "claude"):
    """Resolve a provider by name, importing the SDK only if it is actually used."""
    if name == "scripted":
        return ScriptedProvider()
    if name == "claude":
        from .claude import ClaudeProvider

        return ClaudeProvider()
    raise ValueError(f"unknown provider {name!r}")
