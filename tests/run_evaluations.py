"""Live runner for the refcheck evaluation suite.

Drives the real MCP tools against the live APIs and checks the
deterministic subset of tests/evaluations.xml. This is a manual /
optional harness (it makes network calls) and is intentionally kept out
of the default pytest run. Invoke directly:

    python tests/run_evaluations.py

INSPIRE and the keyless sources are always exercised. The ADS checks run
only when ADS_API_TOKEN is set in the environment.
"""

from __future__ import annotations

import asyncio
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

import httpx

from refcheck.cache import RefCache
from refcheck.clients import ClientRegistry
from refcheck.config import Settings
from refcheck.server import (
    AppContext,
    _native_bibtex,
    get_bibtex,
    search_references,
    verify_reference,
)


def _ctx(app: AppContext) -> SimpleNamespace:
    """Build a minimal stand-in for the MCP Context object."""
    return SimpleNamespace(
        request_context=SimpleNamespace(lifespan_context=app)
    )


async def _run_checks(app: AppContext) -> list[tuple[str, bool, str]]:
    ctx = _ctx(app)
    results: list[tuple[str, bool, str]] = []

    def record(name: str, ok: bool, detail: str = "") -> None:
        results.append((name, ok, detail))

    # 1. INSPIRE native BibTeX export works (keyless).
    inspire = app.registry.get("inspire")
    bib = await inspire.get_bibtex("2702854")
    record("INSPIRE native bibtex export",
           bool(bib) and bib.lstrip().startswith("@")
           and "Vaswani" in bib, f"head={(bib or '')[:32]!r}")

    # 2. Source-matched dispatch: an INSPIRE record yields INSPIRE
    #    BibTeX (texkey with a colon) even though it also has a DOI.
    papers = await inspire.search_by_title(
        "Advantages of the Color Octet Gluon Picture", 1
    )
    ok = bool(papers)
    detail = "no INSPIRE hit"
    if papers:
        native = await _native_bibtex(papers[0], app)
        ok = bool(native) and "Fritzsch" in native and ":" in \
            native.split(",", 1)[0]
        detail = f"key={(native or '').split(',', 1)[0][1:]!r}"
    record("source-matched native bibtex dispatch", ok, detail)

    # 3. INSPIRE participates in search_references.
    r = await search_references(ctx, query="Higgs boson discovery ATLAS",
                                max_results=5, databases=["inspire"])
    sources = {x["source"] for x in r["results"]}
    record("INSPIRE in search_references",
           bool(r["results"]) and sources == {"inspire"},
           f"n={len(r['results'])} sources={sources}")

    # 4. DOI verification returns a matched reference and native BibTeX.
    r = await verify_reference(ctx, doi="10.1016/0370-2693(73)90625-4")
    ok = r["matched_reference"] is not None and bool(r["corrected_bibtex"]) \
        and r["corrected_bibtex"].lstrip().startswith("@")
    record("verify by DOI -> matched ref + bibtex", ok,
           f"verdict={r['verdict']} "
           f"title={(r['matched_reference'] or {}).get('title', '')[:32]!r}")

    # 5. get_bibtex from a mainstream Crossref DOI yields an entry.
    r = await get_bibtex(ctx, doi="10.1103/PhysRevLett.19.1264")
    ok = bool(r["bibtex"]) and r["bibtex"].lstrip().startswith("@")
    record("get_bibtex by Crossref DOI", ok, f"entries={len(r['entries'])}")

    # 6. A fabricated reference is never fully verified.
    r = await verify_reference(
        ctx,
        title="Quantum Neural Transformers for Spatiotemporal "
              "Traffic Prediction",
        authors=["Smith"], year=2023,
    )
    record("fabricated reference not verified",
           r["verdict"] != "verified", f"verdict={r['verdict']}")

    # 7. search_references honours the year_from filter.
    r = await search_references(ctx, query="physics-informed neural networks",
                                max_results=5, year_from=2019)
    years = [x["year"] for x in r["results"] if x["year"] is not None]
    ok = bool(r["results"]) and all(y >= 2019 for y in years)
    record("search year filter", ok,
           f"n={len(r['results'])} years={sorted(set(years))}")

    # ADS checks (only with a token).
    if app.settings.ads_api_token:
        r = await search_references(
            ctx, query="exoplanet atmosphere characterization",
            max_results=5, databases=["ads"],
        )
        docs = r["results"]
        sources = {x["source"] for x in docs}
        record("ADS search returns ads source",
               bool(docs) and sources == {"ads"},
               f"n={len(docs)} sources={sources}")

        # Export BibTeX for the first returned ADS paper via its DOI.
        first_doi = next((x["doi"] for x in docs if x["doi"]), None)
        if first_doi:
            r = await get_bibtex(ctx, doi=first_doi)
            ok = bool(r["bibtex"]) and r["bibtex"].lstrip().startswith("@")
            record("ADS-sourced get_bibtex", ok,
                   f"doi={first_doi} entries={len(r['entries'])}")
        else:
            record("ADS-sourced get_bibtex", False, "no DOI in ADS results")
    else:
        record("ADS checks", True, "skipped (no ADS_API_TOKEN)")

    return results


async def main() -> int:
    settings = Settings.from_env()
    # Use a throwaway cache so the run is not polluted by, or polluting,
    # the user's persistent cache.
    with tempfile.TemporaryDirectory() as tmp:
        settings.cache_path = Path(tmp) / "eval-cache.db"
        cache = RefCache(settings.cache_path)
        await cache.open()
        try:
            async with httpx.AsyncClient(
                timeout=15.0, follow_redirects=True
            ) as http:
                registry = ClientRegistry(http, settings)
                app = AppContext(
                    http=http, registry=registry,
                    settings=settings, cache=cache,
                )
                results = await _run_checks(app)
        finally:
            await cache.close()

    print(f"\nDatabases: {settings.available_databases()}\n")
    passed = 0
    for name, ok, detail in results:
        mark = "PASS" if ok else "FAIL"
        print(f"[{mark}] {name}" + (f"  ({detail})" if detail else ""))
        passed += 1 if ok else 0
    total = len(results)
    print(f"\n{passed}/{total} checks passed")
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
