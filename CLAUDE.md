# refcheck — Academic Reference Verification MCP Server

## Project Overview

Build an MCP server (`refcheck`) that verifies academic references against real publication databases, searches for legitimate references, and exports verified BibTeX entries. This prevents AI hallucinated citations by cross-checking against Crossref, Semantic Scholar, arXiv, and optionally Scopus and IEEE Xplore APIs.

## Problem Statement

LLMs frequently hallucinate academic citations — generating plausible-looking but nonexistent references with fabricated titles, authors, DOIs, or venues. This MCP provides three capabilities:

1. **Verify** a given reference against real databases and return a confidence verdict
2. **Search** for real, verified references on a topic so the AI never needs to fabricate one
3. **Export BibTeX** for verified references, ready to paste into a .bib file

## Architecture

```
refcheck/
├── server.py              # FastMCP server entry point, tool registration
├── clients/
│   ├── crossref.py        # Crossref API client (no key, DOI backbone)
│   ├── semantic_scholar.py # Semantic Scholar API client (free key)
│   ├── arxiv.py           # arXiv API client (no key)
│   ├── scopus.py          # Elsevier/Scopus API client (key required)
│   └── ieee.py            # IEEE Xplore API client (key required)
├── bibtex.py              # BibTeX generation and formatting
├── matching.py            # Fuzzy matching logic for verification
├── models.py              # Pydantic input/output models
├── config.py              # API key management via environment variables
├── requirements.txt
└── README.md
```

## Technology Stack

- **Language**: Python 3.11+
- **MCP Framework**: FastMCP (`mcp` Python SDK)
- **HTTP Client**: `httpx` (async)
- **Validation**: Pydantic v2
- **Fuzzy Matching**: `rapidfuzz` (Levenshtein distance, token set ratio)
- **Transport**: stdio (for Claude Code local usage)

## Tools to Implement

### Tool 1: `verify_reference`

**Purpose**: Takes a citation and checks whether it's real.

**Input** (all optional except at least one of title/doi must be provided):

- `title: str` — Paper title
- `authors: list[str]` — Author names (last names sufficient)
- `year: int` — Publication year
- `doi: str` — DOI string
- `venue: str` — Journal or conference name

**Verification Pipeline**:

1. **DOI lookup first** — If DOI is provided, resolve via Crossref (`https://api.crossref.org/works/{doi}`). This is the highest-confidence path — a Crossref DOI match is essentially 100% verification.
2. **Title search** — Query Semantic Scholar, then arXiv (and Scopus/IEEE if keys available). Normalize the title (lowercase, strip punctuation) before searching.
3. **Fuzzy match** — For each candidate result, compute:
   - Title similarity (token set ratio via `rapidfuzz`, threshold ≥ 85 for match)
   - Author overlap (Jaccard similarity on last names, threshold ≥ 0.5)
   - Year match (exact or ±1 year tolerance)
   - Venue similarity (token set ratio, threshold ≥ 70)
4. **Scoring** — Combine into a composite score and classify:
   - **verified** (≥ 0.85): High confidence the reference is real and accurate
   - **partial_match** (0.5–0.85): A similar paper exists but some fields don't match (e.g., wrong year, misspelled author). Return the closest real match.
   - **not_found** (< 0.5): No matching paper found in any database. Likely hallucinated.

**Output**:

- `verdict: str` — "verified" | "partial_match" | "not_found"
- `confidence: float` — 0.0 to 1.0
- `matched_reference: dict | null` — The real reference found (title, authors, year, doi, venue, url)
- `discrepancies: list[str]` — What didn't match (e.g., "Year mismatch: claimed 2022, found 2021")
- `sources_checked: list[str]` — Which APIs returned results

### Tool 2: `search_references`

**Purpose**: Search for real papers on a topic. Returns only verified, real references.

**Input**:

- `query: str` — Search topic or keywords
- `max_results: int` — Number of results (default 10, max 30)
- `year_from: int` — Filter: earliest publication year
- `year_to: int` — Filter: latest publication year
- `databases: list[str]` — Which databases to query (default: all available)

**Implementation**:

1. Query each selected API with the search terms
2. Deduplicate results by DOI (prefer Crossref/Semantic Scholar metadata when available, as they are most structured)
3. Normalize and format each result consistently

**Output** (list of):

- `title: str`
- `authors: list[str]`
- `year: int`
- `doi: str | null`
- `venue: str`
- `abstract: str` (truncated to ~300 chars)
- `url: str`
- `source: str` — Which database it came from

### Tool 3: `get_bibtex`

**Purpose**: Generate a verified BibTeX entry (or multiple entries) ready for a .bib file. This tool ensures every exported entry corresponds to a real publication.

**Input** (provide ONE of the following):

- `doi: str` — A single DOI to look up
- `dois: list[str]` — Multiple DOIs to batch look up
- `title: str` — Paper title to search for (will verify before exporting)
- `semantic_scholar_id: str` — Semantic Scholar paper ID (S2CID or corpus ID)

**Implementation**:

The preferred BibTeX generation strategy, ordered by quality:

1. **Crossref content negotiation (best quality)** — For any DOI, request BibTeX directly:

   ```
   GET https://api.crossref.org/works/{doi}/transform/application/x-bibtex
   ```

   Crossref returns a well-formatted BibTeX string with correct entry type, all metadata fields, and a stable citation key. This is the gold standard — use it whenever a DOI is available.

2. **Semantic Scholar metadata → construct BibTeX** — If no DOI or Crossref fails, use Semantic Scholar's paper details endpoint to get structured metadata, then build the BibTeX entry programmatically. Use the `citationStyles` field if available from S2 API.

3. **arXiv metadata → construct BibTeX** — For arXiv-only papers without DOIs. Use the `@misc` or `@article` entry type with `eprint`, `archivePrefix`, and `primaryClass` fields.

**BibTeX construction rules** (`bibtex.py`):

- **Citation key format**: `{first_author_last_name}{year}{first_significant_title_word}` all lowercase, e.g., `vaswani2017attention`, `he2016deep`
- **Entry type mapping**:
  - Journal article → `@article`
  - Conference paper → `@inproceedings`
  - Book/book chapter → `@book` / `@inbook`
  - Preprint (arXiv only, no journal) → `@misc` with `eprint` field
  - Thesis → `@phdthesis` / `@mastersthesis`
  - Unknown → `@article` as fallback
- **Required fields per type**:
  - `@article`: author, title, journal, year, volume, number, pages, doi
  - `@inproceedings`: author, title, booktitle, year, pages, doi
  - `@misc`: author, title, year, eprint, archivePrefix, primaryClass
- **Author formatting**: `{Last}, {First} and {Last}, {First}` — standard BibTeX format
- **Special character handling**: Escape `&`, `%`, `#`, `_`, `{`, `}` in titles. Wrap proper nouns and acronyms in `{}` to preserve capitalization (e.g., `{LSTM}`, `{Kalman}`)
- **Always include if available**: `doi`, `url`, `abstract` (as optional field)
- **Never fabricate fields**. If a field (e.g., page numbers, volume) is not available from the API, omit it rather than guessing.

**Output**:

- `bibtex: str` — The complete BibTeX entry/entries as a string, ready to copy-paste or append to a .bib file
- `entries: list[dict]` — Structured data for each entry (citation_key, entry_type, fields, source_api)
- `warnings: list[str]` — Any issues (e.g., "Page numbers not available from Crossref", "Entry type inferred as @article, verify manually")

**Batch mode**: When `dois` list is provided, return all entries concatenated with blank lines between them, suitable for writing directly to a .bib file.

**Example output**:

```bibtex
@article{vaswani2017attention,
  author    = {Vaswani, Ashish and Shazeer, Noam and Parmar, Niki and Uszkoreit, Jakob and Jones, Llion and Gomez, Aidan N. and Kaiser, {\L}ukasz and Polosukhin, Illia},
  title     = {Attention is All You Need},
  journal   = {Advances in Neural Information Processing Systems},
  year      = {2017},
  volume    = {30},
  doi       = {10.48550/arXiv.1706.03762},
  url       = {https://arxiv.org/abs/1706.03762},
}
```

## API Details (Priority Order)

### Tier 1: Core APIs (free, no key or free key — always available)

#### Crossref (DOI verification backbone)

- **Base URL**: `https://api.crossref.org/`
- **Auth**: No key needed. Use "polite pool" by adding email to requests: `mailto:your@email.edu` in the User-Agent or as a query param.
- **Key endpoints**:
  - `GET /works/{doi}` — Lookup by DOI (returns full metadata)
  - `GET /works?query.title={title}` — Search by title
  - `GET /works/{doi}/transform/application/x-bibtex` — **Direct BibTeX export**
  - `HEAD /works/{doi}` — Fast existence check (200 = exists, 404 = doesn't)
- **Rate limit**: ~50 req/sec in polite pool
- **Coverage**: 156M+ records, virtually all DOI-registered publications
- **Why it's #1**: The canonical DOI registry. If a DOI exists in the scholarly world, Crossref almost certainly knows about it. Also the only API that returns native BibTeX via content negotiation.
- **Key env var**: `CROSSREF_EMAIL` (for polite pool, strongly recommended)

#### Semantic Scholar (broad title/author search)

- **Base URL**: `https://api.semanticscholar.org/graph/v1/`
- **Auth**: Free API key (request at semanticscholar.org/product/api). Works without key but at shared rate limit.
- **Key endpoints**:
  - `GET /paper/search?query={query}` — Keyword search
  - `GET /paper/{paper_id}` — Lookup by S2 ID, DOI, arXiv ID, etc.
  - `GET /paper/batch` — Batch lookup (up to 500 papers)
  - `GET /author/search?query={name}` — Author search
- **Paper ID formats accepted**: S2 paper ID, DOI (`DOI:10.xxx`), arXiv ID (`ARXIV:2106.xxxxx`), PubMed ID, ACL ID, Corpus ID
- **Rate limit**: 1 RPS with API key; shared 1000 RPS without (unreliable)
- **Coverage**: 200M+ papers across all fields
- **Why it's #2**: Broadest coverage, excellent structured JSON, supports lookup by many ID types, has citation/reference graph. The `semanticscholar` Python package (`pip install semanticscholar`) provides a convenient async client.
- **Key env var**: `S2_API_KEY`
- **Fields to request**: `title,authors,year,venue,externalIds,abstract,publicationTypes,journal,citationCount,url`

#### arXiv (preprints)

- **Base URL**: `http://export.arxiv.org/api/query`
- **Auth**: None needed
- **Search**: GET with `search_query` param (e.g., `ti:"attention is all you need"`)
- **Response**: Atom XML feed — parse with `xml.etree.ElementTree`
- **Rate limit**: Respect 3-second delay between requests
- **Fields available**: title, authors, abstract, published date, arxiv ID, categories
- **Coverage**: All arXiv preprints (~2.5M papers)
- **Why it matters**: Many CS/transportation/ML papers appear on arXiv before (or instead of) formal publication. Essential for recent work.
- **Notes**: No DOI in arXiv API; construct URL as `https://arxiv.org/abs/{id}`. For BibTeX, use `@misc` with `eprint` field.

### Tier 2: Domain-Specific APIs (API key required, free academic tier)

#### Scopus / Elsevier

- **Base URL**: `https://api.elsevier.com/content/search/scopus`
- **Auth**: Header `X-ELS-APIKey: {key}` or `apiKey` query param
- **Search**: GET with `query` param using Scopus search syntax (e.g., `TITLE("attention is all you need")`)
- **Response**: JSON with `search-results.entry[]`
- **Rate limit**: ~2 requests/second for free academic keys
- **Fields available**: title, authors, DOI, publication name, year, citation count, EID
- **Key env var**: `SCOPUS_API_KEY`
- **Docs**: https://dev.elsevier.com/documentation/ScopusSearchAPI.wadl

#### IEEE Xplore

- **Base URL**: `https://ieeexploreapi.ieee.org/api/v1/search/articles`
- **Auth**: `apikey` query param
- **Search**: GET with `querytext` param
- **Response**: JSON with `articles[]`
- **Rate limit**: 200 requests/day on free tier
- **Fields available**: title, authors, DOI, publication_title, publication_year, abstract, html_url
- **Key env var**: `IEEE_API_KEY`
- **Docs**: https://developer.ieee.org/docs/read/Searching_for_Articles

### Tier 3: Optional Future APIs

These are not in scope for initial implementation but are worth noting:

- **OpenAlex** (`https://api.openalex.org/`) — Free, no key, 250M+ works. Open-source replacement for Microsoft Academic Graph. Good Crossref alternative.
- **DBLP** (`https://dblp.org/search/publ/api`) — Free, no key. CS-specific, excellent for conference papers.
- **PubMed/NCBI** — Free API key. Biomedical focus.

### NOT using: Google Scholar

There is no official Google Scholar API. All available options involve scraping (ToS violation, fragile, paid services). Between Crossref, Semantic Scholar, and arXiv, we already have broader and more reliable coverage.

## Configuration

Use environment variables for API keys. The server should work in **degraded mode** — if a key is missing, skip that API and note it in the response. Crossref and arXiv always work (no key needed), so the server is always functional.

```python
# config.py
import os

CROSSREF_EMAIL = os.getenv("CROSSREF_EMAIL")  # strongly recommended for polite pool
S2_API_KEY = os.getenv("S2_API_KEY")           # free, request at semanticscholar.org
SCOPUS_API_KEY = os.getenv("SCOPUS_API_KEY")   # optional
IEEE_API_KEY = os.getenv("IEEE_API_KEY")        # optional

def available_databases() -> list[str]:
    dbs = ["crossref", "arxiv"]  # always available
    dbs.append("semantic_scholar")  # works without key, better with key
    if SCOPUS_API_KEY:
        dbs.append("scopus")
    if IEEE_API_KEY:
        dbs.append("ieee")
    return dbs
```

## Fuzzy Matching Details (`matching.py`)

Key matching functions:

```python
from rapidfuzz import fuzz

def title_similarity(claimed: str, found: str) -> float:
    """Token set ratio handles word reordering and partial matches."""
    return fuzz.token_set_ratio(normalize(claimed), normalize(found)) / 100.0

def author_overlap(claimed: list[str], found: list[str]) -> float:
    """Jaccard similarity on normalized last names."""
    c = {normalize_name(a) for a in claimed}
    f = {normalize_name(a) for a in found}
    if not c or not f:
        return 0.0
    return len(c & f) / len(c | f)

def compute_verdict(title_sim, author_sim, year_match, venue_sim) -> tuple[str, float]:
    """Weighted composite score → verdict."""
    score = (
        0.45 * title_sim +
        0.25 * author_sim +
        0.15 * (1.0 if year_match else 0.0) +
        0.15 * venue_sim
    )
    if score >= 0.85:
        return "verified", score
    elif score >= 0.50:
        return "partial_match", score
    else:
        return "not_found", score
```

## BibTeX Generation Details (`bibtex.py`)

```python
def make_citation_key(authors: list[str], year: int, title: str) -> str:
    """Generate citation key: first_author_last_name + year + first_significant_word."""
    stopwords = {"a", "an", "the", "on", "in", "for", "of", "and", "with", "to", "is", "are"}
    last_name = extract_last_name(authors[0]).lower() if authors else "unknown"
    words = title.lower().split()
    significant = next((w for w in words if w not in stopwords and len(w) > 2), words[0] if words else "untitled")
    # Remove non-alphanumeric chars
    significant = re.sub(r'[^a-z0-9]', '', significant)
    return f"{last_name}{year}{significant}"

def to_bibtex(entry_type: str, key: str, fields: dict[str, str]) -> str:
    """Format a dict of fields into a BibTeX entry string."""
    lines = [f"@{entry_type}{{{key},"]
    for field, value in fields.items():
        if value:  # skip None/empty
            lines.append(f"  {field:<13} = {{{value}}},")
    lines.append("}")
    return "\n".join(lines)

def format_authors_bibtex(authors: list[str]) -> str:
    """Convert ['Ashish Vaswani', 'Noam Shazeer'] to 'Vaswani, Ashish and Shazeer, Noam'."""
    formatted = []
    for author in authors:
        parts = author.strip().split()
        if len(parts) >= 2:
            formatted.append(f"{parts[-1]}, {' '.join(parts[:-1])}")
        else:
            formatted.append(author)
    return " and ".join(formatted)

def protect_capitals(title: str) -> str:
    """Wrap acronyms and proper nouns in {} for BibTeX capitalization protection."""
    # Protect all-caps words (acronyms): LSTM → {LSTM}
    title = re.sub(r'\b([A-Z]{2,})\b', r'{\1}', title)
    # Protect words starting with capital mid-sentence (proper nouns): Kalman → {Kalman}
    # Skip first word of title
    words = title.split()
    if len(words) > 1:
        for i in range(1, len(words)):
            if words[i][0:1].isupper() and not words[i].startswith('{'):
                words[i] = '{' + words[i] + '}'
    return ' '.join(words)
```

## Claude Code Integration

### Installation in `~/.claude.json`

```json
{
  "mcpServers": {
    "refcheck": {
      "command": "python",
      "args": ["/path/to/refcheck/server.py"],
      "env": {
        "CROSSREF_EMAIL": "your@umn.edu",
        "S2_API_KEY": "your-s2-key",
        "SCOPUS_API_KEY": "your-key-here",
        "IEEE_API_KEY": "your-key-here"
      }
    }
  }
}
```

### Example Usage in Claude Code

```
> Use refcheck to verify this reference: Li, Y., Yu, R., Shahabi, C., & Liu, Y. (2018). Diffusion convolutional recurrent neural network: Data-driven traffic forecasting. ICLR 2018.

> Search refcheck for recent papers on physics-informed neural networks for traffic state estimation, get me bibtex for the top 5.

> Get bibtex for DOI 10.1109/TITS.2021.3054197

> Get bibtex for these DOIs and save to references.bib: 10.1109/TITS.2021.3054197, 10.48550/arXiv.1706.03762, 10.1145/3292500.3330919
```

## Implementation Order

1. **`models.py`** — Define all Pydantic input/output models first
2. **`config.py`** — Environment variable handling
3. **`clients/crossref.py`** — Implement Crossref client (DOI lookup, title search, BibTeX export)
4. **`clients/semantic_scholar.py`** — Implement Semantic Scholar client (title/keyword search, paper details)
5. **`clients/arxiv.py`** — Implement arXiv client
6. **`matching.py`** — Fuzzy matching functions with unit tests
7. **`bibtex.py`** — BibTeX generation and formatting utilities
8. **`server.py`** — Wire up the FastMCP server with all three tools
9. **Test end-to-end** — Verify with known real papers, fake citations, and BibTeX output
10. **`clients/scopus.py`** — Add Scopus client (optional)
11. **`clients/ieee.py`** — Add IEEE client (optional)
12. **Integration test** — Test with Claude Code

## Testing Strategy

### Known-real references (should return "verified"):

- "Attention Is All You Need" — Vaswani et al., 2017, NeurIPS
- "Deep Residual Learning for Image Recognition" — He et al., 2016, CVPR
- "Diffusion Convolutional Recurrent Neural Network: Data-Driven Traffic Forecasting" — Li et al., 2018, ICLR
- A paper from your own publication list for domain-specific testing

### Known-fake references (should return "not_found"):

- "Quantum Neural Transformers for Spatiotemporal Traffic Prediction" — Smith et al., 2023, Nature
- "Deep Reinforcement Learning for Autonomous Intersection Control: A Survey" — Johnson & Williams, 2024, IEEE TITS (fabricated)

### Partial match tests (should return "partial_match"):

- Real title + wrong year
- Real title + misspelled author
- Slightly altered title of a real paper

### BibTeX tests:

- DOI lookup → compare output with known-good BibTeX from publisher
- arXiv-only paper → verify `@misc` with `eprint` field
- Batch DOI lookup → verify concatenated output is valid .bib
- Title with special characters (LaTeX, Unicode) → verify proper escaping
- Citation key generation → verify format and uniqueness

## Error Handling

- API timeouts: 10-second timeout per API call; skip that source and note in response
- Rate limiting: Implement exponential backoff; for arXiv, enforce 3-second minimum between calls
- Malformed input: Pydantic validation with clear error messages
- All APIs down: Return an informative error, never a hallucinated result
- BibTeX: If Crossref content negotiation fails, fall back to constructing from metadata. Always warn about missing fields.

## Important Constraints

- **Never fabricate results.** If no match is found, say so. The entire point of this tool is trustworthiness.
- **Never fabricate BibTeX fields.** Omit unknown fields rather than guessing page numbers, volume, etc.
- **Respect rate limits.** Especially arXiv (3s delay) and IEEE (200/day).
- **Normalize aggressively.** Titles may have different capitalization, special characters, or LaTeX artifacts. Strip all of these before comparison.
- **Prefer DOI matching.** A DOI match is essentially a 100% confidence verification. Always try DOI → Crossref first.
- **Prefer Crossref BibTeX.** Crossref content negotiation returns publisher-quality BibTeX. Only construct manually as fallback.
