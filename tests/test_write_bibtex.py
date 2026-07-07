"""Tests for BibTeX parsing, merging, and the write_bibtex tool."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

from refcheck.bibtex import parse_entry_key, split_bibtex_entries
from refcheck.server import _merge_bibtex_text, write_bibtex


ENTRY_A = (
    "@article{smith2020deep,\n"
    "  author = {Smith, Jane},\n"
    "  title  = {A Deep Study},\n"
    "  year   = {2020},\n"
    "}"
)

# Same key as ENTRY_A but corrected year, to exercise replacement.
ENTRY_A_FIXED = (
    "@article{smith2020deep,\n"
    "  author = {Smith, Jane},\n"
    "  title  = {A Deep Study},\n"
    "  year   = {2021},\n"
    "}"
)

ENTRY_B = (
    "@inproceedings{doe2019net,\n"
    "  author    = {Doe, John},\n"
    "  title     = {Nested {BibTeX} Braces},\n"
    "  booktitle = {Proc. of Things},\n"
    "  year      = {2019},\n"
    "}"
)


def test_split_handles_nested_braces():
    """A field value with nested braces stays a single entry."""
    entries = split_bibtex_entries(ENTRY_A + "\n\n" + ENTRY_B)
    assert len(entries) == 2
    assert "Nested {BibTeX} Braces" in entries[1]


def test_parse_entry_key_and_unkeyed_blocks():
    """Citation keys parse; @comment/@string yield no key."""
    assert parse_entry_key(ENTRY_A) == "smith2020deep"
    assert parse_entry_key("@comment{ignore me}") is None
    assert parse_entry_key("@string{acm = {ACM}}") is None


def test_merge_replaces_existing_key():
    """Merge mode replaces an entry whose key already exists."""
    merged, added, updated, skipped, total = _merge_bibtex_text(
        ENTRY_A, ENTRY_A_FIXED, "merge"
    )
    assert updated == ["smith2020deep"]
    assert added == []
    assert total == 1
    # The year field flips; the key still legitimately contains 2020.
    assert "{2021}" in merged and "{2020}" not in merged


def test_merge_appends_new_key():
    """Merge mode appends an entry with an unseen key."""
    merged, added, updated, skipped, total = _merge_bibtex_text(
        ENTRY_A, ENTRY_B, "merge"
    )
    assert added == ["doe2019net"]
    assert updated == []
    assert total == 2


def test_append_mode_skips_existing_key():
    """Append mode leaves an existing key untouched."""
    merged, added, updated, skipped, total = _merge_bibtex_text(
        ENTRY_A, ENTRY_A_FIXED, "append"
    )
    assert skipped == ["smith2020deep"]
    assert updated == []
    assert "2020" in merged and "2021" not in merged


def test_overwrite_mode_discards_existing():
    """Overwrite mode drops prior content entirely."""
    merged, added, updated, skipped, total = _merge_bibtex_text(
        ENTRY_A, ENTRY_B, "overwrite"
    )
    assert "smith2020deep" not in merged
    assert total == 1


def _fake_ctx():
    """Minimal MCP context; unused when bibtex text is supplied."""
    return SimpleNamespace(
        request_context=SimpleNamespace(lifespan_context=object())
    )


def test_write_bibtex_creates_and_updates_file(tmp_path):
    """End-to-end: create a .bib, then correct an entry in place."""
    target = tmp_path / "refs.bib"

    first = asyncio.run(
        write_bibtex(_fake_ctx(), str(target), bibtex=ENTRY_A)
    )
    assert first["written"] is True
    assert first["added"] == ["smith2020deep"]
    assert target.exists()

    second = asyncio.run(
        write_bibtex(_fake_ctx(), str(target), bibtex=ENTRY_A_FIXED)
    )
    assert second["updated"] == ["smith2020deep"]
    text = target.read_text(encoding="utf-8")
    assert "{2021}" in text and "{2020}" not in text
    # The corrected file still holds exactly one entry.
    assert len(split_bibtex_entries(text)) == 1


def test_write_bibtex_rejects_unknown_mode(tmp_path):
    """An invalid mode is reported and nothing is written."""
    target = tmp_path / "refs.bib"
    result = asyncio.run(
        write_bibtex(
            _fake_ctx(), str(target), bibtex=ENTRY_A, mode="clobber"
        )
    )
    assert result["written"] is False
    assert not target.exists()
    assert result["warnings"]
