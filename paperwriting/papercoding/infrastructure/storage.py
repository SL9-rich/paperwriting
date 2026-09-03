from __future__ import annotations

import hashlib
import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path


from papercoding.domain.models import now, uid, encode, empty_paper, ConflictError


class Store:
    def __init__(self, root: Path):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        (self.root / "files").mkdir(exist_ok=True)
        self.path = self.root / "papercoding.sqlite"
        with self.db() as c:
            c.executescript("""
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS py_paper (id INTEGER PRIMARY KEY CHECK(id=1), revision INTEGER NOT NULL, payload TEXT NOT NULL, updated TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS py_versions (id TEXT PRIMARY KEY, label TEXT NOT NULL, payload TEXT NOT NULL, created TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS py_items (id TEXT PRIMARY KEY, kind TEXT NOT NULL, payload TEXT NOT NULL, created TEXT NOT NULL);
            CREATE INDEX IF NOT EXISTS py_items_kind ON py_items(kind);
            CREATE TABLE IF NOT EXISTS py_jobs (id TEXT PRIMARY KEY, kind TEXT NOT NULL, status TEXT NOT NULL, progress INTEGER NOT NULL, phase TEXT NOT NULL, result TEXT NOT NULL, error TEXT NOT NULL, created TEXT NOT NULL, updated TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS py_runs (id TEXT PRIMARY KEY, status TEXT NOT NULL, payload TEXT NOT NULL, created TEXT NOT NULL, updated TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS py_steps (id INTEGER PRIMARY KEY, run_id TEXT NOT NULL, role TEXT NOT NULL, payload TEXT NOT NULL, created TEXT NOT NULL);
            CREATE INDEX IF NOT EXISTS py_steps_run ON py_steps(run_id);
            CREATE TABLE IF NOT EXISTS py_workflow_calls (run_id TEXT NOT NULL, step_key TEXT NOT NULL, request_hash TEXT NOT NULL, result TEXT NOT NULL, tokens INTEGER NOT NULL, created TEXT NOT NULL, PRIMARY KEY(run_id,step_key));
            CREATE TABLE IF NOT EXISTS py_settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            """)
            c.execute(
                "INSERT OR IGNORE INTO py_paper VALUES(1,0,?,?)", (encode(empty_paper()), now())
            )

    @contextmanager
    def db(self):
        c = sqlite3.connect(self.path, timeout=30)
        c.row_factory = sqlite3.Row
        try:
            yield c
            c.commit()
        except BaseException:
            c.rollback()
            raise
        finally:
            c.close()

    def paper(self):
        with self.db() as c:
            row = c.execute("SELECT * FROM py_paper WHERE id=1").fetchone()
        return json.loads(row["payload"]), row["revision"]

    def save(self, paper, revision):
        self.validate(paper)
        with self.db() as c:
            changed = c.execute(
                "UPDATE py_paper SET payload=?,revision=revision+1,updated=? WHERE id=1 AND revision=?",
                (encode(paper), now(), revision),
            ).rowcount
            if not changed:
                raise ConflictError("论文已在另一操作中更新，请刷新后重试，避免覆盖新内容。")
        return revision + 1

    @staticmethod
    def validate(paper):
        if not isinstance(paper.get("title"), str) or not paper["title"].strip():
            raise ValueError("请输入论文标题。")
        structure = paper.get("structure")
        if not isinstance(structure, list) or not structure or structure[0].get("id") != "01":
            raise ValueError("摘要必须保留在目录第一位。")
        ids = [s["id"] for s in structure]
        if len(ids) != len(set(ids)) or any(
            not isinstance(s.get("title"), str) or not s["title"].strip() for s in structure
        ):
            raise ValueError("章节编号不能重复，标题不能为空。")
        if any(not isinstance(paper["sections"].get(i), str) for i in ids):
            raise ValueError("章节正文格式不正确。")

    def version(self, label):
        with self.db() as c:
            c.execute("BEGIN IMMEDIATE")
            row = c.execute("SELECT payload FROM py_paper WHERE id=1").fetchone()
            c.execute(
                "INSERT INTO py_versions VALUES(?,?,?,?)",
                (uid(), label or "手动保存", row[0], now()),
            )

    def versions(self):
        with self.db() as c:
            return [
                dict(r)
                for r in c.execute("SELECT * FROM py_versions ORDER BY created DESC,rowid DESC")
            ]

    def restore(self, version_id, revision):
        with self.db() as c:
            c.execute("BEGIN IMMEDIATE")
            current = c.execute("SELECT * FROM py_paper WHERE id=1").fetchone()
            if current["revision"] != revision:
                raise ConflictError("论文已更新，请刷新后恢复。")
            row = c.execute("SELECT * FROM py_versions WHERE id=?", (version_id,)).fetchone()
            if not row:
                raise ValueError("版本不存在。")
            c.execute(
                "INSERT INTO py_versions VALUES(?,?,?,?)",
                (uid(), "恢复前自动备份", current["payload"], now()),
            )
            restored = json.loads(row["payload"])
            self.validate(restored)
            c.execute(
                "UPDATE py_paper SET payload=?,revision=revision+1,updated=? WHERE id=1",
                (encode(restored), now()),
            )
            c.execute(
                "INSERT INTO py_versions VALUES(?,?,?,?)",
                (uid(), "恢复：" + row["label"], encode(restored), now()),
            )

    def delete_version(self, version_id):
        with self.db() as c:
            c.execute("DELETE FROM py_versions WHERE id=?", (version_id,))

    def put_item(self, kind, payload, item_id=None):
        item_id = item_id or uid()
        with self.db() as c:
            c.execute(
                "INSERT INTO py_items VALUES(?,?,?,?) ON CONFLICT(id) DO UPDATE SET payload=excluded.payload",
                (item_id, kind, encode(payload), now()),
            )
        return item_id

    def items(self, kind):
        with self.db() as c:
            return [
                dict(json.loads(r["payload"]), id=r["id"])
                for r in c.execute(
                    "SELECT * FROM py_items WHERE kind=? ORDER BY created DESC,rowid DESC", (kind,)
                )
            ]

    def delete_item(self, item_id):
        with self.db() as c:
            c.execute("DELETE FROM py_items WHERE id=?", (item_id,))
        # Content-addressed files remain for historical versions and recoverability.

    def file(self, content: bytes, filename: str):
        if not content or len(content) > 30 * 1024 * 1024:
            raise ValueError("文件为空或超过 30 MB。")
        suffix = Path(filename).suffix.lower()
        if len(suffix) > 12 or not all(ch.isalnum() or ch == "." for ch in suffix):
            suffix = ".bin"
        relative = "files/" + hashlib.sha256(content).hexdigest() + suffix
        path = self.root / relative
        if not path.exists():
            path.write_bytes(content)
        return relative

    def read_file(self, relative):
        p = (self.root / relative).resolve()
        if not p.is_relative_to(self.root / "files"):
            raise ValueError("无效文件路径。")
        return p.read_bytes()

    def setting(self, key, default=None):
        with self.db() as c:
            row = c.execute("SELECT value FROM py_settings WHERE key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else default

    def set_setting(self, key, value):
        with self.db() as c:
            c.execute("INSERT OR REPLACE INTO py_settings VALUES(?,?)", (key, encode(value)))

    def new_run(self, instruction, allowed, base_revision=None, workflow=False):
        paper, revision = self.paper()
        if base_revision is not None and base_revision != revision:
            raise ConflictError("生成前论文已更新，请刷新后重试。")
        ids = {s["id"] for s in paper["structure"]}
        if not allowed or len(allowed) != len(set(allowed)) or not set(allowed) <= ids:
            raise ValueError("请选择有效且不重复的修改章节。")
        run_id = uid()
        payload = dict(
            instruction=instruction,
            allowed=allowed,
            base_revision=revision,
            candidates=[],
            tokens=0,
            calls=0,
        )
        if workflow:
            payload.update(engine="langgraph-v1", paper_snapshot=paper, graph_stage="等待执行")
        with self.db() as c:
            c.execute(
                "INSERT INTO py_runs VALUES(?,?,?,?,?)",
                (run_id, "drafting", encode(payload), now(), now()),
            )
        return run_id

    def runs(self):
        with self.db() as c:
            return [
                dict(r, payload=json.loads(r["payload"]))
                for r in c.execute("SELECT * FROM py_runs ORDER BY created DESC,rowid DESC")
            ]

    def update_run(self, run_id, status, **changes):
        with self.db() as c:
            c.execute("BEGIN IMMEDIATE")
            row = c.execute("SELECT * FROM py_runs WHERE id=?", (run_id,)).fetchone()
            if not row or row["status"] in {"approved", "rejected"}:
                raise ValueError("运行不存在或已结束。")
            payload = json.loads(row["payload"])
            payload.update(changes)
            c.execute(
                "UPDATE py_runs SET status=?,payload=?,updated=? WHERE id=?",
                (status, encode(payload), now(), run_id),
            )

    def step(self, run_id, role, payload):
        with self.db() as c:
            c.execute(
                "INSERT INTO py_steps(run_id,role,payload,created) VALUES(?,?,?,?)",
                (run_id, role, encode(payload), now()),
            )

    def steps(self, run_id):
        with self.db() as c:
            return [
                dict(r, payload=json.loads(r["payload"]))
                for r in c.execute("SELECT * FROM py_steps WHERE run_id=? ORDER BY id", (run_id,))
            ]

    def apply(self, run_id, _workflow=False):
        with self.db() as c:
            c.execute("BEGIN IMMEDIATE")
            run = c.execute("SELECT * FROM py_runs WHERE id=?", (run_id,)).fetchone()
            current = c.execute("SELECT * FROM py_paper WHERE id=1").fetchone()
            if not run or run["status"] != "waiting_for_approval":
                raise ValueError("此候选稿已处理或尚未生成。")
            payload = json.loads(run["payload"])
            if payload.get("engine") == "langgraph-v1" and not _workflow:
                raise ValueError("请通过 LangGraph 审批检查点应用候选稿。")
            if current["revision"] != payload["base_revision"]:
                raise ConflictError("生成后论文或材料已改变，请重新生成候选稿。原文未被覆盖。")
            candidates = payload["candidates"]
            if (
                not isinstance(candidates, list)
                or any(not isinstance(s, dict) for s in candidates)
                or len(candidates) != len(payload["allowed"])
                or {s.get("section_id") for s in candidates} != set(payload["allowed"])
            ):
                raise ValueError("候选稿与授权章节不一致。")
            # Revalidate at the final write boundary, including internally persisted
            # candidates; generation-time validation alone is not sufficient.
            if any(
                not isinstance(s.get("revised_text"), str) or not s["revised_text"].strip()
                for s in candidates
            ):
                raise ValueError("候选正文必须为非空文本，原文未被覆盖。")
            paper = json.loads(current["payload"])
            c.execute(
                "INSERT INTO py_versions VALUES(?,?,?,?)",
                (uid(), "应用候选稿前自动备份", current["payload"], now()),
            )
            for section in candidates:
                paper["sections"][section["section_id"]] = section["revised_text"]
            self.validate(paper)
            c.execute(
                "UPDATE py_paper SET payload=?,revision=revision+1,updated=? WHERE id=1",
                (encode(paper), now()),
            )
            if payload.get("engine") == "langgraph-v1":
                payload["graph_stage"] = "已应用"
            c.execute(
                "UPDATE py_runs SET status='approved',payload=?,updated=? WHERE id=?",
                (encode(payload), now(), run_id),
            )

    def jobs(self):
        with self.db() as c:
            return [
                dict(r, result=json.loads(r["result"]))
                for r in c.execute(
                    "SELECT * FROM py_jobs ORDER BY created DESC,rowid DESC LIMIT 40"
                )
            ]

    def job_update(self, job_id, status="running", progress=0, phase="", result=None, error=""):
        with self.db() as c:
            c.execute(
                "UPDATE py_jobs SET status=?,progress=?,phase=?,result=?,error=?,updated=? WHERE id=?",
                (status, progress, phase, encode(result or {}), error, now(), job_id),
            )

    def backup_bytes(self):
        import tempfile

        with tempfile.TemporaryDirectory() as folder:
            target = Path(folder) / "backup.sqlite"
            with self.db() as src, sqlite3.connect(target) as dst:
                src.backup(dst)
            return target.read_bytes()
