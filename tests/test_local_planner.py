"""本地 planner 的工具/参数校验。"""

import pytest
from langchain_core.tools import tool

from app.agent.aiops.local_planner import (
    _plan_is_valid,
    repair_args,
    repair_tool_names,
    try_local_plan,
)


@tool
def list_all_services() -> dict:
    """List services."""
    return {"total": 1}


@tool
def query_cpu_metrics(service_name: str) -> dict:
    """Query CPU."""
    return {"value": 1}


def _steps(tool_name: str, args: dict) -> list[dict]:
    return [
        {
            "id": "s1",
            "kind": "tool",
            "tool": "list_all_services",
            "args": {},
            "description": "list",
        },
        {
            "id": "s2",
            "kind": "tool",
            "tool": tool_name,
            "args": args,
            "description": "query",
        },
    ]


def test_local_plan_accepts_valid_tool_and_args():
    assert _plan_is_valid(
        _steps("query_cpu_metrics", {"service_name": "agent-test-server"}),
        [list_all_services, query_cpu_metrics],
    )


def test_local_plan_rejects_unknown_tool():
    assert not _plan_is_valid(
        _steps("query_disk_metrics", {}), [list_all_services, query_cpu_metrics]
    )


def test_local_plan_rejects_unknown_args():
    assert not _plan_is_valid(
        _steps("query_cpu_metrics", {"query": "cpu"}),
        [list_all_services, query_cpu_metrics],
    )


def test_local_plan_requires_service_discovery():
    steps = _steps("query_cpu_metrics", {"service_name": "x"})[1:]
    assert not _plan_is_valid(steps, [list_all_services, query_cpu_metrics])



@pytest.mark.asyncio
async def test_local_planner_disabled_by_default():
    assert await try_local_plan("cpu high", "agent-test-server", [], "run") is None



def test_repair_maps_near_miss_tool_name():
    steps = _steps("query_service_logs", {"service_name": "x"})
    fixed = repair_tool_names(steps, [list_all_services, query_cpu_metrics])
    # 相似度低于阈值时保持原样，由 _plan_is_valid 拒绝
    assert fixed[1]["tool"] == "query_service_logs"


def test_repair_maps_close_name_when_candidate_exists():
    @tool
    def search_service_logs(service_name: str) -> dict:
        """Search service logs."""
        return {}

    steps = _steps("query_service_logs", {"service_name": "x"})
    fixed = repair_tool_names(steps, [list_all_services, search_service_logs])
    assert fixed[1]["tool"] == "search_service_logs"



@tool
def search_service_logs(service_name: str, log_level: str = "ERROR", keyword: str = "") -> dict:
    """Search service logs."""
    return {}


def test_repair_args_maps_close_parameter_name():
    steps = _steps("search_service_logs", {"service_name": "x", "level": "ERROR"})
    fixed = repair_args(steps, [list_all_services, search_service_logs])
    assert fixed[1]["args"]["log_level"] == "ERROR"
    assert "level" not in fixed[1]["args"]


def test_repair_args_fills_required_service_name():
    steps = _steps("search_service_logs", {"keyword": "timeout"})
    steps[1]["description"] = "query logs for agent-test-server"
    fixed = repair_args(steps, [list_all_services, search_service_logs])
    assert fixed[1]["args"]["service_name"] == "agent-test-server"


def test_repair_args_marks_unrepairable_step():
    steps = _steps("search_service_logs", {"keyword": "timeout"})
    steps[1]["description"] = "query logs"
    fixed = repair_args(steps, [list_all_services, search_service_logs])
    assert fixed[1].get("_invalid") is True


def test_autodl_prompt_skew_is_closed(monkeypatch):
    """AutoDL adapter 的 train/serve prompt 必须逐字一致。

    实测（G8）：数据集用 `compact_catalog(catalog, with_params=True)` 生成，
    运行时却按 `LOCAL_LLM_WITH_PARAMS=False` 渲染——模型看到的目录少了一整段
    参数签名。user turn 同样不一致：训练是六行（平台/采集方式/目标/目标角色/
    告警文本/请只输出 JSON），运行时是两行 `服务: x`。两处 skew 叠加会凭空
    低估模型表现（实测磁盘场景直接输出截断）。
    """
    from app.config import config
    from app.agent.aiops import local_planner as lp

    monkeypatch.setattr(config, "local_llm_model_ssh", "qwen-autodl")
    monkeypatch.setattr(config, "local_llm_with_params", False)

    # AutoDL 模型：训练用什么就渲染什么
    assert lp._with_params_for("qwen-autodl") is True
    # 通用模型：沿用配置项，行为不变
    assert lp._with_params_for("qwen-ops") is False

    ssh_turn = lp._user_turn("qwen-autodl", "autodl-ssh", "磁盘满了", "ssh")
    assert ssh_turn.startswith("平台：Linux\n采集方式：SSH 只读\n")
    assert "目标：autodl-ssh" in ssh_turn
    assert "目标角色：远程 Linux 算力节点" in ssh_turn
    assert "告警文本：磁盘满了" in ssh_turn
    assert ssh_turn.endswith("请只输出 JSON。")

    # 通用模型保持历史两行格式
    generic = lp._user_turn("qwen-ops", "svc-a", "CPU 高", "cloud")
    assert generic == "服务: svc-a\n告警文本: CPU 高\n请输出 JSON。"
    # AutoDL 模型但目标不是 ssh 时，不该套用 SSH 语境
    assert lp._user_turn("qwen-autodl", "svc-a", "CPU 高", "cloud") == generic
