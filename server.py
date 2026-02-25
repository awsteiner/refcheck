"""FastMCP server entry point — refcheck academic reference verification."""

import asyncio
import sys
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Optional

import httpx
from mcp.server.fastmcp import Context, FastMCP

from bibtex import build_bibtex_entry, to_bibtex
from clients import ClientRegistry
from config import Settings
from matching import score_candidate, title_similarity
from models import (
    BibtexEntry,
    BibtexResult,
    MatchedReference,
    PaperMetadata,
    SearchResult,
    VerifyResult,
)


@dataclass
class AppContext:
    http: httpx.AsyncClient
    registry: ClientRegistry
    settings: Settings


@asynccontextmanager
async def app_lifespan(server: FastMCP):
    settings = Settings.from_env()
    async with httpx.AsyncClient(timeout=10.0, follow_redirects=True) as http:
        registry = ClientRegistry(http, settings)
        ctx = AppContext(http=http, registry=registry, settings=settings)
        yield ctx


mcp = FastMCP("refcheck", lifespan=app_lifespan)


def _get_ctx(ctx: Context) -> AppContext:
    """Extract AppContext from MCP context."""
    return ctx.request_context.lifespan_context


# ---------------------------------------------------------------------------
# Tool 1: verify_reference
# ---------------------------------------------------------------------------


@mcp.tool()
async def verify_reference(
    ctx: Context,
    title: Optional[str] = None,
    authors: Optional[list[str]] = None,
    year: Optional[int] = None,
    doi: Optional[str] = None,
    venue: Optional[str] = None,
) -> dict:
    """Verify an academic reference against real publication databases.

    Checks whether a citation is real by looking it up in Crossref, Semantic Scholar,
    arXiv, and optionally Scopus/IEEE. Returns a confidence verdict: "verified",
    "partial_match", or "not_found".

    At least one of `title` or `doi` must be provided.
    """
    if not title and not doi:
        return VerifyResult(
            verdict="not_found",
            confidence=0.0,
            discrepancies=["At least one of title or doi must be provided"],
            sources_checked=[],
        ).model_dump()

    app = _get_ctx(ctx)
    authors = authors or []
    sources_checked: list[str] = []

    # Step 1: DOI lookup (highest confidence path)
    if doi:
        crossref = app.registry.get("crossref")
        if crossref:
            paper = await crossref.get_by_doi(doi)
            if paper:
                sources_checked.append("crossref")
                verdict, confidence, discrepancies = score_candidate(
                    title, authors, year, venue, paper
                )
                # DOI match from Crossref is essentially 100% verification for existence
                if confidence < 1.0:
                    confidence = max(confidence, 0.90)
                    if verdict == "not_found":
                        verdict = "partial_match"
                # Get corrected BibTeX when paper is found
                corrected_bib = await _get_corrected_bibtex(paper, app)
                return VerifyResult(
                    verdict=verdict,
                    confidence=confidence,
                    matched_reference=_to_matched_ref(paper),
                    discrepancies=discrepancies,
                    sources_checked=sources_checked,
                    corrected_bibtex=corrected_bib,
                ).model_dump()

    # Step 2: Title search across all clients
    if title:
        clients = app.registry.get_all()
        tasks = [c.search_by_title(title, limit=5) for c in clients]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        all_candidates: list[PaperMetadata] = []
        for client, result in zip(clients, results):
            if isinstance(result, Exception):
                print(f"[verify] {client.name} error: {result}", file=sys.stderr)
                continue
            sources_checked.append(client.name)
            all_candidates.extend(result)

        # Score all candidates, pick best
        best_verdict = "not_found"
        best_confidence = 0.0
        best_discrepancies: list[str] = []
        best_paper: Optional[PaperMetadata] = None

        for candidate in all_candidates:
            v, c, d = score_candidate(title, authors, year, venue, candidate)
            if c > best_confidence:
                best_verdict = v
                best_confidence = c
                best_discrepancies = d
                best_paper = candidate

        # Get corrected BibTeX when a match is found
        corrected_bib = None
        if best_paper and best_verdict in ("verified", "partial_match"):
            corrected_bib = await _get_corrected_bibtex(best_paper, app)

        return VerifyResult(
            verdict=best_verdict,
            confidence=best_confidence,
            matched_reference=_to_matched_ref(best_paper) if best_paper else None,
            discrepancies=best_discrepancies,
            sources_checked=sources_checked,
            corrected_bibtex=corrected_bib,
        ).model_dump()

    return VerifyResult(
        verdict="not_found",
        confidence=0.0,
        sources_checked=sources_checked,
    ).model_dump()


def _to_matched_ref(paper: PaperMetadata) -> MatchedReference:
    return MatchedReference(
        title=paper.title,
        authors=paper.authors,
        year=paper.year,
        doi=paper.doi,
        venue=paper.venue,
        url=paper.url,
    )


async def _get_corrected_bibtex(paper: PaperMetadata, app: AppContext) -> Optional[str]:
    """Get correct BibTeX for a matched paper. Crossref first, then construct."""
    # Try Crossref content negotiation if DOI available (gold standard)
    if paper.doi:
        crossref = app.registry.get("crossref")
        if crossref:
            from clients.crossref import CrossrefClient
            if isinstance(crossref, CrossrefClient):
                bib_text = await crossref.get_bibtex(paper.doi)
                if bib_text:
                    return bib_text.strip()

    # Fallback: construct from metadata
    key, entry_type, fields = build_bibtex_entry(paper)
    return to_bibtex(entry_type, key, fields)


# ---------------------------------------------------------------------------
# Tool 2: search_references
# ---------------------------------------------------------------------------


@mcp.tool()
async def search_references(
    ctx: Context,
    query: str,
    max_results: int = 10,
    year_from: Optional[int] = None,
    year_to: Optional[int] = None,
    databases: Optional[list[str]] = None,
) -> dict:
    """Search for real, verified academic papers on a topic.

    Returns only real references from Crossref, Semantic Scholar, arXiv, and
    optionally Scopus/IEEE. Use this to find legitimate citations instead of
    letting the AI fabricate them.
    """
    app = _get_ctx(ctx)
    clients = app.registry.get_search_clients(databases)

    # Parallel search across all selected databases
    tasks = [c.search_by_query(query, limit=max_results) for c in clients]
    results = await asyncio.gather(*tasks, return_exceptions=True)

    all_papers: list[PaperMetadata] = []
    sources_queried: list[str] = []
    for client, result in zip(clients, results):
        if isinstance(result, Exception):
            print(f"[search] {client.name} error: {result}", file=sys.stderr)
            continue
        sources_queried.append(client.name)
        all_papers.extend(result)

    # Filter by year range
    if year_from is not None:
        all_papers = [p for p in all_papers if p.year is not None and p.year >= year_from]
    if year_to is not None:
        all_papers = [p for p in all_papers if p.year is not None and p.year <= year_to]

    # Deduplicate: by DOI first, then by title similarity
    deduped = _deduplicate(all_papers)

    # Trim to max_results
    deduped = deduped[:max_results]

    return {
        "results": [
            SearchResult(
                title=p.title,
                authors=p.authors,
                year=p.year,
                doi=p.doi,
                venue=p.venue,
                abstract=p.abstract,
                url=p.url,
                source=p.source,
            ).model_dump()
            for p in deduped
        ],
        "total": len(deduped),
        "sources_queried": sources_queried,
    }


def _deduplicate(papers: list[PaperMetadata]) -> list[PaperMetadata]:
    """Deduplicate papers by DOI, then by title similarity >= 0.95."""
    # Priority: crossref > semantic_scholar > arxiv > others
    source_priority = {"crossref": 0, "semantic_scholar": 1, "scopus": 2, "ieee": 3, "arxiv": 4}

    seen_dois: dict[str, PaperMetadata] = {}
    no_doi: list[PaperMetadata] = []

    for p in papers:
        if p.doi:
            doi_lower = p.doi.lower()
            if doi_lower in seen_dois:
                existing = seen_dois[doi_lower]
                if source_priority.get(p.source, 99) < source_priority.get(existing.source, 99):
                    seen_dois[doi_lower] = p
            else:
                seen_dois[doi_lower] = p
        else:
            no_doi.append(p)

    result = list(seen_dois.values())

    # Deduplicate DOI-less papers by title similarity
    for p in no_doi:
        is_dup = False
        for existing in result:
            if title_similarity(p.title, existing.title) >= 0.95:
                is_dup = True
                break
        if not is_dup:
            result.append(p)

    return result


# ---------------------------------------------------------------------------
# Tool 3: get_bibtex
# ---------------------------------------------------------------------------


@mcp.tool()
async def get_bibtex(
    ctx: Context,
    doi: Optional[str] = None,
    dois: Optional[list[str]] = None,
    title: Optional[str] = None,
    semantic_scholar_id: Optional[str] = None,
) -> dict:
    """Generate verified BibTeX entries for academic papers.

    Provide ONE of: a single DOI, a list of DOIs, a paper title, or a Semantic Scholar ID.
    Returns BibTeX entries ready to paste into a .bib file. Every exported entry corresponds
    to a real publication — fields are never fabricated.
    """
    app = _get_ctx(ctx)
    entries: list[BibtexEntry] = []
    bibtex_parts: list[str] = []
    warnings: list[str] = []

    # Collect all DOIs to process
    all_dois: list[str] = []
    if doi:
        all_dois.append(doi)
    if dois:
        all_dois.extend(dois)

    # Process DOIs
    if all_dois:
        crossref = app.registry.get("crossref")
        for d in all_dois:
            entry, bib, warns = await _bibtex_from_doi(d, app, crossref)
            if entry:
                entries.append(entry)
                bibtex_parts.append(bib)
            warnings.extend(warns)

    # Process title
    if title:
        entry, bib, warns = await _bibtex_from_title(title, app)
        if entry:
            entries.append(entry)
            bibtex_parts.append(bib)
        warnings.extend(warns)

    # Process Semantic Scholar ID
    if semantic_scholar_id:
        entry, bib, warns = await _bibtex_from_s2id(semantic_scholar_id, app)
        if entry:
            entries.append(entry)
            bibtex_parts.append(bib)
        warnings.extend(warns)

    if not entries:
        warnings.append("No BibTeX entries could be generated for the given input.")

    return BibtexResult(
        bibtex="\n\n".join(bibtex_parts),
        entries=entries,
        warnings=warnings,
    ).model_dump()


async def _bibtex_from_doi(
    doi: str, app: AppContext, crossref
) -> tuple[Optional[BibtexEntry], str, list[str]]:
    """Try Crossref content negotiation first, fall back to metadata construction."""
    warnings: list[str] = []

    # Try Crossref native BibTeX (gold standard)
    if crossref:
        from clients.crossref import CrossrefClient
        if isinstance(crossref, CrossrefClient):
            bib_text = await crossref.get_bibtex(doi)
            if bib_text:
                # Parse the citation key from the bibtex
                import re
                key_match = re.search(r"@\w+\{([^,]+),", bib_text)
                key = key_match.group(1) if key_match else doi
                type_match = re.search(r"@(\w+)\{", bib_text)
                entry_type = type_match.group(1) if type_match else "article"
                return (
                    BibtexEntry(
                        citation_key=key,
                        entry_type=entry_type,
                        fields={"raw_bibtex": "from_crossref"},
                        source_api="crossref",
                    ),
                    bib_text.strip(),
                    warnings,
                )

    # Fallback: get metadata and construct
    paper = await _get_paper_by_doi(doi, app)
    if not paper:
        warnings.append(f"Could not retrieve metadata for DOI {doi}")
        return None, "", warnings

    key, entry_type, fields = build_bibtex_entry(paper)
    bib_text = to_bibtex(entry_type, key, fields)
    warnings.append(f"BibTeX for {doi} constructed from metadata (Crossref content negotiation unavailable)")

    return (
        BibtexEntry(citation_key=key, entry_type=entry_type, fields=fields, source_api=paper.source),
        bib_text,
        warnings,
    )


async def _bibtex_from_title(
    title: str, app: AppContext
) -> tuple[Optional[BibtexEntry], str, list[str]]:
    """Search for paper by title, verify, then generate BibTeX."""
    warnings: list[str] = []

    clients = app.registry.get_all()
    tasks = [c.search_by_title(title, limit=3) for c in clients]
    results = await asyncio.gather(*tasks, return_exceptions=True)

    best_paper: Optional[PaperMetadata] = None
    best_sim = 0.0

    for result in results:
        if isinstance(result, Exception):
            continue
        for paper in result:
            sim = title_similarity(title, paper.title)
            if sim > best_sim:
                best_sim = sim
                best_paper = paper

    if not best_paper or best_sim < 0.80:
        warnings.append(f"No close match found for title '{title}'")
        return None, "", warnings

    if best_sim < 0.95:
        warnings.append(f"Title match is approximate ({best_sim:.0%}): '{best_paper.title}'")

    # If we found a DOI, try Crossref BibTeX first
    if best_paper.doi:
        crossref = app.registry.get("crossref")
        entry, bib, w = await _bibtex_from_doi(best_paper.doi, app, crossref)
        warnings.extend(w)
        if entry:
            return entry, bib, warnings

    # Construct from metadata
    key, entry_type, fields = build_bibtex_entry(best_paper)
    bib_text = to_bibtex(entry_type, key, fields)

    return (
        BibtexEntry(citation_key=key, entry_type=entry_type, fields=fields, source_api=best_paper.source),
        bib_text,
        warnings,
    )


async def _bibtex_from_s2id(
    s2_id: str, app: AppContext
) -> tuple[Optional[BibtexEntry], str, list[str]]:
    """Lookup by Semantic Scholar ID, then generate BibTeX."""
    warnings: list[str] = []

    s2_client = app.registry.get("semantic_scholar")
    if not s2_client:
        warnings.append("Semantic Scholar client not available")
        return None, "", warnings

    from clients.semantic_scholar import SemanticScholarClient
    if isinstance(s2_client, SemanticScholarClient):
        paper = await s2_client.get_by_id(s2_id)
        if not paper:
            warnings.append(f"Paper not found for Semantic Scholar ID {s2_id}")
            return None, "", warnings

        # If DOI available, try Crossref BibTeX
        if paper.doi:
            crossref = app.registry.get("crossref")
            entry, bib, w = await _bibtex_from_doi(paper.doi, app, crossref)
            warnings.extend(w)
            if entry:
                return entry, bib, warnings

        key, entry_type, fields = build_bibtex_entry(paper)
        bib_text = to_bibtex(entry_type, key, fields)

        return (
            BibtexEntry(citation_key=key, entry_type=entry_type, fields=fields, source_api="semantic_scholar"),
            bib_text,
            warnings,
        )

    warnings.append("Semantic Scholar client type mismatch")
    return None, "", warnings


async def _get_paper_by_doi(doi: str, app: AppContext) -> Optional[PaperMetadata]:
    """Try all clients that support DOI lookup."""
    for name in ["crossref", "semantic_scholar"]:
        client = app.registry.get(name)
        if client:
            paper = await client.get_by_doi(doi)
            if paper:
                return paper
    return None


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    mcp.run(transport="stdio")
