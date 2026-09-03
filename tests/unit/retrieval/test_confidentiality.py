"""D.Quals confidentiality tagging: full record content is always shown; a
bracketed tag is prepended to text/record_summary/deck_summary when
confidential_project/confidential_client is set. No dropping, no redaction,
no citation changes. Query/response time only — no OpenSearch/Airtable/
network calls needed since tag_confidential_hits operates purely on
already-shaped SearchResult objects."""

from __future__ import annotations

from retrieval.confidentiality import (
    ensure_required_fetch_fields,
    is_confidential_client,
    is_confidential_project,
    tag_confidential_hits,
)
from retrieval.models import Citation, SearchResult


def _hit(
    *,
    source: str = "d_quals",
    source_type: str = "semantic",
    text: str = "",
    metadata: dict | None = None,
    payload: dict | None = None,
    citation_url: str | None = None,
    citations: list | None = None,
) -> SearchResult:
    return SearchResult(
        source=source,
        source_type=source_type,
        score=1.0,
        text=text,
        metadata=metadata or {},
        payload=payload or {},
        citation_url=citation_url,
        citations=citations or [],
    )


# -- tagging: project only, client only, both, neither -----------------------


def test_confidential_project_tags_semantic_hit():
    hit = _hit(
        source_type="semantic",
        text="A secret initiative.",
        metadata={"confidential_project": ["CONFIDENTIAL"]},
    )
    result = tag_confidential_hits([hit])
    assert result[0].text == "**[CONFIDENTIAL PROJECT]** A secret initiative."
    assert len(result) == 1  # nothing dropped


def test_confidential_project_tags_structured_hit():
    hit = _hit(
        source_type="structured",
        text="Project Name: Secret Corridor Study",
        metadata={"Confidential Project": ["CONFIDENTIAL"]},
    )
    result = tag_confidential_hits([hit])
    assert result[0].text == "**[CONFIDENTIAL PROJECT]** Project Name: Secret Corridor Study"


def test_confidential_client_tags_and_keeps_client_name_visible():
    hit = _hit(
        source_type="structured",
        text="Engagement with Acme Corp on market entry.",
        metadata={
            "Confidential Client": ["CONFIDENTIAL"],
            "Client Organisation": ["Acme Corp"],
        },
    )
    result = tag_confidential_hits([hit])
    assert result[0].text == "**[CONFIDENTIAL CLIENT]** Engagement with Acme Corp on market entry."
    assert "Acme Corp" in result[0].text  # name is NOT redacted
    assert result[0].metadata["Client Organisation"] == ["Acme Corp"]  # untouched


def test_both_flags_set_uses_combined_tag():
    hit = _hit(
        text="Fully sensitive engagement.",
        metadata={
            "confidential_project": ["CONFIDENTIAL"],
            "confidential_client": ["CONFIDENTIAL"],
        },
    )
    result = tag_confidential_hits([hit])
    assert result[0].text == "**[CONFIDENTIAL PROJECT & CLIENT]** Fully sensitive engagement."


def test_neither_flag_set_leaves_hit_completely_untouched():
    hit = _hit(text="Ordinary project.", metadata={"client_organisation": ["Beta Corp"]})
    result = tag_confidential_hits([hit])
    assert result[0].text == "Ordinary project."
    assert result[0].metadata == {"client_organisation": ["Beta Corp"]}


def test_blank_or_missing_flags_leave_hit_untouched():
    hit_missing = _hit(text="No flags present.")
    hit_blank = _hit(text="Flags blank.", metadata={"confidential_project": [], "confidential_client": []})
    for hit in (hit_missing, hit_blank):
        result = tag_confidential_hits([hit])
        assert result[0].text == hit.text


# -- tag also applied to record_summary/deck_summary -------------------------


def test_tag_prepended_to_record_summary_and_deck_summary():
    hit = _hit(
        metadata={
            "confidential_client": ["CONFIDENTIAL"],
            "client_organisation": ["Acme Corp"],
            "record_summary": "This project was for Acme Corp.",
            "deck_summary": "Acme Corp engagement overview.",
        },
    )
    result = tag_confidential_hits([hit])
    assert result[0].metadata["record_summary"] == "**[CONFIDENTIAL CLIENT]** This project was for Acme Corp."
    assert result[0].metadata["deck_summary"] == "**[CONFIDENTIAL CLIENT]** Acme Corp engagement overview."


def test_no_record_summary_or_deck_summary_present_is_a_noop_for_those_keys():
    hit = _hit(text="x", metadata={"confidential_project": ["CONFIDENTIAL"]})
    result = tag_confidential_hits([hit])
    assert "record_summary" not in result[0].metadata
    assert "deck_summary" not in result[0].metadata


# -- citations/links are NOT touched (this is the reversal from the old --
# -- drop/redact behavior, which used to suppress citation_url/citations) --


def test_citations_and_links_are_preserved_for_confidential_client():
    hit = _hit(
        source_type="structured",
        metadata={"Confidential Client": ["CONFIDENTIAL"], "Client Organisation": ["Acme Corp"]},
        citation_url="https://airtable.com/appX/tblY/recZ",
        citations=[Citation(cite_id="1", kind="airtable_record", label="Acme Corp record")],
    )
    result = tag_confidential_hits([hit])
    assert result[0].citation_url == "https://airtable.com/appX/tblY/recZ"
    assert len(result[0].citations) == 1


def test_citations_and_links_are_preserved_for_confidential_project():
    hit = _hit(
        source_type="semantic",
        metadata={"confidential_project": ["CONFIDENTIAL"]},
        citation_url="https://dalberg-bucket.s3.amazonaws.com/raw/x/deck.pptx?sig=abc",
        citations=[Citation(cite_id="1", kind="document_section", label="Deck section")],
    )
    result = tag_confidential_hits([hit])
    assert result[0].citation_url == "https://dalberg-bucket.s3.amazonaws.com/raw/x/deck.pptx?sig=abc"
    assert len(result[0].citations) == 1


# -- defensive value shapes ---------------------------------------------------


def test_scalar_string_facet_values_handled():
    hit = _hit(text="A project.", metadata={"confidential_project": "CONFIDENTIAL"})
    result = tag_confidential_hits([hit])
    assert result[0].text == "**[CONFIDENTIAL PROJECT]** A project."


# -- source gating -------------------------------------------------------------


def test_non_d_quals_source_untouched():
    hit = _hit(
        source="dalberg_profiles",
        text="Acme Corp mention",
        metadata={
            "confidential_project": ["CONFIDENTIAL"],
            "confidential_client": ["CONFIDENTIAL"],
        },
    )
    result = tag_confidential_hits([hit])
    assert result[0].text == "Acme Corp mention"


def test_is_confidential_helpers_respect_source_gating():
    hit = _hit(source="dalberg_profiles", metadata={"confidential_project": ["CONFIDENTIAL"]})
    assert is_confidential_project(hit) is False
    hit2 = _hit(source="d_quals", metadata={"confidential_project": ["CONFIDENTIAL"]})
    assert is_confidential_project(hit2) is True
    assert is_confidential_client(hit2) is False


# -- mixed batch ---------------------------------------------------------------


def test_mixed_batch_tags_independently_nothing_dropped():
    project_hit = _hit(text="secret project", metadata={"confidential_project": ["CONFIDENTIAL"]})
    client_hit = _hit(
        text="Acme Corp project",
        metadata={"confidential_client": ["CONFIDENTIAL"], "client_organisation": ["Acme Corp"]},
    )
    clean_hit = _hit(text="Beta Corp project", metadata={"client_organisation": ["Beta Corp"]})

    result = tag_confidential_hits([project_hit, client_hit, clean_hit])

    assert len(result) == 3  # nothing dropped
    assert result[0].text == "**[CONFIDENTIAL PROJECT]** secret project"
    assert result[1].text == "**[CONFIDENTIAL CLIENT]** Acme Corp project"
    assert result[2].text == "Beta Corp project"


# -- caller-narrowed `fields` must not bypass detection (still relevant: a
# real production incident showed detection itself can be defeated by a
# narrowed Airtable `fields` request, regardless of what happens once
# detected -- see specs/003-confidential-redaction/research.md #7). --


def test_missing_confidentiality_key_is_indistinguishable_from_blank():
    hit = _hit(
        source_type="structured",
        metadata={"Name": "MCC PSOA Liberia Stage 2", "Client Organisation": ["MCC"]},
    )
    assert is_confidential_project(hit) is False  # the hazard, not the fix


def test_ensure_required_fetch_fields_injects_confidentiality_columns():
    narrowed = ["Name", "Client Organisation", "Project Description (1-paragraph)"]
    result = ensure_required_fetch_fields("d_quals", narrowed)
    assert "Confidential Project" in result
    assert "Confidential Client" in result
    assert "Client Organisation" in result
    assert result.count("Client Organisation") == 1


def test_ensure_required_fetch_fields_noop_when_fields_not_narrowed():
    assert ensure_required_fetch_fields("d_quals", None) is None
    assert ensure_required_fetch_fields("d_quals", []) == []


def test_ensure_required_fetch_fields_noop_for_non_confidential_source():
    narrowed = ["Display Name"]
    assert ensure_required_fetch_fields("dalberg_profiles", narrowed) == narrowed


def test_ensure_required_fetch_fields_closes_the_detection_gap():
    narrowed = ["Name", "Client Organisation"]
    fetched_fields = ensure_required_fetch_fields("d_quals", narrowed)
    all_airtable_columns = {
        "Name": "MCC PSOA Liberia Stage 2",
        "Client Organisation": ["Millennium Challenge Corporation (MCC)"],
        "Confidential Project": ["CONFIDENTIAL"],
        "Confidential Client": ["NON-CONFIDENTIAL"],
    }
    hit = _hit(
        source_type="structured",
        text="Project Name: MCC PSOA Liberia Stage 2",
        metadata={k: v for k, v in all_airtable_columns.items() if k in fetched_fields},
    )
    assert is_confidential_project(hit) is True
    result = tag_confidential_hits([hit])
    assert result[0].text.startswith("**[CONFIDENTIAL PROJECT]**")
