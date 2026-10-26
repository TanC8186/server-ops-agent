"""BE-06 回归：跨请求资源边界。

覆盖四件事：

1. 并发准入：超限请求排队，超时后带 ``retry_after`` 拒绝；释放名额能唤醒等待者；
   等待集合在任何路径（成功/超时/取消）后都会清空，不会无限增长。
2. 单次运行预算：按种类硬上限，超额抛类型化 ``BudgetExhausted``；
   两个 agent 共享同一预算并发取用也不会超支。
3. 日志参数收敛：limit / 关键词数量 / 单关键词长度都被夹住，并说明丢了什么。
4. prompt 截断：字符串保留头尾并带显式标记；dict 先解析再截断，结果仍是合法 JSON。
"""

from __future__ import annotations

import asyncio
import json

import pytest

from app.config import Settings
from app.core.budgets import (
    AdmissionController,
    AdmissionRejected,
    BoundedLogParams,
    BudgetExhausted,
    RequestBudget,
    truncate_for_prompt,
)

# ---------------------------------------------------------------------------
# 配置字段
# ---------------------------------------------------------------------------


def test_config_has_budget_fields_with_expected_defaults():
    fields = Settings.model_fields
    assert fields["max_concurrent_diagnostics"].default == 2
    assert fields["admission_queue_timeout_seconds"].default == 30
    assert fields["diagnostic_max_llm_calls"].default == 12
    assert fields["diagnostic_max_prompt_chars"].default == 20000
    assert fields["diagnostic_max_evidence_chars"].default == 60000
    assert fields["diagnostic_log_limit"].default == 100
    assert fields["diagnostic_log_max_keywords"].default == 5


# ---------------------------------------------------------------------------
# AdmissionController
# ---------------------------------------------------------------------------


async def test_over_limit_request_waits_then_rejects_with_retry_after():
    controller = AdmissionController(max_concurrent=1, queue_timeout=0.05)
    async with controller:
        assert controller.stats.active == 1
        with pytest.raises(AdmissionRejected) as excinfo:
            async with controller:
                pytest.fail("超限请求不应拿到名额")
        assert excinfo.value.retry_after > 0
        stats = controller.stats
        assert stats.rejected_total == 1
        assert stats.timeouts_total == 1
        assert stats.waiting == 0
        assert stats.active == 1

    assert controller.stats.active == 0


async def test_released_slot_admits_waiter():
    controller = AdmissionController(max_concurrent=1, queue_timeout=1.0)
    acquired = asyncio.Event()

    async def waiter() -> None:
        async with controller:
            acquired.set()

    async with controller:
        task = asyncio.create_task(waiter())
        await asyncio.sleep(0.02)
        assert controller.stats.waiting == 1
        assert not acquired.is_set()
    await asyncio.wait_for(task, timeout=1.0)
    assert acquired.is_set()
    assert controller.stats.waiting == 0
    assert controller.stats.active == 0
    assert controller.stats.rejected_total == 0


async def test_waiting_set_does_not_grow_unbounded_after_rejections():
    controller = AdmissionController(max_concurrent=1, queue_timeout=0.03)

    async def attempt() -> str:
        async with controller:
            return "admitted"

    async with controller:
        results = await asyncio.gather(*[attempt() for _ in range(20)], return_exceptions=True)

    assert all(isinstance(item, AdmissionRejected) for item in results)
    stats = controller.stats
    assert stats.waiting == 0
    assert stats.rejected_total == 20
    assert stats.timeouts_total == 20
    assert stats.active == 0
    # 名额没有被等待者“吃掉”：控制器仍可立即放行
    async with controller:
        assert controller.stats.active == 1


async def test_cancelled_waiter_is_removed_from_waiting_set():
    controller = AdmissionController(max_concurrent=1, queue_timeout=5.0)

    async def waiter() -> None:
        async with controller:
            pytest.fail("被取消的等待者不应拿到名额")

    async with controller:
        task = asyncio.create_task(waiter())
        await asyncio.sleep(0.02)
        assert controller.stats.waiting == 1
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert controller.stats.waiting == 0
        assert controller.stats.active == 1
        # 取消的等待者没有把名额吞掉，后续仍可正常获取

    async with controller:
        assert controller.stats.active == 1


# ---------------------------------------------------------------------------
# RequestBudget
# ---------------------------------------------------------------------------


def _budget(**overrides: int) -> RequestBudget:
    limits = {
        "tool_calls": 2,
        "llm_calls": 1,
        "subprocess_calls": 1,
        "prompt_chars": 100,
        "evidence_chars": 200,
    }
    limits.update(overrides)
    return RequestBudget(**limits)


def test_budget_reserve_accumulates_and_snapshot_reports_used_and_limit():
    budget = _budget(tool_calls=3)
    assert budget.reserve("tool_calls", 2) == 2
    assert budget.snapshot()["tool_calls"] == {"used": 2, "limit": 3, "remaining": 1}


def test_budget_exhaustion_is_typed_with_kind():
    budget = _budget(tool_calls=2)
    budget.reserve("tool_calls", 2)
    with pytest.raises(BudgetExhausted) as excinfo:
        budget.reserve("tool_calls", 1)
    assert excinfo.value.kind == "tool_calls"
    assert excinfo.value.limit == 2
    assert budget.snapshot()["tool_calls"]["used"] == 2


def test_budget_rejects_unknown_kind_and_negative_amount():
    budget = _budget()
    with pytest.raises(ValueError):
        budget.reserve("network_calls", 1)
    with pytest.raises(ValueError):
        budget.reserve("tool_calls", -1)


def test_budget_accepts_mcp_alias_for_subprocess_calls():
    budget = _budget(subprocess_calls=1)
    budget.reserve("mcp_calls", 1)
    assert budget.snapshot()["subprocess_calls"]["used"] == 1
    with pytest.raises(BudgetExhausted):
        budget.reserve("mcp_calls", 1)


def test_budget_single_reserve_larger_than_cap_fails():
    budget = _budget(llm_calls=3)
    with pytest.raises(BudgetExhausted):
        budget.reserve("llm_calls", 4)
    # 失败的预留不能留下半截计数
    assert budget.snapshot()["llm_calls"]["used"] == 0


async def test_two_agents_sharing_one_budget_cannot_overspend():
    budget = _budget(tool_calls=5)

    async def agent(name: str) -> tuple[int, int]:
        accepted = 0
        rejected = 0
        for _ in range(10):
            try:
                budget.reserve("tool_calls", 1)
                accepted += 1
            except BudgetExhausted:
                rejected += 1
            await asyncio.sleep(0)  # 让另一个 agent 插进来
        return accepted, rejected

    results = await asyncio.gather(agent("a"), agent("b"))
    assert sum(accepted for accepted, _ in results) == 5
    assert sum(rejected for _, rejected in results) == 15
    assert budget.snapshot()["tool_calls"]["used"] == 5


def test_request_budget_from_config_reads_caps():
    from types import SimpleNamespace

    cfg = SimpleNamespace(
        diagnostic_max_tool_calls=7,
        diagnostic_max_llm_calls=3,
        diagnostic_max_prompt_chars=1234,
        diagnostic_max_evidence_chars=5678,
    )
    budget = RequestBudget.from_config(cfg)
    snapshot = budget.snapshot()
    assert snapshot["tool_calls"]["limit"] == 7
    assert snapshot["subprocess_calls"]["limit"] == 7
    assert snapshot["llm_calls"]["limit"] == 3
    assert snapshot["prompt_chars"]["limit"] == 1234
    assert snapshot["evidence_chars"]["limit"] == 5678


# ---------------------------------------------------------------------------
# BoundedLogParams
# ---------------------------------------------------------------------------


def test_log_params_clamp_limit_and_keywords_and_report_dropped():
    helper = BoundedLogParams(max_limit=50, max_keywords=3)
    params, notes = helper.clamp(
        {
            "topic_id": "topic-1",
            "limit": 500,
            "query": "a,b,c,d,e",
        }
    )
    assert params["limit"] == 50
    assert params["topic_id"] == "topic-1"
    assert params["query"] == "a,b,c"
    joined = " ".join(notes)
    assert "500" in joined and "50" in joined
    assert "d" in joined and "e" in joined


def test_log_params_truncate_each_keyword_to_64_chars():
    helper = BoundedLogParams(max_limit=100, max_keywords=5)
    long_keyword = "x" * 80
    params, notes = helper.clamp({"limit": 10, "query": f"error,{long_keyword}"})
    keywords = params["query"].split(",")
    assert len(keywords[1]) == 64
    assert notes


def test_log_params_do_not_mutate_input_and_handle_bad_limit():
    helper = BoundedLogParams(max_limit=20, max_keywords=2)
    original = {"limit": "not-a-number", "query": "a"}
    params, notes = helper.clamp(original)
    assert original["limit"] == "not-a-number"
    assert params["limit"] == 20
    assert notes


def test_log_params_from_config_uses_settings_values():
    class Cfg:
        diagnostic_log_limit = 42
        diagnostic_log_max_keywords = 2

    helper = BoundedLogParams.from_config(Cfg())
    params, notes = helper.clamp({"limit": 1000, "query": "a,b,c"})
    assert params["limit"] == 42
    assert params["query"] == "a,b"
    assert notes


# ---------------------------------------------------------------------------
# truncate_for_prompt
# ---------------------------------------------------------------------------


def test_truncate_string_keeps_head_and_tail_with_marker():
    text = "HEAD" + "x" * 1000 + "TAIL"
    result = truncate_for_prompt(text, 120)
    assert result.truncated is True
    assert len(result.text) <= 120
    assert result.text.startswith("HEAD")
    assert result.text.endswith("TAIL")
    assert "truncated" in result.text.lower()
    assert result.note


def test_truncate_string_under_budget_is_unchanged():
    result = truncate_for_prompt("short", 100)
    assert result.text == "short"
    assert result.truncated is False
    assert result.note == ""


def test_truncate_dict_prefers_dropping_list_items_and_stays_valid_json():
    payload = {
        "service": "svc-a",
        "logs": [{"message": "m" * 50, "i": i} for i in range(50)],
        "meta": {"region": "ap-beijing"},
    }
    result = truncate_for_prompt(payload, 400)
    assert result.truncated is True
    assert len(result.text) <= 400
    parsed = json.loads(result.text)  # 必须是合法 JSON，不能从字符串中间切
    assert parsed["service"] == "svc-a"
    assert parsed["meta"]["region"] == "ap-beijing"
    assert len(parsed["logs"]) < 50
    assert result.note


def test_truncate_dict_with_only_large_strings_still_returns_valid_json():
    payload = {"blob": "y" * 5000, "name": "demo"}
    result = truncate_for_prompt(payload, 200)
    assert result.truncated is True
    assert len(result.text) <= 200
    parsed = json.loads(result.text)
    assert parsed["_truncated"] is True


def test_truncate_small_dict_under_budget_is_unchanged():
    payload = {"a": 1}
    result = truncate_for_prompt(payload, 100)
    assert result.truncated is False
    assert json.loads(result.text) == payload


def test_truncate_rejects_non_positive_budget():
    with pytest.raises(ValueError):
        truncate_for_prompt("abc", 0)
