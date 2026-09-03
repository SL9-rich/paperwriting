"""Deterministic retrieval contracts; no downloads or external API calls."""

from contextlib import closing

import numpy as np
import pytest

from papercoding.infrastructure import retrieval as rag
from papercoding.infrastructure.storage import Store


class Embedder:
    def __init__(self):
        self.passages = []

    def passage_embed(self, texts, **kwargs):
        for text in texts:
            self.passages.append(text)
            yield self.vector(text)

    def query_embed(self, texts, **kwargs):
        for text in texts:
            yield self.vector(text)

    @staticmethod
    def vector(text):
        if any(term in text for term in ["automobile", "vehicle"]):
            return np.array([1, 0, 0])
        if "ocean" in text:
            return np.array([0, 1, 0])
        return np.array([0, 0, 1])


@pytest.fixture
def setup(tmp_path, monkeypatch):
    store, model = Store(tmp_path), Embedder()
    monkeypatch.setattr(rag, "get_embedder", lambda name: model)
    monkeypatch.setattr(rag, "model_name", lambda: "fixture-model")
    return store, model


def add(store, text, ident="source"):
    store.put_item("literature", {"filename": ident + ".pdf", "text": text}, ident)


def test_semantic_route_recovers_keyword_miss_and_stable_citation(setup):
    store, model = setup
    add(store, "An automobile consumes energy.")
    add(store, "The ocean is deep.", "other")
    assert rag.retrieve(store, "vehicle", mode="keyword")["results"] == []
    hits = rag.retrieve(store, "vehicle")["results"]
    assert hits[0]["sourceName"] == "source.pdf"
    assert hits[0]["retrievalMethods"] == ["vector"]
    ident = next(k for k, row in rag.chunks(store).items() if row["documentId"] == "source")
    assert hits[0]["chunkId"] == ident
    both = rag.retrieve(store, "automobile")["results"][0]
    assert both["retrievalMethods"] == ["keyword", "vector"]
    assert both["score"] == pytest.approx(2 / 61)


def test_persistence_incremental_updates_and_deletion(setup):
    store, model = setup
    add(store, "automobile")
    rag.retrieve(store, "vehicle")
    assert len(model.passages) == 1
    reopened = Store(store.root)
    rag.retrieve(reopened, "vehicle")
    assert len(model.passages) == 1  # cached on disk, no passage re-embedding
    add(store, "ocean", "other")
    rag.retrieve(store, "vehicle")
    assert len(model.passages) == 2
    add(store, "updated automobile")
    rag.retrieve(store, "vehicle")
    assert len(model.passages) == 3
    assert rag.index_status(store)["indexed"] == 2
    store.delete_item("source")
    assert rag.retrieve(store, "vehicle")["results"] == []
    with closing(rag.connect(store)) as c:
        assert c.execute("SELECT COUNT(*) FROM vectors").fetchone()[0] == 1


def test_model_namespace_prevents_mixed_embeddings(setup, monkeypatch):
    store, model = setup
    add(store, "automobile")
    rag.retrieve(store, "vehicle")
    monkeypatch.setattr(rag, "model_name", lambda: "another-model")
    assert rag.index_status(store)["pending"] == 1
    rag.retrieve(store, "vehicle")
    assert len(model.passages) == 2


def test_explicit_fallback_and_vector_only_error(setup, monkeypatch):
    store, _ = setup
    add(store, "automobile")

    def unavailable(name):
        raise RuntimeError("offline")

    monkeypatch.setattr(rag, "get_embedder", unavailable)
    result = rag.retrieve(store, "automobile")
    assert result["actualMode"] == "keyword" and result["warning"]
    assert result["results"][0]["retrievalMethods"] == ["keyword"]
    assert rag.retrieve(store, "unmatched")["warning"]  # visible even with zero hits
    with pytest.raises(ValueError, match="向量检索不可用"):
        rag.retrieve(store, "automobile", mode="vector")
    assert rag.retrieve(store, "automobile", mode="keyword")["warning"] == ""


def test_empty_query_does_not_load_model_and_invalid_mode(setup, monkeypatch):
    store, _ = setup
    add(store, "automobile")
    monkeypatch.setattr(rag, "get_embedder", lambda name: pytest.fail("unexpected model call"))
    assert rag.retrieve(store, "  ")["results"] == []
    with pytest.raises(ValueError, match="检索模式"):
        rag.retrieve(store, "vehicle", mode="bogus")


def test_delete_during_embedding_excludes_stale_evidence(setup, monkeypatch):
    store, model = setup
    add(store, "automobile")
    original = model.query_embed

    def remove(texts, **kwargs):
        store.delete_item("source")
        yield from original(texts, **kwargs)

    monkeypatch.setattr(model, "query_embed", remove)
    assert rag.retrieve(store, "vehicle")["results"] == []


def test_failed_rebuild_preserves_previous_index(setup, monkeypatch):
    store, model = setup
    add(store, "automobile")
    rag.retrieve(store, "vehicle")

    def invalid(texts, **kwargs):
        yield np.array([float("nan"), 0, 0])

    monkeypatch.setattr(model, "passage_embed", invalid)
    with pytest.raises(ValueError, match="无效向量"):
        rag.build_index(store)
    assert rag.index_status(store)["indexed"] == 1
    assert rag.retrieve(store, "vehicle", mode="vector")["results"][0]["documentId"] == "source"


def test_long_text_embedding_includes_tail(setup):
    _, model = setup
    result = rag.embed_texts(model, ["a" * 1000 + "automobile"])
    assert any("automobile" in text for text in model.passages)
    assert result[0][0] > 0
