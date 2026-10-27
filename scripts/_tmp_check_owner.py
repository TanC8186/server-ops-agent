"""验证 owner 链条补丁真的把归属写进 run_start（在 app 副本上运行）。

流程：``/api/aiops`` → ``aiops_service.diagnose(owner=...)`` →
``run_diagnostic`` → ``trace_event(run_id, "run_start", owner=...)``。
只替换模型/运行器（外部 I/O），trace 与 API 用真实实现。
"""
import asyncio
import json
import sys
from pathlib import Path

ROOT = Path(sys.argv[1])
TRACES = Path(sys.argv[2])
sys.path.insert(0, str(ROOT))

from app import observability  # noqa: E402

observability.TRACE_DIR = TRACES

from app.core import run_context  # noqa: E402

run_context.TRACE_DIR = TRACES

from app.services import diagnostic_runtime  # noqa: E402
from app.services import aiops_service as svc_module  # noqa: E402


async def fake_runner(user_input, session_id, execution_mode, **kwargs):
    """替身运行器：捕获 owner 形参，产出最小终态事件序列。"""
    fake_runner.seen = kwargs
    yield {"type": "status", "stage": "running", "message": "ok"}
    yield {"type": "complete", "response": "报告正文", "lifecycle": "completed",
           "quality": "sufficient"}


# 直接调 run_diagnostic：把 local_runtime 换成替身，避免真实模型/网络。
import app.agent.aiops.local_runtime as local_runtime  # noqa: E402

local_runtime.run_local = fake_runner


async def main():
    from app.config import config

    config.local_llm_base_url = "http://127.0.0.1:9999/v1"  # 只为通过配置校验
    events = []
    async for event in diagnostic_runtime.run_diagnostic(
        "CPU 高", "session-owner", "local", owner="principal:key-abc123def456",
    ):
        events.append(event)

    assert events[-1]["type"] == "complete", events[-1]

    files = list(TRACES.glob("*.jsonl"))
    assert len(files) == 1, files
    run_id = files[0].stem
    lines = [json.loads(line) for line in files[0].read_text(encoding="utf-8").splitlines() if line.strip()]
    start = next(item for item in lines if item.get("event") == "run_start")
    assert start.get("owner") == "principal:key-abc123def456", start
    print("TRACE OWNER OK:", start["owner"])

    # 归属读回 + 判定
    assert run_context.run_owner(run_id) == "principal:key-abc123def456"
    assert run_context.load_run_context(run_id, "session-owner",
                                        "principal:key-abc123def456") is not None
    assert run_context.load_run_context(run_id, "session-owner", "principal:other") is None
    print("RUN_CONTEXT OWNER CHAIN OK")


asyncio.run(main())
