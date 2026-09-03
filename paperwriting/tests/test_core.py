import io
import json

import httpx
import pytest
from docx import Document

from papercoding.infrastructure.storage import Store, ConflictError
from papercoding.infrastructure.llm import Provider, ModelError
from papercoding.application.services import (
    revise,
    write_full,
    merge_translations,
    translate_text,
    translate_batch,
    add_template,
    add_pdf,
)
from papercoding.application.jobs import Jobs
from papercoding.infrastructure.exporting import docx_bytes, english_paper


@pytest.fixture
def store(tmp_path):
    return Store(tmp_path / "data")


class Fake:
    def __init__(self, responses):
        self.responses = iter(responses)

    def json(self, *args, **kwargs):
        return next(self.responses), 100


def test_stale_save_and_candidate_application_atomic(store):
    original, revision = store.paper()
    run = store.new_run("rewrite", ["02"])
    store.update_run(
        run, "waiting_for_approval", candidates=[{"section_id": "02", "revised_text": "new"}]
    )
    store.apply(run)
    with pytest.raises(ValueError):
        store.apply(run)
    assert store.paper()[0]["sections"]["02"] == "new"
    assert len(store.versions()) == 1
    with pytest.raises(ConflictError):
        store.save(original, revision)


def test_concurrent_candidate_cannot_overwrite(store):
    run = store.new_run("rewrite", ["02"])
    store.update_run(
        run, "waiting_for_approval", candidates=[{"section_id": "02", "revised_text": "new"}]
    )
    paper, revision = store.paper()
    paper["sections"]["02"] = "manual"
    store.save(paper, revision)
    with pytest.raises(ConflictError):
        store.apply(run)
    assert store.paper()[0]["sections"]["02"] == "manual"


def test_restore_preserves_current_and_history(store):
    store.version("first")
    old = store.versions()[0]["id"]
    paper, rev = store.paper()
    paper["sections"]["02"] = "manual"
    store.save(paper, rev)
    store.restore(old, store.paper()[1])
    assert store.paper()[0]["sections"]["02"] == ""
    assert any(json.loads(v["payload"])["sections"]["02"] == "manual" for v in store.versions())
    store.delete_version(old)
    assert len(store.versions()) == 2


def test_scope_rejection_and_reviewer_revision(store):
    with pytest.raises(ValueError):
        store.new_run("attack", ["missing"])
    provider = Fake(
        [
            {"section_id": "02", "revised_text": "initial"},
            {"verdict": "revise", "issues": ["needs evidence"]},
            {"action": "accept"},
            {"section_id": "02", "revised_text": "bounded"},
            {"verdict": "pass", "issues": []},
        ]
    )
    result = revise(store, provider, "improve", ["02"])
    run = store.runs()[0]
    assert run["status"] == "waiting_for_approval"
    assert run["payload"]["calls"] == 5
    assert store.paper()[0]["sections"]["02"] == ""
    from papercoding.application.workflow import decide

    decide(store, result["run_id"], True)
    assert store.paper()[0]["sections"]["02"] == "bounded"


def test_invalid_rag_citation_fails_without_writes(store):
    candidate = {"section_id": "02", "revised_text": "made up [RAG:invalid]"}
    fake = Fake(
        [candidate, {"verdict": "pass"}, {"action": "accept"}, candidate, {"verdict": "pass"}]
    )
    with pytest.raises(ValueError, match="无效 RAG"):
        revise(store, fake, "improve", ["02"])
    assert store.runs()[0]["status"] == "failed"
    assert store.paper()[0]["sections"]["02"] == ""


def test_full_generation_requires_later_approval(store):
    paper, rev = store.paper()
    paper["outline"] = {"sections": paper["structure"]}
    store.save(paper, rev)
    fake = Fake(
        [
            {
                "sections": [
                    {"section_id": s["id"], "revised_text": "generated"} for s in paper["structure"]
                ]
            }
        ]
    )
    result = write_full(store, fake)
    assert store.paper()[0]["sections"]["01"] == ""
    store.apply(result["run_id"])
    assert store.paper()[0]["sections"]["01"] == "generated"


def test_formula_guard():
    text = "公式 $$x=1$$"
    with pytest.raises(ValueError, match="公式"):
        translate_text(Fake([{"translated_text": "Formula missing"}]), text)
    result = translate_text(Fake([{"translated_text": "Formula [[PC_FORMULA_0]]"}]), text)
    assert result["candidate"] == "Formula $$x=1$$"


def test_translation_merge_is_atomic_and_checks_source(store):
    paper, rev = store.paper()
    paper["sections"]["02"] = "中文"
    store.save(paper, rev)
    results = translate_batch(
        store, Fake([{"translated_text": "Title"}, {"translated_text": "English"}]), ["02"], True
    )
    merge_translations(store, results["translations"])
    with pytest.raises(ConflictError):
        merge_translations(store, results["translations"])
    paper, rev = store.paper()
    paper["sections"]["02"] = "new source"
    store.save(paper, rev)
    with pytest.raises(ConflictError):
        merge_translations(store, results["translations"], True)
    with pytest.raises(ValueError):
        english_paper(store.paper()[0])


def test_docx_preserves_template_but_not_fiction(store):
    doc = Document()
    doc.add_paragraph("fictional old paper")
    doc.sections[0].header.paragraphs[0].text = "Journal"
    data = io.BytesIO()
    doc.save(data)
    add_template(store, "Test Journal", [("template.docx", data.getvalue())])
    paper, rev = store.paper()
    paper["title"] = "Actual research"
    paper["sections"]["02"] = "Measured data"
    paper["references"] = "Verified reference"
    store.save(paper, rev)
    result = Document(io.BytesIO(docx_bytes(store, paper)))
    text = "\n".join(p.text for p in result.paragraphs)
    assert "Actual research" in text and "Measured data" in text
    assert "fictional old paper" not in text
    assert result.sections[0].header.paragraphs[0].text == "Journal"
    assert "Verified reference" in text


def test_file_paths_cannot_escape(store):
    with pytest.raises(ValueError):
        store.read_file("../.env")
    relative = store.file(b"hello", "../../evil.txt")
    assert relative.startswith("files/") and store.read_file(relative) == b"hello"
    with pytest.raises(ValueError):
        add_pdf(store, "fake.pdf", b"not pdf")


def test_provider_missing_key_and_malformed_json():
    with pytest.raises(ModelError):
        Provider(key="").json("s", "u")
    transport = httpx.MockTransport(
        lambda request: httpx.Response(200, json={"choices": [{"message": {"content": "broken"}}]})
    )
    with pytest.raises(ModelError):
        Provider(
            key="test", model="test", base_url="https://model.example/v1", transport=transport
        ).json("s", "u")


def test_provider_uses_configured_remote_and_json_contract():
    def handler(request):
        assert str(request.url) == "https://model.example/v1/chat/completions"
        body = json.loads(request.content)
        assert body["response_format"]["type"] == "json_object"
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": '{"ok": true}'}}],
                "usage": {"total_tokens": 42},
            },
        )

    assert Provider(
        key="test",
        model="test",
        base_url="https://model.example/v1",
        transport=httpx.MockTransport(handler),
    ).json("s", "u") == ({"ok": True}, 42)


def test_background_job_result_and_restart_recovery(store):
    jobs = Jobs(store)
    job_id = jobs.submit("test", lambda progress: {"done": True})
    jobs.pool.shutdown(wait=True)
    row = next(x for x in store.jobs() if x["id"] == job_id)
    assert row["status"] == "success" and row["result"] == {"done": True}
    store.job_update(job_id, status="running", phase="old worker")
    restarted = Jobs(store)
    assert store.jobs()[0]["status"] == "interrupted"
    restarted.pool.shutdown(wait=True)


def test_pdf_textless_upload_is_explicit(store):
    from pypdf import PdfWriter

    writer = PdfWriter()
    writer.add_blank_page(width=100, height=100)
    buffer = io.BytesIO()
    writer.write(buffer)
    add_pdf(store, "blank.pdf", buffer.getvalue())
    assert store.items("literature")[0]["status"] == "needs_ocr"


def test_apply_revalidates_persisted_blank_candidate(store):
    run = store.new_run("rewrite", ["02"])
    store.update_run(
        run, "waiting_for_approval", candidates=[{"section_id": "02", "revised_text": "   "}]
    )
    before = (store.paper(), store.versions(), store.runs())
    with pytest.raises(ValueError, match="非空文本"):
        store.apply(run)
    assert (store.paper(), store.versions(), store.runs()) == before
