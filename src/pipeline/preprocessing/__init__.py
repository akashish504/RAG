"""Shared preprocessing layer for all ingestion sources.

Normalizers live here so Airtable, SharePoint, SQS, and future ingestion
pipelines can all reuse the same preprocessing logic without import cycles.
"""
