"""_build_metadata_header: target-specific metadata_columns (e.g. D-Quals
'Project Description') are injected into the normalized.txt front-matter so they
become structured OpenSearch metadata + co-embedded search text.
"""

from __future__ import annotations

from pipeline.airtable_ingestion.pipeline import _build_metadata_header

_DESC = "Project Description (1-paragraph)"


def test_extra_field_is_added_to_frontmatter() -> None:
    fields = {_DESC: "Rural electrification across three districts.", "Client": "Gates"}
    h = _build_metadata_header(fields, extra_fields=(_DESC,))
    assert h.startswith("---\n") and h.rstrip().endswith("---")
    assert f"{_DESC}: Rural electrification across three districts." in h
    assert "Client: Gates" in h


def test_extra_field_not_duplicated_when_already_standard() -> None:
    # 'Description' is already a standard header field — passing it as an extra
    # must not emit it twice.
    fields = {"Description": "Hello"}
    h = _build_metadata_header(fields, extra_fields=("Description",))
    assert h.count("Description: Hello") == 1


def test_empty_when_no_relevant_fields_present() -> None:
    assert _build_metadata_header({"Unrelated": "x"}, extra_fields=(_DESC,)) == ""


def test_list_values_are_joined() -> None:
    h = _build_metadata_header({"Practice Area": ["Health", "Energy"]})
    assert "Practice Area: Health, Energy" in h
