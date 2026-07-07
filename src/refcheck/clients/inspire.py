"""INSPIRE-HEP API client — high-energy physics coverage.

INSPIRE-HEP (https://inspirehep.net) is the canonical literature
database for high-energy physics. The REST API requires no key and
offers native BibTeX export, which makes it a good peer to Crossref
for this domain.
"""

from __future__ import annotations

import asyncio
import sys
import time

import httpx

from refcheck.models import PaperMetadata

_BASE_URL = "https://inspirehep.net/api/literature"

# Fields requested from the API. Restricting the field set keeps
# responses small and avoids pulling the full record.
_FIELDS = (
    "titles,authors.full_name,dois,arxiv_eprints,abstracts,"
    "earliest_date,publication_info,citation_count,control_number"
)

# INSPIRE allows 15 requests per 5 second window per IP. A ~0.4 second
# gap between requests keeps us comfortably under that limit.
_MIN_DELAY = 0.4


class InspireClient:
    name = "inspire"

    def __init__(
        self, http: httpx.AsyncClient, email: str | None = None
    ) -> None:
        self._http = http
        self._email = email
        self._last_request_time: float = 0.0

    def _headers(self) -> dict[str, str]:
        ua = "refcheck/0.1"
        if self._email:
            ua = f"refcheck/0.1 (mailto:{self._email})"
        return {"User-Agent": ua}

    async def _rate_limit(self) -> None:
        elapsed = time.monotonic() - self._last_request_time
        if elapsed < _MIN_DELAY:
            await asyncio.sleep(_MIN_DELAY - elapsed)
        self._last_request_time = time.monotonic()

    def _parse_hit(self, hit: dict) -> PaperMetadata | None:
        meta = hit.get("metadata", {})

        titles = meta.get("titles") or []
        title = titles[0].get("title", "") if titles else ""
        if not title:
            return None

        authors = []
        for a in meta.get("authors") or []:
            name = a.get("full_name", "")
            if name:
                authors.append(name)

        doi = None
        dois = meta.get("dois") or []
        if dois:
            doi = dois[0].get("value")

        arxiv_id = None
        eprints = meta.get("arxiv_eprints") or []
        pub_types: list[str] = []
        if eprints:
            arxiv_id = eprints[0].get("value")
            # arXiv categories double as coarse publication types and
            # supply the BibTeX primaryClass field downstream.
            pub_types = list(eprints[0].get("categories") or [])

        year = None
        earliest = meta.get("earliest_date", "")
        if earliest and len(earliest) >= 4 and earliest[:4].isdigit():
            year = int(earliest[:4])

        # Prefer the first publication_info entry that names a journal.
        venue = ""
        for info in meta.get("publication_info") or []:
            journal = info.get("journal_title")
            if journal:
                venue = journal
                break

        abstract = ""
        abstracts = meta.get("abstracts") or []
        if abstracts:
            abstract = abstracts[0].get("value", "") or ""

        recid = meta.get("control_number")
        inspire_id = str(recid) if recid is not None else None
        url = f"https://inspirehep.net/literature/{recid}" if recid else ""

        return PaperMetadata(
            title=title,
            authors=authors,
            year=year,
            doi=doi,
            venue=venue,
            abstract=abstract[:300] if abstract else "",
            url=url,
            source="inspire",
            arxiv_id=arxiv_id,
            publication_types=pub_types,
            citation_count=meta.get("citation_count"),
            inspire_id=inspire_id,
        )

    async def _search(self, query: str, limit: int) -> list[PaperMetadata]:
        await self._rate_limit()
        try:
            resp = await self._http.get(
                _BASE_URL,
                params={
                    "q": query,
                    "size": str(limit),
                    "fields": _FIELDS,
                },
                headers=self._headers(),
            )
            resp.raise_for_status()
            hits = resp.json().get("hits", {}).get("hits", [])
            papers = [self._parse_hit(h) for h in hits]
            return [p for p in papers if p is not None]
        except Exception as e:
            print(f"[inspire] search error: {e}", file=sys.stderr)
            return []

    async def search_by_title(
        self, title: str, limit: int = 5
    ) -> list[PaperMetadata]:
        return await self._search(f"title {title}", limit)

    async def search_by_query(
        self, query: str, limit: int = 10
    ) -> list[PaperMetadata]:
        return await self._search(query, limit)

    async def get_by_doi(self, doi: str) -> PaperMetadata | None:
        results = await self._search(f"doi {doi}", 1)
        return results[0] if results else None

    async def get_by_arxiv(self, arxiv_id: str) -> PaperMetadata | None:
        """Look up a record by its arXiv identifier."""
        results = await self._search(f"arxiv {arxiv_id}", 1)
        return results[0] if results else None

    async def get_bibtex(self, inspire_id: str) -> str | None:
        """Get native BibTeX for an INSPIRE record by its recid.

        Uses the ``?format=bibtex`` content negotiation, analogous to
        Crossref's BibTeX export. Returns None on any failure.
        """
        await self._rate_limit()
        try:
            resp = await self._http.get(
                f"{_BASE_URL}/{inspire_id}",
                params={"format": "bibtex"},
                headers=self._headers(),
            )
            if resp.status_code == 404:
                return None
            resp.raise_for_status()
            return resp.text.strip()
        except Exception as e:
            print(f"[inspire] bibtex export error: {e}", file=sys.stderr)
            return None
