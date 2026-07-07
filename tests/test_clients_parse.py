"""Offline parsing tests for the INSPIRE-HEP and NASA ADS clients.

These use captured API response fragments so they run without any
network access or API keys.
"""

from refcheck.clients.ads import ADSClient
from refcheck.clients.inspire import InspireClient

# A trimmed real INSPIRE literature hit (journal article with a DOI).
_INSPIRE_HIT = {
    "metadata": {
        "control_number": 4328,
        "titles": [{"title": "Advantages of the Color Octet Gluon Picture"}],
        "authors": [
            {"full_name": "Fritzsch, H."},
            {"full_name": "Gell-Mann, M."},
            {"full_name": "Leutwyler, H."},
        ],
        "dois": [{"value": "10.1016/0370-2693(73)90625-4"}],
        "arxiv_eprints": [],
        "abstracts": [{"value": "It is pointed out that there are several "
                       "advantages in abstracting properties of hadrons."}],
        "earliest_date": "1973-11-01",
        "publication_info": [
            {"journal_title": "Phys.Lett.B", "journal_volume": "47",
             "page_start": "365", "year": 1973},
        ],
        "citation_count": 1234,
    }
}

# A trimmed real INSPIRE hit for an arXiv preprint (no DOI).
_INSPIRE_ARXIV_HIT = {
    "metadata": {
        "control_number": 2702854,
        "titles": [{"title": "Attention Is All You Need"}],
        "authors": [{"full_name": "Vaswani, Ashish"}],
        "dois": None,
        "arxiv_eprints": [{"categories": ["cs.CL", "cs.LG"],
                           "value": "1706.03762"}],
        "abstracts": [{"value": "The dominant sequence transduction models."}],
        "earliest_date": "2017-06-12",
        "publication_info": [{"cnum": "C17-12-04.2"}],
        "citation_count": 596,
    }
}

# A trimmed representative NASA ADS search doc.
_ADS_DOC = {
    "title": ["The mass of the neutron star in Vela X-1"],
    "author": ["Barziv, O.", "Kaper, L.", "Van Kerkwijk, M. H."],
    "year": "2001",
    "doi": ["10.1051/0004-6361:20011492"],
    "bibcode": "2001A&A...377..925B",
    "abstract": "We present an analysis of the radial velocity curve.",
    "pub": "Astronomy and Astrophysics",
    "identifier": ["arXiv:astro-ph/0108237", "2001A&A...377..925B"],
}


def test_inspire_parse_journal_article():
    client = InspireClient(None)
    paper = client._parse_hit(_INSPIRE_HIT)
    assert paper is not None
    assert paper.title.startswith("Advantages of the Color Octet")
    assert paper.authors[0] == "Fritzsch, H."
    assert paper.doi == "10.1016/0370-2693(73)90625-4"
    assert paper.year == 1973
    assert paper.venue == "Phys.Lett.B"
    assert paper.source == "inspire"
    assert paper.inspire_id == "4328"
    assert paper.arxiv_id is None
    assert len(paper.abstract) <= 300


def test_inspire_parse_arxiv_preprint_without_doi():
    client = InspireClient(None)
    paper = client._parse_hit(_INSPIRE_ARXIV_HIT)
    assert paper is not None
    assert paper.doi is None
    assert paper.arxiv_id == "1706.03762"
    assert "cs.CL" in paper.publication_types
    assert paper.year == 2017
    assert paper.inspire_id == "2702854"
    # No journal in publication_info -> empty venue, not a crash.
    assert paper.venue == ""


def test_inspire_parse_missing_title_returns_none():
    client = InspireClient(None)
    assert client._parse_hit({"metadata": {"titles": []}}) is None


def test_ads_parse_doc():
    client = ADSClient(None, "dummy-token")
    paper = client._parse_doc(_ADS_DOC)
    assert paper is not None
    assert paper.title.startswith("The mass of the neutron star")
    assert paper.authors[0] == "Barziv, O."
    assert paper.doi == "10.1051/0004-6361:20011492"
    assert paper.year == 2001
    assert paper.venue == "Astronomy and Astrophysics"
    assert paper.source == "ads"
    assert paper.bibcode == "2001A&A...377..925B"
    # arXiv id extracted from the identifier list.
    assert paper.arxiv_id == "astro-ph/0108237"
    assert paper.url.endswith("2001A&A...377..925B")


def test_ads_parse_doc_missing_title_returns_none():
    client = ADSClient(None, "dummy-token")
    assert client._parse_doc({"bibcode": "x"}) is None
