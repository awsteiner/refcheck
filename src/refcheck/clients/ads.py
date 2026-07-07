"""NASA ADS API client — astronomy and astrophysics coverage.

The NASA Astrophysics Data System (https://ui.adsabs.harvard.edu) is the
canonical literature database for astronomy. Its API requires a free
personal token (Bearer auth) and provides a dedicated BibTeX export
endpoint keyed by bibcode.
"""

from __future__ import annotations

import sys

import httpx

from refcheck.models import PaperMetadata

_BASE_URL = "https://api.adsabs.harvard.edu/v1"

# Fields requested from the search endpoint (Solr ``fl`` parameter).
_FIELDS = "title,author,year,doi,bibcode,abstract,pub,identifier"

# ADS applies per-endpoint daily quotas (commonly ~5000). We track a
# conservative in-process count and stop before exhausting it. The count
# resets whenever the server process restarts.
_DAILY_LIMIT = 5000


class ADSClient:
    name = "ads"

    def __init__(self, http: httpx.AsyncClient, api_token: str) -> None:
        self._http = http
        self._api_token = api_token
        self._daily_calls = 0

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._api_token}"}

    def _over_limit(self) -> bool:
        if self._daily_calls >= _DAILY_LIMIT:
            print("[ads] daily request limit reached", file=sys.stderr)
            return True
        return False

    def _extract_arxiv(self, identifiers: list[str]) -> str | None:
        for ident in identifiers:
            low = ident.lower()
            if low.startswith("arxiv:"):
                return ident.split(":", 1)[1]
        return None

    def _parse_doc(self, doc: dict) -> PaperMetadata | None:
        titles = doc.get("title") or []
        title = titles[0] if titles else ""
        if not title:
            return None

        authors = list(doc.get("author") or [])

        doi = None
        dois = doc.get("doi") or []
        if dois:
            doi = dois[0]

        year = None
        raw_year = doc.get("year")
        if raw_year is not None:
            try:
                year = int(raw_year)
            except (TypeError, ValueError):
                year = None

        bibcode = doc.get("bibcode")
        arxiv_id = self._extract_arxiv(doc.get("identifier") or [])

        url = ""
        if bibcode:
            url = f"https://ui.adsabs.harvard.edu/abs/{bibcode}"
        elif doi:
            url = f"https://doi.org/{doi}"

        abstract = doc.get("abstract") or ""

        return PaperMetadata(
            title=title,
            authors=authors,
            year=year,
            doi=doi,
            venue=doc.get("pub", "") or "",
            abstract=abstract[:300] if abstract else "",
            url=url,
            source="ads",
            arxiv_id=arxiv_id,
            bibcode=bibcode,
        )

    async def _search(self, query: str, limit: int) -> list[PaperMetadata]:
        if self._over_limit():
            return []
        try:
            self._daily_calls += 1
            resp = await self._http.get(
                f"{_BASE_URL}/search/query",
                params={"q": query, "rows": str(limit), "fl": _FIELDS},
                headers=self._headers(),
            )
            resp.raise_for_status()
            docs = resp.json().get("response", {}).get("docs", [])
            papers = [self._parse_doc(d) for d in docs]
            return [p for p in papers if p is not None]
        except Exception as e:
            print(f"[ads] search error: {e}", file=sys.stderr)
            return []

    async def search_by_title(
        self, title: str, limit: int = 5
    ) -> list[PaperMetadata]:
        # Escape embedded quotes to keep the Solr phrase query valid.
        safe = title.replace('"', " ")
        return await self._search(f'title:"{safe}"', limit)

    async def search_by_query(
        self, query: str, limit: int = 10
    ) -> list[PaperMetadata]:
        return await self._search(query, limit)

    async def get_by_doi(self, doi: str) -> PaperMetadata | None:
        results = await self._search(f'doi:"{doi}"', 1)
        return results[0] if results else None

    async def get_bibtex(self, bibcode: str) -> str | None:
        """Get native BibTeX for a record via the ADS export endpoint.

        Posts the bibcode to ``/export/bibtex`` and returns the exported
        entry text. Returns None on any failure.
        """
        if self._over_limit():
            return None
        try:
            self._daily_calls += 1
            resp = await self._http.post(
                f"{_BASE_URL}/export/bibtex",
                json={"bibcode": [bibcode], "sort": "no sort"},
                headers=self._headers(),
            )
            if resp.status_code == 404:
                return None
            resp.raise_for_status()
            export = resp.json().get("export", "")
            return export.strip() if export else None
        except Exception as e:
            print(f"[ads] bibtex export error: {e}", file=sys.stderr)
            return None
