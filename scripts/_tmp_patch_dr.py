"""在临时副本上试打 diagnostic_runtime / aiops 的 owner 补丁（不改仓库文件）。

这两处不在我的 scope 里，但**没有它们，run 归属永远是空的**：trace 的
``run_start`` 不写 owner，``principal.authorize`` 就退化成“无 owner 放行”。
这里验证补丁可行，并把原文交给 team-lead 应用。
"""
import io
import sys

dr_path, aiops_path = sys.argv[1], sys.argv[2]
NL = chr(10)

# ── diagnostic_runtime：run_start 写 owner ───────────────────
s = io.open(dr_path, encoding="utf-8").read()

old_sig = (
    'async def run_diagnostic(user_input: str, session_id: str, execution_mode: str,' + NL +
    '                         *, service_name: str | None = None,' + NL +
    '                         target_choice: str | None = None,' + NL +
    '                         target_candidate_id: str | None = None,' + NL +
    '                         request_context: dict | None = None):'
)
new_sig = (
    'async def run_diagnostic(user_input: str, session_id: str, execution_mode: str,' + NL +
    '                         *, service_name: str | None = None,' + NL +
    '                         target_choice: str | None = None,' + NL +
    '                         target_candidate_id: str | None = None,' + NL +
    '                         request_context: dict | None = None,' + NL +
    '                         owner: str = ""):'
)
assert s.count(old_sig) == 1, "dr signature anchor"
s = s.replace(old_sig, new_sig)

old_start = (
    '    trace_event(run_id, "run_start", session_id=session_id, execution_mode=execution_mode,' + NL +
    '                input_chars=len(user_input), deadline_s=config.diagnostic_timeout_seconds)'
)
new_start = (
    '    trace_event(run_id, "run_start", session_id=session_id, execution_mode=execution_mode,' + NL +
    '                input_chars=len(user_input), deadline_s=config.diagnostic_timeout_seconds,' + NL +
    '                owner=owner or None)'
)
assert s.count(old_start) == 1, "dr run_start anchor"
s = s.replace(old_start, new_start)

old_doc = '    ``target_choice`` 会在传入的 ``target_choice`` 为空时兜底。' + NL + '    """'
new_doc = (
    '    ``target_choice`` 会在传入的 ``target_choice`` 为空时兜底。' + NL + NL +
    '    ``owner`` 是发起本次运行的 principal id，写进 ``run_start``：读/回放/清理' + NL +
    '    据此判定归属（见 ``app.core.principal``）。空串表示未认证调用，记录为' + NL +
    '    ``None``，读取侧按“无 owner”兼容处理。' + NL +
    '    """'
)
assert s.count(old_doc) == 1, "dr doc anchor"
s = s.replace(old_doc, new_doc)

io.open(dr_path, "w", encoding="utf-8").write(s)

# ── aiops：把 principal 传给服务 ────────────────────────────
a = io.open(aiops_path, encoding="utf-8").read()

old_import = (
    'from app.config import config' + NL +
    'from app.models.aiops import AIOpsRequest'
)
new_import = (
    'from app.config import config' + NL +
    'from app.core import principal as acl' + NL +
    'from app.models.aiops import AIOpsRequest'
)
assert a.count(old_import) == 1, "aiops import anchor"
a = a.replace(old_import, new_import)

old_kw = (
    '    if accepted is not None:' + NL +
    '        # 旧服务只接受 session_id 时不能因为新增字段变成 TypeError。' + NL +
    '        kwargs = {key: value for key, value in kwargs.items() if key in accepted}'
)
new_kw = (
    '    if accepted is not None:' + NL +
    '        # 旧服务只接受 session_id 时不能因为新增字段变成 TypeError。' + NL +
    '        kwargs = {key: value for key, value in kwargs.items() if key in accepted}'
)
assert a.count(old_kw) == 1, "aiops kwargs anchor"

io.open(aiops_path, "w", encoding="utf-8").write(a)
print("DR PATCH OK (aiops needs a call-site param; see report)")
