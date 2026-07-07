"""FastMCP server entry point — refcheck academic reference verification."""

import asyncio
import os
import sys
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import httpx
from mcp.server.fastmcp import Context, FastMCP
from mcp.types import ToolAnnotations

from refcheck.bibtex import (
    build_bibtex_entry,
    parse_entry_key,
    split_bibtex_entries,
    to_bibtex,
)
from refcheck.cache import RefCache
from refcheck.clients import ClientRegistry
from refcheck.config import Settings
from refcheck.matching import score_candidate, title_similarity
from refcheck.models import (
    BibtexEntry,
    BibtexResult,
    MatchedReference,
    PaperMetadata,
    SearchResult,
    VerifyResult,
    WriteBibtexResult,
)


@dataclass
class AppContext:
    http: httpx.AsyncClient
    registry: ClientRegistry
    settings: Settings
    cache: RefCache


@asynccontextmanager
async def app_lifespan(server: FastMCP):
    settings = Settings.from_env()
    cache = RefCache(settings.cache_path)
    await cache.open()
    try:
        async with httpx.AsyncClient(timeout=10.0, follow_redirects=True) as http:
            registry = ClientRegistry(http, settings)
            ctx = AppContext(
                http=http, registry=registry, settings=settings, cache=cache
            )
            yield ctx
    finally:
        await cache.close()


mcp = FastMCP("refcheck", lifespan=app_lifespan)


def _get_ctx(ctx: Context) -> AppContext:
    """Extract AppContext from MCP context."""
    return ctx.request_context.lifespan_context


# ---------------------------------------------------------------------------
# Tool 1: verify_reference
# ---------------------------------------------------------------------------


@mcp.tool(
    annotations=ToolAnnotations(
        title="Verify Reference",
        readOnlyHint=True,
        openWorldHint=True,
    )
)
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

    # Step 0: Check cache first
    cached_paper: Optional[PaperMetadata] = None
    cached_bib: Optional[str] = None
    if doi:
        hit = await app.cache.get_paper_by_doi(doi)
        if hit:
            cached_paper, cached_bib = hit
    if not cached_paper and title:
        hit = await app.cache.get_paper_by_title(title)
        if hit:
            cached_paper, cached_bib = hit

    if cached_paper:
        sources_checked.append("cache")
        verdict, confidence, discrepancies = score_candidate(
            title, authors, year, venue, cached_paper
        )
        if doi and confidence < 1.0:
            confidence = max(confidence, 0.90)
            if verdict == "not_found":
                verdict = "partial_match"
        return VerifyResult(
            verdict=verdict,
            confidence=confidence,
            matched_reference=_to_matched_ref(cached_paper),
            discrepancies=discrepancies,
            sources_checked=sources_checked,
            corrected_bibtex=cached_bib,
        ).model_dump()

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
                # Cache the result
                await app.cache.save_paper(paper, corrected_bib)
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
            # Cache the best match
            await app.cache.save_paper(best_paper, corrected_bib)

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


async def _native_bibtex(paper: PaperMetadata, app: AppContext) -> Optional[str]:
    """Fetch publisher-quality BibTeX from a native export endpoint.

    Dispatches on the paper's source and available identifiers rather
    than isinstance checks: the record's own database (INSPIRE by recid,
    ADS by bibcode) is preferred, then Crossref content negotiation by
    DOI. Any client exposing a ``get_bibtex`` method is eligible. Returns
    None when no native BibTeX is available.
    """
    # (source, native identifier) pairs to try, most authoritative first.
    attempts: list[tuple[str, Optional[str]]] = []
    if paper.source == "inspire" and paper.inspire_id:
        attempts.append(("inspire", paper.inspire_id))
    if paper.source == "ads" and paper.bibcode:
        attempts.append(("ads", paper.bibcode))
    if paper.doi:
        attempts.append(("crossref", paper.doi))

    for source, native_id in attempts:
        client = app.registry.get(source)
        get_bibtex = getattr(client, "get_bibtex", None) if client else None
        if get_bibtex is None or not native_id:
            continue
        bib_text = await get_bibtex(native_id)
        if bib_text:
            return bib_text.strip()
    return None


async def _get_corrected_bibtex(paper: PaperMetadata, app: AppContext) -> Optional[str]:
    """Get correct BibTeX for a matched paper. Native export first, then construct."""
    bib_text = await _native_bibtex(paper, app)
    if bib_text:
        return bib_text

    # Fallback: construct from metadata
    key, entry_type, fields = build_bibtex_entry(paper)
    return to_bibtex(entry_type, key, fields)


# ---------------------------------------------------------------------------
# Tool 2: search_references
# ---------------------------------------------------------------------------


@mcp.tool(
    annotations=ToolAnnotations(
        title="Search References",
        readOnlyHint=True,
        openWorldHint=True,
    )
)
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

    # Check search cache first
    cached = await app.cache.get_search(
        query,
        year_from=year_from,
        year_to=year_to,
        databases=databases,
        max_results=max_results,
    )
    if cached is not None:
        return {
            "results": cached,
            "total": len(cached),
            "sources_queried": ["cache"],
        }

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

    result_dicts = [
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
    ]

    # Cache search results and individual papers
    await app.cache.save_search(
        query,
        result_dicts,
        year_from=year_from,
        year_to=year_to,
        databases=databases,
        max_results=max_results,
    )
    for p in deduped:
        await app.cache.save_paper(p)

    return {
        "results": result_dicts,
        "total": len(deduped),
        "sources_queried": sources_queried,
    }


def _deduplicate(papers: list[PaperMetadata]) -> list[PaperMetadata]:
    """Deduplicate papers by DOI, then by title similarity >= 0.95."""
    # Priority: crossref > semantic_scholar > arxiv > others
    source_priority = {
        "crossref": 0, "ads": 1, "inspire": 2, "semantic_scholar": 3,
        "scopus": 4, "ieee": 5, "arxiv": 6,
    }

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


def _bibtex_entry_from_text(bib_text: str, source_api: str) -> BibtexEntry:
    """Build a BibtexEntry envelope from a raw BibTeX string.

    Parses the citation key and entry type out of the text. The raw
    BibTeX itself is carried in the sibling ``bibtex`` string, so the
    ``fields`` dict only records the provenance marker.
    """
    import re

    key_match = re.search(r"@\w+\{([^,]+),", bib_text)
    key = key_match.group(1) if key_match else source_api
    type_match = re.search(r"@(\w+)\{", bib_text)
    entry_type = type_match.group(1) if type_match else "article"
    return BibtexEntry(
        citation_key=key,
        entry_type=entry_type,
        fields={"raw_bibtex": f"from_{source_api}"},
        source_api=source_api,
    )


async def _collect_bibtex(
    app: AppContext,
    doi: Optional[str] = None,
    dois: Optional[list[str]] = None,
    title: Optional[str] = None,
    semantic_scholar_id: Optional[str] = None,
) -> tuple[list[BibtexEntry], list[str], list[str]]:
    """Resolve identifiers to verified BibTeX, checking the cache first.

    Shared by get_bibtex and write_bibtex. Returns the structured
    entries, the raw BibTeX text parts, and any warnings. Every entry
    corresponds to a real publication; fields are never fabricated.
    """
    entries: list[BibtexEntry] = []
    bibtex_parts: list[str] = []
    warnings: list[str] = []

    # Collect all DOIs to process
    all_dois: list[str] = []
    if doi:
        all_dois.append(doi)
    if dois:
        all_dois.extend(dois)

    # Process DOIs (check cache first)
    if all_dois:
        crossref = app.registry.get("crossref")
        for d in all_dois:
            # Try cache first
            cached_bib = await app.cache.get_bibtex_by_doi(d)
            if cached_bib:
                import re as _re
                key_match = _re.search(r"@\w+\{([^,]+),", cached_bib)
                key = key_match.group(1) if key_match else d
                type_match = _re.search(r"@(\w+)\{", cached_bib)
                entry_type = type_match.group(1) if type_match else "article"
                entries.append(
                    BibtexEntry(
                        citation_key=key,
                        entry_type=entry_type,
                        fields={"raw_bibtex": "from_cache"},
                        source_api="cache",
                    )
                )
                bibtex_parts.append(cached_bib)
                continue
            # Cache miss — fetch from APIs
            entry, bib, warns = await _bibtex_from_doi(d, app, crossref)
            if entry:
                entries.append(entry)
                bibtex_parts.append(bib)
                # Cache the BibTeX
                await app.cache.save_bibtex(d, bib)
            warnings.extend(warns)

    # Process title (check cache first)
    if title:
        cached_hit = await app.cache.get_paper_by_title(title)
        if cached_hit and cached_hit[1]:
            paper, cached_bib = cached_hit
            import re as _re
            key_match = _re.search(r"@\w+\{([^,]+),", cached_bib)
            key = key_match.group(1) if key_match else "cached"
            type_match = _re.search(r"@(\w+)\{", cached_bib)
            entry_type = type_match.group(1) if type_match else "article"
            entries.append(
                BibtexEntry(
                    citation_key=key,
                    entry_type=entry_type,
                    fields={"raw_bibtex": "from_cache"},
                    source_api="cache",
                )
            )
            bibtex_parts.append(cached_bib)
        else:
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

    return entries, bibtex_parts, warnings


@mcp.tool(
    annotations=ToolAnnotations(
        title="Get BibTeX",
        readOnlyHint=True,
        openWorldHint=True,
    )
)
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
    entries, bibtex_parts, warnings = await _collect_bibtex(
        app, doi=doi, dois=dois, title=title,
        semantic_scholar_id=semantic_scholar_id,
    )
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
    import re
    warnings: list[str] = []

    # Try Crossref native BibTeX (gold standard)
    if crossref:
        from refcheck.clients.crossref import CrossrefClient
        if isinstance(crossref, CrossrefClient):
            bib_text = await crossref.get_bibtex(doi)
            if bib_text:
                bib_text = bib_text.strip()
                # Cache the BibTeX
                await app.cache.save_bibtex(doi, bib_text)
                # Parse the citation key from the bibtex
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
                    bib_text,
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

    # Prefer the record's own database BibTeX for INSPIRE/ADS records.
    if best_paper.source in ("inspire", "ads"):
        native = await _native_bibtex(best_paper, app)
        if native:
            entry = _bibtex_entry_from_text(native, best_paper.source)
            await app.cache.save_paper(best_paper, native)
            return entry, native, warnings

    # If we found a DOI, try Crossref BibTeX first
    if best_paper.doi:
        crossref = app.registry.get("crossref")
        entry, bib, w = await _bibtex_from_doi(best_paper.doi, app, crossref)
        warnings.extend(w)
        if entry:
            # Cache paper with BibTeX
            await app.cache.save_paper(best_paper, bib)
            return entry, bib, warnings

    # Construct from metadata
    key, entry_type, fields = build_bibtex_entry(best_paper)
    bib_text = to_bibtex(entry_type, key, fields)
    # Cache paper with constructed BibTeX
    await app.cache.save_paper(best_paper, bib_text)

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

    from refcheck.clients.semantic_scholar import SemanticScholarClient
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
                await app.cache.save_paper(paper, bib)
                return entry, bib, warnings

        key, entry_type, fields = build_bibtex_entry(paper)
        bib_text = to_bibtex(entry_type, key, fields)
        await app.cache.save_paper(paper, bib_text)

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
# Tool 4: cache_stats
# ---------------------------------------------------------------------------


@mcp.tool(
    annotations=ToolAnnotations(
        title="Cache Statistics",
        readOnlyHint=True,
        openWorldHint=False,
    )
)
async def cache_stats(ctx: Context) -> dict:
    """Show refcheck cache statistics (cached papers, searches, etc.)."""
    app = _get_ctx(ctx)
    return await app.cache.stats()


# ---------------------------------------------------------------------------
# Tool 5: write_bibtex
# ---------------------------------------------------------------------------


def _merge_bibtex_text(
    existing: str, incoming: str, mode: str
) -> tuple[str, list[str], list[str], list[str], int]:
    """Merge incoming BibTeX into an existing document by citation key.

    ``mode`` is one of ``merge`` (replace entries whose key already
    exists and append new ones), ``append`` (add only keys not present),
    or ``overwrite`` (discard existing content). Returns the merged text
    along with the added, updated, and skipped keys and the total entry
    count. Unkeyed blocks such as @comment or @string are preserved.
    """
    existing_entries = split_bibtex_entries(existing) if existing else []
    incoming_entries = split_bibtex_entries(incoming)

    # Ordered list of (key, raw); a None key marks an unkeyed block.
    ordered: list[tuple[Optional[str], str]] = []
    index_by_key: dict[str, int] = {}
    added: list[str] = []
    updated: list[str] = []
    skipped: list[str] = []

    if mode != "overwrite":
        for raw in existing_entries:
            key = parse_entry_key(raw)
            if key is not None:
                index_by_key[key] = len(ordered)
            ordered.append((key, raw))

    for raw in incoming_entries:
        key = parse_entry_key(raw)
        if key is None:
            # Unkeyed block: always append, never deduplicated.
            ordered.append((None, raw))
            continue
        if key in index_by_key:
            if mode == "append":
                skipped.append(key)
                continue
            # merge mode: replace the existing entry in place.
            ordered[index_by_key[key]] = (key, raw)
            updated.append(key)
        else:
            index_by_key[key] = len(ordered)
            ordered.append((key, raw))
            added.append(key)

    merged = "\n\n".join(raw for _, raw in ordered)
    if merged and not merged.endswith("\n"):
        merged += "\n"
    total = sum(1 for key, _ in ordered if key is not None)
    return merged, added, updated, skipped, total


@mcp.tool(
    annotations=ToolAnnotations(
        title="Write BibTeX File",
        readOnlyHint=False,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=True,
    )
)
async def write_bibtex(
    ctx: Context,
    path: str,
    bibtex: Optional[str] = None,
    doi: Optional[str] = None,
    dois: Optional[list[str]] = None,
    title: Optional[str] = None,
    semantic_scholar_id: Optional[str] = None,
    mode: str = "merge",
) -> dict:
    """Write or update verified BibTeX entries in a .bib file on disk.

    Provide the entries either as raw ``bibtex`` text or by identifier
    (``doi``, ``dois``, ``title``, or ``semantic_scholar_id``), in which
    case verified BibTeX is generated exactly as get_bibtex would.
    ``mode`` controls how they combine with any existing file: ``merge``
    (default) replaces entries whose citation key already exists and
    appends new ones, ``append`` adds only keys not already present, and
    ``overwrite`` replaces the whole file. Entries are deduplicated by
    citation key and the file is written atomically.
    """
    app = _get_ctx(ctx)
    warnings: list[str] = []
    target = Path(path).expanduser()

    if mode not in {"merge", "append", "overwrite"}:
        warnings.append(
            f"Unknown mode '{mode}'; expected merge, append, or overwrite."
        )
        return WriteBibtexResult(
            path=str(target), warnings=warnings
        ).model_dump()

    # Resolve the incoming BibTeX: explicit text wins, otherwise generate
    # verified entries from the supplied identifiers.
    incoming = bibtex.strip() if bibtex else ""
    if not incoming and (doi or dois or title or semantic_scholar_id):
        _, bibtex_parts, gen_warnings = await _collect_bibtex(
            app, doi=doi, dois=dois, title=title,
            semantic_scholar_id=semantic_scholar_id,
        )
        warnings.extend(gen_warnings)
        incoming = "\n\n".join(bibtex_parts)

    if not incoming:
        warnings.append(
            "No BibTeX to write: supply bibtex text or an identifier."
        )
        return WriteBibtexResult(
            path=str(target), warnings=warnings
        ).model_dump()

    existing = ""
    if target.exists():
        existing = target.read_text(encoding="utf-8")

    merged, added, updated, skipped, total = _merge_bibtex_text(
        existing, incoming, mode
    )

    # Write atomically: write a sibling temp file, then replace.
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(target.name + ".tmp")
    tmp.write_text(merged, encoding="utf-8")
    os.replace(tmp, target)

    return WriteBibtexResult(
        path=str(target),
        written=True,
        added=added,
        updated=updated,
        skipped=skipped,
        total_entries=total,
        warnings=warnings,
    ).model_dump()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    """Entry point for the refcheck MCP server."""
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
