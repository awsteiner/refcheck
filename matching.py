"""Fuzzy matching logic for reference verification."""

from __future__ import annotations

import re
import unicodedata

from rapidfuzz import fuzz

from models import PaperMetadata


def normalize_text(text: str) -> str:
    """Lowercase, strip accents, remove punctuation."""
    text = text.lower().strip()
    # Strip accents
    text = unicodedata.normalize("NFKD", text)
    text = "".join(c for c in text if not unicodedata.combining(c))
    # Remove punctuation
    text = re.sub(r"[^\w\s]", "", text)
    # Collapse whitespace
    text = re.sub(r"\s+", " ", text)
    return text


def normalize_author_last_name(name: str) -> str:
    """Extract and normalize last name from 'First Last' or 'Last, First' format."""
    name = name.strip()
    if not name:
        return ""
    if "," in name:
        # "Last, First" format
        last = name.split(",")[0].strip()
    else:
        # "First Last" format
        parts = name.split()
        last = parts[-1] if parts else name
    return normalize_text(last)


def title_similarity(claimed: str, found: str) -> float:
    """Token set ratio handles word reordering and partial matches."""
    c = normalize_text(claimed)
    f = normalize_text(found)
    if not c or not f:
        return 0.0
    return fuzz.token_set_ratio(c, f) / 100.0


def author_overlap(claimed: list[str], found: list[str]) -> float:
    """Jaccard similarity on normalized last names."""
    c = {normalize_author_last_name(a) for a in claimed if a}
    f = {normalize_author_last_name(a) for a in found if a}
    c.discard("")
    f.discard("")
    if not c or not f:
        return 0.0
    return len(c & f) / len(c | f)


def year_match(claimed: int | None, found: int | None) -> bool:
    """Exact or +/-1 year tolerance."""
    if claimed is None or found is None:
        return False
    return abs(claimed - found) <= 1


def venue_similarity(claimed: str | None, found: str | None) -> float:
    """Token set ratio on venue names."""
    if not claimed or not found:
        return 0.0
    c = normalize_text(claimed)
    f = normalize_text(found)
    if not c or not f:
        return 0.0
    return fuzz.token_set_ratio(c, f) / 100.0


def compute_match_score(
    title_sim: float,
    author_sim: float,
    year_matched: bool,
    venue_sim: float,
) -> tuple[str, float]:
    """Weighted composite score -> verdict."""
    score = (
        0.45 * title_sim
        + 0.25 * author_sim
        + 0.15 * (1.0 if year_matched else 0.0)
        + 0.15 * venue_sim
    )
    if score >= 0.85:
        return "verified", round(score, 4)
    elif score >= 0.50:
        return "partial_match", round(score, 4)
    else:
        return "not_found", round(score, 4)


def find_discrepancies(
    claimed_title: str | None,
    claimed_authors: list[str],
    claimed_year: int | None,
    claimed_venue: str | None,
    found: PaperMetadata,
) -> list[str]:
    """List specific mismatches between claimed and found references."""
    discrepancies = []

    if claimed_title and found.title:
        sim = title_similarity(claimed_title, found.title)
        if sim < 0.95:
            discrepancies.append(
                f"Title mismatch (similarity {sim:.0%}): claimed '{claimed_title}', found '{found.title}'"
            )

    if claimed_authors and found.authors:
        overlap = author_overlap(claimed_authors, found.authors)
        if overlap < 1.0:
            claimed_set = {normalize_author_last_name(a) for a in claimed_authors if a}
            found_set = {normalize_author_last_name(a) for a in found.authors if a}
            missing = claimed_set - found_set
            extra = found_set - claimed_set
            if missing:
                discrepancies.append(f"Authors not found in real paper: {', '.join(sorted(missing))}")
            if extra:
                discrepancies.append(f"Additional authors in real paper: {', '.join(sorted(extra))}")

    if claimed_year is not None and found.year is not None:
        if claimed_year != found.year:
            discrepancies.append(f"Year mismatch: claimed {claimed_year}, found {found.year}")

    if claimed_venue and found.venue:
        sim = venue_similarity(claimed_venue, found.venue)
        if sim < 0.80:
            discrepancies.append(
                f"Venue mismatch: claimed '{claimed_venue}', found '{found.venue}'"
            )

    return discrepancies


def score_candidate(
    claimed_title: str | None,
    claimed_authors: list[str],
    claimed_year: int | None,
    claimed_venue: str | None,
    candidate: PaperMetadata,
) -> tuple[str, float, list[str]]:
    """Score a single candidate against claimed reference. Returns (verdict, confidence, discrepancies)."""
    t_sim = title_similarity(claimed_title or "", candidate.title) if claimed_title else 0.0
    a_sim = author_overlap(claimed_authors, candidate.authors) if claimed_authors else 0.0
    y_match = year_match(claimed_year, candidate.year)
    v_sim = venue_similarity(claimed_venue, candidate.venue) if claimed_venue else 0.0

    verdict, confidence = compute_match_score(t_sim, a_sim, y_match, v_sim)
    discrepancies = find_discrepancies(claimed_title, claimed_authors, claimed_year, claimed_venue, candidate)

    return verdict, confidence, discrepancies
