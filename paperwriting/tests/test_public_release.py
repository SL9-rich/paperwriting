"""Public distribution contracts. No model downloads or paid calls."""

import json
import httpx
import pytest
from starlette.testclient import TestClient
from papercoding.infrastructure.llm import Provider, ModelError
from papercoding.infrastructure.retrieval import get_embedder, retrieve
from papercoding.infrastructure.storage import Store
from papercoding.presentation.api import create_app


def test_blank_install_has_no_materials_and_no_model(tmp_path):
    app = create_app(tmp_path / "data")
    try:
        with TestClient(app) as client:
            state = client.get("/api/state").json()
            assert not state["configured"]
            assert not state["embeddings_enabled"]
            assert not any(state["paper"]["sections"].values())
            assert not state["paper"]["materials"]["thesis"]
            assert not state["paper"]["metadata"]
            assert not state["paper"]["template"]
            assert not state["paper"]["references"]
            for key in ("literature", "templates", "assets", "jobs", "runs", "versions", "chats"):
                assert state[key] == []
            assert client.get("/").status_code == 200
            assert client.get("/static/base.css").status_code == 200
            assert (
                client.post(
                    "/api/action", json={"action": "import_browser", "data": {}}
                ).status_code
                == 400
            )
    finally:
        app.state.jobs.pool.shutdown(wait=True)


def test_unconfigured_model_never_makes_request():
    def no_request(request):
        pytest.fail("Unconfigured release contacted a model")

    with pytest.raises(ModelError, match="尚未配置"):
        Provider(transport=httpx.MockTransport(no_request)).json("system", "user")


def test_embedding_opt_in_and_keyword_fallback(tmp_path):
    get_embedder.cache_clear()
    with pytest.raises(ValueError, match="未启用"):
        get_embedder("not-downloaded")
    store = Store(tmp_path / "data")
    store.put_item(
        "literature", {"filename": "synthetic.txt", "text": "A synthetic example about agents."}
    )
    result = retrieve(store, "agents")
    assert result["actualMode"] == "keyword" and result["warning"]
    assert len(result["results"]) == 1


def test_local_endpoint_can_run_without_authorization():
    def handler(request):
        assert str(request.url) == "http://127.0.0.1:8000/v1/chat/completions"
        assert "authorization" not in request.headers
        assert json.loads(request.content)["model"] == "my-local-model"
        return httpx.Response(200, json={"choices": [{"message": {"content": '{"ok":true}'}}]})

    result, _ = Provider(
        model="my-local-model",
        base_url="http://127.0.0.1:8000/v1",
        transport=httpx.MockTransport(handler),
    ).json("s", "u")
    assert result == {"ok": True}


@pytest.mark.parametrize(
    "url",
    [
        "http://model.example/v1",
        "https://user:pass@model.example/v1",
        "https://model.example/v1?key=secret",
        "file:///tmp/model",
    ],
)
def test_unsafe_endpoint_rejected_before_request(url):
    def no_request(request):
        pytest.fail("Unsafe request was sent")

    with pytest.raises(ModelError):
        Provider(
            key="test", model="test", base_url=url, transport=httpx.MockTransport(no_request)
        ).json("s", "u")
