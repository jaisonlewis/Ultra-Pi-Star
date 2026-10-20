from __future__ import annotations

from .base import MediaProvider, PlaybackRef


class ProviderRegistry:
    """Maps media_entry.provider -> provider implementation.

    Only providers listed in Config.enabled_providers are active, keeping the
    'approved provider path' enforceable (Impl Phase 2 acceptance check).
    """

    def __init__(self) -> None:
        self._providers: dict[str, MediaProvider] = {}

    def register(self, provider: MediaProvider) -> None:
        self._providers[provider.name] = provider

    def enabled(self, name: str) -> bool:
        return name in self._providers

    def get(self, name: str) -> MediaProvider | None:
        return self._providers.get(name)

    def resolve(self, entry: dict) -> PlaybackRef | None:
        provider = self._providers.get(entry.get("provider", ""))
        return provider.resolve(entry) if provider else None

    def search_remote(self, query: str, limit: int = 10) -> list[dict]:
        """Merge results from all enabled providers that support search."""
        results: list[dict] = []
        for p in self._providers.values():
            search = getattr(p, "search", None)
            if not search:
                continue
            try:
                results.extend(search(query, limit))
            except Exception as exc:  # provider down -> local results still work
                results.append({"provider": p.name, "error": str(exc),
                                "title": f"{p.name} search failed", "remote": True,
                                "entry_id": None})
        return results
