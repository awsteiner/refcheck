"""Crossref API client — DOI backbone and BibTeX source."""

from __future__ import annotations

import sys

import httpx

from models import PaperMetadata


class CrossrefClient:
    name = "crossref"

    def __init__(self, http: httpx.AsyncClient, email: str | None = None) -> None:
        self._http = http
        self._email = email

    def _params(self, extra: dict | None = None) -> dict:
        params = {}
        if self._email:
            params["mailto"] = self._email
        if extra:
            params.update(extra)
        return params

    def _parse_item(self, item: dict) -> PaperMetadata:
        authors = []
        for a in item.get("author", []):
            given = a.get("given", "")
            family = a.get("family", "")
            if given and family:
                authors.append(f"{given} {family}")
            elif family:
                authors.append(family)

        year = None
        date_parts = item.get("published", {}).get("date-parts", [[]])
        if not date_parts or not date_parts[0]:
            date_parts = item.get("issued", {}).get("date-parts", [[]])
        if date_parts and date_parts[0]:
            year = date_parts[0][0]

        venue = ""
        container = item.get("container-title", [])
        if container:
            venue = container[0]

        pub_types = []
        cr_type = item.get("type", "")
        if cr_type:
            pub_types.append(cr_type)

        doi = item.get("DOI", "")
        url = item.get("URL", "")
        if doi and not url:
            url = f"https://doi.org/{doi}"

        abstract = item.get("abstract", "")
        if abstract:
            # Crossref abstracts sometimes contain JATS XML tags
            import re
            abstract = re.sub(r"<[^>]+>", "", abstract).strip()

        return PaperMetadata(
            title=item.get("title", [""])[0] if isinstance(item.get("title"), list) else item.get("title", ""),
            authors=authors,
            year=year,
            doi=doi,
            venue=venue,
            abstract=abstract[:300] if abstract else "",
            url=url,
            source="crossref",
            publication_types=pub_types,
            citation_count=item.get("is-referenced-by-count"),
        )

    async def get_by_doi(self, doi: str) -> PaperMetadata | None:
        try:
            resp = await self._http.get(
                f"https://api.crossref.org/works/{doi}",
                params=self._params(),
            )
            if resp.status_code == 404:
                return None
            resp.raise_for_status()
            return self._parse_item(resp.json()["message"])
        except Exception as e:
            print(f"[crossref] DOI lookup error: {e}", file=sys.stderr)
            return None

    async def search_by_title(self, title: str, limit: int = 5) -> list[PaperMetadata]:
        try:
            resp = await self._http.get(
                "https://api.crossref.org/works",
                params=self._params({"query.title": title, "rows": str(limit)}),
            )
            resp.raise_for_status()
            items = resp.json().get("message", {}).get("items", [])
            return [self._parse_item(item) for item in items]
        except Exception as e:
            print(f"[crossref] title search error: {e}", file=sys.stderr)
            return []

    async def search_by_query(self, query: str, limit: int = 10) -> list[PaperMetadata]:
        try:
            resp = await self._http.get(
                "https://api.crossref.org/works",
                params=self._params({"query": query, "rows": str(limit)}),
            )
            resp.raise_for_status()
            items = resp.json().get("message", {}).get("items", [])
            return [self._parse_item(item) for item in items]
        except Exception as e:
            print(f"[crossref] query search error: {e}", file=sys.stderr)
            return []

    async def get_bibtex(self, doi: str) -> str | None:
        """Get BibTeX via Crossref content negotiation (gold standard)."""
        try:
            resp = await self._http.get(
                f"https://api.crossref.org/works/{doi}/transform/application/x-bibtex",
                params=self._params(),
            )
            if resp.status_code == 404:
                return None
            resp.raise_for_status()
            return resp.text
        except Exception as e:
            print(f"[crossref] bibtex export error: {e}", file=sys.stderr)
            return None
