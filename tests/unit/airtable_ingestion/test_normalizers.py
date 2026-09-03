from pipeline.airtable_ingestion.normalizers import (
    normalize_facet_value,
    normalize_identifier,
    sanitize_attachment_filename,
    slugify_column_name,
    slugify_table_name,
)


def test_slugify_table_name() -> None:
    assert slugify_table_name("Dalberg Profiles") == "dalberg_profiles"


def test_normalize_identifier_keeps_email_safe_chars() -> None:
    assert normalize_identifier(" John+test@Example.com ") == "john+test@example.com"


def test_slugify_column_name() -> None:
    assert slugify_column_name("CV Attachment") == "cv_attachment"


def test_sanitize_attachment_filename() -> None:
    assert sanitize_attachment_filename("John CV (Final).PDF") == "john_cv_final.pdf"


def test_normalize_facet_value_folds_fullwidth_ampersand() -> None:
    # full-width ＆ (U+FF06) folds to ASCII & so mixed encodings store as one value
    assert normalize_facet_value("Cities ＆ Infrastructure") == "Cities & Infrastructure"


def test_normalize_facet_value_preserves_case_and_punctuation() -> None:
    # unlike the slug helpers, case, spaces and punctuation are preserved
    assert normalize_facet_value("  D. Capital  ") == "D. Capital"
    assert normalize_facet_value("Spain,Latin America") == "Spain,Latin America"
