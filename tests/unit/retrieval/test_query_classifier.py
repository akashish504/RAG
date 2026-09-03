"""Unit tests for rule-based query classification."""

from __future__ import annotations

from retrieval.models import FieldDescriptor, SchemaDescriptor
from retrieval.planner.query_classifier import (
    classify_question,
    lookup_fields_for_schema,
)


def _profiles_schema() -> SchemaDescriptor:
    return SchemaDescriptor(
        source="dalberg_profiles",
        display_name="Dalberg Profiles",
        description="Employee profiles",
        capabilities=["semantic", "structured"],
        identifier_field="Email",
        fields=[
            FieldDescriptor(name="Email", type="email"),
            FieldDescriptor(name="Display Name", type="singleLineText"),
            FieldDescriptor(name="Job Title", type="singleLineText"),
            FieldDescriptor(name="Office Location", type="singleSelect"),
            FieldDescriptor(name="Skills", type="multilineText"),
            FieldDescriptor(name="Interests", type="multilineText"),
            FieldDescriptor(name="Languages", type="multipleSelects"),
        ],
    )


def test_guitar_query_classified_as_literal_keyword() -> None:
    schema = _profiles_schema()
    result = classify_question(question="Who at Dalberg plays guitar?", schema=schema)
    assert result is not None
    assert result.kind == "literal_keyword"
    assert result.mode == "airtable_only"
    assert result.keyword == "guitar"
    assert result.airtable_formula == 'FIND("guitar", LOWER({Skills}))'
    assert "Skills" in result.target_fields


def test_french_speaker_uses_languages_field() -> None:
    schema = _profiles_schema()
    result = classify_question(question="Who speaks French?", schema=schema)
    assert result is not None
    assert result.mode == "airtable_only"
    assert result.airtable_formula == 'FIND("French", LOWER({Languages}))'


def test_expertise_query_not_classified() -> None:
    schema = _profiles_schema()
    result = classify_question(
        question="Who has worked on health financing in East Africa?",
        schema=schema,
    )
    assert result is None


def test_mixed_query_is_hybrid() -> None:
    schema = _profiles_schema()
    result = classify_question(
        question="Find someone with private sector experience who also speaks French",
        schema=schema,
    )
    assert result is not None
    assert result.kind == "mixed"
    assert result.mode == "hybrid"
    assert 'FIND("French"' in result.airtable_formula
    assert result.semantic_query


def test_lookup_fields_include_skills_interests_languages() -> None:
    schema = _profiles_schema()
    fields = lookup_fields_for_schema(schema, matched_fields=["Skills"])
    assert "Display Name" in fields
    assert "Job Title" in fields
    assert "Office Location" in fields
    assert "Skills" in fields
    assert "Interests" in fields
    assert "Languages" in fields
