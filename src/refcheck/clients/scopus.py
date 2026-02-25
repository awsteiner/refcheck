"""Elsevier/Scopus API client (requires API key)."""

from __future__ import annotations

import sys

import httpx

from refcheck.models import PaperMetadata

_BASE_URL = "https://api.elsevier.com/content/search/scopus"


class ScopusClient:
    name = "scopus"

    def __init__(self, http: httpx.AsyncClient, api_key: str, insttoken: str | None = None) -> None:
        self._http = http
        self._api_key = api_key
        self._insttoken = insttoken

    def _headers(self) -> dict[str, str]:
        headers = {"X-ELS-APIKey": self._api_key, "Accept": "application/json"}
        if self._insttoken:
            headers["X-ELS-Insttoken"] = self._insttoken
        return headers

    def _parse_entry(self, entry: dict) -> PaperMetadata:
        title = entry.get("dc:title", "")

        # Authors: Scopus returns "dc:creator" for first author only in search
        authors = []
        creator = entry.get("dc:creator", "")
        if creator:
            authors.append(creator)

        year = None
        cover_date = entry.get("prism:coverDate", "")
        if cover_date:
            try:
                year = int(cover_date[:4])
            except ValueError:
                pass

        doi = entry.get("prism:doi", "")
        venue = entry.get("prism:publicationName", "")

        url = ""
        for link in entry.get("link", []):
            if link.get("@ref") == "scopus":
                url = link.get("@href", "")
                break

        citation_count = None
        cited = entry.get("citedby-count")
        if cited is not None:
            try:
                citation_count = int(cited)
            except ValueError:
                pass

        return PaperMetadata(
            title=title,
            authors=authors,
            year=year,
            doi=doi if doi else None,
            venue=venue,
            abstract="",  # Scopus search doesn't return abstracts
            url=url,
            source="scopus",
            citation_count=citation_count,
        )

    async def _search(self, query: str, limit: int) -> list[PaperMetadata]:
        try:
            resp = await self._http.get(
                _BASE_URL,
                params={"query": query, "count": str(limit)},
                headers=self._headers(),
            )
            resp.raise_for_status()
            entries = resp.json().get("search-results", {}).get("entry", [])
            # Skip error entries
            return [self._parse_entry(e) for e in entries if "error" not in e]
        except Exception as e:
            print(f"[scopus] search error: {e}", file=sys.stderr)
            return []

    async def search_by_title(self, title: str, limit: int = 5) -> list[PaperMetadata]:
        return await self._search(f'TITLE("{title}")', limit)

    async def search_by_query(self, query: str, limit: int = 10) -> list[PaperMetadata]:
        return await self._search(query, limit)

    async def get_by_doi(self, doi: str) -> PaperMetadata | None:
        results = await self._search(f'DOI("{doi}")', 1)
        return results[0] if results else None
