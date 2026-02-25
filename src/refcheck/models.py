"""Pydantic v2 input/output models for refcheck."""

from pydantic import BaseModel, Field


class PaperMetadata(BaseModel):
    """Internal shared model across all API clients."""

    title: str = ""
    authors: list[str] = Field(default_factory=list)
    year: int | None = None
    doi: str | None = None
    venue: str = ""
    abstract: str = ""
    url: str = ""
    source: str = ""
    arxiv_id: str | None = None
    s2_id: str | None = None
    publication_types: list[str] = Field(default_factory=list)
    citation_count: int | None = None


# --- verify_reference models ---


class VerifyInput(BaseModel):
    """Input for verify_reference tool."""

    title: str | None = None
    authors: list[str] = Field(default_factory=list)
    year: int | None = None
    doi: str | None = None
    venue: str | None = None


class MatchedReference(BaseModel):
    """The real reference found during verification."""

    title: str
    authors: list[str]
    year: int | None = None
    doi: str | None = None
    venue: str = ""
    url: str = ""


class VerifyResult(BaseModel):
    """Output of verify_reference tool."""

    verdict: str  # "verified" | "partial_match" | "not_found"
    confidence: float
    matched_reference: MatchedReference | None = None
    discrepancies: list[str] = Field(default_factory=list)
    sources_checked: list[str] = Field(default_factory=list)
    corrected_bibtex: str | None = None


# --- search_references models ---


class SearchInput(BaseModel):
    """Input for search_references tool."""

    query: str
    max_results: int = Field(default=10, ge=1, le=30)
    year_from: int | None = None
    year_to: int | None = None
    databases: list[str] | None = None


class SearchResult(BaseModel):
    """A single search result."""

    title: str
    authors: list[str]
    year: int | None = None
    doi: str | None = None
    venue: str = ""
    abstract: str = ""
    url: str = ""
    source: str = ""


# --- get_bibtex models ---


class BibtexInput(BaseModel):
    """Input for get_bibtex tool."""

    doi: str | None = None
    dois: list[str] | None = None
    title: str | None = None
    semantic_scholar_id: str | None = None


class BibtexEntry(BaseModel):
    """Structured data for a single BibTeX entry."""

    citation_key: str
    entry_type: str
    fields: dict[str, str]
    source_api: str


class BibtexResult(BaseModel):
    """Output of get_bibtex tool."""

    bibtex: str
    entries: list[BibtexEntry] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
