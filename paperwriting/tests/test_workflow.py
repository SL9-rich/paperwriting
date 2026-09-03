import subprocess
import sys
from pathlib import Path

import pytest

from papercoding.infrastructure.storage import Store, ConflictError
from papercoding.application.jobs import Jobs
from papercoding.application.workflow import create_run, execute, decide, revise, run_record, locked


class Fake:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.calls = []

    def json(self, system, user, max_tokens=4000):
        self.calls.append(system)
        value = next(self.responses)
        if isinstance(value, Exception):
            raise value
        return value, 100


def results(section="02"):
    return [
        {"section_id": section, "revised_text": "新正文"},
        {"verdict": "pass", "issues": []},
        {"action": "accept"},
    ]


def test_durable_human_approval_and_repeat_guard(tmp_path):
    store = Store(tmp_path)
    run = revise(store, Fake(results()), "修改", ["02"])["run_id"]
    assert run_record(store, run)["status"] == "waiting_for_approval"
    assert store.paper()[0]["sections"]["02"] == ""
    assert (tmp_path / "langgraph.sqlite").exists()
    # No Provider available after restart. Approval must not call any model.
    fresh = Store(tmp_path)
    with pytest.raises(ValueError, match="审批检查点"):
        fresh.apply(run)
    decide(fresh, run, True)
    assert fresh.paper()[0]["sections"]["02"] == "新正文"
    before = (fresh.paper(), fresh.versions(), fresh.runs())
    with pytest.raises(ValueError, match="已处理"):
        decide(fresh, run, True)
    assert (fresh.paper(), fresh.versions(), fresh.runs()) == before


def test_failure_resume_skips_successful_writer(tmp_path):
    store = Store(tmp_path)
    ident = create_run(store, "修改", ["02"])
    with pytest.raises(RuntimeError):
        execute(store, Fake([results()[0], RuntimeError("network down")]), ident)
    resumed = Fake(results()[1:])
    execute(Store(tmp_path), resumed, ident)
    assert len(resumed.calls) == 2 and not any("只修改指定" in x for x in resumed.calls)
    assert len([s for s in store.steps(ident) if s["role"] == "Writer"]) == 1
    assert run_record(store, ident)["payload"]["calls"] == 4  # failed logical attempt also reserved
    assert run_record(store, ident)["payload"]["tokens"] == 300


def test_real_process_exit_recovers_checkpoint(tmp_path):
    store = Store(tmp_path)
    ident = create_run(store, "修改", ["02"])
    code = """
import os,sys
from pathlib import Path
from papercoding.infrastructure.storage import Store
from papercoding.application.workflow import execute
class Crash:
 def json(self,system,user,max_tokens=4000):
  if '独立审查' in system: os._exit(17)
  return {'section_id':'02','revised_text':'进程中断前已生成'},100
execute(Store(Path(sys.argv[1])),Crash(),sys.argv[2])
"""
    process = subprocess.run(
        [sys.executable, "-c", code, str(tmp_path), ident],
        cwd=Path(__file__).resolve().parents[1],
        timeout=30,
    )
    assert process.returncode == 17
    restarted = Store(tmp_path)
    jobs = Jobs(restarted)
    jobs.pool.shutdown(wait=True)
    assert run_record(restarted, ident)["status"] == "interrupted"
    provider = Fake(results()[1:])
    execute(restarted, provider, ident)
    assert len(provider.calls) == 2
    decide(restarted, ident, True)
    assert restarted.paper()[0]["sections"]["02"] == "进程中断前已生成"
    assert len(restarted.versions()) == 1


def test_receipt_survives_gap_before_graph_checkpoint(tmp_path, monkeypatch):
    import papercoding.application.workflow as workflow

    store = Store(tmp_path)
    ident = create_run(store, "修改", ["02"])
    original = workflow.call_model

    def crash_after_receipt(*args, **kwargs):
        result = original(*args, **kwargs)
        if args[4] == "Writer":
            raise RuntimeError("after durable receipt, before node return")
        return result

    monkeypatch.setattr(workflow, "call_model", crash_after_receipt)
    with pytest.raises(RuntimeError):
        execute(store, Fake([results()[0]]), ident)
    monkeypatch.setattr(workflow, "call_model", original)
    provider = Fake(results()[1:])
    execute(store, provider, ident)
    assert len(provider.calls) == 2
    assert run_record(store, ident)["payload"]["calls"] == 3
    assert run_record(store, ident)["payload"]["tokens"] == 300


def test_stale_paper_cannot_approve_or_spend_more_calls(tmp_path):
    store = Store(tmp_path)
    ident = revise(store, Fake(results()), "修改", ["02"])["run_id"]
    paper, rev = store.paper()
    paper["sections"]["02"] = "作者手改"
    store.save(paper, rev)
    with pytest.raises(ConflictError):
        decide(store, ident, True)
    assert store.paper()[0]["sections"]["02"] == "作者手改"
    decide(store, ident, False)
    assert run_record(store, ident)["status"] == "rejected"
    assert store.versions() == []


def test_multisection_and_single_rework(tmp_path):
    store = Store(tmp_path)
    provider = Fake(
        [
            results()[0],
            {"verdict": "revise"},
            {"action": "revise"},
            {"section_id": "02", "revised_text": "返修稿"},
            {"verdict": "revise", "issues": ["作者仍需判断"]},
            *results("03"),
        ]
    )
    ident = revise(store, provider, "修改", ["02", "03"])["run_id"]
    assert len(provider.calls) == 8
    assert len(run_record(store, ident)["payload"]["candidates"]) == 2
    decide(store, ident, True)
    assert store.paper()[0]["sections"]["02"] == "返修稿"
    assert store.paper()[0]["sections"]["03"] == "新正文"


def test_budget_is_persisted_and_cannot_reset_on_resume(tmp_path):
    store = Store(tmp_path)
    ident = create_run(store, "修改", ["02"])
    store.update_run(ident, "interrupted", calls=24)
    provider = Fake([])
    with pytest.raises(ValueError, match="预算"):
        execute(store, provider, ident)
    with pytest.raises(ValueError, match="预算"):
        execute(Store(tmp_path), provider, ident)
    assert provider.calls == [] and run_record(store, ident)["payload"]["calls"] == 24


def test_parallel_invoke_cannot_enter_same_run(tmp_path):
    store = Store(tmp_path)
    ident = create_run(store, "修改", ["02"])
    with locked(store, ident):
        with pytest.raises(ValueError, match="正在执行"):
            execute(store, Fake([]), ident)
    assert run_record(store, ident)["payload"]["calls"] == 0


def test_changed_candidate_cannot_bypass_approval_snapshot(tmp_path):
    store = Store(tmp_path)
    ident = revise(store, Fake(results()), "修改", ["02"])["run_id"]
    store.update_run(
        ident,
        "waiting_for_approval",
        candidates=[{"section_id": "02", "revised_text": "篡改后的候选"}],
    )
    with pytest.raises(ValueError, match="检查点不一致"):
        decide(store, ident, True)
    assert store.paper()[0]["sections"]["02"] == ""
    assert store.versions() == []


def test_publish_crash_can_recreate_approval_interrupt(tmp_path, monkeypatch):
    store = Store(tmp_path)
    ident = create_run(store, "修改", ["02"])
    original = store.update_run

    def stop_after_publish(ident, status, **changes):
        original(ident, status, **changes)
        if status == "waiting_for_approval":
            raise SystemExit("crash before checkpoint")

    monkeypatch.setattr(store, "update_run", stop_after_publish)
    with pytest.raises(SystemExit):
        execute(store, Fake(results()), ident)
    fresh = Store(tmp_path)
    assert run_record(fresh, ident)["status"] == "waiting_for_approval"
    decide(fresh, ident, True)
    assert fresh.paper()[0]["sections"]["02"] == "新正文"
    assert run_record(fresh, ident)["payload"]["calls"] == 3


def test_paper_commit_is_authoritative_if_final_checkpoint_fails(tmp_path, monkeypatch):
    store = Store(tmp_path)
    ident = revise(store, Fake(results()), "修改", ["02"])["run_id"]
    original = store.apply

    def committed_then_error(*args, **kwargs):
        original(*args, **kwargs)
        raise RuntimeError("crash after paper commit")

    monkeypatch.setattr(store, "apply", committed_then_error)
    assert decide(store, ident, True)["status"] == "approved"
    assert len(store.versions()) == 1
    with pytest.raises(ValueError, match="已处理"):
        decide(Store(tmp_path), ident, True)
    assert len(store.versions()) == 1


def test_missing_checkpoint_never_silently_regenerates(tmp_path):
    store = Store(tmp_path)
    ident = revise(store, Fake(results()), "修改", ["02"])["run_id"]
    (tmp_path / "langgraph.sqlite").unlink()
    with pytest.raises(ValueError, match="检查点文件缺失"):
        decide(store, ident, True)
    assert store.paper()[0]["sections"]["02"] == ""
    decide(store, ident, False)
    assert run_record(store, ident)["status"] == "rejected"


def test_backup_restore_preserves_human_approval(tmp_path):
    from papercoding.application.workflow import backup_state

    store = Store(tmp_path / "original")
    ident = revise(store, Fake(results()), "修改", ["02"])["run_id"]
    snapshot = backup_state(store)
    assert set(snapshot) == {"papercoding.sqlite", "langgraph.sqlite"}
    target = tmp_path / "restored"
    target.mkdir()
    for name, data in snapshot.items():
        (target / name).write_bytes(data)
    restored = Store(target)
    decide(restored, ident, True)
    assert restored.paper()[0]["sections"]["02"] == "新正文"
    assert store.paper()[0]["sections"]["02"] == ""
    with locked(store, ident):
        with pytest.raises(ValueError, match="正在执行"):
            backup_state(store)
