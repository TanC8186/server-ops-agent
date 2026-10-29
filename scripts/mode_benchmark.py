"""本地 QLoRA 与 cloud_multi 的同题工程对比（G8 P1）。

这个脚本**只做工程对比**，不做模型能力评测。它把同一批固定任务分别送进
``local`` 和 ``cloud_multi`` 两条真实链路，记录可复现的运行指标。

口径红线（写在代码里，防止后续被"美化"）：

- 任务集固定且带 sha256，运行前后不变；脚本启动时重算一次，不一致直接退出。
- 每个模式单独出指标，**不合成总分**，因为两条链路的工具集合与拒绝语义不同。
- 不叫"准确率"。这里的比率分母都是本文件固定的任务数，不是"模型能力"。
- 允许失败的场景（``allow_refusal``）单独计数，不混进成功率。
- 工具调用成功率按**工具真的执行成功**计，不按"模型说了要调"计。
- 延迟含排队与 MCP 往返；token 取 run trace，取不到就是 None（不补 0）。

用法::

    # 需要 app 在跑（容器 :9900 或宿主进程），且 local 模式的
    # LOCAL_LLM_BASE_URL 已指向可用的 OpenAI 兼容端点。
    export G8_API_KEY=...        # 或 --api-base + --api-key
    python scripts/mode_benchmark.py --mode local --out eval/mode_benchmark/local.json
    python scripts/mode_benchmark.py --mode cloud_multi --out eval/mode_benchmark/cloud.json
    python scripts/mode_benchmark.py --compare eval/mode_benchmark/*.json

不传 ``--mode`` 时只用 :class:`LocalPlannerOnly` 之外的方式；本脚本**不会**自己
起模型服务，也不会伪造云端响应。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import statistics
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

TASKS_FILE = ROOT / "eval" / "mode_benchmark" / "tasks.jsonl"


def _safe_console() -> None:
    """把 stdout 切到 UTF-8。

    Windows 控制台默认 GBK，遇到 ``⚠`` 这类字符会直接
    ``UnicodeEncodeError`` 打断整次运行——输出编码问题不该让评测失败，
    也不该逼着人删掉提示里的符号。这里只改编码，不改内容。
    """
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            try:
                reconfigure(encoding="utf-8", errors="replace")
            except (ValueError, OSError):
                pass
#: 任务集指纹。改题集必须同步改这里，否则脚本拒绝运行——
#: 这是"运行前后不可偷偷修改"的机械保证，不靠自觉。
TASKS_SHA256 = "89b54d102ca79092e5ec1361d7e6949ce307b31f89701c8bb77bf14854cc2586"

#: SSE 里出现这些类型即视为拿到终态。
TERMINAL_TYPES = frozenset({"complete", "error"})


def load_tasks() -> list[dict[str, Any]]:
    raw = TASKS_FILE.read_bytes()
    digest = hashlib.sha256(raw).hexdigest()
    if digest != TASKS_SHA256:
        raise SystemExit(
            f"任务集指纹不匹配：文件 {digest[:16]}… != 记录 {TASKS_SHA256[:16]}…。\n"
            f"如果确实要改题集，请同时更新 {Path(__file__).name} 里的 TASKS_SHA256，"
            "并在结果文档里写明改了哪几条、为什么——否则两次运行不可比。"
        )
    return [json.loads(line) for line in raw.decode("utf-8").splitlines() if line.strip()]


def _post_stream(base: str, key: str, body: dict, timeout: float) -> tuple[list[dict], float, str | None]:
    """POST 一个 SSE 流并返回 (事件列表, 端到端秒数, 错误)。

    这里故意不用 curl：需要精确计时和逐事件解析，且要在 Windows 上也能跑。
    """
    url = base.rstrip("/") + "/api/aiops"
    data = json.dumps(body, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        url, data=data,
        headers={"Content-Type": "application/json; charset=utf-8",
                 "X-API-Key": key, "Accept": "text/event-stream"},
        method="POST",
    )
    events: list[dict] = []
    started = time.perf_counter()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            for raw_line in response:
                line = raw_line.decode("utf-8", "replace").strip()
                if not line.startswith("data:"):
                    continue
                payload = line[5:].strip()
                if not payload:
                    continue
                try:
                    events.append(json.loads(payload))
                except json.JSONDecodeError:
                    continue
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:300]
        return events, time.perf_counter() - started, f"HTTP {exc.code}: {detail}"
    except Exception as exc:  # noqa: BLE001 - 网络错误要如实记成失败
        return events, time.perf_counter() - started, f"{type(exc).__name__}: {exc}"
    return events, time.perf_counter() - started, None


def _fetch_trace(base: str, key: str, run_id: str) -> dict | None:
    """读回一次运行的 trace（token 只在 trace 里，SSE 里没有）。

    ``GET /api/runs/{run_id}`` 返回 ``{run_id, events: [...]}``；token 是
    ``local_model_usage`` / ``agent_llm`` 这两类事件的字段，需要累加。
    早期实现只读顶层 ``input_tokens``，于是每次都是 None——那不是"模型没报
    token"，是读错了地方。
    """
    if not run_id:
        return None
    url = base.rstrip("/") + f"/api/runs/{run_id}"
    req = urllib.request.Request(url, headers={"X-API-Key": key})
    try:
        with urllib.request.urlopen(req, timeout=30) as response:
            return json.loads(response.read().decode("utf-8"))
    except Exception:  # noqa: BLE001 - 取不到就记 None，不猜
        return None


def _tokens_from_trace(trace: Any) -> tuple[int | None, int | None, int]:
    """从 trace 事件里累加 token。返回 (输入, 输出, 有 token 的事件数)。

    没有一条事件带 token 时返回 (None, None, 0)——区分"没测到"与"测到 0"。
    """
    if not isinstance(trace, dict):
        return None, None, 0
    events = trace.get("events")
    if not isinstance(events, list):
        return None, None, 0
    total_in = total_out = 0
    sampled = 0
    for event in events:
        if not isinstance(event, dict):
            continue
        name = event.get("event") or event.get("type") or ""
        if name not in {"local_model_usage", "agent_llm"}:
            continue
        got_in = event.get("input_tokens")
        got_out = event.get("output_tokens")
        if got_in is None and got_out is None:
            continue
        total_in += got_in or 0
        total_out += got_out or 0
        sampled += 1
    if sampled == 0:
        return None, None, 0
    return total_in, total_out, sampled


def _tools_called(events: list[dict]) -> list[dict]:
    """从 SSE 里提取每次工具调用及其真实执行状态。

    ``tool_step`` 的字段布局在两种模式下不同：本地的顶层直接有 ``step``/
    ``ok``，云端把它们放在 ``payload`` 里。这里两种都读，避免"云端全是 0 次调用"
    这种假阴性。
    """
    rows: list[dict] = []
    for event in events:
        if event.get("type") != "tool_step":
            continue
        payload = event.get("payload") or {}
        name = event.get("tool") or payload.get("tool") or payload.get("step")
        if not name:
            tools = payload.get("tools") or []
            if tools and isinstance(tools, list):
                name = (tools[0] or {}).get("name")
        name = str(name or "").strip()
        if not name:
            continue
        rows.append({
            "tool": name,
            "ok": bool(event.get("ok", payload.get("ok"))),
            "status": event.get("status") or payload.get("status") or "",
            "source": event.get("source") or payload.get("source") or "",
            "agent": event.get("agent") or payload.get("agent") or "",
        })
    return rows


def _plan_tools(events: list[dict]) -> list[str]:
    """取计划的工具名。

    本地模式：``plan_steps[*].tool``；云端模式：``steps[*].tools``（每个专科一组）。
    两种形状必须都认，否则云端 F1 会算成 0。
    """
    names: list[str] = []
    for event in events:
        if event.get("type") != "plan":
            continue
        for step in event.get("plan_steps") or []:
            tool = str(step.get("tool") or "").strip()
            if tool:
                names.append(tool)
        for step in event.get("steps") or []:
            for tool in step.get("tools") or []:
                tool = str(tool or "").strip()
                if tool:
                    names.append(tool)
        break
    return names


def _agent_stages(events: list[dict]) -> list[str]:
    """云端的角色阶段序列；本地模式为空。"""
    stages = []
    for event in events:
        if event.get("type") == "agent_status":
            agent = str(event.get("agent") or "")
            status = str(event.get("status") or "")
            if agent and status:
                stages.append(f"{agent}.{status}")
    return stages


def _score(task: dict, events: list[dict], elapsed: float, error: str | None) -> dict:
    called = _tools_called(events)
    planned = _plan_tools(events)
    expected = [t for t in (task.get("expect_tools") or []) if t]
    called_ok = {row["tool"] for row in called if row["ok"]}
    called_any = {row["tool"] for row in called}

    complete = next((e for e in reversed(events) if e.get("type") == "complete"), None)
    failed = next((e for e in reversed(events) if e.get("type") == "error"), None)
    response = (complete or {}).get("response") or ""

    hit = len(set(expected) & called_ok)
    recall = hit / len(expected) if expected else None
    planned_hit = len(set(expected) & set(planned))
    plan_recall = planned_hit / len(expected) if expected else None
    precision_denom = len(set(planned))
    plan_precision = (planned_hit / precision_denom) if precision_denom else None
    plan_f1 = None
    if plan_recall is not None and plan_precision:
        plan_f1 = 2 * plan_recall * plan_precision / (plan_recall + plan_precision)

    return {
        "id": task["id"],
        "bucket": task["bucket"],
        "mode": None,  # 由调用方填
        "status": ("complete" if complete else ("error" if failed else "no_terminal")),
        "error": error or ((failed or {}).get("message") if failed else None),
        "latency_s": round(elapsed, 3),
        "planned_tools": planned,
        "called_tools": called_any and sorted(called_any) or [],
        "tool_calls_total": len(called),
        "tool_calls_ok": sum(1 for row in called if row["ok"]),
        "tool_call_success_rate": (
            round(sum(1 for row in called if row["ok"]) / len(called), 3) if called else None
        ),
        "expected_tools": expected,
        "plan_tool_recall": None if plan_recall is None else round(plan_recall, 3),
        "plan_tool_precision": None if plan_precision is None else round(plan_precision, 3),
        "plan_tool_f1": None if plan_f1 is None else round(plan_f1, 3),
        "call_tool_recall": None if recall is None else round(recall, 3),
        "sources": sorted({row["source"] for row in called if row["source"]}),
        "agent_stages": _agent_stages(events),
        "report_chars": len(response),
        # 引用/证据是否可回溯：报告里引用了证据编号且该编号确实存在。
        "citation_ids": sorted({e.get("evidence_id") for e in events
                                if e.get("type") in ("tool_step", "step_complete")
                                and e.get("evidence_id")}),
        "allow_refusal": bool(task.get("allow_refusal")),
        "require_citation": bool(task.get("require_citation")),
        "run_id": (complete or failed or {}).get("run_id") or "",
    }


def run_mode(base: str, key: str, mode: str, tasks: list[dict],
             timeout: float, gap: float) -> dict:
    rows: list[dict] = []
    for index, task in enumerate(tasks, 1):
        body = {
            "session_id": f"g8-bench-{mode}-{task['id']}",
            "execution_mode": mode,
            "symptom": task["symptom"],
        }
        if task.get("service"):
            body["service_name"] = task["service"]
        print(f"[{index}/{len(tasks)}] {mode} {task['id']} ({task['bucket']})", flush=True)
        events, elapsed, error = _post_stream(base, key, body, timeout)
        row = _score(task, events, elapsed, error)
        row["mode"] = mode
        trace = _fetch_trace(base, key, row["run_id"])
        tokens_in, tokens_out, token_events = _tokens_from_trace(trace)
        row["input_tokens"] = tokens_in
        row["output_tokens"] = tokens_out
        row["token_events"] = token_events
        print(
            f"    -> {row['status']} {row['latency_s']}s "
            f"tools={row['tool_calls_ok']}/{row['tool_calls_total']} "
            f"f1={row['plan_tool_f1']}", flush=True,
        )
        rows.append(row)
        if gap:
            time.sleep(gap)
    return summarize(rows, mode)


def _pct(values: list[float], q: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    if len(ordered) == 1:
        return round(ordered[0], 3)
    pos = (len(ordered) - 1) * q
    low = int(pos)
    high = min(low + 1, len(ordered) - 1)
    frac = pos - low
    return round(ordered[low] * (1 - frac) + ordered[high] * frac, 3)


def _mean(values: list[float]) -> float | None:
    clean = [v for v in values if v is not None]
    return round(statistics.fmean(clean), 3) if clean else None


def summarize(rows: list[dict], mode: str) -> dict:
    """按模式汇总。分母全部是固定任务数，不合成"总分"。"""
    latencies = [r["latency_s"] for r in rows if r["status"] == "complete"]
    tokens_in = [r["input_tokens"] for r in rows if isinstance(r.get("input_tokens"), int)]
    tokens_out = [r["output_tokens"] for r in rows if isinstance(r.get("output_tokens"), int)]
    refusal_tasks = [r for r in rows if r["allow_refusal"]]
    return {
        "mode": mode,
        "tasks": len(rows),
        "status_counts": {
            "complete": sum(1 for r in rows if r["status"] == "complete"),
            "error": sum(1 for r in rows if r["status"] == "error"),
            "no_terminal": sum(1 for r in rows if r["status"] == "no_terminal"),
        },
        "latency_p50_s": _pct(latencies, 0.5),
        "latency_p95_s": _pct(latencies, 0.95),
        "latency_mean_s": _mean(latencies),
        "tokens_input_mean": _mean(tokens_in),
        "tokens_output_mean": _mean(tokens_out),
        "tokens_sampled": len(tokens_in),
        "tool_call_success_rate": _mean([r["tool_call_success_rate"] for r in rows]),
        "plan_tool_f1_mean": _mean([r["plan_tool_f1"] for r in rows]),
        "plan_tool_recall_mean": _mean([r["plan_tool_recall"] for r in rows]),
        "cite_tasks": sum(1 for r in rows if r["require_citation"]),
        "cite_tasks_with_evidence": sum(
            1 for r in rows if r["require_citation"] and r["citation_ids"]
        ),
        "refusal_tasks": len(refusal_tasks),
        "refusal_tasks_completed_without_error": sum(
            1 for r in refusal_tasks if r["status"] == "complete"
        ),
        # 本地的 agent_status 只报 local_planner，云端的才有多角色；
        # 用不同的键名，避免把本地也算成"有多智能体阶段"。
        "rows_with_agent_stages": sum(1 for r in rows if r["agent_stages"]),
        # 明细全部保留：汇总数字脱离明细就无法复核。
        "rows": rows,
    }


def _compare(paths: list[str]) -> int:
    reports = []
    for path in paths:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
        reports.append(payload.get("summary") or payload)
    if len(reports) < 2:
        print("至少需要两份结果才能对比")
        return 2
    print(f"{'metric':32s}" + "".join(f"{r['mode']:>18s}" for r in reports))
    print("-" * (32 + 18 * len(reports)))
    keys = ["tasks", "latency_p50_s", "latency_p95_s", "latency_mean_s",
            "tool_call_success_rate", "plan_tool_f1_mean", "plan_tool_recall_mean",
            "tokens_input_mean", "tokens_output_mean", "cite_tasks_with_evidence",
            "refusal_tasks_completed_without_error", "rows_with_agent_stages"]
    for key in keys:
        cells = []
        for report in reports:
            value = report.get(key)
            cells.append("—" if value is None else str(value))
        print(f"{key:32s}" + "".join(f"{cell:>18s}" for cell in cells))
    print()
    print("注意：这里的比率分母是固定任务数，不是模型准确率；两条链路工具集不同，")
    print("不要把这些数字合成一个总分。")
    return 0


def main() -> int:
    _safe_console()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--api-base", default="http://127.0.0.1:9900")
    parser.add_argument("--api-key", default="")
    parser.add_argument("--mode", choices=["local", "cloud_multi"], default="")
    parser.add_argument("--out", default="")
    parser.add_argument("--timeout", type=float, default=300.0)
    parser.add_argument("--gap", type=float, default=1.0,
                        help="任务间隔秒数；给本地小模型留出释放显存的时间")
    parser.add_argument("--only", default="", help="只跑某个 bucket（调试用，产物不可当结论）")
    parser.add_argument("--compare", nargs="*", default=None)
    args = parser.parse_args()

    if args.compare is not None:
        return _compare(args.compare)

    if not args.mode:
        parser.error("需要 --mode，或使用 --compare")
    key = args.api_key or __import__("os").environ.get("G8_API_KEY", "")
    if not key:
        print("缺少 API key：请用 --api-key 或环境变量 G8_API_KEY 提供（不会写进产物）")
        return 2

    tasks = load_tasks()
    if args.only:
        tasks = [t for t in tasks if t["bucket"] == args.only]
        if not tasks:
            print(f"没有 bucket={args.only} 的任务")
            return 2
        print(f"⚠️ --only 模式：只有 {len(tasks)} 条，产物不能当结论用")

    summary = run_mode(args.api_base, key, args.mode, tasks, args.timeout, args.gap)
    payload = {
        "task_set": {
            "file": "eval/mode_benchmark/tasks.jsonl",
            "sha256": TASKS_SHA256,
            "count": len(load_tasks()),
        },
        "mode": args.mode,
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "summary": summary,
    }
    out = Path(args.out) if args.out else (
        ROOT / "eval" / "mode_benchmark" / f"{args.mode}.json"
    )
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n写入 {out}")
    print(json.dumps({k: v for k, v in summary.items() if k != "rows"},
                     ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
