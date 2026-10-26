# Claude Code 停止后的续接提示词

在 Claude Code **新一轮**粘贴以下内容，先读当前代码，不要重发失败的旧 Edit。

---

你上一轮因为 `EDIT_GUARD_STALE_INPUT / EDIT_LOOP_GUARD_STOP` 停止了，任务尚未完成。保留停止前已落盘的修改，禁止 reset、clean、整文件回退或关闭 hook。

请先读：

1. `docs/claude-code-handoff/COMPLETION_AUDIT_2026-10-26.md`
2. `docs/claude-code-handoff/CLAUDE_CODE_TASK.md`
3. `docs/claude-code-handoff/ENTERPRISE_REVIEW.md`
4. `docs/claude-code-handoff/M2_API_CONTRACT.md`（这是待实现契约，不是完成证明）。

先处理当前阻断：

- Read `servers/ssh_server.py` 当前前 90 行，以及 `servers/_config.py`、`servers/cls_server.py`、`servers/monitor_server.py` 的配置段。
- ssh_server 已使用 read_env/read_env_int 却缺导入，并仍 load_dotenv。完成统一只读配置迁移，兼容当前直接脚本启动和模块导入。
- 为 Edit 复制当前内容中的小而唯一 old_string，仅尝试一次；若再次不匹配，停止该 Edit 并重新 Read，不能原样重试。
- 三个 MCP server 仍有真实 load_dotenv 调用；新 test_server_env_isolation 的字符串扫描也把 _config 的历史说明误判为执行。修正实际配置污染，并用 AST/行为检测消除注释误报。

复核已观察到的失败：

```powershell
.\.venv\Scripts\python.exe -m ruff check servers/ssh_server.py --select F821
.\.venv\Scripts\python.exe -m pytest tests/test_server_env_isolation.py -q
.\.venv\Scripts\python.exe -m pytest tests -q
```

当前完整 pytest 在 test_eval_scoring/test_ssh_safety 的导入阶段失败；排除三个相关文件的子集仍有 7 失败、1 跳过。修复后必须运行完整套件，不得把排除失败文件当成验收通过。

然后继续完成原任务，特别注意以下“已写但未接入”的断点：

1. `web/src/state.ts`、`sse.ts` 25 项单元测试通过，但 App.tsx/chat.ts 仍旧实现。真实工具事件还缺 step_id/tool_call_id；target_required 也未接页面候选选择。
2. 新 knowledge_service/core.retrieval 只被测试调用，实际 chat API 仍用旧 RagAgentService。改接真实 API→新服务→VectorStoreRetriever→VectorStoreManager，外部边界才用 fake。
3. VectorStoreRetriever 调用不存在的 similarity_search_with_score，回退后候选分数全部变成 1；不能假装评分存在。修复检索分数语义及实际不可用/miss 的区别。
4. embedding 描述符尚未用于建库、写入或查询；索引 L2 与 descriptor cosine 不一致。必须在真实 I/O 边界校验，而不只是测试 helper。
5. ThreadPoolExecutor 的上下文退出等待让检索 timeout 失效；超长单行代码可能超 token；上传同名并发的 activate/rollback 在锁外，需补行为回归。
6. claims 结构化核验没有进入最终渲染，仍可错指标/漏中文相邻数值；CLS 包装仍吞错误；topic 发现/空 topics 目录仍可越界。
7. budgets 尚未接运行链路，deadline/cancel 尚未接 runner；本地结果质量和终态 trace 仍缺字段。
8. M2 文档/作业/revision/撤销与前端 KB 管理未实现；规范原始快照只有 3/11，不能写成知识库已构建。
9. RAG 历史、诊断知识联动、run_id 追问、应用持久化、冻结构建与真实评测继续按原任务完成。

前端当前可用检查命令：

```powershell
cd web
npm run build
npm run lint
node --experimental-strip-types --test src/sse.test.ts src/state.test.ts
```

本机 Node 22.17 需要上面的 strip-types 参数；将测试命令和版本要求纳入项目配置。build/lint 与新模块测试通过均不能替代真实页面接线和浏览器任务旅程。

不要打印 .env、密钥或真实日志；先用固定 fixture 验证。按 M0→M1→M2/M3→M4→M5 推进，保留原有用户改动。最终补 `IMPLEMENTATION_STATUS.md` 和 `VALIDATION_RESULTS.md`，逐项写已修复、已验证、未完成和原因；没有真实运行的结果不能写成已通过。
