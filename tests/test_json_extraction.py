"""真实模型输出形态下的 JSON 提取（2026-10-27 真实 smoke 暴露的缺陷）。

真实调用（Anthropic 兼容网关 / DeepSeek）经常返回三反引号围栏、前缀说明或
尾部解释。旧实现直接 ``json.loads(reply)``，于是这些**正常输出**被记成
``JSONDecodeError``，Reviewer 与 logs 专科各失败一次、报告退化为“没有观测”。

本文件锁定 ``_extract_json`` 的行为：能取到的必须取到，取不到的必须如实失败
（不能靠猜造出一个合法 JSON）。
"""

from __future__ import annotations

import json

import pytest

from app.agent.aiops.cloud_multi import _extract_json


@pytest.mark.parametrize(
    "raw",
    [
        '{"a": 1}',
        "```json\n{\"a\": 1}\n```",
        "```\n{\"a\": 1}\n```",
        "好的，计划如下：\n{\"a\": 1}",
        "{\"a\": 1}\n以上是计划。",
    ],
)
def test_extracts_json_from_common_model_wrappers(raw):
    assert json.loads(_extract_json(raw)) == {"a": 1}


def test_handles_nested_objects_and_braces_inside_strings():
    raw = '前言 {"a": {"b": 2}, "c": "}"} 尾巴'

    assert json.loads(_extract_json(raw)) == {"a": {"b": 2}, "c": "}"}


def test_plain_json_is_not_rewritten():
    raw = '{"a": 1}'

    assert _extract_json(raw) == raw


@pytest.mark.parametrize("raw", ['{"a": 1, "b":', "", "我不能输出 JSON。"])
def test_unparseable_output_still_fails_loudly(raw):
    with pytest.raises((ValueError, TypeError)):
        json.loads(_extract_json(raw))


def test_truncated_output_returns_partial_for_diagnosis_not_a_fake_object():
    """截断输出必须原样暴露，不能补全成看似合法的 JSON。"""
    raw = '{"a": 1, "b":'

    extracted = _extract_json(raw)

    assert extracted == raw[start_index(raw):]
    with pytest.raises(json.JSONDecodeError):
        json.loads(extracted)


def start_index(raw: str) -> int:
    return raw.find("{")


def test_ask_retries_once_on_unparseable_output_then_succeeds():
    """真实 smoke：模型先返回截断/带说明的回复，重试一次应能拿到合法 JSON。

    只重试“输出不可解析”，且最多 JSON_ATTEMPTS 次；既不无限重试，也不把
    第一次的坏输出当成最终失败（那会让报告退化为“没有观测”）。
    """
    import asyncio
    from types import SimpleNamespace

    from app.agent.aiops import cloud_multi

    class Model:
        def __init__(self) -> None:
            self.calls = 0
            self.messages: list = []

        async def ainvoke(self, messages):
            self.calls += 1
            self.messages.append(list(messages))
            if self.calls == 1:
                # 被 max_tokens 截断的典型形态：花括号没闭合。
                return SimpleNamespace(content='{"assignments": [{"agent": "metr',
                                       usage_metadata={}, response_metadata={"finish_reason": "max_tokens"})
            return SimpleNamespace(
                content='```json\n{"assignments": [{"agent": "metrics", "focus": "c", "tools": []},'
                        ' {"agent": "logs", "focus": "l", "tools": []}]}\n```',
                usage_metadata={}, response_metadata={"finish_reason": "stop"},
            )

    model = Model()
    traces: list[tuple[str, dict]] = []
    with __import__("unittest.mock", fromlist=["patch"]).patch.object(
        cloud_multi, "trace_event", lambda run_id, event, **fields: traces.append((event, fields))
    ):
        plan = asyncio.run(cloud_multi._ask(
            model, [("system", "x"), ("user", "y")],
            cloud_multi.SupervisorPlan, agent="supervisor", run_id="r",
        ))

    assert model.calls == 2, "必须恰好重试一次"
    assert {item.agent for item in plan.assignments} == {"metrics", "logs"}
    # 重试时追加了“只输出 JSON”的指令，而不是原样重发。
    # 首次是 max_tokens 截断 -> 重试指令必须是"精简"版，而非笼统的"只输出 JSON"。
    retry_text = str(model.messages[1])
    assert "精简" in retry_text and "完整闭合" in retry_text
    events = [name for name, _ in traces]
    assert "agent_llm_bad_json" in events
    # 首个 trace 是 agent_llm_start（没有 attempt）；坏 JSON 那条带 attempt=1。
    bad = next(fields for name, fields in traces if name == "agent_llm_bad_json")
    assert bad.get("attempt") == 1


def test_ask_does_not_retry_network_errors():
    """网络/鉴权错误不重试：重试只会多花钱，且掩盖真实故障。"""
    import asyncio

    from app.agent.aiops import cloud_multi

    class Boom:
        def __init__(self) -> None:
            self.calls = 0

        async def ainvoke(self, messages):
            self.calls += 1
            raise RuntimeError("connection reset")

    model = Boom()
    with __import__("unittest.mock", fromlist=["patch"]).patch.object(
        cloud_multi, "trace_event", lambda *a, **k: None
    ):
        with pytest.raises(RuntimeError):
            asyncio.run(cloud_multi._ask(
                model, [("system", "x"), ("user", "y")],
                cloud_multi.SupervisorPlan, agent="supervisor", run_id="r",
            ))

    assert model.calls == 1
