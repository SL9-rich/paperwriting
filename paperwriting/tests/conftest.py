"""Keep tests isolated from all real model credentials and user data."""

import pytest


@pytest.fixture(autouse=True)
def isolated_config(tmp_path, monkeypatch):
    monkeypatch.setenv("LLM_API_KEY", "")
    monkeypatch.setenv("LLM_BASE_URL", "")
    monkeypatch.setenv("LLM_MODEL", "")
    monkeypatch.setenv("PAPERCODING_ENABLE_EMBEDDINGS", "false")
    monkeypatch.setenv("PAPERCODING_DATA_DIR", str(tmp_path / "runtime"))
    monkeypatch.setenv("PAPERCODING_EMBEDDING_CACHE", str(tmp_path / "models"))
