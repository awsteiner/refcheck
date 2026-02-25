"""arXiv API client — preprint coverage."""

from __future__ import annotations

import asyncio
import sys
import time
import xml.etree.ElementTree as ET

import httpx

from refcheck.models import PaperMetadata

_NS = {"atom": "http://www.w3.org/2005/Atom"}
_BASE_URL = "http://export.arxiv.org/api/query"
_MIN_DELAY = 3.0  # arXiv requires >= 3s between requests


class ArxivClient:
    name = "arxiv"

    def __init__(self, http: httpx.AsyncClient) -> None:
        self._http = http
        self._last_request_time: float = 0.0

    async def _rate_limit(self) -> None:
        elapsed = time.monotonic() - self._last_request_time
        if elapsed < _MIN_DELAY:
            await asyncio.sleep(_MIN_DELAY - elapsed)
        self._last_request_time = time.monotonic()

    def _parse_entries(self, xml_text: str) -> list[PaperMetadata]:
        results = []
        try:
            root = ET.fromstring(xml_text)
        except ET.ParseError as e:
            print(f"[arxiv] XML parse error: {e}", file=sys.stderr)
            return []

        for entry in root.findall("atom:entry", _NS):
            title_el = entry.find("atom:title", _NS)
            title = title_el.text.strip().replace("\n", " ") if title_el is not None and title_el.text else ""
            if not title:
                continue

            authors = []
            for author_el in entry.findall("atom:author", _NS):
                name_el = author_el.find("atom:name", _NS)
                if name_el is not None and name_el.text:
                    authors.append(name_el.text.strip())

            published_el = entry.find("atom:published", _NS)
            year = None
            if published_el is not None and published_el.text:
                year = int(published_el.text[:4])

            abstract_el = entry.find("atom:summary", _NS)
            abstract = ""
            if abstract_el is not None and abstract_el.text:
                abstract = abstract_el.text.strip().replace("\n", " ")

            # Extract arxiv_id from entry URL
            id_el = entry.find("atom:id", _NS)
            arxiv_id = ""
            url = ""
            if id_el is not None and id_el.text:
                url = id_el.text.strip()
                # URL format: http://arxiv.org/abs/2106.12345v1
                arxiv_id = url.split("/abs/")[-1]
                # Remove version suffix for cleaner ID
                if "v" in arxiv_id:
                    arxiv_id = arxiv_id.rsplit("v", 1)[0]

            # Extract primary category
            pub_types = []
            category_el = entry.find("{http://arxiv.org/schemas/atom}primary_category")
            if category_el is not None:
                term = category_el.get("term", "")
                if term:
                    pub_types.append(term)

            results.append(PaperMetadata(
                title=title,
                authors=authors,
                year=year,
                doi=None,
                venue="arXiv",
                abstract=abstract[:300] if abstract else "",
                url=url,
                source="arxiv",
                arxiv_id=arxiv_id,
                publication_types=pub_types,
            ))

        return results

    async def search_by_title(self, title: str, limit: int = 5) -> list[PaperMetadata]:
        await self._rate_limit()
        try:
            resp = await self._http.get(
                _BASE_URL,
                params={
                    "search_query": f'ti:"{title}"',
                    "max_results": str(limit),
                    "sortBy": "relevance",
                },
            )
            resp.raise_for_status()
            return self._parse_entries(resp.text)
        except Exception as e:
            print(f"[arxiv] title search error: {e}", file=sys.stderr)
            return []

    async def search_by_query(self, query: str, limit: int = 10) -> list[PaperMetadata]:
        await self._rate_limit()
        try:
            resp = await self._http.get(
                _BASE_URL,
                params={
                    "search_query": f"all:{query}",
                    "max_results": str(limit),
                    "sortBy": "relevance",
                },
            )
            resp.raise_for_status()
            return self._parse_entries(resp.text)
        except Exception as e:
            print(f"[arxiv] query search error: {e}", file=sys.stderr)
            return []

    async def get_by_doi(self, doi: str) -> PaperMetadata | None:
        """arXiv doesn't support DOI lookup directly; return None."""
        return None
