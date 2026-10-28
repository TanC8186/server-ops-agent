# Claude Code 第五阶段任务：质量与生产化收尾

先读取 FINAL_ACCEPTANCE_REVIEW_2026-10-27.md、BLIND_EVAL_2026-10-27.md、MILVUS_ACCEPTANCE_2026-10-27.md、DOCKER_ACCEPTANCE_2026-10-27.md、CLOUD_SMOKE_2026-10-27.md 和 BROWSER_JOURNEY_2026-10-27.md。

当前工作区已经通过 717 个后端测试、53 个前端测试，真实 Milvus/镜像/LLM/当前浏览器也有有限证据。不要回退现有修复，也不要把 0.9828 的 Recall@3 写成问答准确率。不要新增泛化防御层、无业务目标的兜底逻辑或单独的冒烟测试；只实现下面直接支撑用户流程和数据正确性的工作。

下面五项来自 Claude Code 的真实依赖发现，已经完成，不要重复实现：

- package-lock 缺 version 已修复，npm ci 可用；后续只保留 lockfile 校验。
- Milvus 2.5 的实体数读取已改为 count(*) 优先；不要再用额外兜底层掩盖实体数不一致。
- 陈旧镜像复用问题已有验收记录；后续在当前提交重新构建时使用明确的镜像 digest/force-recreate。
- 模型围栏、前缀和截断 JSON 已由 extract/一次重试处理；不要继续堆叠通用输出清洗器。
- statistics.count/total 已纳入 count 事实；不要扩展成没有数据依据的自由事实推断。

这些条目只需要保留现有回归用例，并在当前提交重新构建/运行时确认，不属于下一阶段新增功能。

按下列顺序工作：

1. 建立新的 calibration/dev-2 集，不修改 77 条 frozen blind；实现分域/版本感知的 gate 校准，补真实 LLM 引用支持率、事实正确率和无答案误答评测。
2. 在候选检索前实现 OS、产品、版本、有效期和文档级 ACL 过滤；保留单租户 demo 开关，测试未授权内容不进入 reranker/模型。
3. 使用 staging MCP 的同名双实例返回，完成真实浏览器目标选择；补索引失败 retry、SSE 断流续读、tab 恢复、真实引用卡片。
4. 处理 Pydantic、PyMilvus、Ruff 的弃用警告；优化 18.9GB 镜像或记录可接受取舍；CI 固定 --pull --no-cache 或镜像 digest，避免复用陈旧镜像。
5. 对 retention、abandoned、跨进程 trace 锁、job/ACL/active revision 元数据做长时间和并发测试。
6. 真实 SSH 仍需用户授权的 staging 主机；在没有授权前不要伪造 SSH 结果，继续把它列为未验证。

每项核心场景都要留下当前 commit、数据/manifest hash、环境版本和失败分母。完成后同步 IMPLEMENTATION_STATUS.md、VALIDATION_RESULTS.md 和 README；保留历史失败记录，不要覆盖或删除。默认不连接生产、不打印密钥、不执行变更命令。新增验证必须服务于具体用户流程或数据一致性，不要为了增加测试数量而增加测试。
