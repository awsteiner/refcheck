"""SQLite cache for refcheck — avoids redundant API calls across sessions."""

from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path
from typing import Optional

import aiosqlite

from refcheck.models import PaperMetadata

# TTL defaults (seconds)
PAPER_TTL = 30 * 24 * 3600  # 30 days
SEARCH_TTL = 7 * 24 * 3600  # 7 days

_SCHEMA = """\
CREATE TABLE IF NOT EXISTS papers (
    doi TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    authors TEXT NOT NULL DEFAULT '[]',
    year INTEGER,
    venue TEXT DEFAULT '',
    abstract TEXT DEFAULT '',
    url TEXT DEFAULT '',
    source TEXT DEFAULT '',
    arxiv_id TEXT,
    s2_id TEXT,
    publication_types TEXT DEFAULT '[]',
    citation_count INTEGER,
    bibtex TEXT,
    cached_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS papers_by_title (
    title_lower TEXT PRIMARY KEY,
    doi TEXT,
    paper_json TEXT NOT NULL,
    bibtex TEXT,
    cached_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS searches (
    query_hash TEXT PRIMARY KEY,
    query TEXT NOT NULL,
    results_json TEXT NOT NULL,
    cached_at REAL NOT NULL
);
"""


class RefCache:
    """Async SQLite cache for paper metadata, BibTeX, and search results."""

    def __init__(
        self,
        db_path: Path,
        paper_ttl: int = PAPER_TTL,
        search_ttl: int = SEARCH_TTL,
    ):
        self.db_path = db_path
        self.paper_ttl = paper_ttl
        self.search_ttl = search_ttl
        self._db: Optional[aiosqlite.Connection] = None

    async def open(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._db = await aiosqlite.connect(str(self.db_path))
        self._db.row_factory = aiosqlite.Row
        await self._db.executescript(_SCHEMA)
        await self._db.commit()

    async def close(self) -> None:
        if self._db:
            await self._db.close()
            self._db = None

    # ── Paper by DOI ──────────────────────────────────────────────────────

    async def get_paper_by_doi(
        self, doi: str
    ) -> Optional[tuple[PaperMetadata, Optional[str]]]:
        """Return (PaperMetadata, bibtex) if cached and fresh, else None."""
        row = await self._fetchone(
            "SELECT * FROM papers WHERE doi = ? AND cached_at > ?",
            (doi.lower(), time.time() - self.paper_ttl),
        )
        if not row:
            return None
        return self._row_to_paper(row), row["bibtex"]

    async def save_paper(
        self, paper: PaperMetadata, bibtex: Optional[str] = None
    ) -> None:
        """Upsert a paper. Indexes by DOI (if present) and by title."""
        now = time.time()
        if paper.doi:
            await self._execute(
                """INSERT OR REPLACE INTO papers
                   (doi, title, authors, year, venue, abstract, url, source,
                    arxiv_id, s2_id, publication_types, citation_count, bibtex,
                    cached_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    paper.doi.lower(),
                    paper.title,
                    json.dumps(paper.authors),
                    paper.year,
                    paper.venue,
                    paper.abstract,
                    paper.url,
                    paper.source,
                    paper.arxiv_id,
                    paper.s2_id,
                    json.dumps(paper.publication_types),
                    paper.citation_count,
                    bibtex,
                    now,
                ),
            )
        if paper.title:
            await self._execute(
                """INSERT OR REPLACE INTO papers_by_title
                   (title_lower, doi, paper_json, bibtex, cached_at)
                   VALUES (?, ?, ?, ?, ?)""",
                (
                    paper.title.lower().strip(),
                    paper.doi,
                    paper.model_dump_json(),
                    bibtex,
                    now,
                ),
            )
        if self._db:
            await self._db.commit()

    # ── Paper by title ────────────────────────────────────────────────────

    async def get_paper_by_title(
        self, title: str
    ) -> Optional[tuple[PaperMetadata, Optional[str]]]:
        """Exact title match (case-insensitive)."""
        row = await self._fetchone(
            "SELECT * FROM papers_by_title WHERE title_lower = ? AND cached_at > ?",
            (title.lower().strip(), time.time() - self.paper_ttl),
        )
        if not row:
            return None
        paper = PaperMetadata.model_validate_json(row["paper_json"])
        return paper, row["bibtex"]

    # ── BibTeX by DOI ─────────────────────────────────────────────────────

    async def get_bibtex_by_doi(self, doi: str) -> Optional[str]:
        """Return cached BibTeX string for a DOI."""
        row = await self._fetchone(
            "SELECT bibtex FROM papers WHERE doi = ? AND bibtex IS NOT NULL "
            "AND cached_at > ?",
            (doi.lower(), time.time() - self.paper_ttl),
        )
        return row["bibtex"] if row else None

    async def save_bibtex(self, doi: str, bibtex: str) -> None:
        """Update just the BibTeX for an existing cached paper."""
        await self._execute(
            "UPDATE papers SET bibtex = ?, cached_at = ? WHERE doi = ?",
            (bibtex, time.time(), doi.lower()),
        )
        await self._execute(
            "UPDATE papers_by_title SET bibtex = ?, cached_at = ? WHERE doi = ?",
            (bibtex, time.time(), doi.lower()),
        )
        if self._db:
            await self._db.commit()

    # ── Search results ────────────────────────────────────────────────────

    async def get_search(
        self,
        query: str,
        year_from: Optional[int] = None,
        year_to: Optional[int] = None,
        databases: Optional[list[str]] = None,
        max_results: int = 10,
    ) -> Optional[list[dict]]:
        """Return cached search results if fresh."""
        qhash = self._search_hash(query, year_from, year_to, databases, max_results)
        row = await self._fetchone(
            "SELECT results_json FROM searches WHERE query_hash = ? AND cached_at > ?",
            (qhash, time.time() - self.search_ttl),
        )
        if not row:
            return None
        return json.loads(row["results_json"])

    async def save_search(
        self,
        query: str,
        results: list[dict],
        year_from: Optional[int] = None,
        year_to: Optional[int] = None,
        databases: Optional[list[str]] = None,
        max_results: int = 10,
    ) -> None:
        """Cache search results."""
        qhash = self._search_hash(query, year_from, year_to, databases, max_results)
        await self._execute(
            """INSERT OR REPLACE INTO searches
               (query_hash, query, results_json, cached_at)
               VALUES (?, ?, ?, ?)""",
            (qhash, query, json.dumps(results), time.time()),
        )
        if self._db:
            await self._db.commit()

    # ── Stats ─────────────────────────────────────────────────────────────

    async def stats(self) -> dict:
        """Return cache statistics."""
        papers = await self._fetchone("SELECT COUNT(*) as n FROM papers")
        titles = await self._fetchone("SELECT COUNT(*) as n FROM papers_by_title")
        searches = await self._fetchone("SELECT COUNT(*) as n FROM searches")
        return {
            "papers_by_doi": papers["n"] if papers else 0,
            "papers_by_title": titles["n"] if titles else 0,
            "cached_searches": searches["n"] if searches else 0,
        }

    # ── Internals ─────────────────────────────────────────────────────────

    def _row_to_paper(self, row: aiosqlite.Row) -> PaperMetadata:
        return PaperMetadata(
            title=row["title"],
            authors=json.loads(row["authors"]),
            year=row["year"],
            doi=row["doi"],
            venue=row["venue"] or "",
            abstract=row["abstract"] or "",
            url=row["url"] or "",
            source=row["source"] or "",
            arxiv_id=row["arxiv_id"],
            s2_id=row["s2_id"],
            publication_types=json.loads(row["publication_types"]),
            citation_count=row["citation_count"],
        )

    @staticmethod
    def _search_hash(
        query: str,
        year_from: Optional[int],
        year_to: Optional[int],
        databases: Optional[list[str]],
        max_results: int,
    ) -> str:
        key = json.dumps(
            {
                "q": query.lower().strip(),
                "yf": year_from,
                "yt": year_to,
                "db": sorted(databases) if databases else None,
                "mr": max_results,
            },
            sort_keys=True,
        )
        return hashlib.sha256(key.encode()).hexdigest()[:16]

    async def _fetchone(
        self, sql: str, params: tuple = ()
    ) -> Optional[aiosqlite.Row]:
        if not self._db:
            return None
        async with self._db.execute(sql, params) as cursor:
            return await cursor.fetchone()

    async def _execute(self, sql: str, params: tuple = ()) -> None:
        if self._db:
            await self._db.execute(sql, params)
