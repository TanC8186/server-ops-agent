# Claude Code 下一轮整改提示词（2026-10-27）

请先阅读 `ACCEPTANCE_REVIEW_2026-10-27.md` 和原始任务书；以当前工作区为准，保留既有修改，不做 reset/clean/回退。当前全套 pytest 528 passed、1 skipped，前端 44 passed、build/lint 通过，但新的独立反例仍可复现。

先运行这三个离线探针，理解输出，不访问真实外部服务：

```powershell
.\.venv\Scripts\python.exe docs/claude-code-handoff/probes/rag_probe_2026_10_27.py
.\.venv\Scripts\python.exe docs/claude-code-handoff/probes/runtime_probe_2026_10_27.py
.\.venv\Scripts\python.exe docs/claude-code-handoff/probes/ui_eval_probe_2026_10_27.py
```

探针退出 0 仅表示执行完成；bug_reproduced=true 或异常字段表示缺陷仍存在。不要通过更改 fixture、删除反例、mock 整条被测链路来消除证据。

按报告 A01–A08 先修：

1. 检索可见性绑定 active revision，隔离 staging/failed/superseded；更新/回滚/撤销与并发查询保持一致。
2. 批量插入失败清理全部预生成候选 IDs，处理删除失败，旧 active 保留；不可仅清理成功返回的 inserted。
3. strict_local 真正约束 provider；local 知识工具注入本地检索能力或拒绝远端，远端 factory/HTTP 次数必须为 0，不修改全局配置切换模式。
4. 前后端使用服务端候选 ID/完整可信目标身份，保留同名多实例，点击后能绑定具体实例。
5. 目标参数补全只使用工具 schema 接受字段；合法 topic 查询成功，越界仍拒绝。
6. 以结构化字段校验 metric/value/unit/target/time，修复 12ms 被写成 999ms/12%/其他目标昨天的结果仍 supported。
7. 接上实际日志参数、证据字节预算与 prompt 摘录，limit=999999 与 evidence_chars=10 的反例受到真实限制。
8. transient 初始化失败支持锁保护/退避恢复，永久 invalid_index 区别处理。

再完成 A09–A13：

- 修复混合 seq/无 seq 的分页游标；前端续页和断流恢复到原 run。
- 评测列表兼容旧 list 与 {manifest,rows}，返回 manifest，贯通面板。
- 云 runtime 的取消标记、清理、审计和后续工具禁止调用一致。
- 未配置模式刷新、FE-09、MCP/模型可用性反馈，更新当前构建的浏览器验收。
- 统一 Compose Milvus 冷启动依赖策略，补实际镜像/恢复验证；parent 回填、正文质量、多轮上下文、retention/abandoned/权限与独立盲测按原范围完成或诚实标未完成。

为每个反例添加期望行为回归测试，保留真实 API/service/runtime/store 逻辑，仅替换外部 SDK/网络/Milvus I/O。版本化测试要使用“先写一行再抛错”的 fake，不能只用调用前直接抛错的 fake。

源码修好后，重新执行完整 pytest、前端 test/build/lint、静态检查和三个探针；修复后的探针不应再显示上述缺陷。更新 IMPLEMENTATION_STATUS/VALIDATION_RESULTS，区分已实现、测试替身验证、真实依赖验证，修正目前偏乐观或矛盾的完成声明。
