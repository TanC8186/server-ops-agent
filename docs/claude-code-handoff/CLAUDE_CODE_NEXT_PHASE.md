# 给 Claude Code 的下一阶段任务

请以当前工作区为准，先读取：

- docs/claude-code-handoff/NEXT_PHASE_PLAN_2026-10-27.md
- docs/claude-code-handoff/IMPLEMENTATION_STATUS.md
- docs/claude-code-handoff/VALIDATION_RESULTS.md
- docs/claude-code-handoff/probes/

上一阶段 A01–A13 的三个离线探针已全部 bug_reproduced=false，后端 621 passed/1 skipped，前端 53 passed，build/lint 通过。不要回退这些修复，也不要把测试替身结果写成真实依赖验收。

本轮目标从“修复已知离线反例”切换为“真实依赖闭环与质量证据”：

1. 先解决 Docker Hub token/代理问题，在隔离环境完成 docker build、docker compose up、健康探针、SIGTERM、MCP 异常退出和命名卷重建测试。构建网络失败时记录环境阻断，不改 Dockerfile 绕过。
2. 启动 staging Milvus，完成 11 个 core 文档的真实导入/查询/active revision 更新/部分写入失败清理/descriptor mismatch/停止恢复；保存 collection、metric、实体数、job 和 manifest 证据。
3. 用当前 build 重新跑浏览器旅程，覆盖同名实例 target_candidate_id、知识库 job/retry/citation、1100/1024/768/390/375、真实中文 IME、aria-live、tab 恢复和断流续读。
4. 用 staging 云模型、只读监控和 SSH 做有限真实冒烟；测试非 JSON/超时/取消/降级，记录 token/延迟/费用和远端取消边界。
5. 建立 50–100 条独立 blind RAG 集，保留 dev 阈值拟合边界；核对 needs_review 许可和 DK-03 正文质量；报告 Recall/MRR/nDCG、引用支持率、无答案误答率、p50/p95、CPU/RAM/VRAM。
6. 补 purge_expired_traces() 的实际调度、并发追加保护、principal/资源权限、知识片段与观测关联和有界多轮上下文。
7. 同步 IMPLEMENTATION_STATUS.md 与 VALIDATION_RESULTS.md：日期、commit、621/53 测试数字、Docker build 网络失败和真实依赖结果必须一致；删除“已验证”但没有真实证据的措辞。

所有新增测试优先保持真实 application service/runtime/store，只有 SDK、网络和 Milvus I/O 使用替身。默认不连接生产，不执行修改命令，不打印 .env、密钥或真实日志。每一阶段留下命令、构建 hash、环境版本、原始结果和失败分母，最后再决定对外可以引用哪些指标。
