"""CLI entry point for refcheck — replaces MCP stdio with direct command-line usage."""

import asyncio
import json
import sys
from typing import Optional

import httpx

from refcheck.bibtex import build_bibtex_entry, to_bibtex
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
)


async def _make_context():
    settings = Settings.from_env()
    http = httpx.AsyncClient(timeout=10.0, follow_redirects=True)
    registry = ClientRegistry(http, settings)
    return http, registry, settings


# ---------------------------------------------------------------------------
# verify
# ---------------------------------------------------------------------------

async def cmd_verify(
    title: Optional[str] = None,
    authors: Optional[list[str]] = None,
    year: Optional[int] = None,
    doi: Optional[str] = None,
    venue: Optional[str] = None,
) -> dict:
    if not title and not doi:
        return VerifyResult(
            verdict="not_found", confidence=0.0,
            discrepancies=["At least one of title or doi must be provided"],
            sources_checked=[],
        ).model_dump()

    http, registry, settings = await _make_context()
    authors = authors or []
    sources_checked: list[str] = []

    try:
        # DOI lookup
        if doi:
            crossref = registry.get("crossref")
            if crossref:
                paper = await crossref.get_by_doi(doi)
                if paper:
                    sources_checked.append("crossref")
                    verdict, confidence, discrepancies = score_candidate(
                        title, authors, year, venue, paper
                    )
                    if confidence < 1.0:
                        confidence = max(confidence, 0.90)
                        if verdict == "not_found":
                            verdict = "partial_match"
                    corrected_bib = await _get_corrected_bibtex(paper, registry)
                    return VerifyResult(
                        verdict=verdict, confidence=confidence,
                        matched_reference=_to_matched_ref(paper),
                        discrepancies=discrepancies,
                        sources_checked=sources_checked,
                        corrected_bibtex=corrected_bib,
                    ).model_dump()

        # Title search
        if title:
            clients = registry.get_all()
            tasks = [c.search_by_title(title, limit=5) for c in clients]
            results = await asyncio.gather(*tasks, return_exceptions=True)

            all_candidates: list[PaperMetadata] = []
            for client, result in zip(clients, results):
                if isinstance(result, Exception):
                    print(f"[verify] {client.name} error: {result}", file=sys.stderr)
                    continue
                sources_checked.append(client.name)
                all_candidates.extend(result)

            best_verdict = "not_found"
            best_confidence = 0.0
            best_discrepancies: list[str] = []
            best_paper: Optional[PaperMetadata] = None

            for candidate in all_candidates:
                v, c, d = score_candidate(title, authors, year, venue, candidate)
                if c > best_confidence:
                    best_verdict, best_confidence, best_discrepancies = v, c, d
                    best_paper = candidate

            corrected_bib = None
            if best_paper and best_verdict in ("verified", "partial_match"):
                corrected_bib = await _get_corrected_bibtex(best_paper, registry)

            return VerifyResult(
                verdict=best_verdict, confidence=best_confidence,
                matched_reference=_to_matched_ref(best_paper) if best_paper else None,
                discrepancies=best_discrepancies,
                sources_checked=sources_checked,
                corrected_bibtex=corrected_bib,
            ).model_dump()

        return VerifyResult(
            verdict="not_found", confidence=0.0, sources_checked=sources_checked,
        ).model_dump()
    finally:
        await http.aclose()


# ---------------------------------------------------------------------------
# search
# ---------------------------------------------------------------------------

async def cmd_search(
    query: str,
    max_results: int = 10,
    year_from: Optional[int] = None,
    year_to: Optional[int] = None,
    databases: Optional[list[str]] = None,
) -> dict:
    http, registry, settings = await _make_context()

    try:
        clients = registry.get_search_clients(databases)
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

        if year_from is not None:
            all_papers = [p for p in all_papers if p.year and p.year >= year_from]
        if year_to is not None:
            all_papers = [p for p in all_papers if p.year and p.year <= year_to]

        deduped = _deduplicate(all_papers)[:max_results]

        return {
            "results": [
                SearchResult(
                    title=p.title, authors=p.authors, year=p.year,
                    doi=p.doi, venue=p.venue, abstract=p.abstract,
                    url=p.url, source=p.source,
                ).model_dump()
                for p in deduped
            ],
            "total": len(deduped),
            "sources_queried": sources_queried,
        }
    finally:
        await http.aclose()


# ---------------------------------------------------------------------------
# bibtex
# ---------------------------------------------------------------------------

async def cmd_bibtex(
    doi: Optional[str] = None,
    dois: Optional[list[str]] = None,
    title: Optional[str] = None,
    semantic_scholar_id: Optional[str] = None,
) -> dict:
    http, registry, settings = await _make_context()

    try:
        entries: list[BibtexEntry] = []
        bibtex_parts: list[str] = []
        warnings: list[str] = []

        all_dois: list[str] = []
        if doi:
            all_dois.append(doi)
        if dois:
            all_dois.extend(dois)

        if all_dois:
            crossref = registry.get("crossref")
            for d in all_dois:
                entry, bib, warns = await _bibtex_from_doi(d, registry, crossref)
                if entry:
                    entries.append(entry)
                    bibtex_parts.append(bib)
                warnings.extend(warns)

        if title:
            entry, bib, warns = await _bibtex_from_title(title, registry)
            if entry:
                entries.append(entry)
                bibtex_parts.append(bib)
            warnings.extend(warns)

        if semantic_scholar_id:
            entry, bib, warns = await _bibtex_from_s2id(semantic_scholar_id, registry)
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
    finally:
        await http.aclose()


# ---------------------------------------------------------------------------
# Shared helpers (adapted from server.py, no MCP Context dependency)
# ---------------------------------------------------------------------------

def _to_matched_ref(paper: PaperMetadata) -> MatchedReference:
    return MatchedReference(
        title=paper.title, authors=paper.authors, year=paper.year,
        doi=paper.doi, venue=paper.venue, url=paper.url,
    )


async def _get_corrected_bibtex(paper: PaperMetadata, registry: ClientRegistry) -> Optional[str]:
    if paper.doi:
        crossref = registry.get("crossref")
        if crossref:
            from refcheck.clients.crossref import CrossrefClient
            if isinstance(crossref, CrossrefClient):
                bib_text = await crossref.get_bibtex(paper.doi)
                if bib_text:
                    return bib_text.strip()
    key, entry_type, fields = build_bibtex_entry(paper)
    return to_bibtex(entry_type, key, fields)


async def _bibtex_from_doi(doi, registry, crossref):
    warnings = []
    if crossref:
        from refcheck.clients.crossref import CrossrefClient
        if isinstance(crossref, CrossrefClient):
            import re
            bib_text = await crossref.get_bibtex(doi)
            if bib_text:
                key_match = re.search(r"@\w+\{([^,]+),", bib_text)
                key = key_match.group(1) if key_match else doi
                type_match = re.search(r"@(\w+)\{", bib_text)
                entry_type = type_match.group(1) if type_match else "article"
                return (
                    BibtexEntry(citation_key=key, entry_type=entry_type,
                                fields={"raw_bibtex": "from_crossref"}, source_api="crossref"),
                    bib_text.strip(), warnings,
                )

    paper = await _get_paper_by_doi(doi, registry)
    if not paper:
        warnings.append(f"Could not retrieve metadata for DOI {doi}")
        return None, "", warnings
    key, entry_type, fields = build_bibtex_entry(paper)
    bib_text = to_bibtex(entry_type, key, fields)
    warnings.append(f"BibTeX for {doi} constructed from metadata")
    return BibtexEntry(citation_key=key, entry_type=entry_type, fields=fields, source_api=paper.source), bib_text, warnings


async def _bibtex_from_title(title, registry):
    warnings = []
    clients = registry.get_all()
    tasks = [c.search_by_title(title, limit=3) for c in clients]
    results = await asyncio.gather(*tasks, return_exceptions=True)

    best_paper, best_sim = None, 0.0
    for result in results:
        if isinstance(result, Exception):
            continue
        for paper in result:
            sim = title_similarity(title, paper.title)
            if sim > best_sim:
                best_sim, best_paper = sim, paper

    if not best_paper or best_sim < 0.80:
        warnings.append(f"No close match found for title '{title}'")
        return None, "", warnings
    if best_sim < 0.95:
        warnings.append(f"Title match is approximate ({best_sim:.0%}): '{best_paper.title}'")

    if best_paper.doi:
        crossref = registry.get("crossref")
        entry, bib, w = await _bibtex_from_doi(best_paper.doi, registry, crossref)
        warnings.extend(w)
        if entry:
            return entry, bib, warnings

    key, entry_type, fields = build_bibtex_entry(best_paper)
    return BibtexEntry(citation_key=key, entry_type=entry_type, fields=fields, source_api=best_paper.source), to_bibtex(entry_type, key, fields), warnings


async def _bibtex_from_s2id(s2_id, registry):
    warnings = []
    s2_client = registry.get("semantic_scholar")
    if not s2_client:
        warnings.append("Semantic Scholar client not available")
        return None, "", warnings
    from refcheck.clients.semantic_scholar import SemanticScholarClient
    if isinstance(s2_client, SemanticScholarClient):
        paper = await s2_client.get_by_id(s2_id)
        if not paper:
            warnings.append(f"Paper not found for Semantic Scholar ID {s2_id}")
            return None, "", warnings
        if paper.doi:
            crossref = registry.get("crossref")
            entry, bib, w = await _bibtex_from_doi(paper.doi, registry, crossref)
            warnings.extend(w)
            if entry:
                return entry, bib, warnings
        key, entry_type, fields = build_bibtex_entry(paper)
        return BibtexEntry(citation_key=key, entry_type=entry_type, fields=fields, source_api="semantic_scholar"), to_bibtex(entry_type, key, fields), warnings
    warnings.append("Semantic Scholar client type mismatch")
    return None, "", warnings


async def _get_paper_by_doi(doi, registry):
    for name in ["crossref", "semantic_scholar"]:
        client = registry.get(name)
        if client:
            paper = await client.get_by_doi(doi)
            if paper:
                return paper
    return None


def _deduplicate(papers):
    source_priority = {
        "crossref": 0, "ads": 1, "inspire": 2, "semantic_scholar": 3,
        "scopus": 4, "ieee": 5, "arxiv": 6,
    }
    seen_dois: dict[str, PaperMetadata] = {}
    no_doi = []
    for p in papers:
        if p.doi:
            doi_lower = p.doi.lower()
            if doi_lower in seen_dois:
                if source_priority.get(p.source, 99) < source_priority.get(seen_dois[doi_lower].source, 99):
                    seen_dois[doi_lower] = p
            else:
                seen_dois[doi_lower] = p
        else:
            no_doi.append(p)
    result = list(seen_dois.values())
    for p in no_doi:
        if not any(title_similarity(p.title, e.title) >= 0.95 for e in result):
            result.append(p)
    return result


# ---------------------------------------------------------------------------
# Main CLI dispatcher
# ---------------------------------------------------------------------------

USAGE = """Usage: python -m refcheck.cli <command> [JSON args]

Commands:
  verify   '{"title": "...", "doi": "...", "authors": [...], "year": 2024, "venue": "..."}'
  search   '{"query": "...", "max_results": 10, "year_from": 2020, "year_to": 2025}'
  bibtex   '{"doi": "...", "title": "...", "dois": [...], "semantic_scholar_id": "..."}'
"""


def main():
    if len(sys.argv) < 3:
        print(USAGE, file=sys.stderr)
        sys.exit(1)

    command = sys.argv[1]
    try:
        args = json.loads(sys.argv[2])
    except json.JSONDecodeError as e:
        print(f"Invalid JSON: {e}", file=sys.stderr)
        sys.exit(1)

    dispatch = {
        "verify": cmd_verify,
        "search": cmd_search,
        "bibtex": cmd_bibtex,
    }

    if command not in dispatch:
        print(f"Unknown command: {command}. Use: {', '.join(dispatch)}", file=sys.stderr)
        sys.exit(1)

    result = asyncio.run(dispatch[command](**args))
    json.dump(result, sys.stdout, indent=2, ensure_ascii=False)
    print()


if __name__ == "__main__":
    main()
