"""Local hybrid retrieval: lexical match + persisted dense vectors + RRF.

The dense index is an exact cosine scan, not an approximate ANN index.
Original evidence IDs and chunk boundaries remain compatible with saved runs.
"""

from __future__ import annotations

import hashlib
import logging
import os
import re
import sqlite3
import threading
from contextlib import closing
from functools import lru_cache
from pathlib import Path

import numpy as np
from papercoding.config import embeddings_enabled, embedding_cache, load_config, data_directory

DEFAULT_MODEL = "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"
INDEX_VERSION = "fastembed-mean-w240-s200-v1"
logger = logging.getLogger(__name__)
_model_lock = threading.RLock()


def model_name():
    return os.getenv("PAPERCODING_EMBEDDING_MODEL", DEFAULT_MODEL)


@lru_cache(maxsize=8)
def index_lock(path):
    return threading.RLock()


@lru_cache(maxsize=2)
def get_embedder(name):
    if not embeddings_enabled():
        raise ValueError(
            "语义检索未启用；安装 requirements-embeddings.txt 并设置 PAPERCODING_ENABLE_EMBEDDINGS=true 后重启。"
        )
    from fastembed import TextEmbedding

    # Use verified system certificates on macOS; never disable TLS verification.
    if Path("/etc/ssl/cert.pem").exists():
        os.environ.setdefault("SSL_CERT_FILE", "/etc/ssl/cert.pem")
        os.environ.setdefault("REQUESTS_CA_BUNDLE", "/etc/ssl/cert.pem")
    return TextEmbedding(name, cache_dir=str(embedding_cache()), threads=2)


def chunks(store):
    result = {}
    for doc in store.items("literature"):
        text = doc.get("text", "")
        for offset in range(0, len(text), 1020):
            content = text[offset : offset + 1200]
            if not content.strip():
                continue
            ident = doc["id"] + ":" + hashlib.sha256(content.encode()).hexdigest()[:12]
            result.setdefault(
                ident,
                dict(
                    chunkId=ident,
                    documentId=doc["id"],
                    sourceName=doc.get("filename", "未命名文献"),
                    content=content,
                    offset=offset,
                ),
            )
    return result


def keyword_rank(records, query):
    terms = set(re.findall(r"[a-z][a-z0-9_-]+", query.lower()))
    for run in re.findall(r"[\u3400-\u9fff]+", query):
        terms.update(run[i : i + 2] for i in range(max(1, len(run) - 1)))
    scores = {
        ident: sum(term in row["content"].lower() for term in terms)
        for ident, row in records.items()
    }
    return sorted(((k, v) for k, v in scores.items() if v), key=lambda x: (-x[1], x[0]))


def normalized(values):
    vector = np.asarray(values, dtype=np.float32)
    if vector.ndim != 1 or not vector.size or not np.isfinite(vector).all():
        raise ValueError("向量模型返回了无效向量。")
    norm = np.linalg.norm(vector)
    if norm <= 0:
        raise ValueError("向量模型返回了零向量。")
    return vector / norm


def embed_texts(model, texts, query=False):
    # Pool short windows so long evidence chunks / Chinese text are not represented
    # only by their beginning. Version this policy with the persisted model namespace.
    windows, spans = [], []
    for text in texts:
        start = len(windows)
        windows.extend(text[i : i + 240] for i in range(0, len(text), 200))
        spans.append((start, len(windows)))
    method = model.query_embed if query else model.passage_embed
    vectors = [normalized(v) for v in method(windows, batch_size=16)]
    if len(vectors) != len(windows):
        raise ValueError("向量数量与文本不一致。")
    return [normalized(np.mean(vectors[a:b], axis=0)) for a, b in spans]


def connect(store):
    c = sqlite3.connect(store.root / "rag_index.sqlite", timeout=30)
    c.execute("PRAGMA journal_mode=WAL")
    c.execute(
        "CREATE TABLE IF NOT EXISTS vectors (namespace TEXT NOT NULL, chunk_id TEXT NOT NULL, dimension INTEGER NOT NULL, vector BLOB NOT NULL, PRIMARY KEY(namespace,chunk_id))"
    )
    return c


def sync_index(store, records, name, progress=lambda *args: None, force=False):
    namespace = name + ":" + INDEX_VERSION
    with closing(connect(store)) as c:
        cached = {
            ident: np.frombuffer(blob, dtype="<f4").copy()
            for ident, dim, blob in c.execute(
                "SELECT chunk_id,dimension,vector FROM vectors WHERE namespace=?", (namespace,)
            )
            if len(blob) == dim * 4
        }
        missing = [key for key in records if force or key not in cached]
        if missing:
            with _model_lock:
                model = get_embedder(name)
                for offset in range(0, len(missing), 16):
                    batch = missing[offset : offset + 16]
                    vectors = embed_texts(model, [records[k]["content"] for k in batch])
                    cached.update(zip(batch, vectors))
                    progress(
                        round((offset + len(batch)) / len(missing) * 95),
                        f"向量索引：{offset + len(batch)}/{len(missing)} 个片段",
                    )
        # Atomic publish: failed embeddings leave the previous index intact.
        with c:
            c.executemany(
                "DELETE FROM vectors WHERE namespace=? AND chunk_id=?",
                [(namespace, key) for key in cached if key not in records],
            )
            c.executemany(
                "INSERT OR REPLACE INTO vectors VALUES(?,?,?,?)",
                [
                    (namespace, key, len(cached[key]), cached[key].astype("<f4").tobytes())
                    for key in missing
                ],
            )
        return {key: cached[key] for key in records}


def index_status(store):
    records = chunks(store)
    name = model_name()
    namespace = name + ":" + INDEX_VERSION
    with closing(connect(store)) as c:
        ids = {
            row[0]
            for row in c.execute("SELECT chunk_id FROM vectors WHERE namespace=?", (namespace,))
        }
    return dict(
        model=name,
        chunks=len(records),
        indexed=len(ids & records.keys()),
        pending=len(records.keys() - ids),
        backend="sqlite-exact-cosine",
    )


def build_index(store, progress=lambda *args: None):
    with index_lock(str(store.root)):
        records = chunks(store)
        sync_index(store, records, model_name(), progress, force=True)
        progress(100, "向量索引已更新")
        return index_status(store)


def retrieve(store, query, limit=8, mode="hybrid"):
    if mode not in {"hybrid", "keyword", "vector"}:
        raise ValueError("检索模式必须为 hybrid、keyword 或 vector。")
    query = str(query).strip()
    limit = max(1, min(50, int(limit)))
    if len(query) > 30000:
        raise ValueError("检索内容过长，请缩小到具体问题或段落。")
    if not query:
        return dict(results=[], requestedMode=mode, actualMode=mode, warning="")
    with index_lock(str(store.root)):
        records = chunks(store)
        lexical = keyword_rank(records, query) if mode != "vector" else []
        dense, warning, actual = [], "", mode
        if records and mode != "keyword":
            try:
                name = model_name()
                vectors = sync_index(store, records, name)
                with _model_lock:
                    q = embed_texts(get_embedder(name), [query], query=True)[0]
                dense = sorted(
                    ((key, float(np.dot(normalized(v), q))) for key, v in vectors.items()),
                    key=lambda x: (-x[1], x[0]),
                )
                dense = [(k, score) for k, score in dense if score > 0]
            except Exception:
                logger.exception("Local vector retrieval failed")
                if mode == "vector":
                    raise ValueError(
                        "向量检索不可用，请检查模型下载和本机依赖，或切换关键词模式。"
                    ) from None
                warning = "向量检索暂不可用，本次仅使用关键词结果；请检查模型下载或重建索引。"
                actual = "keyword"
        # Fuse ranks, never add incomparable lexical counts and cosine scores.
        depth = max(20, limit * 4)
        selected = {}
        for route, ranking in [("keyword", lexical[:depth]), ("vector", dense[:depth])]:
            for rank, (key, score) in enumerate(ranking, 1):
                row = selected.setdefault(
                    key,
                    dict(
                        records[key],
                        score=0.0,
                        retrievalMethods=[],
                        keywordScore=None,
                        vectorScore=None,
                        keywordRank=None,
                        vectorRank=None,
                    ),
                )
                row["retrievalMethods"].append(route)
                row[route + "Score"], row[route + "Rank"] = score, rank
                row["score"] += 1.0 / (60 + rank) if actual == "hybrid" else score
        # Deletions or edits during model execution must not leak old evidence.
        live = chunks(store)
        hits = [r for key, r in selected.items() if key in live]
        for row in hits:
            row["sourceName"] = live[row["chunkId"]]["sourceName"]
            row["retrievalMode"] = actual
            row["retrievalWarning"] = warning
        hits.sort(key=lambda r: (-r["score"], r["chunkId"]))
        return dict(results=hits[:limit], requestedMode=mode, actualMode=actual, warning=warning)


if __name__ == "__main__":
    import json
    from papercoding.infrastructure.storage import Store

    load_config()
    store = Store(data_directory())
    print(
        json.dumps(
            build_index(store, lambda percent, phase: print(phase, flush=True)), ensure_ascii=False
        )
    )
