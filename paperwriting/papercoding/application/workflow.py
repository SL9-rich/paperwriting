"""Persistent LangGraph revision workflow. Local, explicit human approval."""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from contextlib import contextmanager, ExitStack
from typing import TypedDict

from filelock import FileLock, Timeout
from langsmith import tracing_context
from langgraph.graph import StateGraph, START, END
from langgraph.types import Command, interrupt
from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer

from papercoding.domain.models import ConflictError, encode, now

ENGINE = "langgraph-v1"


class State(TypedDict, total=False):
    run_id: str
    paper: dict
    instruction: str
    allowed: list[str]
    index: int
    inputs: dict
    candidate: dict
    review: dict
    decision: dict
    candidates: list[dict]
    approval: str


def run_record(store, ident):
    with store.db() as c:
        row = c.execute("SELECT * FROM py_runs WHERE id=?", (ident,)).fetchone()
    if not row:
        raise ValueError("运行不存在。")
    return dict(row, payload=json.loads(row["payload"]))


@contextmanager
def locked(store, ident):
    # Hash caller-supplied IDs before using a filesystem path.
    folder = store.root / "workflow-locks"
    folder.mkdir(exist_ok=True)
    name = hashlib.sha256(str(ident).encode()).hexdigest()
    try:
        with FileLock(str(folder / (name + ".lock")), timeout=0):
            yield
    except Timeout:
        raise ValueError("此任务正在执行，请稍后再试。") from None


def create_run(store, instruction, allowed):
    if not isinstance(instruction, str) or not instruction.strip():
        raise ValueError("请填写修改要求。")
    with locked(store, "__workflow_catalog__"):
        return store.new_run(instruction, allowed, workflow=True)


def record_step(store, ident, key, role, data):
    with store.db() as c:
        c.execute("BEGIN IMMEDIATE")
        exists = c.execute(
            "SELECT 1 FROM py_steps WHERE run_id=? AND json_extract(payload,'$.graph_key')=?",
            (ident, key),
        ).fetchone()
        if not exists:
            c.execute(
                "INSERT INTO py_steps(run_id,role,payload,created) VALUES(?,?,?,?)",
                (ident, role, encode(dict(data, graph_key=key)), now()),
            )


def call_model(store, provider, ident, key, role, system, data, limit):
    """Reserve a bounded logical call, persist receipt before returning to graph."""
    request_hash = hashlib.sha256(encode([system, data, limit]).encode()).hexdigest()
    with store.db() as c:
        c.execute("BEGIN IMMEDIATE")
        run = c.execute("SELECT * FROM py_runs WHERE id=?", (ident,)).fetchone()
        payload = json.loads(run["payload"])
        receipt = c.execute(
            "SELECT * FROM py_workflow_calls WHERE run_id=? AND step_key=?", (ident, key)
        ).fetchone()
        if receipt:
            if receipt["request_hash"] != request_hash:
                raise ValueError("检查点与当前提示词不兼容，请重新生成任务。")
            if payload["tokens"] > 80000:
                raise ValueError("达到本次 Token 预算，请重新生成。")
            return json.loads(receipt["result"])
        if payload["calls"] >= 24 or payload["tokens"] + limit > 80000:
            raise ValueError("达到本次 24 次调用或 80,000 Token 预算，请缩小范围。")
        payload["calls"] += 1
        payload["graph_stage"] = role
        c.execute(
            "UPDATE py_runs SET payload=?,updated=? WHERE id=?", (encode(payload), now(), ident)
        )
    output, tokens = provider.json(system, encode(data), limit)
    with store.db() as c:
        c.execute("BEGIN IMMEDIATE")
        run = c.execute("SELECT payload FROM py_runs WHERE id=?", (ident,)).fetchone()
        payload = json.loads(run[0])
        payload["tokens"] += tokens
        c.execute(
            "INSERT INTO py_workflow_calls VALUES(?,?,?,?,?,?)",
            (ident, key, request_hash, encode(output), tokens, now()),
        )
        c.execute(
            "INSERT INTO py_steps(run_id,role,payload,created) VALUES(?,?,?,?)",
            (ident, role, encode(dict(result=output, tokens=tokens, graph_key=key)), now()),
        )
        c.execute(
            "UPDATE py_runs SET payload=?,updated=? WHERE id=?", (encode(payload), now(), ident)
        )
    if payload["tokens"] > 80000:
        raise ValueError("达到本次 Token 预算，请缩小范围。")
    return output


def build_graph(store, provider, saver, progress):
    from .contracts import PRINCIPLES, validate_candidates
    from papercoding.infrastructure.retrieval import retrieve

    writer_prompt = ' 只修改指定章节，引用仅用 [RAG:chunkId]。返回 {"section_id":"","revised_text":"","change_summary":[]}。'
    review_prompt = (
        ' 独立审查证据、数字与过度结论，返回 {"verdict":"pass或revise","issues":[],"summary":""}。'
    )

    def mark(s, stage):
        store.update_run(s["run_id"], "drafting", graph_stage=stage, error="")
        progress(min(95, round(s["index"] / len(s["allowed"]) * 100)), stage)

    def invoke(s, role, prompt, data, limit):
        mark(s, role)
        return call_model(
            store,
            provider,
            s["run_id"],
            f"{s['index']}:{role}",
            role,
            PRINCIPLES + prompt,
            data,
            limit,
        )

    def retrieve_node(s):
        mark(s, "Retrieval")
        paper = s["paper"]
        section = s["allowed"][s["index"]]
        title = next(x["title"] for x in paper["structure"] if x["id"] == section)
        retrieval = retrieve(
            store, s["instruction"] + " " + title + " " + paper["sections"][section][:6000]
        )
        record_step(
            store,
            s["run_id"],
            f"{s['index']}:Retrieval",
            "Retrieval",
            dict(
                evidence=retrieval["results"],
                mode=retrieval["actualMode"],
                warning=retrieval["warning"],
            ),
        )
        return {
            "inputs": dict(
                instruction=s["instruction"],
                section_id=section,
                current=paper["sections"][section],
                materials=paper["materials"],
                style=paper["style"],
                evidence=retrieval["results"],
            )
        }

    def writer(s):
        result = invoke(s, "Writer", writer_prompt, s["inputs"], 6000)
        validate_candidates([result], [s["inputs"]["section_id"]])
        return {"candidate": result}

    def reviewer(s):
        result = invoke(
            s, "Evidence Reviewer", review_prompt, dict(s["inputs"], candidate=s["candidate"]), 2500
        )
        return {"review": result}

    def coordinator(s):
        return {
            "decision": invoke(
                s,
                "Coordinator",
                ' 根据审查决定，返回 {"action":"accept或revise","reason":""}。',
                s["review"],
                1200,
            )
        }

    def invalid(s):
        return sorted(
            set(re.findall(r"\[RAG:([^\]]+)\]", s["candidate"]["revised_text"]))
            - {x["chunkId"] for x in s["inputs"]["evidence"]}
        )

    def route(s):
        return (
            "rework"
            if s["review"].get("verdict") != "pass"
            or s["decision"].get("action") != "accept"
            or invalid(s)
            else "collect"
        )

    def rework(s):
        result = invoke(
            s,
            "Writer 返工",
            writer_prompt,
            dict(
                s["inputs"],
                previous=s["candidate"],
                review=s["review"],
                invalid_citations=invalid(s),
            ),
            6000,
        )
        validate_candidates([result], [s["inputs"]["section_id"]])
        return {"candidate": result}

    def rereview(s):
        return {
            "review": invoke(
                s,
                "Evidence Reviewer 复审",
                review_prompt,
                dict(s["inputs"], candidate=s["candidate"]),
                2500,
            )
        }

    def collect(s):
        if invalid(s):
            raise ValueError("候选稿包含无效 RAG 引用，已阻止应用，请重新生成。")
        candidates = s["candidates"] + [dict(s["candidate"], review=s["review"])]
        store.update_run(
            s["run_id"],
            "drafting",
            candidates=candidates,
            checkpoint=s["inputs"]["section_id"],
            graph_stage="保存章节候选",
        )
        return {"candidates": candidates, "index": s["index"] + 1}

    def publish(s):
        store.update_run(
            s["run_id"],
            "waiting_for_approval",
            candidates=s["candidates"],
            graph_stage="等待作者确认",
        )
        return {}

    def approval(s):
        decision = interrupt({"run_id": s["run_id"], "message": "请检查候选稿后确认应用或放弃。"})
        if decision not in ["approve", "reject"]:
            raise ValueError("审批决定无效。")
        return {"approval": decision}

    def commit(s):
        run = run_record(store, s["run_id"])
        # SQLite business transaction is the authority if a process exits between
        # committing the paper and saving the final graph checkpoint.
        if run["status"] in ["approved", "rejected"]:
            return {}
        if s["approval"] == "approve":
            if run["payload"]["candidates"] != s["candidates"]:
                raise ValueError("候选稿与审批检查点不一致，已拒绝写入。")
            store.apply(s["run_id"], _workflow=True)
        else:
            store.update_run(s["run_id"], "rejected", graph_stage="作者已放弃")
        return {}

    graph = StateGraph(State)
    for name, node in [
        ("retrieve", retrieve_node),
        ("writer", writer),
        ("reviewer", reviewer),
        ("coordinator", coordinator),
        ("rework", rework),
        ("rereview", rereview),
        ("collect", collect),
        ("publish", publish),
        ("approval", approval),
        ("commit", commit),
    ]:
        graph.add_node(name, node)
    graph.add_edge(START, "retrieve")
    graph.add_edge("retrieve", "writer")
    graph.add_edge("writer", "reviewer")
    graph.add_edge("reviewer", "coordinator")
    graph.add_conditional_edges("coordinator", route)
    graph.add_edge("rework", "rereview")
    graph.add_edge("rereview", "collect")
    graph.add_conditional_edges(
        "collect", lambda s: "retrieve" if s["index"] < len(s["allowed"]) else "publish"
    )
    graph.add_edge("publish", "approval")
    graph.add_edge("approval", "commit")
    graph.add_edge("commit", END)
    return graph.compile(checkpointer=saver)


def execute(store, provider, ident, progress=lambda *args: None, decision=None):
    with locked(store, ident), tracing_context(enabled=False):
        run = run_record(store, ident)
        payload = run["payload"]
        if payload.get("engine") != ENGINE:
            raise ValueError("旧版任务没有 LangGraph 检查点，请重新生成。")
        if run["status"] in ["approved", "rejected"]:
            raise ValueError("此候选稿已处理。")
        # Do not spend further model calls or authorize stale output.
        if store.paper()[1] != payload["base_revision"] and decision != "reject":
            raise ConflictError("生成后论文或材料已改变，请重新生成任务，原文未被覆盖。")
        path = store.root / "langgraph.sqlite"
        connection = sqlite3.connect(path, check_same_thread=False, timeout=30)
        try:
            connection.execute("PRAGMA journal_mode=WAL")
            saver = SqliteSaver(
                connection,
                serde=JsonPlusSerializer(allowed_msgpack_modules=[], pickle_fallback=False),
            )
            graph = build_graph(store, provider, saver, progress)
            config = {
                "configurable": {"thread_id": ident},
                "recursion_limit": 1000,
                "callbacks": [],
            }
            snapshot = graph.get_state(config)
            waiting = any(t.interrupts for t in snapshot.tasks)
            if not snapshot.values and (payload.get("candidates") or store.steps(ident)):
                if decision == "reject":
                    store.update_run(ident, "rejected", graph_stage="作者已放弃")
                    return {"run_id": ident, "status": "rejected"}
                raise ValueError("LangGraph 检查点文件缺失，请恢复完整 data 目录或重新生成任务。")
            if decision is not None:
                # If publish committed just before a crash, advance only the pure
                # publishing/approval nodes to recreate the durable interrupt.
                if (
                    run["status"] == "waiting_for_approval"
                    and not waiting
                    and snapshot.next
                    and set(snapshot.next) <= {"publish", "approval"}
                ):
                    graph.invoke(None, config, durability="sync")
                    snapshot = graph.get_state(config)
                    waiting = any(t.interrupts for t in snapshot.tasks)
                if run["status"] != "waiting_for_approval" or not waiting:
                    raise ValueError("任务尚未到达人工审批检查点。")
                value = Command(resume=decision)
            elif waiting:
                # Reconcile publish-before-interrupt crashes; never approve on resume.
                store.update_run(ident, "waiting_for_approval", graph_stage="等待作者确认")
                return {"run_id": ident, "status": "waiting_for_approval"}
            elif not snapshot.values:
                value = State(
                    run_id=ident,
                    paper=payload["paper_snapshot"],
                    instruction=payload["instruction"],
                    allowed=payload["allowed"],
                    index=0,
                    candidates=[],
                )
            else:
                value = None
            try:
                graph.invoke(value, config, durability="sync")
            except Exception as error:
                status = run_record(store, ident)["status"]
                if status in ["approved", "rejected"]:
                    return {"run_id": ident, "status": status}
                store.update_run(
                    ident, "failed", error=str(error), graph_stage="执行失败，可从检查点继续"
                )
                raise
            return {"run_id": ident, "status": run_record(store, ident)["status"]}
        finally:
            connection.close()


def revise(store, provider, instruction, allowed, progress=lambda *args: None):
    return execute(store, provider, create_run(store, instruction, allowed), progress)


def decide(store, ident, approve):
    run = run_record(store, ident)
    if run["payload"].get("engine") == ENGINE:
        # Approving/rejecting a checkpoint must never need an LLM call.
        return execute(store, None, ident, decision="approve" if approve else "reject")
    if approve:
        store.apply(ident)
    else:
        store.update_run(ident, "rejected")


def backup_state(store):
    """Consistent business/checkpoint snapshots while graph writers are excluded."""
    import tempfile
    from pathlib import Path

    with locked(store, "__workflow_catalog__"), ExitStack() as stack:
        for run in store.runs():
            if run["payload"].get("engine") == ENGINE:
                stack.enter_context(locked(store, run["id"]))
        result = {"papercoding.sqlite": store.backup_bytes()}
        path = store.root / "langgraph.sqlite"
        if path.exists():
            with tempfile.TemporaryDirectory() as folder:
                target = Path(folder) / "langgraph.sqlite"
                src = sqlite3.connect(path)
                dst = sqlite3.connect(target)
                try:
                    src.backup(dst)
                finally:
                    src.close()
                    dst.close()
                result["langgraph.sqlite"] = target.read_bytes()
        return result
