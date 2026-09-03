from pipeline.embedding_pipeline.reader.s3_reader import parse_s3_key


def test_parse_airtable_shaped_s3_key() -> None:
    parsed = parse_s3_key("raw/contracts/recABC123/proposal_text.txt")

    assert parsed.table_name == "contracts"
    assert parsed.primary_key == "recABC123"
    assert parsed.column_name == "proposal_text"
    assert parsed.filename == "proposal_text.txt"


def test_parse_nested_raw_layout_with_column_folder() -> None:
    parsed = parse_s3_key("raw/dalberg_profiles/john@example.com/cv_attachment/file.pdf")

    assert parsed.table_name == "dalberg_profiles"
    assert parsed.primary_key == "john@example.com"
    assert parsed.column_name == "cv_attachment"
    assert parsed.filename == "file.pdf"


def test_parse_flat_sample_key_uses_standalone_fallback() -> None:
    parsed = parse_s3_key("sample.txt")

    assert parsed.table_name == "standalone"
    assert parsed.primary_key == "sample"
    assert parsed.column_name == "sample"
    assert parsed.filename == "sample.txt"
