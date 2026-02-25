# refcheck

**Academic reference verification for AI assistants.**

An [MCP](https://modelcontextprotocol.io/) server that cross-checks academic citations against real publication databases. It catches hallucinated references, finds real papers on a topic, and exports publication-ready BibTeX — all from within Claude Code, Claude Desktop, or any MCP-compatible client.

## The Problem

Large language models frequently hallucinate academic citations: plausible-looking references with fabricated titles, authors, DOIs, or venues. A single hallucinated citation in a paper or grant proposal can undermine credibility.

**refcheck** solves this by giving your AI assistant direct access to Crossref, Semantic Scholar, and arXiv. Every reference it returns is backed by a real database record.

## What It Does

| Tool | Purpose |
|------|---------|
| `verify_reference` | Check if a citation is real. Returns `verified`, `partial_match`, or `not_found` with a confidence score and corrected BibTeX. |
| `search_references` | Find real papers on a topic. Never fabricates — only returns database-backed results. |
| `get_bibtex` | Export publication-ready BibTeX by DOI, title, or Semantic Scholar ID. Supports batch export. |

## Quick Start

### 1. Install

```bash
git clone https://github.com/UMN-Choi-Lab/refcheck.git
cd refcheck
pip install -r requirements.txt
```

**Requirements:** Python 3.11+

### 2. Configure Claude Code

Add to your Claude Code settings (`~/.claude.json`):

```json
{
  "mcpServers": {
    "refcheck": {
      "command": "python",
      "args": ["/absolute/path/to/refcheck/server.py"],
      "env": {
        "CROSSREF_EMAIL": "you@university.edu"
      }
    }
  }
}
```

### 3. Use It

```
> Verify this reference: Li, Y., Yu, R., Shahabi, C., & Liu, Y. (2018).
  Diffusion convolutional recurrent neural network: Data-driven traffic
  forecasting. ICLR 2018.

> Find me 5 recent papers on physics-informed neural networks for traffic
  state estimation and get their BibTeX.

> Get BibTeX for DOI 10.1016/j.trc.2024.104873

> Verify all references in my refs.bib and replace any incorrect entries
  with the corrected BibTeX.
```

## Tools

### `verify_reference`

Checks whether a citation is real by querying multiple databases and fuzzy-matching the results.

**Parameters:**

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `title` | `str` | At least one of `title` or `doi` | Paper title |
| `authors` | `list[str]` | No | Author names (last names sufficient) |
| `year` | `int` | No | Publication year |
| `doi` | `str` | At least one of `title` or `doi` | DOI string |
| `venue` | `str` | No | Journal or conference name |

**Verification pipeline:**

1. **DOI lookup** (if provided) — Resolves via Crossref. A DOI match is near-certain verification.
2. **Title search** — Queries all available databases in parallel.
3. **Fuzzy matching** — Scores each candidate on title similarity (token set ratio), author overlap (Jaccard on last names), year match (+/-1 tolerance), and venue similarity.
4. **Verdict** — Weighted composite score produces a classification:

| Verdict | Score | Meaning |
|---------|-------|---------|
| `verified` | >= 0.85 | High confidence the reference is real and accurate |
| `partial_match` | 0.50 - 0.85 | A similar paper exists but some fields differ |
| `not_found` | < 0.50 | No match in any database. Likely hallucinated. |

**Automatic BibTeX correction:** When the verdict is `verified` or `partial_match`, the response includes a `corrected_bibtex` field with the correct BibTeX entry from the matched paper. This means a single call both detects errors and provides the fix.

**Example response (partial match):**

```json
{
  "verdict": "partial_match",
  "confidence": 0.72,
  "matched_reference": {
    "title": "Attention is All You Need",
    "authors": ["Ashish Vaswani", "Noam Shazeer", "..."],
    "year": 2017,
    "doi": "10.48550/arXiv.1706.03762",
    "venue": "Advances in Neural Information Processing Systems",
    "url": "https://doi.org/10.48550/arXiv.1706.03762"
  },
  "discrepancies": ["Year mismatch: claimed 2018, found 2017"],
  "sources_checked": ["crossref", "semantic_scholar", "arxiv"],
  "corrected_bibtex": "@article{vaswani2017attention,\n  author = {Vaswani, Ashish and ...},\n  title = {Attention is All You Need},\n  year = {2017},\n  doi = {10.48550/arXiv.1706.03762},\n}"
}
```

### `search_references`

Searches for real papers across all configured databases. Results are deduplicated by DOI and title similarity.

**Parameters:**

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `query` | `str` | *required* | Search topic or keywords |
| `max_results` | `int` | 10 | Number of results (1-30) |
| `year_from` | `int` | None | Earliest publication year |
| `year_to` | `int` | None | Latest publication year |
| `databases` | `list[str]` | All available | Which databases to query |

**Each result includes:** title, authors, year, DOI, venue, abstract (truncated), URL, and source database.

### `get_bibtex`

Generates verified BibTeX entries. Prefers Crossref content negotiation (publisher-quality BibTeX) and falls back to metadata construction when needed.

**Parameters (provide one):**

| Parameter | Type | Description |
|-----------|------|-------------|
| `doi` | `str` | Single DOI |
| `dois` | `list[str]` | Multiple DOIs (batch mode) |
| `title` | `str` | Paper title (searches and verifies first) |
| `semantic_scholar_id` | `str` | Semantic Scholar paper ID |

**Example — batch DOI export:**

```
get_bibtex(dois=["10.1016/j.trc.2024.104873", "10.48550/arXiv.1706.03762"])
```

Returns concatenated BibTeX entries ready to append to a `.bib` file:

```bibtex
@article{vaswani2017attention,
  author        = {Vaswani, Ashish and Shazeer, Noam and Parmar, Niki and ...},
  title         = {Attention is All You Need},
  journal       = {Advances in Neural Information Processing Systems},
  year          = {2017},
  doi           = {10.48550/arXiv.1706.03762},
  url           = {https://doi.org/10.48550/arXiv.1706.03762},
}
```

**BibTeX conventions:**
- Citation keys follow `{lastname}{year}{keyword}` format (e.g., `vaswani2017attention`)
- Acronyms and proper nouns are wrapped in `{}` to preserve capitalization
- Entry types are inferred from metadata (`@article`, `@inproceedings`, `@misc`, etc.)
- Fields are never fabricated. Missing data (e.g., page numbers) is omitted rather than guessed.

## Databases

refcheck queries multiple academic databases in parallel. It works out of the box with no API keys — Crossref and arXiv are free and unauthenticated. Adding optional keys unlocks additional sources and better rate limits.

| Database | Key Required | Coverage | Notes |
|----------|:---:|----------|-------|
| **Crossref** | No | 156M+ DOI-registered publications | DOI backbone. Set `CROSSREF_EMAIL` for faster rate limits. |
| **Semantic Scholar** | No | 200M+ papers | Broadest coverage. Free API key recommended. |
| **arXiv** | No | 2.5M+ preprints | Essential for recent ML/CS work. 3s rate limit enforced. |
| **Scopus** | Yes | 90M+ records | Elsevier API key + optional institutional token. |
| **IEEE Xplore** | Yes | IEEE/IET publications | Free tier: 200 requests/day. |

The server operates in **degraded mode** when keys are missing — it skips unavailable APIs and reports which sources were checked.

## Configuration

Copy the example env file and fill in your keys:

```bash
cp .env.example .env
```

Then edit `.env`:

```bash
# Strongly recommended — enables Crossref "polite pool" (~50 req/sec)
CROSSREF_EMAIL=you@university.edu

# Recommended — free key from https://www.semanticscholar.org/product/api
S2_API_KEY=your-s2-key

# Optional — Elsevier API key from https://dev.elsevier.com/
ELSEVIER_KEY=
# Optional — Elsevier institutional token (higher rate limits)
ELSEVIER_INSTTOKEN=

# Optional — free key from https://developer.ieee.org/
IEEE_API_KEY=
```

The `.env` file is gitignored. The server loads it automatically on startup via `python-dotenv`.

Alternatively, you can set environment variables directly or pass them in the `env` block of your MCP server config in `~/.claude.json`.

## Architecture

```
refcheck/
├── server.py              # FastMCP entry point, three tool definitions
├── clients/
│   ├── __init__.py        # Client protocol & registry
│   ├── crossref.py        # Crossref: DOI lookup, title search, BibTeX export
│   ├── semantic_scholar.py # Semantic Scholar: keyword search, paper lookup
│   ├── arxiv.py           # arXiv: title/keyword search, XML parsing
│   ├── scopus.py          # Scopus: search with API key (optional)
│   └── ieee.py            # IEEE Xplore: search with API key (optional)
├── matching.py            # Fuzzy matching: title, author, year, venue scoring
├── bibtex.py              # BibTeX generation, formatting, citation keys
├── models.py              # Pydantic v2 input/output models
├── config.py              # Environment variable management
└── requirements.txt       # mcp, httpx, pydantic, rapidfuzz
```

**Key design decisions:**
- **Async throughout** — All API calls use `httpx.AsyncClient` with 10-second timeouts.
- **Parallel queries** — Multiple databases are queried simultaneously via `asyncio.gather`.
- **Graceful degradation** — One failing API never blocks others. Errors are logged to stderr and the response indicates which sources were checked.
- **Never fabricates** — If no match is found, the tool says so. BibTeX fields are omitted rather than guessed.

## Use Cases

- **Writing papers** — Verify every citation before submission. Catch wrong years, misspelled authors, or entirely fabricated references. Partial matches return corrected BibTeX automatically.
- **Fixing .bib files** — Verify references in bulk and replace incorrect entries with database-backed corrected BibTeX in a single pass.
- **Literature reviews** — Search for real papers on a topic and get BibTeX in one step.
- **Grant proposals** — Ensure all referenced prior work actually exists.
- **Teaching** — Help students verify citations in their assignments.
- **Batch BibTeX export** — Collect DOIs from a draft and export a clean `.bib` file.

## Limitations

- **Coverage gaps** — Some niche publications may not appear in the queried databases. A `not_found` result does not guarantee the paper is fake; it may exist in databases not covered (e.g., PubMed for biomedical, DBLP for CS).
- **Metadata quality** — BibTeX constructed from metadata (when Crossref content negotiation is unavailable) may be missing fields like page numbers or volume.
- **Rate limits** — arXiv enforces a 3-second delay between requests. IEEE free tier is limited to 200 requests/day.
- **No Google Scholar** — There is no official Google Scholar API. The existing coverage from Crossref + Semantic Scholar + arXiv is broader and more reliable.

## License

MIT

## Citation

If you use refcheck in your research workflow, a mention is appreciated:

```
refcheck: Academic Reference Verification MCP Server
https://github.com/UMN-Choi-Lab/refcheck
```
