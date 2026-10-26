"""运行追踪接口：读取 trace 与评测结果。"""

import json
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import runs

VALID_RUN = "0123456789abcdef0123456789abcdef"


@pytest.fixture()
def trace_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(runs, "TRACE_DIR", tmp_path / "traces")
    monkeypatch.setattr(runs, "RESULT_DIR", tmp_path / "results")
    (tmp_path / "traces").mkdir(exist_ok=True)
    (tmp_path / "results").mkdir(exist_ok=True)
    return tmp_path


@pytest.fixture()
def client():
    # 只挂载被测 router：主应用的 lifespan 会连接 Milvus，与本接口无关。
    test_app = FastAPI()
    test_app.include_router(runs.router, prefix="/api")
    with TestClient(test_app) as test_client:
        yield test_client


def _write_trace(root: Path, run_id: str, events: list[dict]) -> None:
    path = root / "traces" / f"{run_id}.jsonl"
    path.write_text(
        "\n".join(json.dumps(event, ensure_ascii=False) for event in events) + "\n",
        encoding="utf-8",
    )


def test_list_runs_summarizes_trace(trace_dir, client):
    _write_trace(
        trace_dir,
        VALID_RUN,
        [
            {"event": "run_start", "ts": "2026-10-22T01:00:00", "session_id": "s1"},
            {"event": "planner_route", "backend": "local", "severity": "P2", "category": "cpu"},
            {"event": "plan", "steps": [{"id": "s1"}, {"id": "s2"}]},
            {"event": "tool_step", "step": "a", "input_tokens": 10, "output_tokens": 5},
            {"event": "tool_step", "step": "b", "input_tokens": 3, "output_tokens": 2},
            {"event": "run_complete", "duration_ms": 1234},
        ],
    )

    response = client.get("/api/runs")

    assert response.status_code == 200
    run = response.json()["runs"][0]
    assert run["run_id"] == VALID_RUN
    assert run["session_id"] == "s1"
    assert run["steps"] == 2
    assert run["plan_steps"] == 2
    assert run["backend"] == "local"
    assert run["status"] == "ok"
    assert run["input_tokens"] == 13
    assert run["output_tokens"] == 7


def test_list_runs_counts_agent_plan_as_history_plan(trace_dir, client):
    _write_trace(
        trace_dir,
        VALID_RUN,
        [
            {"event": "run_start", "ts": "2026-10-22T01:00:00"},
            {"event": "agent_plan", "assignments": [{"agent": "metrics"}, {"agent": "logs"}]},
            {"event": "run_complete", "duration_ms": 10},
        ],
    )

    run = client.get("/api/runs").json()["runs"][0]

    assert run["plan_steps"] == 2


def test_get_run_returns_events(trace_dir, client):
    _write_trace(trace_dir, VALID_RUN, [{"event": "run_start", "ts": "t", "seq": 1}])

    response = client.get(f"/api/runs/{VALID_RUN}")

    assert response.status_code == 200
    assert response.json()["events"][0]["event"] == "run_start"


def test_get_run_supports_after_seq_and_limit(trace_dir, client):
    _write_trace(trace_dir, VALID_RUN, [
        {"event": "run_start", "seq": 1},
        {"event": "status", "seq": 2},
        {"event": "run_complete", "seq": 3},
    ])

    resumed = client.get(f"/api/runs/{VALID_RUN}", params={"after_seq": 2}).json()
    limited = client.get(f"/api/runs/{VALID_RUN}", params={"limit": 2}).json()

    assert [e["seq"] for e in resumed["events"]] == [3]
    assert [e["seq"] for e in limited["events"]] == [1, 2]
    assert limited["truncated"] is True
    assert limited["next_after_seq"] == 2


def test_get_unknown_run_returns_404(trace_dir, client):
    assert client.get(f"/api/runs/{VALID_RUN}").status_code == 404


def test_rejected_id_is_indistinguishable_from_missing_run(trace_dir, client):
    """被拒绝的 id 与“不存在”必须给出完全相同的响应，避免成为探测信号。"""
    missing = client.get(f"/api/runs/{VALID_RUN}")
    rejected = client.get("/api/runs/not-a-run-id")

    assert missing.status_code == rejected.status_code == 404
    assert missing.json() == rejected.json() == {"detail": "run not found"}


def test_list_runs_skips_entry_resolving_outside_without_500(trace_dir, client, monkeypatch):
    """traces 内的条目解析后越界时应被跳过（而非 500），合法条目照常返回。"""
    _write_trace(trace_dir, VALID_RUN, [{"event": "run_start", "seq": 1}])
    outside_id = "f" * 32
    escaping = trace_dir / "traces" / f"{outside_id}.jsonl"
    escaping.write_text(json.dumps({"event": "secret"}) + "\n", encoding="utf-8")
    escaped_target = trace_dir / "secret.jsonl"
    escaped_target.write_text(json.dumps({"event": "secret"}) + "\n", encoding="utf-8")
    real_resolve = Path.resolve

    def fake_resolve(self, *args, **kwargs):
        if Path(self) == escaping:
            return escaped_target
        return real_resolve(self, *args, **kwargs)

    monkeypatch.setattr(Path, "resolve", fake_resolve)

    response = client.get("/api/runs")

    assert response.status_code == 200
    assert [run["run_id"] for run in response.json()["runs"]] == [VALID_RUN]


def test_rejects_out_of_range_pagination(trace_dir, client):
    assert client.get("/api/runs", params={"limit": 0}).status_code == 422
    assert client.get(f"/api/runs/{VALID_RUN}", params={"limit": 99_999}).status_code == 422


def test_pagination_reaches_terminal_event_with_mixed_seq_trace(trace_dir, client):
    """A09：混有 seq / 无 seq 的 trace，小分页也必须能读到终态。"""
    events = [
        {"event": "run_start"},
        {"event": "status", "seq": 1},
        {"event": "evidence", "data": 1},
        {"event": "tool_step"},
        {"event": "status", "seq": 2},
        {"event": "run_complete"},
    ]
    _write_trace(trace_dir, VALID_RUN, events)

    first = client.get(
        f"/api/runs/{VALID_RUN}", params={"after_seq": 1, "limit": 2}
    ).json()

    # 要么本页就没截断（一次给完），要么必须给出可用的续读游标。
    assert first["truncated"] is False or first["next_after_seq"] is not None
    if first["truncated"]:
        second = client.get(
            f"/api/runs/{VALID_RUN}",
            params={"after_seq": first["next_after_seq"], "limit": 2},
        ).json()
        seen = first["events"] + second["events"]
    else:
        seen = first["events"]

    names = [e["event"] for e in seen]
    assert names[-1] == "run_complete"
    # 无遗漏、无重复：after_seq=1 表示从 status seq=1 之后开始，整体保持文件顺序。
    assert seen == events[2:]


def test_small_page_walk_covers_every_event_exactly_once(trace_dir, client):
    """A10 通用回归：全 seq 的 trace，limit=3 逐页走完不漏不重。"""
    events = [{"event": "step", "seq": seq} for seq in range(1, 10)]
    events.append({"event": "run_complete", "seq": 10})
    _write_trace(trace_dir, VALID_RUN, events)

    collected: list[dict] = []
    after_seq = 0
    pages = 0
    while True:
        page = client.get(
            f"/api/runs/{VALID_RUN}", params={"after_seq": after_seq, "limit": 3}
        ).json()
        collected.extend(page["events"])
        pages += 1
        assert pages < 20, "分页没有收敛"
        if not page["truncated"]:
            assert page["next_after_seq"] is None
            break
        assert page["next_after_seq"] is not None
        after_seq = page["next_after_seq"]

    assert collected == events
    assert pages == 4
    assert collected[-1]["event"] == "run_complete"


def test_truncated_page_always_has_continuation_cursor(trace_dir, client):
    """边界安全：truncated 为真时 next_after_seq 必须非空，且能走到终态。"""
    events = [
        {"event": "run_start", "seq": 1},
        {"event": "status", "seq": 2},
        {"event": "note"},
        {"event": "run_complete", "seq": 3},
    ]
    _write_trace(trace_dir, VALID_RUN, events)

    for limit in (1, 2, 3):
        collected: list[dict] = []
        after_seq = 0
        while True:
            page = client.get(
                f"/api/runs/{VALID_RUN}",
                params={"after_seq": after_seq, "limit": limit},
            ).json()
            if page["truncated"]:
                assert page["next_after_seq"] is not None
                after_seq = page["next_after_seq"]
            collected.extend(page["events"])
            assert len(collected) <= len(events), "分页重复读取"
            if not page["truncated"]:
                break
        assert collected == events, f"limit={limit} 读取结果与原始 trace 不一致"


def test_eval_results_accept_manifest_rows_format(trace_dir, client):
    """A10：新格式 {"manifest", "rows"} 必须被接受并带出 manifest。"""
    (trace_dir / "results" / "x.json").write_text(
        json.dumps({
            "manifest": {"execution_mode": "local"},
            "rows": [{"tool_f1": 1.0, "task_success": True,
                      "input_tokens": 3, "output_tokens": 2}],
        }),
        encoding="utf-8",
    )

    response = client.get("/api/eval/results")

    assert response.status_code == 200
    results = response.json()["results"]
    assert len(results) == 1
    result = results[0]
    assert result["manifest"]["execution_mode"] == "local"
    assert len(result["rows"]) == 1
    assert result["mean_tool_f1"] == 1.0
    assert result["success_rate"] == 1.0
    assert result["total_tokens"] == 5


def test_eval_results_aggregate(trace_dir, client):
    rows = [
        {"id": "a", "tool_f1": 0.5, "task_success": True, "input_tokens": 10, "output_tokens": 5},
        {"id": "b", "tool_f1": 1.0, "task_success": False, "input_tokens": 2, "output_tokens": 1},
    ]
    (trace_dir / "results" / "2026-10-22.json").write_text(
        json.dumps(rows), encoding="utf-8"
    )

    response = client.get("/api/eval/results")

    result = response.json()["results"][0]
    assert result["mean_tool_f1"] == 0.75
    assert result["success_rate"] == 0.5
    assert result["total_tokens"] == 18
    # 旧格式没有 manifest 段，补 null 而不是猜一个。
    assert result["manifest"] is None
