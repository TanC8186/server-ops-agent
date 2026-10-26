"""P0-03 回归：run_id 不得越界读取 traces 目录外的文件（Windows 路径语义）。"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from urllib.parse import quote

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import runs

VALID_ID = "0123456789abcdef0123456789abcdef"


@pytest.fixture()
def trace_root(tmp_path, monkeypatch):
    root = tmp_path / "work"
    traces = root / "logs" / "traces"
    traces.mkdir(parents=True)
    # 两个越界目标：traces 的兄弟文件（`..\x` 可直接命中）与工作区外上级文件。
    outside = root / "outside.jsonl"
    outside.write_text(
        json.dumps({"event": "run_start", "run_id": "outside", "secret": "OUTSIDE-DATA"}) + "\n",
        encoding="utf-8",
    )
    sibling = root / "logs" / "sibling.jsonl"
    sibling.write_text(json.dumps({"event": "secret", "value": "SIBLING"}) + "\n", encoding="utf-8")
    monkeypatch.setattr(runs, "TRACE_DIR", traces)
    monkeypatch.setattr(runs, "RESULT_DIR", root / "eval" / "results")
    return {"root": root, "traces": traces, "outside": outside, "sibling": sibling}


@pytest.fixture()
def client():
    # 只挂载被测 router：主应用 lifespan 会连 Milvus，与被测路径逻辑无关。
    test_app = FastAPI()
    test_app.include_router(runs.router, prefix="/api")
    with TestClient(test_app) as test_client:
        yield test_client


@pytest.fixture()
def read_spy(monkeypatch):
    """记录 `_read_jsonl` 实际收到的解析后路径，用于证明“没有被读取”。"""
    seen: list[Path] = []
    original = runs._read_jsonl

    def _spy(path):
        seen.append(Path(path).resolve())
        return original(path)

    monkeypatch.setattr(runs, "_read_jsonl", _spy)
    return seen


def _write_valid_trace(traces: Path, run_id: str = VALID_ID) -> None:
    (traces / f"{run_id}.jsonl").write_text(
        json.dumps({"event": "run_start", "run_id": run_id, "session_id": "s"}) + "\n",
        encoding="utf-8",
    )


def test_valid_hex_run_id_is_readable(trace_root, client):
    _write_valid_trace(trace_root["traces"])

    response = client.get(f"/api/runs/{VALID_ID}")

    assert response.status_code == 200
    assert response.json()["events"][0]["run_id"] == VALID_ID


@pytest.mark.parametrize(
    "run_id",
    [
        "..\\outside",                     # 编码反斜杠（原始缺陷）
        "..%5Coutside",                    # 已编码反斜杠
        "..%5coutside",
        "..%255Coutside",                  # double-encoded backslash
        "%2e%2e%5Coutside",
        "..%2Foutside",                    # 编码正斜杠
        "../outside",
        "logs%5Csibling",
        "C:%5CWindows%5Cwin.ini",          # 盘符 + 绝对路径
        "C:/Windows/win.ini",
        "%5C%5Cserver%5Cshare%5Cx",        # UNC
        "\\\\?\\C:\\Windows\\win.ini",      # 扩展长度前缀
        "%00",
        VALID_ID + ".%5C..%5Coutside",
        "OUTSIDE",
        "outside",
    ],
)
def test_traversal_forms_never_reach_outside_files(trace_root, client, run_id):
    _write_valid_trace(trace_root["traces"])

    response = client.get(f"/api/runs/{run_id}")

    assert response.status_code in {400, 404, 422}, run_id
    assert "OUTSIDE-DATA" not in response.text
    assert "SIBLING" not in response.text
    assert str(trace_root["root"]) not in response.text


def test_dot_segments_are_not_directory_listings(trace_root, client):
    """`..` 解析到 traces 的上级目录；不能因为上级文件存在就返回内容。"""
    _write_valid_trace(trace_root["traces"])

    for run_id in ("%2e", "%2e%2e", ".jsonl", "...", "%2e%2e%2foutside"):
        response = client.get(f"/api/runs/{run_id}")
        assert response.status_code in {400, 404, 422}, run_id
        assert "OUTSIDE-DATA" not in response.text


def test_absolute_path_is_rejected_even_if_it_exists(trace_root, client):
    target = trace_root["outside"].as_posix()

    for candidate in (quote(target, safe=""), target.replace("/", "%5C")):
        response = client.get(f"/api/runs/{candidate}")
        assert response.status_code in {400, 404, 422}
        assert "OUTSIDE-DATA" not in response.text


def test_symlink_or_reparse_escape_is_rejected(trace_root, client):
    """即使 traces 内出现指向外部的链接，解析后的真实路径也必须仍在目录内。"""
    link = trace_root["traces"] / f"{VALID_ID}.jsonl"
    try:
        link.symlink_to(trace_root["outside"])
    except (OSError, NotImplementedError):
        pytest.skip("当前环境不允许创建符号链接")

    response = client.get(f"/api/runs/{VALID_ID}")

    assert response.status_code in {400, 404}
    assert "OUTSIDE-DATA" not in response.text


def test_non_hex_legacy_names_are_not_served(trace_root, client):
    """不符合服务端 ID 契约的历史文件名不能被读取。"""
    (trace_root["traces"] / "run-1.jsonl").write_text(
        json.dumps({"event": "run_start"}) + "\n", encoding="utf-8")

    assert client.get("/api/runs/run-1").status_code in {400, 404}


def test_list_runs_ignores_files_outside_id_contract(trace_root, client):
    _write_valid_trace(trace_root["traces"])
    (trace_root["traces"] / "notes.txt").write_text("not a trace", encoding="utf-8")

    payload = client.get("/api/runs").json()

    assert [run["run_id"] for run in payload["runs"]] == [VALID_ID]


ESCAPING_IDS = [
    "..%5Coutside",                    # 反斜杠 + 上一级
    "..%5C..%5Coutside",               # 双层反斜杠，往上两级
    "..\\..\\outside",                 # 未编码的双层反斜杠
    "..%5Clogs%5Csibling",             # 先上一个再进 logs（绕路）
    "logs%5Csibling",                  # logs 下的兄弟文件
    "C:%5CWindows%5Cwin.ini",          # 盘符绝对路径
    "%5C%5Cserver%5Cshare%5Cx",        # UNC
    "%5C%5C%3F%5CC:%5CWindows%5Cwin.ini",  # 扩展长度前缀（编码）
    "\\\\?\\C:\\Windows\\win.ini",     # 扩展长度前缀（原文）
    VALID_ID.upper(),                  # 大写十六进制不属于服务端契约
    "run-1",                           # 历史非十六进制名
    "2026-10-22",
]


@pytest.mark.parametrize("run_id", ESCAPING_IDS)
def test_rejected_ids_never_read_any_file(trace_root, client, read_spy, run_id):
    """不只断言响应里没有敏感串，而是断言 `_read_jsonl` 根本没被调用。"""
    _write_valid_trace(trace_root["traces"])
    base = trace_root["traces"].resolve()

    response = client.get(f"/api/runs/{run_id}")

    assert response.status_code == 404, run_id
    assert response.json() == {"detail": "run not found"}, run_id
    assert read_spy == [], f"{run_id} 触发了读取: {read_spy}"
    assert all(path.is_relative_to(base) for path in read_spy), read_spy


def test_accepted_id_reads_only_inside_traces(trace_root, client, read_spy):
    """对照组：合法 ID 只读 traces 内的文件，证明 spy 真的能记录读取。"""
    _write_valid_trace(trace_root["traces"])
    base = trace_root["traces"].resolve()

    assert client.get(f"/api/runs/{VALID_ID}").status_code == 200

    assert len(read_spy) == 1
    assert read_spy[0] == (base / f"{VALID_ID}.jsonl").resolve()
    assert read_spy[0].is_relative_to(base)


@pytest.mark.parametrize("run_id", ESCAPING_IDS)
def test_list_runs_never_reads_escaping_paths(trace_root, client, read_spy, run_id):
    """列表接口同样不能因为文件名看起来合法就去读取越界内容。"""
    _write_valid_trace(trace_root["traces"])
    base = trace_root["traces"].resolve()

    client.get("/api/runs")

    assert all(path.is_relative_to(base) for path in read_spy), (run_id, read_spy)


def _make_junction(link: Path, target: Path) -> None:
    """在 Windows 上创建目录 junction；无权限时跳过。"""
    if os.name != "nt":
        pytest.skip("仅在 Windows 上验证 junction 语义")
    result = subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(link), str(target)],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        if "1314" in (result.stdout + result.stderr):
            pytest.skip("当前权限不允许创建 junction（WinError 1314）")
        pytest.skip(f"当前环境无法创建 junction: {result.stdout or result.stderr}")


def test_junction_directory_named_as_run_id_is_refused(trace_root, client, read_spy):
    """traces 内出现指向外部的 junction 时，列表与详情都必须拒绝。"""
    outside_dir = trace_root["root"] / "junction-target"
    outside_dir.mkdir()
    (outside_dir / f"{VALID_ID}.jsonl").write_text(
        json.dumps({"event": "run_start", "secret": "JUNCTION-DATA"}) + "\n",
        encoding="utf-8",
    )
    # traces 下的 `<id>.jsonl` 是指向外部的目录 junction。
    link = trace_root["traces"] / f"{VALID_ID}.jsonl"
    _make_junction(link, outside_dir)

    detail = client.get(f"/api/runs/{VALID_ID}")
    listing = client.get("/api/runs")
    base = trace_root["traces"].resolve()

    assert detail.status_code == 404
    assert "JUNCTION-DATA" not in detail.text
    assert [run["run_id"] for run in listing.json()["runs"]] == []
    assert all(path.is_relative_to(base) for path in read_spy), read_spy


def test_list_runs_refuses_simulated_reparse_escape(trace_root, client, read_spy, monkeypatch):
    """模拟文件级重解析点：名字在 traces 内、解析后落在目录外。

    Windows 未授予建符号链接权限时 `symlink_to` 会失败，这里直接替换
    `Path.resolve` 的结果来覆盖同一段包含性校验，不依赖 OS 特权。
    """
    traces = trace_root["traces"]
    _write_valid_trace(traces)
    outside = trace_root["outside"]
    link = traces / f"{VALID_ID}.jsonl"
    real_resolve = Path.resolve

    def fake_resolve(self, *args, **kwargs):
        if Path(self) == link:
            return outside
        return real_resolve(self, *args, **kwargs)

    monkeypatch.setattr(Path, "resolve", fake_resolve)
    base = real_resolve(traces)

    detail = client.get(f"/api/runs/{VALID_ID}")
    listing = client.get("/api/runs")

    assert detail.status_code == 404
    assert [run["run_id"] for run in listing.json()["runs"]] == []
    assert all(path.is_relative_to(base) for path in read_spy), read_spy


def test_run_eval_read_trace_rejects_traversal(tmp_path, monkeypatch):
    """scripts/run_eval.py 的 trace 读取必须复用同一套校验。"""
    from scripts import run_eval

    traces = tmp_path / "logs" / "traces"
    traces.mkdir(parents=True)
    secret = tmp_path / "outside.jsonl"
    secret.write_text(json.dumps({"event": "secret"}) + "\n", encoding="utf-8")
    (traces / f"{VALID_ID}.jsonl").write_text(
        json.dumps({"event": "run_start", "seq": 1}) + "\n", encoding="utf-8"
    )
    monkeypatch.setattr(run_eval, "ROOT", tmp_path)
    monkeypatch.setattr(run_eval, "TRACE_DIR", traces)

    def _no_secret(rows):
        assert all(row.get("event") != "secret" for row in rows), rows
        return rows

    assert _no_secret(run_eval._read_trace("..\\outside")) == []
    assert _no_secret(run_eval._read_trace("..%5Coutside")) == []
    assert _no_secret(run_eval._read_trace("..\\..\\outside")) == []
    assert _no_secret(run_eval._read_trace(secret.as_posix())) == []
    assert _no_secret(run_eval._read_trace("")) == []
    assert [e["event"] for e in run_eval._read_trace(VALID_ID)] == ["run_start"]
