"""AIOps 诊断服务：HTTP 层到请求级诊断运行时的唯一入口。

历史上的 Plan-Execute-Replan LangGraph 分支已被移除。原因是
``AIOpsRequest.execution_mode`` 为必选（默认 ``local``），``service.diagnose``
收到的模式永远不是 ``None``，因此旧图在 HTTP 上不可达；它的 ``plan`` /
``diagnosis`` 事件契约也已与真实运行时不符。

当前契约：

- ``diagnose`` / ``execute`` 默认 ``execution_mode="local"``，并直接转发给
  ``app.services.diagnostic_runtime.run_diagnostic``。
- 事件由运行时的统一信封规范化（``schema_version/event_id/seq/run_id/
  session_id/execution_mode/type/ts/payload``），本模块不重写事件形状。
- 每次运行的状态由运行时按 ``run_id`` 隔离；本服务无进程级图、checkpointer
  或跨请求缓存。
"""

from __future__ import annotations

import inspect
from collections.abc import AsyncGenerator
from typing import Any

from app.services.diagnostic_runtime import run_diagnostic

#: 未提供症状时使用的默认排查任务。
DEFAULT_DIAGNOSIS_TASK = "请检查目标服务的运行状态，查询指标和日志，并基于证据给出排查建议。"


def _supported_kwargs(target, **kwargs: Any) -> dict[str, Any]:
    """按目标函数的真实签名裁剪关键字参数。

    ``run_diagnostic`` 可能被旧版本或测试替身替换（没有 ``target_choice`` /
    ``request_context`` 形参）；多传一个可选字段不应该变成 TypeError。
    """
    try:
        parameters = inspect.signature(target).parameters
    except (TypeError, ValueError):
        return dict(kwargs)
    if any(p.kind is inspect.Parameter.VAR_KEYWORD for p in parameters.values()):
        return dict(kwargs)
    return {key: value for key, value in kwargs.items() if key in parameters}


class AIOpsService:
    """把诊断请求转发给请求级运行时；不持有跨请求状态。"""

    async def execute(
        self,
        user_input: str,
        session_id: str = "default",
        execution_mode: str = "local",
        service_name: str | None = None,
        target_choice: str | None = None,
        target_candidate_id: str | None = None,
        request_context: dict | None = None,
    ) -> AsyncGenerator[dict[str, Any], None]:
        """执行一次诊断并流式返回运行时事件。

        Args:
            user_input: 用户任务/症状描述，作为诊断任务文本。
            session_id: 会话 ID，仅用于追踪，不参与状态复用。
            execution_mode: ``local`` 或 ``cloud_multi``；默认 ``local``。
            service_name: 可选的目标服务名；多候选中未指定时运行时会要求选择。
            target_choice: 用户在候选卡片里的显式选择，与 ``service_name`` 同义。
            target_candidate_id: 服务端签发的稳定候选 ID；同名多实例必须用它绑定。
            request_context: 协议预留的请求上下文；其中可包含 ``target_choice``。

        Yields:
            Dict[str, Any]: 规范化后的运行时事件（status/tool_step/report/
            complete/error 等）。终态为 ``complete`` 或 ``error``。
        """
        kwargs = _supported_kwargs(
            run_diagnostic,
            service_name=service_name,
            target_choice=target_choice,
            target_candidate_id=target_candidate_id,
            request_context=request_context,
        )
        async for event in run_diagnostic(user_input, session_id, execution_mode, **kwargs):
            yield event

    async def diagnose(
        self,
        session_id: str = "default",
        execution_mode: str = "local",
        symptom: str | None = None,
        service_name: str | None = None,
        target_choice: str | None = None,
        target_candidate_id: str | None = None,
        request_context: dict | None = None,
    ) -> AsyncGenerator[dict[str, Any], None]:
        """按症状执行 AIOps 诊断，事件原样透传运行时输出。

        Args:
            session_id: 会话 ID。
            execution_mode: ``local`` 或 ``cloud_multi``；默认 ``local``。
            symptom: 用户症状描述；为空时使用默认排查任务。
            service_name: 可选目标服务名。
            target_choice: 用户在候选卡片里的显式选择；与 ``service_name`` 同义。
            target_candidate_id: 服务端签发的稳定候选 ID（同名多实例必须用它）。
            request_context: 协议预留的请求上下文；其中可包含 ``target_choice``。

        Yields:
            Dict[str, Any]: 运行时事件；``complete`` 事件携带 ``response`` 报告。
        """
        task = (symptom or DEFAULT_DIAGNOSIS_TASK).strip() or DEFAULT_DIAGNOSIS_TASK
        async for event in self.execute(
            task,
            session_id,
            execution_mode=execution_mode,
            service_name=service_name,
            target_choice=target_choice,
            target_candidate_id=target_candidate_id,
            request_context=request_context,
        ):
            yield event


# 全局单例
aiops_service = AIOpsService()
