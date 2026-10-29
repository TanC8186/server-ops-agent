"""
AIOps 智能运维接口
"""

import inspect
import json
from typing import Any

from fastapi import APIRouter, Header
from loguru import logger
from sse_starlette.sse import EventSourceResponse

from app.config import config
from app.core import principal as acl
from app.models.aiops import AIOpsRequest
from app.services.aiops_service import aiops_service

router = APIRouter()

#: 终态事件类型：一条 SSE 流最多交付其中一个。
#:
#: ``target_required`` **不是**终态：它后面必须紧跟 ``complete``
#: （status=target_required）。旧实现把它当终态直接 break，于是运行器来不及
#: 写 ``run_complete``，运行历史里这次运行永远是 ``status=running``、
#: ``lifecycle=null``，前端也无法从 trace 恢复候选卡片。
TERMINAL_EVENT_TYPES = frozenset({"complete", "error"})


def _is_terminal(event: dict[str, Any]) -> bool:
    """判断事件是否结束本次诊断。

    ``target_required`` 只表示“等用户选择”，随后仍由 ``complete`` 收尾；
    这里只认 ``complete`` / ``error`` 两种真终态。
    """
    return event.get("type") in TERMINAL_EVENT_TYPES


def _accepted_parameters(callable_obj) -> set[str] | None:
    """返回可接受的关键字参数名；接受 ``**kwargs`` 时返回 ``None``。

    服务与运行时都可能由旧版本或测试替身提供（没有新形参）。这里按签名转发，
    避免为了一个可选字段把调用变成 TypeError。
    """
    try:
        parameters = inspect.signature(callable_obj).parameters
    except (TypeError, ValueError):
        return set()
    if any(p.kind is inspect.Parameter.VAR_KEYWORD for p in parameters.values()):
        return None
    return {name for name in parameters if name != "self"}


def _service_kwargs(request: AIOpsRequest, owner: str | None = None) -> dict[str, Any]:
    """把请求字段投影到服务当前真正接受的参数上。

    ``target_choice`` 是用户在 ``target_required`` 候选卡片里的显式选择；
    运行时的公开签名目前只有 ``service_name``（绑定同一个目标目录字段），
    因此当服务不接受 ``target_choice``/``request_context`` 时，用选择值覆盖
    ``service_name`` —— 这是同一次用户显式动作，不是自动重试。

    ``time_window`` 已在模型层校验（end > start、≤ 7 天）；运行时暂未消费它，
    只做协议记录，不拼进 symptom 文本。
    """
    accepted = _accepted_parameters(aiops_service.diagnose)
    choice = request.target_choice

    kwargs: dict[str, Any] = {
        "session_id": request.session_id or "default",
        "execution_mode": request.execution_mode,
        "symptom": request.symptom,
        "service_name": request.service_name,
    }
    if accepted is None or "target_choice" in accepted:
        kwargs["target_choice"] = choice
    elif choice and not request.target_candidate_id:
        # 当前服务/运行时的公开签名只有 service_name；它正是目标目录的绑定键，
        # 因此把用户的显式选择投影到 service_name（不是自动重试，是同一个动作）。
        # 有候选 ID 时优先走候选 ID，避免服务名把实例信息丢掉。
        kwargs["service_name"] = choice

    if accepted is None or "target_candidate_id" in accepted:
        kwargs["target_candidate_id"] = request.target_candidate_id

    if accepted is None or "owner" in accepted:
        # G3-3：owner 由 API 层从**已验证**的 API Key 推导，不取自请求体，
        # 模型/客户端都不能伪造归属。
        kwargs["owner"] = owner

    if accepted is None or "request_context" in accepted:
        context: dict[str, Any] = {}
        if choice:
            context["target_choice"] = choice
        if request.target_candidate_id:
            context["target_candidate_id"] = request.target_candidate_id
        if request.time_window is not None:
            context["time_window"] = request.time_window.model_dump()
        kwargs["request_context"] = context

    if accepted is not None:
        # 旧服务只接受 session_id 时不能因为新增字段变成 TypeError。
        kwargs = {key: value for key, value in kwargs.items() if key in accepted}

    if request.time_window is not None:
        # 只记录形状，不记录可能含用户数据的原始值。
        logger.info(
            f"[会话 {kwargs.get('session_id')}] 诊断时间窗已校验: "
            f"{request.time_window.start} → {request.time_window.end}"
        )
    return kwargs


@router.get("/execution-modes")
async def execution_modes():
    """配置状态，不探测模型服务，也不返回凭证。

    除了 ``configured``，这里还返回三件"选模式时必须知道、但前端不该自己编"
    的事实（G8 P1 前端复查 FE-G8-01）：

    - ``requires_credentials``：跑起来需要哪个服务端配置项（只给**变量名**，
      不会是值）；
    - ``failure_behavior``：失败时到底停还是切，直接对应运行器的
      ``allow_cloud_fallback=False`` 行为；
    - ``resource_cost``：本机算力占用 vs 按 token 计费。

    这些是**配置形态**，不是探测结果：``configured=true`` 只表示"填了"，不表示
    "连得上"。真正 readiness 以 ``/health`` 的 ``capabilities`` 为准。
    """
    return {
        "modes": [
            {
                "id": "local",
                "label": "本地端侧",
                "model": config.local_llm_model,
                "configured": bool(config.local_llm_base_url.strip()),
                "description": "本地小模型规划、只读工具执行与证据报告；不回退在线模型。",
                "requires_credentials": ["LOCAL_LLM_BASE_URL"],
                "failure_behavior": "stop",
                "failure_text": "本地模式已停止：不会自动切换到在线模型",
                "resource_cost": "占用本机 GPU/CPU；告警文本与工具结果不出本机",
            },
            {
                "id": "cloud_multi",
                "label": "在线多智能体",
                "model": config.llm_model,
                "configured": bool(config.llm_api_key.strip())
                and not config.llm_api_key.startswith("your-"),
                "description": "主管调度指标与日志专科并行分析，由审查角色汇总证据；使用在线 API。",
                "requires_credentials": ["LLM_API_KEY"],
                "failure_behavior": "report_error",
                "failure_text": "在线模式调用失败：本次运行报错结束，不会改用本地模型",
                "resource_cost": "不占本机算力；文本会发往模型服务商并按 token 计费",
            },
        ]
    }


@router.post("/aiops")
async def diagnose_stream(
    request: AIOpsRequest,
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
):
    """
    AIOps 故障诊断接口（流式 SSE）

    **功能说明：**
    - 按 ``execution_mode`` 选择 ``local`` 或 ``cloud_multi`` 诊断运行时，
      默认 ``local``
    - 请求直接转发给 ``app.services.diagnostic_runtime.run_diagnostic``；
      没有 Plan-Execute-Replan 图，也没有 ``diagnosis`` 包装层
    - 流式返回诊断过程和结果；每个事件由运行时补齐统一信封
      （``schema_version/event_id/seq/run_id/session_id/execution_mode/type/ts/payload``）

    **SSE 事件类型：**

    1. `status` - 运行阶段状态（如 ``run_start``、``target_bound``、
       ``local_queue``）
       ```json
       {
         "type": "status",
         "stage": "target_bound",
         "message": "本次诊断目标：svc-a",
         "service_name": "svc-a",
         "run_id": "...",
         "session_id": "session-123",
         "seq": 2,
         "payload": {"stage": "target_bound", "message": "..."}
       }
       ```

    2. `plan` - 本次运行的排查计划（``backend`` 为 ``local`` 或云端主管）
       ```json
       {
         "type": "plan",
         "plan_steps": [{"kind": "tool", "tool": "query_cpu_metrics", "args": {...}}],
         "backend": "local"
       }
       ```

    3. `tool_step` / `step_complete` - 单次工具调用及其结构化证据
       ```json
       {
         "type": "tool_step",
         "step": "query_cpu_metrics",
         "evidence_id": "E2",
         "ok": true,
         "status": "ok",
         "source": "tencent_cloud",
         "result_preview": "..."
       }
       ```

    4. `agent_status` - 云端多智能体角色状态（``cloud_multi`` 模式）
       ```json
       {
         "type": "agent_status",
         "agent": "reviewer",
         "status": "completed",
         "message": "..."
       }
       ```

    5. `report` - 中间或最终报告文本
       ```json
       {
         "type": "report",
         "stage": "final_report",
         "report": "# 故障诊断报告\\n..."
       }
       ```

    6. `complete` - 诊断完成；``response`` 即最终 Markdown 报告
       ```json
       {
         "type": "complete",
         "stage": "complete",
         "response": "# 故障诊断报告\\n...",
         "duration_ms": 4210
       }
       ```

    7. `error` - 终态错误（未配置模型、目标无法绑定、超时、协议错误等）
       ```json
       {
         "type": "error",
         "message": "诊断达到时间预算，已停止"
       }
       ```

    一次运行恰好以一个 ``complete`` 或 ``error`` 结束，之后流关闭。

    **使用示例：**
    ```bash
    curl -X POST "http://localhost:9900/api/aiops" \\
      -H "Content-Type: application/json" \\
      -d '{"session_id": "session-123", "execution_mode": "local", "symptom": "CPU 使用率 97%"}' \\
      --no-buffer
    ```

    **前端使用示例：**
    ```javascript
    // SSE 只支持 GET；前端通过 fetch + ReadableStream 读取 POST 响应。
    const response = await fetch('/api/aiops', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({session_id: 'session-123'}),
    });
    const reader = response.body.getReader();
    // 逐行解析 "data: {...}"，按 data.type 分发：
    //   plan / tool_step / step_complete / agent_status -> 进度
    //   report -> 报告正文
    //   complete -> event.response 为最终报告，结束读取
    //   error    -> event.message 为失败原因，结束读取
    ```

    Args:
        request: AIOps 诊断请求

    Returns:
        SSE 事件流
    """
    session_id = request.session_id or "default"
    # 认证失败映射成 401（不是 500）；dev 模式不要求 key，但归属照样记录。
    owner = acl.current_principal(None, (x_api_key or "").strip()).principal_id
    logger.info(f"[会话 {session_id}] 收到 AIOps 诊断请求（流式）")

    async def event_generator():
        # 终态守卫：服务异常/重复 yield 时也只能交付一个终态。
        terminal_sent = False

        def frame(event: dict[str, Any]) -> dict[str, str]:
            return {"event": "message", "data": json.dumps(event, ensure_ascii=False)}

        try:
            # G3-3：owner 只能来自**已验证**的 API Key（dev 模式映射到单一
            # principal），绝不从请求体或其他可伪造字段取。
            async for event in aiops_service.diagnose(
                **_service_kwargs(request, owner=owner)
            ):
                if not isinstance(event, dict):
                    logger.warning(f"[会话 {session_id}] 忽略非对象事件: {type(event).__name__}")
                    continue
                if terminal_sent:
                    # 服务多 yield 的终态不能再交付，否则前端 reducer 可能回退状态。
                    logger.warning(f"[会话 {session_id}] 忽略重复终态事件: {event.get('type')}")
                    break
                yield frame(event)
                if _is_terminal(event):
                    terminal_sent = True
                    break
            else:
                if not terminal_sent:
                    # 服务提前结束且没有终态：补一个 error，客户端不会无限等待。
                    logger.warning(f"[会话 {session_id}] 诊断流结束但未收到终态事件")
                    yield frame(
                        {
                            "type": "error",
                            "stage": "incomplete",
                            "message": "诊断流意外结束，未收到终态事件",
                        }
                    )
                    terminal_sent = True

            logger.info(f"[会话 {session_id}] AIOps 诊断流式响应完成")

        except Exception as e:
            logger.error(f"[会话 {session_id}] AIOps 诊断流式响应异常: {e}", exc_info=True)
            if terminal_sent:
                # 终态已经交付：异常只落日志，不能追加第二个 error。
                logger.info(f"[会话 {session_id}] 终态已交付，异常不再追加事件")
                return
            yield frame(
                {
                    "type": "error",
                    "stage": "exception",
                    "message": f"诊断异常: {str(e)}",
                }
            )

    return EventSourceResponse(event_generator())
