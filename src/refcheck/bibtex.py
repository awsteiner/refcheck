"""BibTeX generation and formatting utilities."""

from __future__ import annotations

import re

from refcheck.models import PaperMetadata


_STOPWORDS = {"a", "an", "the", "on", "in", "for", "of", "and", "with", "to", "is", "are", "at", "by", "from"}


def extract_last_name(name: str) -> str:
    """Extract last name from 'First Last' or 'Last, First'."""
    name = name.strip()
    if "," in name:
        return name.split(",")[0].strip()
    parts = name.split()
    return parts[-1] if parts else name


def make_citation_key(authors: list[str], year: int | None, title: str) -> str:
    """Generate citation key: {lastname}{year}{first_significant_word}."""
    last_name = extract_last_name(authors[0]).lower() if authors else "unknown"
    last_name = re.sub(r"[^a-z]", "", last_name)

    year_str = str(year) if year else "nd"

    words = title.lower().split()
    significant = next(
        (w for w in words if w not in _STOPWORDS and len(w) > 2),
        words[0] if words else "untitled",
    )
    significant = re.sub(r"[^a-z0-9]", "", significant)

    return f"{last_name}{year_str}{significant}"


def format_authors_bibtex(authors: list[str]) -> str:
    """Convert ['Ashish Vaswani', 'Noam Shazeer'] to 'Vaswani, Ashish and Shazeer, Noam'."""
    formatted = []
    for author in authors:
        author = author.strip()
        if not author:
            continue
        if "," in author:
            # Already in "Last, First" format
            formatted.append(author)
        else:
            parts = author.split()
            if len(parts) >= 2:
                formatted.append(f"{parts[-1]}, {' '.join(parts[:-1])}")
            else:
                formatted.append(author)
    return " and ".join(formatted)


def protect_capitals(title: str) -> str:
    """Wrap acronyms and proper nouns in {} for BibTeX capitalization protection."""
    # Protect all-caps words (acronyms): LSTM -> {LSTM}
    title = re.sub(r"\b([A-Z]{2,})\b", r"{\1}", title)
    # Protect words starting with capital mid-sentence (proper nouns)
    words = title.split()
    if len(words) > 1:
        for i in range(1, len(words)):
            w = words[i]
            if w and w[0].isupper() and not w.startswith("{"):
                words[i] = "{" + w + "}"
    return " ".join(words)


def escape_bibtex(text: str) -> str:
    """Escape special BibTeX characters in text."""
    for char in ["&", "%", "#"]:
        text = text.replace(char, f"\\{char}")
    return text


def infer_entry_type(paper: PaperMetadata) -> str:
    """Map publication types to BibTeX entry type."""
    types = [t.lower() for t in paper.publication_types]

    # Check Crossref types
    if "journal-article" in types:
        return "article"
    if "proceedings-article" in types or "conference" in types:
        return "inproceedings"
    if "book" in types:
        return "book"
    if "book-chapter" in types:
        return "inbook"

    # Check Semantic Scholar types
    if "JournalArticle" in paper.publication_types:
        return "article"
    if "Conference" in paper.publication_types:
        return "inproceedings"
    if "Book" in paper.publication_types:
        return "book"
    if "Review" in paper.publication_types:
        return "article"

    # arXiv preprints
    if paper.arxiv_id and not paper.doi:
        return "misc"

    # Default
    return "article"


def split_bibtex_entries(text: str) -> list[str]:
    """Split a BibTeX document into raw entry strings.

    Each returned string starts at an ``@`` and spans the full
    brace-balanced entry body. Whitespace and stray text between
    entries is ignored. The splitter is brace-aware so entries whose
    field values contain nested braces are kept intact.
    """
    entries: list[str] = []
    i = 0
    length = len(text)
    while i < length:
        # Locate the next entry start.
        at = text.find("@", i)
        if at == -1:
            break
        # Locate the opening brace that follows the entry type.
        brace = text.find("{", at)
        if brace == -1:
            break
        # Walk forward tracking brace depth to find the matching close.
        depth = 0
        j = brace
        while j < length:
            char = text[j]
            if char == "{":
                depth += 1
            elif char == "}":
                depth -= 1
                if depth == 0:
                    j += 1
                    break
            j += 1
        entries.append(text[at:j].strip())
        i = j
    return entries


def parse_entry_key(entry: str) -> str | None:
    """Extract the citation key from a raw BibTeX entry string.

    Returns ``None`` for entries that carry no citation key, such as
    ``@comment``, ``@string``, and ``@preamble`` blocks, so callers can
    preserve those verbatim instead of merging them by key.
    """
    match = re.match(r"\s*@(\w+)\s*\{\s*([^,\s]+)\s*,", entry)
    if not match:
        return None
    entry_type = match.group(1).lower()
    if entry_type in {"comment", "string", "preamble"}:
        return None
    return match.group(2).strip()


def to_bibtex(entry_type: str, key: str, fields: dict[str, str]) -> str:
    """Format a dict of fields into a BibTeX entry string."""
    lines = [f"@{entry_type}{{{key},"]
    for field, value in fields.items():
        if value:
            lines.append(f"  {field:<13} = {{{value}}},")
    lines.append("}")
    return "\n".join(lines)


def build_bibtex_entry(paper: PaperMetadata) -> tuple[str, str, dict[str, str]]:
    """Full pipeline from PaperMetadata -> (citation_key, entry_type, fields)."""
    entry_type = infer_entry_type(paper)
    key = make_citation_key(paper.authors, paper.year, paper.title)

    fields: dict[str, str] = {}

    if paper.authors:
        fields["author"] = format_authors_bibtex(paper.authors)

    if paper.title:
        fields["title"] = protect_capitals(escape_bibtex(paper.title))

    if entry_type == "inproceedings":
        if paper.venue:
            fields["booktitle"] = escape_bibtex(paper.venue)
    elif entry_type in ("article", "book", "inbook"):
        if paper.venue:
            fields["journal"] = escape_bibtex(paper.venue)

    if paper.year:
        fields["year"] = str(paper.year)

    if paper.doi:
        fields["doi"] = paper.doi

    if paper.url:
        fields["url"] = paper.url

    if paper.arxiv_id:
        fields["eprint"] = paper.arxiv_id
        fields["archivePrefix"] = "arXiv"
        # Extract primary class from publication_types if available
        for pt in paper.publication_types:
            if "." in pt:  # arXiv category like "cs.LG"
                fields["primaryClass"] = pt
                break

    if paper.abstract:
        fields["abstract"] = escape_bibtex(paper.abstract)

    return key, entry_type, fields
