# 给 Claude Code 的执行任务书

直接将下方“执行提示词”整段交给 Claude Code。完整问题、证据与验收在同目录的审查报告中；本文件是执行约束和阶段顺序，不代表问题已经修复。

## 执行提示词

你接手的是 `E:\On_Call_Agent\on_call_agent` 的企业服务器运维助手项目。用户希望兼顾本地小模型/QLoRA 与在线 API 多智能体，同时把系统做成可复现、可核验的工程项目。个人设备是 RTX 4060 Laptop 8GB；本地与在线模式由初始界面选择。

请先完整读取：

1. `docs/claude-code-handoff/ENTERPRISE_REVIEW.md`
2. `docs/claude-code-handoff/RAG_CORPUS_PLAN.md`
3. `docs/claude-code-handoff/knowledge_sources.json`
4. `docs/claude-code-handoff/review_snapshot.json`
5. `docs/DUAL_MODE.md` 与现有 README、依赖锁文件、测试和仓库规则。

工作区已有大量尚未提交的双模式实现，是本次审查对象。**保留现有用户改动，禁止 reset/clean/revert 或直接 checkout 基线覆盖当前代码。** `fca7f1c` 仅是当前 Git 基线，不包含全部待整改实现。先记录 status，核对快照哈希；若文件已变化，检查当前行为后更新结论，不机械按旧行号打补丁。

### A. 先复现并修复 M0

先添加能体现问题的回归测试，再修改代码：

- P0-01：在线 agent_status 的 run_id 重复传入 trace_event，使完整运行器中断。必须贯通 FastAPI→真实 service→真实 diagnostic runtime→真实 cloud graph；仅 fake 模型/外部工具。禁止把整个 run_diagnostic 或 service mock 掉后声称已验证主流程。
- P0-02：.env 中的 API_KEY 没有统一进入鉴权配置。独立子进程仅通过 env_file 配置密钥，验证无密钥与错误密钥 401、正确密钥通过；显式开发模式另测。
- P0-03：Windows run_id 路径越界。使用临时目录和合成文件，验证编码反斜杠、斜杠、盘符/绝对路径等不会读取 traces 外文件。
- P0-04：同名文件上传与重建先删旧数据。先保证超限/解析/embedding/插入失败不损坏旧 active 文档与向量；完整 revision 作业继续在 M2 实现。

每项记录修改文件、反例、修复后测试和未覆盖边界。不要仅补日志、改提示词或隐藏错误来绕过根因。

### B. 按依赖完成 M1：统一协议与诊断闭环

1. 定义类型化事件信封和工具返回契约，统一 SSE、trace、回放、前端 reducer。保留字段只由服务端生成，不能从任意 payload 展开覆盖。
2. step_id 与 tool_call_id 分离；实时计划/专科分支与历史一致；发现服务不混进计划完成计数。
3. 可信目标目录绑定 service/instance/region/topic/SSH；多候选进入 target_required，由 UI 选择，不取列表第一项。
4. 错误、空结果、权限不足、demo、部分成功明确区分；CLS 包装不丢底层状态。
5. 生命周期、诊断质量、工具结果各有独立状态；工具全失败或证据不足仍可生成说明，但不能展示为诊断成功。
6. 结构化观测核对数值、指标、目标与时间窗；因果解释保留为假设。引用真实 ID 不等于事实得到支持。
7. 跨请求并发/队列/参数字节/上下文/子调用预算有上界；deadline 下传同步 SDK/SSH。取消后的存活工作如实记录。
8. 前端模式刷新、聊天 fetch/SSE、停止与新请求竞态、运行历史独立加载和 run_id 恢复一并修复。不能恢复时自动 POST 新任务。

已修复的行为要保留：诊断 SSE 分片支持、准确 run_id 回填、防过期响应、本地诊断云隔离、ChatPanel 卸载 abort、Markdown 危险 URL 过滤。

### C. 完成 M2/M3：可管理、可恢复、有引用的知识库

1. 文档使用稳定 document_id、revision、content hash 与 active 状态；临时验证/staging/激活/清理分离。按文档串行更新，导入幂等，旧版本可撤销。
2. 上传有界读取，索引后台有界执行，持久化 job_id 与 queued/running/ready/failed 状态；上传成功不等于索引成功。前端增加文档列表、上传、作业、失败重试、版本与撤销。
3. embedding 描述符与 collection 验证，错维/同维不同模型均拒绝；本地模式预置模型并严格避免远端 embedding fallback。先用 CPU baseline，不能默认常驻大 GPU embedding/reranker。
4. 知识问答固定 retrieve→检索状态/适用性门控→answer；默认没有运维 MCP 工具权限。hit/miss/unavailable/timeout/invalid_index 分开。
5. RetrievedChunk/AnswerCitation 贯通检索工具 artifact→服务→HTTP SSE→前端片段与引用卡片。引用 ID 由服务端为本轮授权命中分配并验证；保留原文定位、版本和来源。
6. 按真实 tokenizer 切分，保留 parent/child、标题、代码缩进、完整命令与注意事项。当前 MiniLM max_seq_length=128；不要假定字符数就是 token 数。
7. 修复历史 checkpoint API、消息角色/工具配对、token 裁剪与会话 TTL/恢复；不要吞异常后返回“空历史”。

公开语料的具体实施按 RAG_CORPUS_PLAN.md：

- 来源目录有 25 页，分 core=11、extension=9、optional=5；目前只是核验过的来源，尚未下载或入库。
- 先处理 core 11；逐页核对许可、版权、版本和署名，保存原始快照/哈希/manifest/notice。许可未明确的条目保持 pending，不把“公开可读”当成可任意复制。
- 白名单抓取采用目录明确 URL 和有限必要章节，不整站爬取，不访问任意用户输入 URL。滚动 latest/main 固定快照与目标版本。
- Linux、Windows、PowerShell 版本、NGINX OSS/Plus 条件和实际部署范围保留；译文关联原文，不伪造历史工单。
- 索引切换到新语料前，先在 staging 验证描述符、数量、权限/版本过滤、代码保真与一组人工核验问题。

### D. 完成 M4：让双模式诊断与知识形成可解释联动

增加受范围限制的 retrieve_knowledge；知识与当前监控/日志分别作为 knowledge/observation 证据。手册不能证明当前服务器已发生某故障。诊断报告列出当前观测、假设、知识依据与缺口。

追问显式携带 run_id，由服务端校验归属后加载对应运行的报告和受控证据摘要；不能只复用一个 session_id 就声称上下文贯通。跨运行/用户不串线。

补浏览器任务旅程：两模式选择、目标歧义、配置恢复、错误/部分成功/取消、刷新历史、文档导入与引用。检查中文 IME、375/768/1024/1440 布局、滚动跟随与键盘/状态播报；静态推测必须经过浏览器再标为已验证。

### E. 完成 M5：部署、资源与可信评测

按能力区分 liveness/readiness，去掉固定 sleep 当就绪的假设；MCP 子进程应被监督或拆成有健康检查的服务。运行元数据/trace 持久化，控制分页/保留/访问归属，写入失败可观测。

锁文件驱动冻结构建，生产不安装开发依赖；npm ci 失败不回退到 npm install 掩盖问题。干净部署后验证依赖掉线与恢复。

先建立当前 dense 检索 baseline 和人工核验开发/盲测集，再决定 hybrid/RRF/rerank；不默认引入 Elasticsearch、额外大模型、分布式任务平台或重写前端。

报告分别包含：检索召回与无答案处理、引用正确、任务完成、目标/数值正确、base 与 QLoRA 对比、延迟、显存/RAM 和在线费用。拟定题量与指标目标不能写成实测结果。不要在不同数据/工具条件下比较模型后宣称训练有效。

### F. 测试与执行边界

默认先用合成/固定 fixture 和测试替身，不连接生产服务器、不运行修改命令。真实测试环境与付费 API 凭证由明确配置启用；费用、调用与副作用可见。不得打印 .env、密钥或真实敏感日志。

按仓库实际配置选择检查命令，至少包括相关 pytest、前端 lint/build、Python lint 与跨层集成测试。诊断和聊天 SSE 都测试真实字节分片/异常 EOF；索引测试错误注入与依赖恢复；路径测试覆盖 Windows 语义。不要为了减少失败而删除既有测试或 mock 掉被测链路。

如果环境缺 Milvus、浏览器或真实模型，把阻碍和未执行范围写清，并继续完成可离线验证部分；不可把跳过标成通过。单元测试通过不代表真实服务/资源指标通过。

### G. 最终交付

- 业务代码、必要配置与迁移/兼容层。
- 回归与跨层测试，实际执行命令和结果。
- 文档来源快照/manifest/notices、索引作业与 collection 描述符；无法完成抓取或待许可项清楚标记。
- README/部署说明/双模式能力说明更新；截图与真实浏览器验证记录。
- `docs/claude-code-handoff/IMPLEMENTATION_STATUS.md`：逐项映射 P0/FE/BE/RAG/OPS 编号，区分已修复、已验证、未完成和原因；列明模型/MCP/Milvus/服务器使用的真实或替身边界。
- `docs/claude-code-handoff/VALIDATION_RESULTS.md`：保存固定版本、数据与脚本入口、实际结果和剩余限制。仅能写可追溯的实验/演示指标。

按 M0→M1→M2/M3→M4→M5 推进，在每阶段留下可运行、可审查的状态。需要做实现选择时优先沿用当前栈和最小可验证方案，避免只有设计文档没有真正闭环。

## 给项目维护者的使用说明

本轮生成的是审查与实施输入，尚未执行上述整改或导入语料。Claude Code 接手时应从当前工作区开始，先复现 P0；审查报告中的行号随修改会变化，快照哈希用于识别变化，不用于覆盖新代码。
