"""Batch extraction pure helpers: request routing, result application, markdown
assembly, and record-level single/multi-file decisions. No S3 / Anthropic.
"""

from __future__ import annotations

import pytest

from pipeline.preprocessing.slides import batch
from pipeline.preprocessing.slides.batch import (
    DeckJob,
    DocJob,
    apply_doc_results,
    assemble_doc_markdown,
    build_doc_requests,
    apply_enrich_results,
    apply_summary_results,
    assemble_deck_markdown,
    assert_isolation,
    build_enrich_requests,
    build_record_summary_requests,
    build_summary_requests,
    duplicate_custom_ids,
    find_record_conflicts,
    representative_file_ids,
)
from pipeline.preprocessing.slides.enricher import _base_slide
from pipeline.preprocessing.slides.extractor import RenderedSlide


def _slide(n: int, *, visual: bool, text: str = "") -> RenderedSlide:
    return RenderedSlide(
        slide_number=n,
        image_png=b"",
        classification="IMAGE" if visual else "TEXT",
        has_visual=visual,
        needs_image=False,
        parsed_title=f"Title {n}",
        parsed_text=text,
        parsed_tables=[],
    )


def _job(custom_id: str, identifier: str, rendered: list[RenderedSlide], **kw) -> DeckJob:
    job = DeckJob(
        custom_id=custom_id,
        identifier=identifier,
        record_id=kw.get("record_id", "rec1"),
        column=kw.get("column", "Deliverable Attachments"),
        original_key=kw.get(
            "original_key", f"raw/d_quals/{identifier}/deliverable_attachments/{custom_id}/deck.pptx"
        ),
        normalized_key=(
            f"raw/d_quals/{identifier}/deliverable_attachments/{custom_id}/{custom_id}__normalized.txt"
        ),
        metadata_header=kw.get("metadata_header", ""),
        base_id=kw.get("base_id", "appX"),
        table_id=kw.get("table_id", "tblX"),
    )
    job.rendered = rendered
    job.slides = [_base_slide(r) for r in rendered]
    job.image_file_ids = {r.slide_number: f"file_{custom_id}_{r.slide_number}"
                          for r in rendered if r.has_visual}
    return job


# -- round 1: enrichment ----------------------------------------------------

def test_enrich_request_attaches_images_for_visual_slides_only() -> None:
    # Every deck gets a correction request; images are attached only for the
    # slides that have one (visual slide 1, not text slide 2).
    visual_job = _job("att1", "p1", [_slide(1, visual=True), _slide(2, visual=False)])
    text_only = _job("att2", "p1", [_slide(1, visual=False)])

    reqs = build_enrich_requests([visual_job, text_only])

    # Both decks produce one request each (group 0), with ::k sub-ids.
    assert sorted(r["custom_id"] for r in reqs) == ["att1::0", "att2::0"]
    visual_content = next(r for r in reqs if r["custom_id"] == "att1::0")["params"]["messages"][0]["content"]
    image_blocks = [b for b in visual_content if b.get("type") == "image"]
    assert image_blocks == [{"type": "image", "source": {"type": "file", "file_id": "file_att1_1"}}]
    # Text-only deck: request exists, no image blocks.
    text_content = next(r for r in reqs if r["custom_id"] == "att2::0")["params"]["messages"][0]["content"]
    assert not [b for b in text_content if b.get("type") == "image"]


def test_big_deck_splits_into_groups_routed_back() -> None:
    from pipeline.preprocessing.slides.enricher import _MAX_SLIDES_PER_CALL
    n = _MAX_SLIDES_PER_CALL + 5  # forces two groups
    job = _job("att1", "p1", [_slide(i, visual=False, text=f"t{i}") for i in range(1, n + 1)])
    reqs = build_enrich_requests([job])
    assert [r["custom_id"] for r in reqs] == ["att1::0", "att1::1"]  # 2 calls, not n
    # Results from both groups route back to the same deck by base id.
    apply_enrich_results([job], {
        "att1::0": "=== SLIDE 1 ===\nTEXT:\nfixed one",
        "att1::1": f"=== SLIDE {n} ===\nTEXT:\nfixed last",
    })
    assert job.slides[0].text == "fixed one"
    assert job.slides[-1].text == "fixed last"


def test_apply_enrich_results_routes_by_base_id() -> None:
    job = _job("att1", "p1", [_slide(1, visual=True)])
    reply = "=== SLIDE 1 ===\nVISUAL:\nA bar chart of revenue by year."
    apply_enrich_results([job], {"att1::0": reply})  # sub-id routes to att1
    assert "bar chart of revenue" in job.slides[0].visuals
    assert job.slides[0].escalated is True


# -- round 2: deck summary --------------------------------------------------

def test_summary_request_built_from_slides() -> None:
    job = _job("att1", "p1", [_slide(1, visual=True, text="Revenue grew 20%.")])
    reqs = build_summary_requests([job])
    assert reqs[0]["custom_id"] == "att1"
    text_block = reqs[0]["params"]["messages"][0]["content"][0]["text"]
    assert "Revenue grew 20%." in text_block


def test_apply_summary_results_sets_summary() -> None:
    job = _job("att1", "p1", [_slide(1, visual=True)])
    apply_summary_results([job], {"att1": "  A deck about revenue.  "})
    assert job.summary == "A deck about revenue."


def test_representative_file_ids_caps_and_keeps_first() -> None:
    ids = {i: f"f{i}" for i in range(1, 30)}
    picked = representative_file_ids(ids, max_images=12)
    assert len(picked) == 12
    assert picked[0] == "f1"


# -- assembly ---------------------------------------------------------------

def test_assemble_markdown_includes_header_summary_and_slides() -> None:
    job = _job("att1", "p1", [_slide(1, visual=False, text="Intro bullet.")],
               metadata_header="---\nName: Proj\n---\n\n")
    job.summary = "A project deck."
    md = assemble_deck_markdown(job)
    assert md.startswith("---\nName: Proj\n---")
    assert "DOCUMENT_SUMMARY: A project deck." in md
    assert "## Slide 1: Title 1" in md


# -- round 3: record summary ------------------------------------------------

def test_single_file_record_reuses_summary_no_request() -> None:
    job = _job("att1", "p1", [_slide(1, visual=True)])
    job.summary = "Lone deck."
    requests, reused = build_record_summary_requests([job])
    assert requests == []
    assert reused == {"p1": "Lone deck."}


def test_multi_file_record_builds_one_request() -> None:
    a = _job("att1", "p1", [_slide(1, visual=True)])
    b = _job("att2", "p1", [_slide(1, visual=True)])
    a.summary, b.summary = "Deck A.", "Deck B."
    requests, reused = build_record_summary_requests([a, b])
    assert reused == {}
    assert len(requests) == 1
    assert requests[0]["custom_id"] == "p1"  # custom_id is the record identifier
    digest = requests[0]["params"]["messages"][0]["content"][0]["text"]
    assert "Deck A." in digest and "Deck B." in digest


class _Store:
    def __init__(self):
        self.texts = {}
        self.jsons = {}
    def put_text(self, key, text):
        self.texts[key] = text
    def put_json(self, key, payload):
        self.jsons[key] = payload


def test_record_summaries_written_with_manifest(monkeypatch) -> None:
    a = _job("att1", "p1", [_slide(1, visual=True)])
    b = _job("att2", "p1", [_slide(1, visual=True)])
    a.summary, b.summary = "Deck A.", "Deck B."

    store = _Store()
    extractor = batch.BatchExtractor(client=object(), store=store, renderer=object())
    extractor._write_record_summaries([a, b], {"p1": "Combined."})
    key = "raw/d_quals/p1/__record_summary.txt"
    assert key in store.texts
    assert "DOCUMENT_SUMMARY: Combined." in store.texts[key]
    assert "- Deliverable Attachments/deck.pptx" in store.texts[key]
    # Parity with sync: the record summary gets its doc_role sidecar.
    meta = store.jsons["raw/d_quals/p1/.airtable_meta.json"]
    assert meta["doc_role"] == "record_summary"
    assert meta["airtable_record_id"] == "rec1"
    assert "original_s3_key" not in meta


def test_record_summary_sidecar_carries_facets() -> None:
    a = _job("att1", "p1", [_slide(1, visual=True)])
    b = _job("att2", "p1", [_slide(1, visual=True)])
    a.summary, b.summary = "Deck A.", "Deck B."
    # Facets ride from the attachment sidecar (set on jobs at discovery) into the
    # record-summary sidecar, so the record summary chunk is also filterable.
    a.facets = {"practice_area": ["Health"], "project_region": "East Africa"}

    store = _Store()
    extractor = batch.BatchExtractor(client=object(), store=store, renderer=object())
    extractor._write_record_summaries([a, b], {"p1": "Combined."})
    meta = store.jsons["raw/d_quals/p1/.airtable_meta.json"]
    assert meta["facets"] == {"practice_area": ["Health"], "project_region": "East Africa"}


# -- document track (PDF/DOCX/XLSX) -----------------------------------------

def _docjob(custom_id: str, identifier: str, name: str, **kw) -> DocJob:
    return DocJob(
        custom_id=custom_id,
        identifier=identifier,
        record_id=kw.get("record_id", "rec1"),
        column="Deliverable Attachments",
        original_key=f"raw/d_quals/{identifier}/deliverable_attachments/{custom_id}/{name}",
        normalized_key=(
            f"raw/d_quals/{identifier}/deliverable_attachments/{custom_id}/{custom_id}__normalized.txt"
        ),
        metadata_header=kw.get("metadata_header", ""),
        base_id="appX",
        table_id="tblX",
    )


def test_doc_request_built_for_uploaded_pdf_only() -> None:
    pdf = _docjob("att1", "p1", "report.pdf")
    pdf.pdf_file_id = "file_pdf1"
    xlsx = _docjob("att2", "p1", "model.xlsx")
    xlsx.text = "## Sheet1\n| a | b |"  # local extraction, no request
    no_pdf = _docjob("att3", "p1", "broken.docx")  # conversion failed, no file id

    reqs = build_doc_requests([pdf, xlsx, no_pdf])

    assert [r["custom_id"] for r in reqs] == ["att1"]  # only the uploaded PDF
    content = reqs[0]["params"]["messages"][0]["content"]
    assert content[0] == {"type": "document",
                          "source": {"type": "file", "file_id": "file_pdf1"}}


def test_apply_doc_results_and_assemble() -> None:
    job = _docjob("att1", "p1", "report.pdf", metadata_header="---\nName: P\n---\n\n")
    apply_doc_results([job], {"att1": "  # Extracted\nbody text  "})
    assert job.text == "# Extracted\nbody text"
    md = assemble_doc_markdown(job)
    assert md.startswith("---\nName: P\n---")
    assert "# Extracted" in md


def test_record_summary_mixes_decks_and_docs() -> None:
    deck = _job("att1", "p1", [_slide(1, visual=True)])
    deck.summary = "Deck summary."
    doc = _docjob("att2", "p1", "proposal.pdf")
    doc.summary = "Proposal summary."
    requests, reused = build_record_summary_requests([deck, doc])
    assert reused == {}
    digest = requests[0]["params"]["messages"][0]["content"][0]["text"]
    assert "Deck summary." in digest and "Proposal summary." in digest


# -- isolation guards -------------------------------------------------------

def test_duplicate_custom_ids_detected() -> None:
    a = _job("att1", "p1", [_slide(1, visual=True)])
    b = _job("att1", "p2", [_slide(1, visual=True)])  # same custom_id!
    assert duplicate_custom_ids([a, b]) == {"att1"}
    with pytest.raises(ValueError, match="overwrite"):
        assert_isolation([a, b])


def test_identifier_mapping_to_two_records_detected() -> None:
    a = _job("att1", "p1", [_slide(1, visual=True)], record_id="recA")
    b = _job("att2", "p1", [_slide(1, visual=True)], record_id="recB")  # same id, diff record
    assert find_record_conflicts([a, b]) == {"p1": {"recA", "recB"}}
    with pytest.raises(ValueError, match="merge"):
        assert_isolation([a, b])


def test_failed_image_upload_degrades_to_parser_only() -> None:
    # A flaky Files upload must NOT crash the deck or the wave — the slide falls
    # back to parser-only (visual flags cleared, no file_id, no KeyError later).
    rendered = [_slide(1, visual=True, text="Bullet.")]

    class _Renderer:
        def render(self, binary):  # noqa: ARG002
            for r in rendered:
                r.image_png = b"PNG"  # give it bytes so upload is attempted
            return rendered

    class _Store:
        def get_bytes(self, key):  # noqa: ARG002
            return b"deck"

    class _Files:
        def upload(self, **kw):  # noqa: ARG002
            raise ValueError("storage down")

    class _Client:
        class beta:  # noqa: N801
            files = _Files()

    job = _job("att1", "p1", [])
    extractor = batch.BatchExtractor(client=_Client(), store=_Store(), renderer=_Renderer())
    extractor.render_and_upload(job)  # must not raise

    assert job.image_file_ids == {}            # nothing uploaded
    assert job.rendered[0].has_visual is False  # degraded to parser-only
    # request building must not KeyError on the missing file_id; the deck still
    # gets a text-only correction request (no image blocks).
    reqs = build_enrich_requests([job])
    assert len(reqs) == 1
    assert not [b for b in reqs[0]["params"]["messages"][0]["content"] if b.get("type") == "image"]


def test_normalized_text_written_even_when_batch_round_fails() -> None:
    # "make sure we get normalized text": a failed enrich/summary batch must
    # still leave parser-extracted normalized.txt for every rendered deck.
    rendered = [_slide(1, visual=True, text="Key finding.")]

    class _Renderer:
        def render(self, binary):  # noqa: ARG002
            for r in rendered:
                r.image_png, r.has_visual, r.needs_image = b"PNG", True, False
            return rendered

    class _Batches:
        def create(self, **kw):  # noqa: ARG002
            raise RuntimeError("batch endpoint down")

    class _Files:
        def upload(self, **kw):  # noqa: ARG002
            return type("F", (), {"id": "file1"})()

    class _Client:
        class beta:  # noqa: N801
            files = _Files()
            class messages:  # noqa: N801
                batches = _Batches()

    class _Store:
        def __init__(self):
            self.texts, self.jsons = {}, {}
        def get_bytes(self, key):  # noqa: ARG002
            return b"deck"
        def put_text(self, key, text):
            self.texts[key] = text
        def put_json(self, key, payload):
            self.jsons[key] = payload

    job = _job("att1", "p1", [])
    store = _Store()
    extractor = batch.BatchExtractor(client=_Client(), store=store, renderer=_Renderer())
    extractor.process_wave([job])  # must not raise despite the failing batch

    nk = "raw/d_quals/p1/deliverable_attachments/att1/att1__normalized.txt"
    assert nk in store.texts
    assert "## Slide 1: Title 1" in store.texts[nk]
    assert "Key finding." in store.texts[nk]


def test_clean_wave_passes_isolation() -> None:
    a = _job("att1", "p1", [_slide(1, visual=True)], record_id="recA")
    b = _job("att2", "p1", [_slide(1, visual=True)], record_id="recA")  # same record, fine
    c = _job("att3", "p2", [_slide(1, visual=True)], record_id="recB")
    assert duplicate_custom_ids([a, b, c]) == set()
    assert find_record_conflicts([a, b, c]) == {}
    assert_isolation([a, b, c])  # no raise
