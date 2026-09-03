import io

import pytest
from docx import Document
from starlette.testclient import TestClient

from papercoding.presentation.api import create_app


class Fake:
    model = "test"
    key = ""

    def json(self, *args, **kwargs):
        raise AssertionError("Unexpected model invocation")


@pytest.fixture
def client(tmp_path):
    app = create_app(tmp_path / "data", provider=Fake())
    with TestClient(app) as client:
        yield client
    app.state.jobs.pool.shutdown(wait=True)


def test_custom_page_and_static_resources(client):
    html = client.get("/").text
    assert "three-column-layout" in html and "chapter-editor" in html and "写作 Agent" in html
    assert "/static/workspace.js" in html
    assert client.get("/static/workspace.js").status_code == 200
    assert client.get("/static/workspace.css").status_code == 200
    assert client.get("/health").json() == {"status": "ok"}


def test_save_version_and_stale_edit(client):
    data = client.get("/api/state").json()
    paper = data["paper"]
    paper["sections"]["02"] = "Actual draft"
    result = client.post(
        "/api/action", json={"action": "save", "revision": data["revision"], "paper": paper}
    )
    assert result.status_code == 200
    assert result.json()["paper"]["sections"]["02"] == "Actual draft"
    assert (
        client.post(
            "/api/action", json={"action": "save", "revision": data["revision"], "paper": paper}
        ).status_code
        == 409
    )
    result = client.post("/api/action", json={"action": "version", "label": "Web version"})
    version = result.json()["versions"][0]
    assert version["label"] == "Web version"
    assert (
        client.post(
            "/api/action",
            json={"action": "restore", "id": version["id"], "revision": result.json()["revision"]},
        ).status_code
        == 200
    )


def test_cross_origin_writes_blocked(client):
    result = client.post(
        "/api/action", json={"action": "version"}, headers={"origin": "https://untrusted.example"}
    )
    assert result.status_code == 403
    assert not client.get("/api/state").json()["versions"]


def test_template_upload_and_export(client):
    document = Document()
    document.add_paragraph("Placeholder template content")
    output = io.BytesIO()
    document.save(output)
    response = client.post(
        "/api/upload",
        data={"kind": "template", "name": "Test journal"},
        files={
            "files": (
                "template.docx",
                output.getvalue(),
                "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            )
        },
    )
    assert response.status_code == 200, response.text
    result = client.get("/api/export?kind=docx")
    assert result.status_code == 200
    doc = Document(io.BytesIO(result.content))
    assert "Placeholder template content" not in "\n".join(p.text for p in doc.paragraphs)
    assert client.get("/api/export?kind=english").status_code == 400


def test_data_upload_preview_plot_and_backup(client):
    result = client.post(
        "/api/upload",
        data={"kind": "asset"},
        files={"files": ("measurements.csv", b"x,y\n1,2\n2,4\n3,5\n", "text/csv")},
    )
    assert result.status_code == 200, result.text
    ident = result.json()["assets"][0]["id"]
    data = client.get("/api/table", params={"id": ident}).json()
    assert data["rows"][0]["y"] == 2
    result = client.post("/api/plot", json={"id": ident, "x": "x", "ys": ["y"], "kind": "line"})
    assert result.status_code == 200, result.text
    assert any(a["kind"] == "figure" for a in result.json()["assets"])
    assert client.get("/api/export?kind=backup").content.startswith(b"PK")
    assert client.get("/api/file?path=../.env").status_code == 400


def test_candidate_apply_api(client):
    store = client.app.state.store
    run = store.new_run("bounded edit", ["02"])
    store.update_run(
        run, "waiting_for_approval", candidates=[{"section_id": "02", "revised_text": "approved"}]
    )
    response = client.post("/api/action", json={"action": "apply", "id": run})
    assert response.status_code == 200
    assert response.json()["paper"]["sections"]["02"] == "approved"
    assert client.post("/api/action", json={"action": "apply", "id": run}).status_code == 400


def test_translation_failure_retains_target_context(tmp_path):
    class BrokenModel:
        model = "test"
        key = "test"

        def json(self, *args, **kwargs):
            raise ValueError("model unavailable")

    app = create_app(tmp_path / "failure", provider=BrokenModel())
    with TestClient(app) as client:
        response = client.post(
            "/api/action",
            json={"action": "translate_one", "section": "__paper_title__", "index": 0},
        )
        assert response.status_code == 200
        app.state.jobs.pool.shutdown(wait=True)
        job = client.get("/api/state").json()["jobs"][0]
        assert job["status"] == "error"
        assert job["result"]["_context"]["section_title"] == "论文标题"
        assert job["result"]["_context"]["index"] == 0
        assert job["result"]["_context"]["mode"] == "translate"


def test_polish_confirmation_preserves_base_and_marks_job(tmp_path):
    class Polisher:
        model = "test"
        key = "test"

        def json(self, *args, **kwargs):
            return {"translated_text": "Polished title"}, 10

    app = create_app(tmp_path / "polish", provider=Polisher())
    with TestClient(app) as client:
        state = client.get("/api/state").json()
        response = client.post(
            "/api/action",
            json={
                "action": "confirm_translation",
                "section": "__paper_title__",
                "index": 0,
                "text": "Base title",
                "revision": state["revision"],
            },
        )
        assert response.status_code == 200
        response = client.post(
            "/api/action",
            json={
                "action": "translate_one",
                "section": "__paper_title__",
                "index": 0,
                "polish": True,
                "english": "Base title",
            },
        )
        assert response.status_code == 200
        app.state.jobs.pool.shutdown(wait=True)
        state = client.get("/api/state").json()
        job = state["jobs"][0]
        assert job["result"]["_context"]["mode"] == "polish"
        response = client.post(
            "/api/action",
            json={
                "action": "confirm_translation",
                "section": "__paper_title__",
                "index": 0,
                "text": "Polished title",
                "revision": state["revision"],
                "polish": True,
                "job_id": job["id"],
            },
        )
        assert response.status_code == 200, response.text
        entry = response.json()["paper"]["translations"]["__paper_title__"][0]
        assert entry["approved"] == "Base title"
        assert entry["polished"] == "Polished title"
        assert "__paper_title__:0:polish" in response.json()["jobs"][0]["result"]["_confirmed"]


def test_retrieval_modes_and_index_status(client, monkeypatch):
    from papercoding.infrastructure import retrieval as rag
    from tests.test_retrieval import Embedder

    monkeypatch.setattr(rag, "get_embedder", lambda name: Embedder())
    store = client.app.state.store
    store.put_item("literature", {"filename": "cars.pdf", "text": "An automobile consumes energy."})
    status = client.post("/api/action", json={"action": "rag_status"}).json()
    assert status["pending"] == 1
    lexical = client.post(
        "/api/action", json={"action": "search", "query": "vehicle", "mode": "keyword"}
    ).json()
    assert lexical["results"] == []
    hybrid = client.post(
        "/api/action", json={"action": "search", "query": "vehicle", "mode": "hybrid"}
    ).json()
    assert hybrid["actualMode"] == "hybrid"
    assert hybrid["results"][0]["sourceName"] == "cars.pdf"
    assert client.post("/api/action", json={"action": "rag_status"}).json()["indexed"] == 1
    assert client.post("/api/action", json={"action": "search", "mode": "bad"}).status_code == 400


def test_graph_revision_http_approval_and_replay(client):
    import time
    from tests.test_workflow import Fake as GraphFake, results

    # This separate application uses only isolated fixture data.
    from tempfile import TemporaryDirectory
    from pathlib import Path

    with TemporaryDirectory() as folder:
        app = create_app(
            Path(folder),
            provider=GraphFake(
                [results()[0], RuntimeError("temporary network failure"), *results()[1:]]
            ),
        )
        try:
            with TestClient(app) as c:
                response = c.post(
                    "/api/action",
                    json={"action": "revise", "instruction": "修改", "allowed": ["02"]},
                )
                assert response.status_code == 200
                deadline = time.monotonic() + 10
                while time.monotonic() < deadline:
                    runs = app.state.store.runs()
                    if app.state.store.jobs() and app.state.store.jobs()[0]["status"] == "error":
                        break
                    time.sleep(0.01)
                run = runs[0]
                assert run["payload"]["engine"] == "langgraph-v1"
                assert run["status"] == "failed"
                assert (
                    c.post(
                        "/api/action", json={"action": "resume_run", "id": run["id"]}
                    ).status_code
                    == 200
                )
                deadline = time.monotonic() + 10
                while time.monotonic() < deadline:
                    if app.state.store.jobs()[0]["status"] in ["success", "error"]:
                        break
                    time.sleep(0.01)
                assert app.state.store.jobs()[0]["status"] == "success"
                assert app.state.store.runs()[0]["status"] == "waiting_for_approval"
                assert (
                    c.post("/api/action", json={"action": "apply", "id": run["id"]}).status_code
                    == 200
                )
                assert (
                    c.post("/api/action", json={"action": "apply", "id": run["id"]}).status_code
                    == 400
                )
                assert len(app.state.store.versions()) == 1
        finally:
            app.state.jobs.pool.shutdown(wait=True)
