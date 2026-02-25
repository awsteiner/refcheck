"""Academic API client registry."""

from __future__ import annotations

from typing import Protocol

import httpx

from config import Settings
from models import PaperMetadata


class AcademicClient(Protocol):
    """Protocol for all academic database clients."""

    name: str

    async def search_by_title(self, title: str, limit: int = 5) -> list[PaperMetadata]: ...

    async def search_by_query(self, query: str, limit: int = 10) -> list[PaperMetadata]: ...

    async def get_by_doi(self, doi: str) -> PaperMetadata | None: ...


class ClientRegistry:
    """Holds all instantiated API clients."""

    def __init__(self, http: httpx.AsyncClient, settings: Settings) -> None:
        self._clients: dict[str, AcademicClient] = {}
        self._http = http
        self._settings = settings
        self._init_clients()

    def _init_clients(self) -> None:
        from clients.crossref import CrossrefClient
        from clients.semantic_scholar import SemanticScholarClient
        from clients.arxiv import ArxivClient

        self._clients["crossref"] = CrossrefClient(self._http, self._settings.crossref_email)
        self._clients["semantic_scholar"] = SemanticScholarClient(self._http, self._settings.s2_api_key)
        self._clients["arxiv"] = ArxivClient(self._http)

        if self._settings.elsevier_key:
            from clients.scopus import ScopusClient
            self._clients["scopus"] = ScopusClient(
                self._http, self._settings.elsevier_key, self._settings.elsevier_insttoken
            )

        if self._settings.ieee_api_key:
            from clients.ieee import IEEEClient
            self._clients["ieee"] = IEEEClient(self._http, self._settings.ieee_api_key)

    def get(self, name: str) -> AcademicClient | None:
        return self._clients.get(name)

    def get_all(self) -> list[AcademicClient]:
        return list(self._clients.values())

    def get_search_clients(self, databases: list[str] | None = None) -> list[AcademicClient]:
        if databases is None:
            return self.get_all()
        return [c for name, c in self._clients.items() if name in databases]

    def available_names(self) -> list[str]:
        return list(self._clients.keys())
