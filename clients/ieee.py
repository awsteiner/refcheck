"""IEEE Xplore API client (requires API key, 200 requests/day limit)."""

from __future__ import annotations

import sys

import httpx

from models import PaperMetadata

_BASE_URL = "https://ieeexploreapi.ieee.org/api/v1/search/articles"


class IEEEClient:
    name = "ieee"

    def __init__(self, http: httpx.AsyncClient, api_key: str) -> None:
        self._http = http
        self._api_key = api_key
        self._daily_calls = 0
        self._daily_limit = 200

    def _check_limit(self) -> bool:
        if self._daily_calls >= self._daily_limit:
            print("[ieee] Daily API limit (200) reached", file=sys.stderr)
            return False
        return True

    def _parse_article(self, article: dict) -> PaperMetadata:
        title = article.get("title", "")

        authors_data = article.get("authors", {}).get("authors", [])
        authors = [a.get("full_name", "") for a in authors_data if a.get("full_name")]

        year = None
        pub_year = article.get("publication_year")
        if pub_year:
            try:
                year = int(pub_year)
            except ValueError:
                pass

        doi = article.get("doi", "")
        venue = article.get("publication_title", "")
        abstract = article.get("abstract", "")
        url = article.get("html_url", "") or article.get("pdf_url", "")

        return PaperMetadata(
            title=title,
            authors=authors,
            year=year,
            doi=doi if doi else None,
            venue=venue,
            abstract=abstract[:300] if abstract else "",
            url=url,
            source="ieee",
        )

    async def _search(self, params: dict, limit: int) -> list[PaperMetadata]:
        if not self._check_limit():
            return []
        try:
            params["apikey"] = self._api_key
            params["max_records"] = str(limit)
            resp = await self._http.get(_BASE_URL, params=params)
            self._daily_calls += 1
            resp.raise_for_status()
            articles = resp.json().get("articles", [])
            return [self._parse_article(a) for a in articles]
        except Exception as e:
            print(f"[ieee] search error: {e}", file=sys.stderr)
            return []

    async def search_by_title(self, title: str, limit: int = 5) -> list[PaperMetadata]:
        return await self._search({"querytext": f'"{title}"'}, limit)

    async def search_by_query(self, query: str, limit: int = 10) -> list[PaperMetadata]:
        return await self._search({"querytext": query}, limit)

    async def get_by_doi(self, doi: str) -> PaperMetadata | None:
        results = await self._search({"doi": doi}, 1)
        return results[0] if results else None
