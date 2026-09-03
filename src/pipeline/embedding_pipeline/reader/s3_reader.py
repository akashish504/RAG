"""S3 reader.

Reads objects from S3 under a configurable prefix. Text files are decoded into
``Document.text``; binary files (PDF, DOCX, PPTX, ...) are kept as
``Document.body`` bytes for downstream parsers. Provenance is parsed from the
S3 key using the convention ``raw/{table_name}/{primary_key}/{column_name}.{ext}``,
with a safe fallback for ad-hoc keys like ``sample.txt``.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Any

from botocore.exceptions import ClientError

from botocore.client import BaseClient

from pipeline.common.ids import document_hash
from pipeline.embedding_pipeline.models import Document, SourceMetadata

TEXT_EXTENSIONS: frozenset[str] = frozenset(
    {
        ".txt",
        ".md",
        ".markdown",
        ".csv",
        ".json",
        ".yaml",
        ".yml",
        ".xml",
        ".html",
        ".htm",
        ".log",
    }
)


@dataclass(frozen=True, slots=True)
class ParsedS3Key:
    """S3 key broken into provenance fields."""

    table_name: str
    primary_key: str
    column_name: str
    filename: str


class S3Reader:
    """Read text and binary documents from S3."""

    def __init__(
        self,
        *,
        bucket: str,
        s3_client: BaseClient,
        default_prefix: str = "raw/",
        encoding: str = "utf-8",
        extensions: Iterable[str] | None = None,
    ) -> None:
        self.bucket = bucket
        self.s3_client = s3_client
        self.default_prefix = default_prefix
        self.encoding = encoding
        self.extensions: set[str] | None = (
            {ext.lower() for ext in extensions} if extensions is not None else None
        )

    def iter_documents(self, prefix: str | None = None) -> Iterator[Document]:
        """Yield every supported object under ``prefix``.

        When both a raw binary (e.g. ``attXXX__cv.docx``) and a normalised text
        file (``attXXX__normalized.txt``) exist for the same Airtable attachment,
        the normalised version wins and the original binary is skipped.  This
        prevents the pipeline from indexing duplicate content.
        """
        effective_prefix = self.default_prefix if prefix is None else prefix

        # Collect all candidate keys in one pass so we can identify which
        # attachment IDs have already been normalised before yielding anything.
        all_items: list[tuple[str, dict]] = []
        paginator = self.s3_client.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=self.bucket, Prefix=effective_prefix):
            for item in page.get("Contents", []):
                key = item["Key"]
                if key.endswith("/") or not self._is_supported(key):
                    continue
                all_items.append((key, item))

        # Build set of directories that contain a normalised file.
        # Key pattern: .../col_slug/{identifier}__normalized.txt
        # Any __normalized.txt in a directory means skip ALL original binaries there.
        _NORM_SUFFIX = "__normalized.txt"
        dirs_with_normalized: set[str] = set()
        for key, _ in all_items:
            p = PurePosixPath(key)
            if p.name.endswith(_NORM_SUFFIX):
                dirs_with_normalized.add(p.parent.as_posix())

        for key, item in all_items:
            if dirs_with_normalized:
                p = PurePosixPath(key)
                # Skip original binaries in any directory that has a normalised file.
                if not p.name.endswith(_NORM_SUFFIX) and p.parent.as_posix() in dirs_with_normalized:
                    continue  # normalized version handles this directory
            yield self.get_document(key, object_summary=item)

    def get_document(
        self,
        key: str,
        *,
        object_summary: dict[str, Any] | None = None,
    ) -> Document:
        """Fetch one S3 object and return it as a Document."""

        response = self.s3_client.get_object(Bucket=self.bucket, Key=key)
        body = response["Body"].read()

        text = ""
        if self._is_text_decodable(key):
            try:
                text = body.decode(self.encoding)
            except UnicodeDecodeError:
                text = ""

        parsed = parse_s3_key(key)
        last_modified = response.get("LastModified")
        meta_key = f"{PurePosixPath(key).parent.as_posix()}/.airtable_meta.json"
        airtable_meta = self._load_airtable_meta(meta_key)
        # ``original_s3_key`` is written into the sidecar by the ingestion
        # pipeline, so the ORIGINAL document key flows into the index with NO
        # extra S3 calls here (the sidecar is already loaded). Citations use it
        # to link the source file (PDF/DOCX), not the normalized .txt. Falls
        # back to ``key`` when absent (e.g. a document indexed directly).
        source_s3_key = _meta_str(airtable_meta, "original_s3_key") or key
        source = SourceMetadata(
            s3_bucket=self.bucket,
            s3_key=key,
            table_name=parsed.table_name,
            primary_key=parsed.primary_key,
            column_name=parsed.column_name,
            filename=parsed.filename,
            source_url=f"s3://{self.bucket}/{key}",
            source_s3_key=source_s3_key,
            airtable_record_id=_meta_str(airtable_meta, "airtable_record_id"),
            airtable_base_id=_meta_str(airtable_meta, "airtable_base_id"),
            airtable_table_id=_meta_str(airtable_meta, "airtable_table_id"),
            doc_role=_meta_str(airtable_meta, "doc_role"),
            facets=airtable_meta.get("facets") or {},
        )

        return Document(
            text=text,
            body=body,
            document_hash=document_hash(body),
            source=source,
            content_type=response.get("ContentType"),
            content_length=response.get("ContentLength")
            or (object_summary or {}).get("Size")
            or len(body),
            last_modified=last_modified.isoformat() if last_modified else None,
            metadata={
                "etag": _normalize_etag(
                    response.get("ETag") or (object_summary or {}).get("ETag")
                ),
                "encoding": self.encoding,
            },
        )

    def _load_airtable_meta(self, meta_key: str) -> dict[str, Any]:
        try:
            resp = self.s3_client.get_object(Bucket=self.bucket, Key=meta_key)
            data = json.loads(resp["Body"].read())
            return data if isinstance(data, dict) else {}
        except ClientError as exc:
            if exc.response["Error"]["Code"] in ("404", "NoSuchKey"):
                return {}
            raise
        except (json.JSONDecodeError, OSError):
            return {}

    def _is_supported(self, key: str) -> bool:
        if self.extensions is None:
            return True
        ext = PurePosixPath(key).suffix.lower()
        return ext in self.extensions

    @staticmethod
    def _is_text_decodable(key: str) -> bool:
        ext = PurePosixPath(key).suffix.lower()
        return ext in TEXT_EXTENSIONS


def _meta_str(meta: dict[str, Any], key: str) -> str | None:
    val = meta.get(key)
    return str(val) if val else None


def parse_s3_key(key: str) -> ParsedS3Key:
    """Parse common raw layouts with a safe fallback.

    Supported layouts:
      1) ``raw/{table}/{primary_key}/{column}.{ext}``
      2) ``raw/{table}/{primary_key}/{column}/{filename}``
    """

    path = PurePosixPath(key)
    parts = path.parts
    filename = path.name
    column_name = path.stem

    if len(parts) >= 4 and parts[0] == "raw":
        table_name = parts[1]
        primary_key = parts[2]
        remainder = parts[3:]
        if len(remainder) == 1:
            column_path = PurePosixPath(remainder[0])
            column_name = column_path.with_suffix("").as_posix()
        else:
            # raw/<table>/<pk>/<column>/<file>
            column_name = remainder[0]
        return ParsedS3Key(
            table_name=table_name,
            primary_key=primary_key,
            column_name=column_name,
            filename=filename,
        )

    parent = path.parent.as_posix()
    table_name = parent if parent != "." else "standalone"
    return ParsedS3Key(
        table_name=table_name,
        primary_key=path.stem,
        column_name=column_name,
        filename=filename,
    )


def _normalize_etag(etag: str | None) -> str | None:
    if etag is None:
        return None
    return etag.strip('"')
