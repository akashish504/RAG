"""Batch-API deck extraction — the offline, ~50%-cheaper path.

The synchronous path (``slides_deck`` → ``BatchedDeckEnricher``) makes one live
``messages.stream`` call per deck. For the full library (~12k decks) that is
expensive; the Anthropic **Message Batches API** halves the price for this offline
work. This module orchestrates it.

Per **wave** of decks:
  1. render + python-pptx parse locally (no LLM), upload each visual slide PNG via
     the Files API and keep its ``file_id`` so batch request bodies stay tiny;
  2. batch round 1 — enrichment (visual delta per deck);
  3. batch round 2 — deck summary (depends on round 1);
  4. batch round 3 — record-level summary (multi-file records only);
  5. assemble ``normalized.txt`` + ``__record_summary.txt`` to S3; delete the Files.

The **pure** helpers (request building, result application, markdown assembly,
record grouping) carry the logic and are unit-tested without S3/Anthropic. They
reuse the SAME prompt/parse code as the sync path (``backends.build_*_content``,
``parse_deck_reply``, ``_merge_enrichment``) so the two paths can never diverge.
"""

from __future__ import annotations

import logging
import zlib
from dataclasses import dataclass, field
from pathlib import PurePosixPath
from typing import Any

import anthropic
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential_jitter,
)

from pipeline.airtable_ingestion.record_summary import (
    build_record_summary_content,
    extract_file_summary,
    usable_sections,
)
from pipeline.preprocessing.slides.backends import (
    build_enrich_content,
    build_summary_content,
    file_image_source,
    parse_deck_reply,
)
from pipeline.preprocessing.slides.enricher import (
    _DIGEST_MAX_CHARS,
    _DIGEST_SLIDE_CHARS,
    _MAX_SLIDES_PER_CALL,
    _SUMMARY_MAX_IMAGES,
    _base_slide,
    _merge_enrichment,
)
from pipeline.preprocessing.slides.extractor import RenderedSlide
from pipeline.preprocessing.slides.models import DeckExtraction, SlideExtraction

log = logging.getLogger(__name__)

_DEFAULT_MODEL = "claude-haiku-4-5"
_ENRICH_MAX_TOKENS = 16384  # headroom so a many-slide enrichment can't truncate
_SUMMARY_MAX_TOKENS = 2048  # room for a dense, descriptive summary
_DOC_MAX_TOKENS = 16384     # PDF/DOCX can be long — generous extraction budget
_FILES_BETA = "files-api-2025-04-14"

_PDF_SUFFIXES = (".pdf",)
_DOCX_SUFFIXES = (".docx", ".doc")
_XLSX_SUFFIXES = (".xlsx", ".xlsm")

_DOC_EXTRACT_PROMPT = """\
Transcribe EVERYTHING in this document as clean, faithful markdown. This is a
lossless extraction, not a summary — miss nothing.

- Preserve all headings, paragraphs, bullet lists (nested as "  -"), captions,
  footnotes and source/citation lines, verbatim.
- Render every table as a GitHub-markdown table — all rows, all cells, headers
  and totals included.
- For charts/figures: state the type and transcribe every readable label and data
  point; do not invent numbers.
- Keep units, dates, %/$ signs and footnote markers exactly.

Reproduce the content exactly; never paraphrase or summarise; never invent text or
numbers that are not present. Output only the extracted markdown.
"""

# Transient Anthropic-side failures worth retrying (e.g. 503 "File storage
# temporarily unavailable", 429, connection drops). 5xx/429 map to these classes.
_RETRYABLE = (
    anthropic.InternalServerError,
    anthropic.RateLimitError,
    anthropic.APIConnectionError,
    anthropic.APITimeoutError,
)


@dataclass(slots=True)
class DeckJob:
    """One deck flowing through the batch pipeline. ``custom_id`` routes results."""

    custom_id: str           # unique per wave (attachment id)
    identifier: str          # record primary key (slug)
    record_id: str           # airtable record id
    column: str
    original_key: str        # S3 key of the source deck (citation target)
    normalized_key: str      # S3 key to write the assembled text
    metadata_header: str = ""  # YAML-ish block prepended to normalized.txt
    base_id: str = ""        # airtable base id (for the record-summary sidecar)
    table_id: str = ""       # airtable table id (for the record-summary sidecar)
    facets: dict[str, Any] = field(default_factory=dict)  # structured facets (record-summary sidecar)
    rendered: list[RenderedSlide] = field(default_factory=list)  # parser slides (image_png cleared post-upload)
    slides: list[SlideExtraction] = field(default_factory=list)  # merged extraction
    image_file_ids: dict[int, str] = field(default_factory=dict)  # slide_number -> Files API id
    summary: str | None = None

    @property
    def file_label(self) -> str:
        return f"{self.column}/{PurePosixPath(self.original_key).name}"


@dataclass(slots=True)
class DocJob:
    """A non-deck file (PDF / DOCX / XLSX) flowing through the batch pipeline.

    PDFs and DOCX (converted to PDF) are extracted by one Batch-API document call
    each; XLSX is extracted locally (openpyxl) during prep, so it needs no call.
    Shares ``identifier``/``summary``/``file_label``/``normalized_key`` with
    ``DeckJob`` so the record-summary helpers treat both uniformly.
    """

    custom_id: str
    identifier: str
    record_id: str
    column: str
    original_key: str
    normalized_key: str
    metadata_header: str = ""
    base_id: str = ""
    table_id: str = ""
    facets: dict[str, Any] = field(default_factory=dict)  # structured facets (record-summary sidecar)
    pdf_file_id: str | None = None  # Files API id of the (converted) PDF, if uploaded
    text: str | None = None         # extracted markdown
    summary: str | None = None      # per-file summary fed to the record summary

    @property
    def file_label(self) -> str:
        return f"{self.column}/{PurePosixPath(self.original_key).name}"


# ---------------------------------------------------------------------------
# Pure helpers (unit-tested; no S3 / Anthropic)
# ---------------------------------------------------------------------------


def _request(custom_id: str, content: list[dict], max_tokens: int, model: str) -> dict:
    return {
        "custom_id": custom_id,
        "params": {
            "model": model,
            "max_tokens": max_tokens,
            "messages": [{"role": "user", "content": content}],
        },
    }


def build_enrich_requests(jobs: list[DeckJob], *, model: str = _DEFAULT_MODEL) -> list[dict]:
    """Batch requests for round 1. A deck is split into groups of ≤_MAX_SLIDES_PER_CALL
    slides so the corrected output can't truncate; each group's custom_id is
    ``"{att_id}::{k}"`` and routes back to the deck in ``apply_enrich_results``."""
    requests: list[dict] = []
    for job in jobs:
        groups = [
            job.rendered[i : i + _MAX_SLIDES_PER_CALL]
            for i in range(0, len(job.rendered), _MAX_SLIDES_PER_CALL)
        ]
        for k, group in enumerate(groups):
            content = build_enrich_content(
                group,
                image_source_for=lambda s, ids=job.image_file_ids: file_image_source(ids[s.slide_number]),
            )
            if content is not None:
                requests.append(_request(f"{job.custom_id}::{k}", content, _ENRICH_MAX_TOKENS, model))
    return requests


def apply_enrich_results(jobs: list[DeckJob], results: dict[str, str]) -> None:
    """Merge round-1 corrections (``custom_id -> reply text``) into each deck. The
    ``::k`` group suffix is stripped so every group routes back to its deck."""
    by_id = {j.custom_id: j for j in jobs}
    for custom_id, reply in results.items():
        job = by_id.get(custom_id.split("::", 1)[0])
        if job is None:
            continue
        enriched = parse_deck_reply(reply)
        by_number = {s.slide_number: s for s in job.slides}
        for number, content in enriched.items():
            target = by_number.get(number)
            if target is not None:
                _merge_enrichment(target, content, extractor_name="haiku")


def build_digest(slides: list[SlideExtraction]) -> str:
    """Text digest fed to the deck-summary call (mirrors BatchedDeckEnricher)."""
    parts: list[str] = []
    for s in slides:
        head = f"Slide {s.slide_number}: {s.title or ''}".strip()
        body = "\n".join(
            p for p in (s.text, *s.tables, s.visuals) if p.strip()
        )[:_DIGEST_SLIDE_CHARS]
        parts.append(f"{head}\n{body}".strip())
    return "\n\n".join(parts)[:_DIGEST_MAX_CHARS]


def representative_file_ids(
    image_file_ids: dict[int, str], *, max_images: int = _SUMMARY_MAX_IMAGES
) -> list[str]:
    """Up to ``max_images`` slide-image ids, evenly spread, always including the first."""
    ordered = [fid for _, fid in sorted(image_file_ids.items())]
    if len(ordered) <= max_images:
        return ordered
    last = len(ordered) - 1
    idxs = sorted({round(i * last / (max_images - 1)) for i in range(max_images)})
    return [ordered[i] for i in idxs]


def build_summary_requests(jobs: list[DeckJob], *, model: str = _DEFAULT_MODEL) -> list[dict]:
    """Batch requests for round 2 (deck summaries). Empty-digest decks are skipped."""
    requests: list[dict] = []
    for job in jobs:
        digest = build_digest(job.slides)
        if not digest.strip():
            continue
        sources = [file_image_source(fid) for fid in representative_file_ids(job.image_file_ids)]
        content = build_summary_content(digest, sources)
        requests.append(_request(job.custom_id, content, _SUMMARY_MAX_TOKENS, model))
    return requests


def apply_summary_results(jobs: list[DeckJob], results: dict[str, str]) -> None:
    by_id = {j.custom_id: j for j in jobs}
    for custom_id, text in results.items():
        job = by_id.get(custom_id)
        if job is not None and text.strip():
            job.summary = text.strip()


def assemble_deck_markdown(job: DeckJob) -> str:
    """Final ``normalized.txt`` body: metadata header + DOCUMENT_SUMMARY + slides."""
    deck = DeckExtraction(
        document_id=PurePosixPath(job.original_key).stem or "deck",
        source_s3_key=job.original_key,
        summary=job.summary,
        slides=job.slides,
    )
    return job.metadata_header + deck.to_markdown()


# -- non-deck documents (PDF / DOCX / XLSX) ---------------------------------


def build_doc_requests(doc_jobs: list[DocJob], *, model: str = _DEFAULT_MODEL) -> list[dict]:
    """Batch requests to extract each PDF/DOCX as one document call. XLSX jobs
    (extracted locally during prep, ``text`` already set) and jobs without an
    uploaded PDF produce no request."""
    requests: list[dict] = []
    for job in doc_jobs:
        if job.pdf_file_id is None or job.text is not None:
            continue
        content = [
            {"type": "document", "source": {"type": "file", "file_id": job.pdf_file_id}},
            {"type": "text", "text": _DOC_EXTRACT_PROMPT},
        ]
        requests.append(_request(job.custom_id, content, _DOC_MAX_TOKENS, model))
    return requests


def apply_doc_results(doc_jobs: list[DocJob], results: dict[str, str]) -> None:
    by_id = {j.custom_id: j for j in doc_jobs}
    for custom_id, text in results.items():
        job = by_id.get(custom_id)
        if job is not None and text.strip():
            job.text = text.strip()


def assemble_doc_markdown(job: DocJob) -> str:
    """Final ``normalized.txt`` body for a non-deck file: header + extracted text."""
    return job.metadata_header + (job.text or "")


def _assemble(job) -> str:
    """Dispatch to the right assembler so deck and doc jobs share record-summary code."""
    return assemble_doc_markdown(job) if isinstance(job, DocJob) else assemble_deck_markdown(job)


# -- record-level summary (round 3) -----------------------------------------


def group_by_record(jobs: list[DeckJob]) -> dict[str, list[DeckJob]]:
    groups: dict[str, list[DeckJob]] = {}
    for job in jobs:
        groups.setdefault(job.identifier, []).append(job)
    return groups


def partition_jobs_for_worker(
    jobs: list[DeckJob], *, worker_id: int, worker_count: int
) -> list[DeckJob]:
    """Shard jobs across parallel workers without splitting a record across workers."""
    if worker_count <= 1:
        return jobs
    if worker_id < 0 or worker_id >= worker_count:
        raise ValueError(f"worker_id must be in [0, {worker_count}), got {worker_id}")
    out: list[DeckJob] = []
    for identifier, group in sorted(group_by_record(jobs).items()):
        if zlib.crc32(identifier.encode()) % worker_count == worker_id:
            out.extend(group)
    return out


# ---------------------------------------------------------------------------
# Isolation guards — keep one file's data from merging into another's
# ---------------------------------------------------------------------------


def duplicate_custom_ids(jobs: list[DeckJob]) -> set[str]:
    """custom_ids that appear more than once — they would overwrite each other in
    result routing AND collide on their S3 normalized_key. Must be empty."""
    seen: set[str] = set()
    dupes: set[str] = set()
    for j in jobs:
        (dupes if j.custom_id in seen else seen).add(j.custom_id)
    return dupes


def find_record_conflicts(jobs: list[DeckJob]) -> dict[str, set[str]]:
    """``identifier -> {record_id, ...}`` for identifiers that map to MORE THAN ONE
    distinct Airtable record. These would be grouped as one record and share a
    single ``__record_summary.txt`` — silently merging two projects. Should be empty;
    a non-empty result means the upstream identifier (Project Number) is not unique.
    """
    by_identifier: dict[str, set[str]] = {}
    for j in jobs:
        if j.record_id:
            by_identifier.setdefault(j.identifier, set()).add(j.record_id)
    return {ident: rids for ident, rids in by_identifier.items() if len(rids) > 1}


def assert_isolation(jobs: list[DeckJob]) -> None:
    """Raise if any cross-file data merge is possible in this wave."""
    dupes = duplicate_custom_ids(jobs)
    if dupes:
        raise ValueError(f"duplicate custom_ids in wave (would overwrite): {sorted(dupes)}")
    conflicts = find_record_conflicts(jobs)
    if conflicts:
        raise ValueError(
            "identifier maps to multiple Airtable records (would merge): "
            + "; ".join(f"{i}->{sorted(r)}" for i, r in conflicts.items())
        )


def record_sections(jobs: list[DeckJob]) -> list[tuple[str, str]]:
    """``(label, per-file summary)`` for a record's decks, used to build its summary."""
    sections: list[tuple[str, str]] = []
    for job in jobs:
        summary = (job.summary or "").strip() or extract_file_summary(_assemble(job))
        if summary:
            sections.append((job.file_label, summary))
    return sections


def build_record_summary_requests(
    jobs: list[DeckJob], *, model: str = _DEFAULT_MODEL
) -> tuple[list[dict], dict[str, str]]:
    """Round-3 requests + pre-resolved single-file summaries.

    Returns ``(requests, reused)`` where ``reused`` maps ``identifier -> summary``
    for records that need no call (0/1 usable file). ``custom_id`` is the record
    identifier.
    """
    requests: list[dict] = []
    reused: dict[str, str] = {}
    for identifier, group in group_by_record(jobs).items():
        usable = usable_sections(record_sections(group))
        if not usable:
            continue
        if len(usable) == 1:
            reused[identifier] = usable[0][1]
            continue
        content = build_record_summary_content(usable)
        requests.append(_request(identifier, content, _SUMMARY_MAX_TOKENS, model))
    return requests, reused


# ---------------------------------------------------------------------------
# Orchestration (thin; injected client + storage)
# ---------------------------------------------------------------------------


class BatchExtractor:
    """Drives the wave pipeline against injected Anthropic + storage clients.

    ``client``  — an ``anthropic.Anthropic`` (uses ``.messages.batches`` and
                  ``.beta.files``).
    ``store``   — object with ``get_bytes(key)``, ``put_text(key, text)``,
                  ``key_exists(key)`` (e.g. an adapter over S3Uploader/boto3).
    ``renderer``— a ``PptxSlideRenderer`` (built lazily by the caller).
    ``poll``    — callable returning when a batch id has ``ended`` (injectable for tests).
    """

    def __init__(
        self, *, client, store, renderer, model: str = _DEFAULT_MODEL, workers: int = 1
    ) -> None:
        self._client = client
        self._store = store
        self._renderer = renderer
        self._model = model
        # Render+upload is the wave bottleneck (LibreOffice subprocess + Files
        # uploads — both release the GIL), so a thread pool gives near-linear
        # speedup up to the core count. The 3 batch rounds stay single-threaded
        # (the Batch API parallelizes them server-side).
        self._workers = max(1, workers)

    # -- Files API -------------------------------------------------------

    @retry(
        reraise=True,
        stop=stop_after_attempt(5),
        wait=wait_exponential_jitter(initial=1, max=60),
        retry=retry_if_exception_type(_RETRYABLE),
    )
    def upload_png(self, png: bytes, *, name: str) -> str:
        uploaded = self._client.beta.files.upload(
            file=(name, png, "image/png"), betas=[_FILES_BETA]
        )
        return uploaded.id

    @retry(
        reraise=True,
        stop=stop_after_attempt(5),
        wait=wait_exponential_jitter(initial=1, max=60),
        retry=retry_if_exception_type(_RETRYABLE),
    )
    def upload_pdf(self, pdf: bytes, *, name: str) -> str:
        uploaded = self._client.beta.files.upload(
            file=(name, pdf, "application/pdf"), betas=[_FILES_BETA]
        )
        return uploaded.id

    def delete_files(self, file_ids: list[str]) -> None:
        for fid in file_ids:
            try:
                self._client.beta.files.delete(fid, betas=[_FILES_BETA])
            except Exception:  # noqa: BLE001 — cleanup is best-effort
                log.warning("batch: failed to delete file %s", fid, exc_info=True)

    # -- render + upload (round 0) --------------------------------------

    def render_and_upload(self, job: DeckJob) -> None:
        """Populate ``job.rendered``/``slides``/``image_file_ids`` from the S3 original."""
        binary = self._store.get_bytes(job.original_key)
        job.rendered = self._renderer.render(binary)
        job.slides = [_base_slide(r) for r in job.rendered]
        from pipeline.preprocessing.slides.extractor import attaches_image  # noqa: PLC0415

        for r in job.rendered:
            if attaches_image(r) and r.image_png:
                try:
                    job.image_file_ids[r.slide_number] = self.upload_png(
                        r.image_png, name=f"{job.custom_id}_s{r.slide_number}.png"
                    )
                except Exception:  # noqa: BLE001 — upload exhausted retries
                    # Degrade THIS slide to parser-only so a flaky upload can't
                    # crash request building (which keys on image_file_ids) or
                    # sink the wave. Clearing the flags drops it from the visual
                    # set in build_enrich_content.
                    log.warning(
                        "batch: image upload failed for %s slide %d — parser-only",
                        job.custom_id, r.slide_number, exc_info=True,
                    )
                    r.has_visual = False
                    r.needs_image = False
            r.image_png = b""  # free memory; batch path never re-reads bytes

    def prep_doc(self, job: DocJob) -> None:
        """Prepare a non-deck file: XLSX extracts locally (sets ``text``); PDF/DOCX
        convert to PDF (DOCX) and upload for one Batch-API extraction call."""
        binary = self._store.get_bytes(job.original_key)
        if binary is None:
            return
        suffix = PurePosixPath(job.original_key).suffix.lower()

        if suffix in _XLSX_SUFFIXES:
            from pipeline.preprocessing.normalizers.llm_content import _process_xlsx  # noqa: PLC0415
            job.text = _process_xlsx(binary, PurePosixPath(job.original_key).name)
            return

        if suffix in _PDF_SUFFIXES:
            pdf = binary
        elif suffix in _DOCX_SUFFIXES:
            from pipeline.preprocessing.normalizers.pptx_converter import pptx_to_pdf_bytes  # noqa: PLC0415
            pdf = pptx_to_pdf_bytes(binary)  # LibreOffice converts DOCX→PDF too
        else:
            return
        if pdf is None:
            log.warning("batch: could not produce PDF for %s", job.original_key)
            return
        job.pdf_file_id = self.upload_pdf(pdf, name=f"{job.custom_id}.pdf")

    # -- batch submit/poll/collect --------------------------------------

    def run_batch(self, requests: list[dict]) -> dict[str, str]:
        """Submit, wait, and collect ``custom_id -> assistant text`` for a round.

        Uses the BETA batches endpoint with the Files beta header — required
        because requests reference uploaded images by ``file_id`` (the GA
        ``messages.batches`` endpoint cannot carry the beta header).
        """
        if not requests:
            return {}
        log.info("  submitting batch of %d request(s)...", len(requests))
        batch = self._client.beta.messages.batches.create(
            requests=requests, betas=[_FILES_BETA]
        )
        log.info("  batch %s submitted; polling until ended", batch.id)
        self._wait(batch.id)
        out: dict[str, str] = {}
        for result in self._client.beta.messages.batches.results(batch.id, betas=[_FILES_BETA]):
            if result.result.type != "succeeded":
                log.warning("batch: request %s did not succeed (%s)",
                            result.custom_id, result.result.type)
                continue
            blocks = getattr(result.result.message, "content", None) or []
            text = "".join(
                getattr(b, "text", "") or "" for b in blocks
                if getattr(b, "type", None) == "text"
            ).strip()
            out[result.custom_id] = text
        return out

    def _wait(self, batch_id: str) -> None:
        import time  # noqa: PLC0415

        while True:
            batch = self._client.beta.messages.batches.retrieve(batch_id, betas=[_FILES_BETA])
            counts = getattr(batch, "request_counts", None)
            log.info("    batch %s: %s %s", batch_id, batch.processing_status, counts or "")
            if batch.processing_status == "ended":
                return
            time.sleep(30)

    # -- whole-wave driver ----------------------------------------------

    def process_wave(
        self, deck_jobs: list[DeckJob], doc_jobs: list[DocJob] | None = None
    ) -> None:
        """Prep → batch rounds → assemble → write → cleanup, for one wave.

        Two tracks share the wave: DECKS (render → enrich → summary) and DOCS
        (PDF/DOCX → one document call; XLSX local). Both contribute to each
        record's combined summary.
        """
        doc_jobs = doc_jobs or []
        all_jobs = [*deck_jobs, *doc_jobs]
        # Defense in depth: never let one file's data bleed into another's.
        assert_isolation(all_jobs)

        # -- prep (the bottleneck): render decks / convert+upload docs, parallel
        def _prep(indexed) -> None:
            i, job = indexed
            name = PurePosixPath(job.original_key).name
            log.info("  [%d/%d] preparing %s", i, len(all_jobs), name)
            try:
                if isinstance(job, DeckJob):
                    self.render_and_upload(job)
                    log.info("        %d slides, %d images", len(job.slides), len(job.image_file_ids))
                else:
                    self.prep_doc(job)
                    log.info("        doc ready (%s)",
                             "xlsx-local" if job.text else ("pdf-uploaded" if job.pdf_file_id else "skipped"))
            except Exception as exc:  # noqa: BLE001 — a bad file must not sink the wave
                # Expected for corrupt / non-PowerPoint / password-protected files
                # (e.g. BadZipFile = the .pptx isn't a valid zip). Skip + flag it
                # clearly — no scary traceback, the wave keeps going.
                log.warning(
                    "batch: SKIPPED unreadable file %s (corrupt, encrypted, or not a real "
                    "%s?) — %s", job.original_key,
                    PurePosixPath(job.original_key).suffix or "file", exc,
                )

        items = list(enumerate(all_jobs, start=1))
        if self._workers > 1:
            from concurrent.futures import ThreadPoolExecutor  # noqa: PLC0415

            with ThreadPoolExecutor(max_workers=self._workers) as pool:
                list(pool.map(_prep, items))
        else:
            for item in items:
                _prep(item)

        # -- deck rounds (best-effort overlays on the parser output)
        try:
            apply_enrich_results(
                deck_jobs, self.run_batch(build_enrich_requests(deck_jobs, model=self._model))
            )
        except Exception:  # noqa: BLE001
            log.warning("batch: enrichment round failed — parser-only text", exc_info=True)
        try:
            apply_summary_results(
                deck_jobs, self.run_batch(build_summary_requests(deck_jobs, model=self._model))
            )
        except Exception:  # noqa: BLE001
            log.warning("batch: summary round failed — no DOCUMENT_SUMMARY", exc_info=True)

        # -- doc round (PDF/DOCX extraction; XLSX already has text)
        try:
            apply_doc_results(
                doc_jobs, self.run_batch(build_doc_requests(doc_jobs, model=self._model))
            )
        except Exception:  # noqa: BLE001
            log.warning("batch: document round failed", exc_info=True)

        # -- write normalized.txt for every file that produced text
        written = 0
        for job in deck_jobs:
            if job.slides:
                self._store.put_text(job.normalized_key, assemble_deck_markdown(job))
                written += 1
        for job in doc_jobs:
            if job.text:
                self._store.put_text(job.normalized_key, assemble_doc_markdown(job))
                written += 1
                # Per-file summary for the record summary (no DOCUMENT_SUMMARY for docs).
                job.summary = extract_file_summary(assemble_doc_markdown(job))
        log.info("batch: wrote %d/%d normalized.txt in wave", written, len(all_jobs))

        # -- record-level summaries over BOTH tracks (best-effort)
        try:
            rec_requests, reused = build_record_summary_requests(all_jobs, model=self._model)
            rec_results = self.run_batch(rec_requests)
            self._write_record_summaries(all_jobs, {**reused, **rec_results})
        except Exception:  # noqa: BLE001
            log.warning("batch: record-summary round failed", exc_info=True)

        # -- cleanup Files objects for the wave (slide images + doc PDFs)
        file_ids = [fid for j in deck_jobs for fid in j.image_file_ids.values()]
        file_ids += [j.pdf_file_id for j in doc_jobs if j.pdf_file_id]
        self.delete_files(file_ids)

    def _write_record_summaries(self, jobs: list[DeckJob], summaries: dict[str, str]) -> None:
        for identifier, group in group_by_record(jobs).items():
            summary = summaries.get(identifier)
            if not summary:
                continue
            lead = group[0]
            header = lead.metadata_header
            manifest = "\n".join(f"- {j.file_label}" for j in group if (j.summary or "").strip())
            body = f"DOCUMENT_SUMMARY: {summary}\n\nFiles in this record:\n{manifest}\n"
            record_dir = PurePosixPath(lead.normalized_key).parent.parent.parent.as_posix()
            self._store.put_text(f"{record_dir}/__record_summary.txt", header + body)
            # Sidecar — matches the sync path's record-summary meta so retrieval
            # tags this as the record-level parent (doc_role) and can cite it.
            if lead.record_id and lead.table_id:
                payload: dict[str, Any] = {
                    "airtable_record_id": lead.record_id,
                    "airtable_base_id": lead.base_id,
                    "airtable_table_id": lead.table_id,
                    "identifier": identifier,
                    "column_name": "",
                    "doc_role": "record_summary",
                }
                if lead.facets:
                    payload["facets"] = lead.facets
                self._store.put_json(f"{record_dir}/.airtable_meta.json", payload)
