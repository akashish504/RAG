from pathlib import Path

from pipeline.airtable_ingestion.config import load_airtable_ingestion_settings


def test_load_airtable_ingestion_settings(monkeypatch) -> None:
    root = Path(__file__).resolve().parents[3]
    config_path = root / "config" / "airtable_ingestion.yaml"

    monkeypatch.setenv("AIRTABLE_PAT_TOKEN", "test_pat")
    monkeypatch.setenv("S3_BUCKET", "test-bucket")
    monkeypatch.setenv("AWS_REGION", "eu-west-1")

    settings = load_airtable_ingestion_settings(config_path=config_path)

    assert settings.s3_bucket == "test-bucket"
    assert settings.target("profiles_sync").database_id == "app0ZvoNuWDMx4NeC"
    assert settings.target("profiles_sync").table_name == "Dalberg Profiles"
    assert settings.target("profiles_sync").attachment_columns == (
        "CV Attachment",
        "Bio Attachment",
    )
