"""在 app 副本上试打并验证三处补丁（diagnostic_runtime / aiops_service / aiops）。

这些文件不在我的 scope 内，但没有它们 owner 永远写不进 run_start，归属校验
就一直是空转。这里在副本上验证补丁可行、语义正确。

用法：python <this> <app_copy_root>
"""
import io
import sys

ROOT = sys.argv[1]
NL = chr(10)


def patch(path, replacements):
    s = io.open(path, encoding="utf-8").read()
    for old, new, label in replacements:
        assert s.count(old) == 1, f"{path}: {label} anchor (count={s.count(old)})"
        s = s.replace(old, new)
    io.open(path, "w", encoding="utf-8").write(s)


DR = ROOT + "/app/services/diagnostic_runtime.py"
SVC = ROOT + "/app/services/aiops_service.py"
API = ROOT + "/app/api/aiops.py"

# ── 1) diagnostic_runtime：形参 + run_start 写 owner ────────
patch(DR, [
    (
        'async def run_diagnostic(user_input: str, session_id: str, execution_mode: str,' + NL +
        '                         *, service_name: str | None = None,' + NL +
        '                         target_choice: str | None = None,' + NL +
        '                         target_candidate_id: str | None = None,' + NL +
        '                         request_context: dict | None = None):',
        'async def run_diagnostic(user_input: str, session_id: str, execution_mode: str,' + NL +
        '                         *, service_name: str | None = None,' + NL +
        '                         target_choice: str | None = None,' + NL +
        '                         target_candidate_id: str | None = None,' + NL +
        '                         request_context: dict | None = None,' + NL +
        '                         owner: str = ""):',
        "signature",
    ),
    (
        '    trace_event(run_id, "run_start", session_id=session_id, execution_mode=execution_mode,' + NL +
        '                input_chars=len(user_input), deadline_s=config.diagnostic_timeout_seconds)',
        '    trace_event(run_id, "run_start", session_id=session_id, execution_mode=execution_mode,' + NL +
        '                input_chars=len(user_input), deadline_s=config.diagnostic_timeout_seconds,' + NL +
        '                owner=owner or None)',
        "run_start owner",
    ),
])

# ── 2) aiops_service：转发 owner ────────────────────────────
patch(SVC, [
    (
        '        target_candidate_id: str | None = None,' + NL +
        '        request_context: dict | None = None,' + NL +
        '    ) -> AsyncGenerator[dict[str, Any], None]:' + NL +
        '        """执行一次诊断并流式返回运行时事件。',
        '        target_candidate_id: str | None = None,' + NL +
        '        request_context: dict | None = None,' + NL +
        '        owner: str = "",' + NL +
        '    ) -> AsyncGenerator[dict[str, Any], None]:' + NL +
        '        """执行一次诊断并流式返回运行时事件。',
        "execute signature",
    ),
    (
        '            request_context=request_context,' + NL +
        '        )' + NL +
        '        async for event in run_diagnostic(user_input, session_id, execution_mode, **kwargs):',
        '            request_context=request_context,' + NL +
        '            owner=owner,' + NL +
        '        )' + NL +
        '        async for event in run_diagnostic(user_input, session_id, execution_mode, **kwargs):',
        "execute forwarding",
    ),
    (
        '        target_candidate_id: str | None = None,' + NL +
        '        request_context: dict | None = None,' + NL +
        '    ) -> AsyncGenerator[dict[str, Any], None]:' + NL +
        '        """按症状执行 AIOps 诊断，事件原样透传运行时输出。',
        '        target_candidate_id: str | None = None,' + NL +
        '        request_context: dict | None = None,' + NL +
        '        owner: str = "",' + NL +
        '    ) -> AsyncGenerator[dict[str, Any], None]:' + NL +
        '        """按症状执行 AIOps 诊断，事件原样透传运行时输出。',
        "diagnose signature",
    ),
    (
        '            target_candidate_id=target_candidate_id,' + NL +
        '            request_context=request_context,' + NL +
        '        ):',
        '            target_candidate_id=target_candidate_id,' + NL +
        '            request_context=request_context,' + NL +
        '            owner=owner,' + NL +
        '        ):',
        "diagnose forwarding",
    ),
])

# ── 3) api/aiops.py：解析 principal 并作为 owner 传入 ────────
patch(API, [
    (
        'from fastapi import APIRouter' + NL +
        'from loguru import logger',
        'from fastapi import APIRouter, Header' + NL +
        'from loguru import logger',
        "fastapi import",
    ),
    (
        'from app.config import config' + NL +
        'from app.models.aiops import AIOpsRequest',
        'from app.config import config' + NL +
        'from app.core import principal as acl' + NL +
        'from app.models.aiops import AIOpsRequest',
        "principal import",
    ),
    (
        'def _service_kwargs(request: AIOpsRequest) -> dict[str, Any]:',
        'def _service_kwargs(request: AIOpsRequest, owner: str = "") -> dict[str, Any]:',
        "_service_kwargs signature",
    ),
    (
        '    if accepted is not None:' + NL +
        '        # 旧服务只接受 session_id 时不能因为新增字段变成 TypeError。' + NL +
        '        kwargs = {key: value for key, value in kwargs.items() if key in accepted}',
        '    if accepted is None or "owner" in accepted:' + NL +
        '        kwargs["owner"] = owner' + NL +
        NL +
        '    if accepted is not None:' + NL +
        '        # 旧服务只接受 session_id 时不能因为新增字段变成 TypeError。' + NL +
        '        kwargs = {key: value for key, value in kwargs.items() if key in accepted}',
        "_service_kwargs owner",
    ),
    (
        '@router.post("/aiops")' + NL +
        'async def diagnose_stream(request: AIOpsRequest):',
        '@router.post("/aiops")' + NL +
        'async def diagnose_stream(request: AIOpsRequest,' + NL +
        '                         x_api_key: str | None = Header(default=None,' + NL +
        '                                                       alias="X-API-Key")):',
        "endpoint signature",
    ),
    (
        '    session_id = request.session_id or "default"' + NL +
        '    logger.info(f"[会话 {session_id}] 收到 AIOps 诊断请求（流式）")',
        '    # 创建本次 run 的 principal：写进 trace 的 run_start，作为归属的唯一事实。' + NL +
        '    # 解析失败（启用鉴权但缺少/错 key）直接 401，不落一份无主的 run。' + NL +
        '    try:' + NL +
        '        caller = acl.current_principal(None, x_api_key or "")' + NL +
        '    except acl.PermissionDenied as exc:' + NL +
        '        from fastapi import HTTPException' + NL +
        NL +
        '        raise HTTPException(status_code=exc.status_code, detail=exc.as_detail()) from exc' + NL +
        '    session_id = request.session_id or "default"' + NL +
        '    logger.info(f"[会话 {session_id}] 收到 AIOps 诊断请求（流式）")',
        "endpoint principal resolution",
    ),
    (
        '            async for event in aiops_service.diagnose(**_service_kwargs(request)):',
        '            async for event in aiops_service.diagnose(' + NL +
        '                **_service_kwargs(request, owner=caller.principal_id)' + NL +
        '            ):',
        "endpoint call",
    ),
])

print("PATCHES APPLIED")
