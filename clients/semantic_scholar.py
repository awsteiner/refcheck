"""Semantic Scholar API client — broadest coverage search."""

from __future__ import annotations

import sys

import httpx

from models import PaperMetadata

_FIELDS = "title,authors,year,venue,externalIds,abstract,publicationTypes,journal,citationCount,url"


class SemanticScholarClient:
    name = "semantic_scholar"

    def __init__(self, http: httpx.AsyncClient, api_key: str | None = None) -> None:
        self._http = http
        self._api_key = api_key

    def _headers(self) -> dict[str, str]:
        if self._api_key:
            return {"x-api-key": self._api_key}
        return {}

    def _parse_paper(self, paper: dict) -> PaperMetadata:
        authors = [a.get("name", "") for a in paper.get("authors", []) if a.get("name")]

        ext_ids = paper.get("externalIds") or {}
        doi = ext_ids.get("DOI")
        arxiv_id = ext_ids.get("ArXiv")
        s2_id = paper.get("paperId")

        venue = paper.get("venue", "")
        if not venue:
            journal = paper.get("journal") or {}
            venue = journal.get("name", "")

        url = paper.get("url", "")
        if not url and doi:
            url = f"https://doi.org/{doi}"
        elif not url and arxiv_id:
            url = f"https://arxiv.org/abs/{arxiv_id}"

        abstract = paper.get("abstract") or ""

        return PaperMetadata(
            title=paper.get("title", ""),
            authors=authors,
            year=paper.get("year"),
            doi=doi,
            venue=venue,
            abstract=abstract[:300] if abstract else "",
            url=url,
            source="semantic_scholar",
            arxiv_id=arxiv_id,
            s2_id=s2_id,
            publication_types=paper.get("publicationTypes") or [],
            citation_count=paper.get("citationCount"),
        )

    async def get_by_doi(self, doi: str) -> PaperMetadata | None:
        try:
            resp = await self._http.get(
                f"https://api.semanticscholar.org/graph/v1/paper/DOI:{doi}",
                params={"fields": _FIELDS},
                headers=self._headers(),
            )
            if resp.status_code == 404:
                return None
            resp.raise_for_status()
            return self._parse_paper(resp.json())
        except Exception as e:
            print(f"[semantic_scholar] DOI lookup error: {e}", file=sys.stderr)
            return None

    async def get_by_id(self, paper_id: str) -> PaperMetadata | None:
        """Lookup by Semantic Scholar paper ID or corpus ID."""
        try:
            resp = await self._http.get(
                f"https://api.semanticscholar.org/graph/v1/paper/{paper_id}",
                params={"fields": _FIELDS},
                headers=self._headers(),
            )
            if resp.status_code == 404:
                return None
            resp.raise_for_status()
            return self._parse_paper(resp.json())
        except Exception as e:
            print(f"[semantic_scholar] ID lookup error: {e}", file=sys.stderr)
            return None

    async def search_by_title(self, title: str, limit: int = 5) -> list[PaperMetadata]:
        return await self._search(title, limit)

    async def search_by_query(self, query: str, limit: int = 10) -> list[PaperMetadata]:
        return await self._search(query, limit)

    async def _search(self, query: str, limit: int) -> list[PaperMetadata]:
        try:
            resp = await self._http.get(
                "https://api.semanticscholar.org/graph/v1/paper/search",
                params={"query": query, "limit": str(limit), "fields": _FIELDS},
                headers=self._headers(),
            )
            resp.raise_for_status()
            data = resp.json().get("data", [])
            return [self._parse_paper(p) for p in data]
        except Exception as e:
            print(f"[semantic_scholar] search error: {e}", file=sys.stderr)
            return []
