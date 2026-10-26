"""工具白名单的边界（M1）。

`search_log` 是 CLS 的底层工具，必须显式加入只读白名单；否则专科 Agent 在
模型输出里引用它时会被当成“未授权工具”，而用户会看到误导性的 unsupported。
"""

from __future__ import annotations

import pytest

from app.agent.aiops import runtime_tools


def test_search_log_is_whitelisted():
    assert "search_log" in runtime_tools.READ_ONLY_TOOLS


def test_every_cls_and_monitor_tool_used_by_specialists_is_whitelisted():
    """cloud_multi 的专科工具目录必须全部可执行，否则专科会被静默降级。"""
    from app.agent.aiops import cloud_multi

    declared = set()
    for names in cloud_multi.SPECIALIST_TOOLS.values():
        declared |= set(names)
    missing = declared - runtime_tools.READ_ONLY_TOOLS
    assert not missing, f"专科声明了未加入白名单的工具: {sorted(missing)}"


def test_ssh_tools_are_a_subset_of_allowed_tools():
    assert runtime_tools.SSH_TOOLS <= runtime_tools.READ_ONLY_TOOLS


def test_discovery_tools_are_allowed():
    assert runtime_tools.DISCOVERY_TOOLS <= runtime_tools.READ_ONLY_TOOLS


@pytest.mark.parametrize(
    "name",
    ["invoke_command", "shell", "run_command", "execute_sql", "delete_file"],
)
def test_write_or_arbitrary_execution_tools_are_not_allowed(name):
    assert name not in runtime_tools.READ_ONLY_TOOLS
