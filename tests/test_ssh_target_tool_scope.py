"""G8 追加：SSH 目标的工具目录收窄。

实测背景（2026-10-29，目标是 AutoDL 的 Linux 容器）：把诊断目标从腾讯云换到
SSH 主机后，本地 planner 仍然规划 `query_cpu_metrics` / `query_memory_metrics` /
`search_service_logs`——全是腾讯云监控与 CLS 工具，对那台机器必然失败，报告里
却把这些失败当成"证据缺口"列出来。用户看到的第一反应是"怎么还有腾讯云的东西！"

根因不在模型：`services.json` 里的 `type: ssh` **没有任何代码消费**，
`Target` 没有这个字段，`create_run_tools` 又把全部 22 个工具一次性塞给 planner。
模型只能从目录里选，目录里有什么它就会选什么。

本文件锁定三件事：
1. `type=ssh`（且声明了 ssh_host）的条目会变成 `Target.kind == "ssh"`；
2. 这种目标的工具目录只剩 SSH 工具 + 与采集方式无关的工具；
3. 云目标的历史行为不变（全量目录）。
"""

from __future__ import annotations

import pytest

from app.agent.aiops.runtime_tools import (
    KNOWLEDGE_TOOL,
    SSH_TOOLS,
    TARGET_AGNOSTIC_TOOLS,
    RunTools,
)
from app.core.targets import Target, catalog_targets

CLOUD_ROW = {
    "service_name": "pay-api",
    "instance_id": "ins-abc123",
    "region": "ap-guangzhou",
}
SSH_ROW = {
    "service_name": "autodl-ssh",
    "instance_id": "autodl-container-73d6479980-a8bf3ae3",
    "region": "seetacloud-sha1",
    "type": "ssh",
    "ssh_host": "connect.sha1.seetacloud.com",
}


def _runtime(tools: dict | None = None) -> RunTools:
    """构造一个只带目录的最小 RunTools；不连 MCP、不执行任何工具。"""
    names = list((tools or {}).keys()) or [
        "list_all_services", "get_service_info", "query_cpu_metrics",
        "query_memory_metrics", "query_disk_metrics", "query_network_metrics",
        "query_process_list", "search_service_logs", "search_log",
        "search_system_logs", "analyze_log_pattern", "get_system_info",
        "get_process_list", "check_disk_usage", "read_log_file",
        "get_current_time", "get_current_timestamp", KNOWLEDGE_TOOL,
    ]

    class _Tool:
        def __init__(self, name: str) -> None:
            self.name = name
            self.description = ""
            self.args_schema = None

    return RunTools([_Tool(n) for n in names], "run-test", "local", lambda _e: None)


# ── 1. 目录条目 → Target.kind ────────────────────────────────


def test_ssh_entry_becomes_ssh_target():
    targets = catalog_targets([SSH_ROW])
    assert len(targets) == 1
    assert targets[0].kind == "ssh"
    assert targets[0].ssh_only is True
    assert targets[0].ssh_in_scope is True


def test_cloud_entry_keeps_historical_kind():
    targets = catalog_targets([CLOUD_ROW])
    assert targets[0].kind == "cloud"
    assert targets[0].ssh_only is False


def test_ssh_entry_without_ssh_host_falls_back_to_cloud():
    """声明 type=ssh 却没给 ssh_host：按云目标处理。

    否则会得到一个"只读 SSH 但不知道连哪"的目标：工具目录收窄成 SSH 专属，
    而 SSH 工具本身连不上，整次诊断只剩空证据。
    """
    row = {k: v for k, v in SSH_ROW.items() if k != "ssh_host"}
    target = catalog_targets([row])[0]
    assert target.kind == "cloud"
    assert target.ssh_only is False


def test_unknown_type_is_cloud():
    target = catalog_targets([{**CLOUD_ROW, "type": "cvm"}])[0]
    assert target.kind == "cloud"


# ── 2. 工具目录收窄 ──────────────────────────────────────────


def test_ssh_target_catalog_excludes_cloud_metric_tools():
    runtime = _runtime()
    runtime.bind(catalog_targets([SSH_ROW])[0])
    names = {tool.name for tool in runtime.tools_for_target()}

    # 云端监控/日志 API 工具必须消失——它们对 SSH 主机只会报与目标无关的错。
    for cloud_tool in (
        "query_cpu_metrics", "query_memory_metrics", "query_disk_metrics",
        "query_network_metrics", "query_process_list", "search_service_logs",
        "search_log", "analyze_log_pattern",
    ):
        assert cloud_tool not in names, f"{cloud_tool} 不应对 SSH 目标可见"

    # SSH 工具与目标无关工具必须保留。
    assert SSH_TOOLS <= names
    assert TARGET_AGNOSTIC_TOOLS <= names
    # 发现类工具是绑定目标的第一步，缺了整次运行起不来。
    assert "list_all_services" in names


def test_cloud_target_catalog_is_unchanged():
    runtime = _runtime()
    runtime.bind(catalog_targets([CLOUD_ROW])[0])
    names = {tool.name for tool in runtime.tools_for_target()}
    # 云目标保持全量目录：这次修复不改变已有语义。
    assert names == set(runtime.by_name)


def test_unbound_runtime_returns_full_catalog():
    """还没绑定目标时给全量：绑定阶段自己要调 list_all_services。"""
    runtime = _runtime()
    assert {t.name for t in runtime.tools_for_target()} == set(runtime.by_name)


# ── 3. 与 planner 的契约：收窄后的目录仍能通过计划校验 ──────


def test_narrowed_catalog_still_contains_first_step_tool():
    """planner 的第一个 tool 步骤必须是 list_all_services。

    收窄时必须保留它，否则本地计划永远过不了校验，本地模式会 100% 停止。
    """
    runtime = _runtime()
    runtime.bind(catalog_targets([SSH_ROW])[0])
    names = {tool.name for tool in runtime.tools_for_target()}
    assert "list_all_services" in names


@pytest.mark.parametrize("tool", sorted(SSH_TOOLS))
def test_every_ssh_tool_is_plannable_for_ssh_target(tool):
    """SSH 目录里的每个工具都要对 SSH 目标可见（不能收窄过头）。"""
    runtime = _runtime()
    runtime.bind(catalog_targets([SSH_ROW])[0])
    assert tool in {t.name for t in runtime.tools_for_target()}
