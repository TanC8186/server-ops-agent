# 2026-10-27 独立验收报告

对象：`E:\On_Call_Agent\on_call_agent` 当前工作区。基线仍为 `fca7f1c` 加未提交实现；以本次软件文件哈希快照为验收对象。没有将前一轮的缺陷直接当作本轮仍然存在。

## 1. 判定

**自动化检查通过，部分软件能力可验收；原任务仍不能判为全部完成。**

本次独立复跑得到 **528 passed、1 skipped**，前端 **44 passed**，build/lint 与指定 Python 静态检查通过。上一轮 SSH 导入阻断已经修复，state/sse/知识库面板与新问答服务确实接入了实际入口。11 个 core 手册的 raw、normalized、notices 已补齐，磁盘文件与两份 manifest 哈希一致。

但新增离线反例复现了知识旧版/失败数据可检索、严格 local 可选远程 embedding、同名多实例无法绑定、合法 topic 查询失败、事实单位核验不完整、证据预算未落实等行为。现有测试通过不能代替这些边界验收。

本轮仅审查、运行检查、创建报告和离线探针；没有修改业务源码、连接真实云/SSH/Milvus、下载新模型或启动项目容器。探针使用临时目录、真实业务类、假工具/向量/provider。探针退出 0 表示执行完成，输出 `bug_reproduced=true` 或异常行为才是验收证据，**不是业务验证通过**。

## 2. 本轮实际验证

| 检查 | 结果 | 边界 |
|---|---|---|
| 完整 pytest，不排除失败文件 | **528 passed, 1 skipped, 2 warnings，79.96s** | 外部依赖用测试替身；skip 为当前环境不能创建符号链接，不算已验证链接逃逸 |
| `npm test` | **44 passed** | 纯逻辑/传输/reducer 测试，无真实浏览器 |
| `npm run build` | **通过** | 当前产物 `index-Cgk3iksr.js`，317.37 kB |
| `npm run lint` | **通过** | oxlint，无输出告警 |
| `ruff check app servers scripts --select F821,F811,F841` | **通过** | 仅这三类规则；有 Ruff 配置迁移提示，不代表全部风格规则通过 |
| raw manifest | **11/11 文件存在且 SHA256 一致** | 3 条 license_status 仍 needs_review |
| normalized manifest | **11/11 文件存在且 SHA256 一致** | 文件齐全与正文完整性、检索质量是不同验收 |
| notices | **11 份文件** | 仍需完成标为 needs_review 的来源核对 |
| `docker info` | **Docker daemon 可用，29.8.2** | 当前情况已不同于旧报告中的 daemon 未启动 |
| `docker compose config --quiet` | **通过** | 仅配置解析，不是镜像/容器运行验收 |
| `docker compose ps --format json` | **本项目没有运行容器** | 本轮没有 build/up 或变更其他项目的容器 |

pytest 命令：

```powershell
.\.venv\Scripts\python.exe -m pytest tests -p no:cacheprovider -q -o addopts= --tb=short --show-capture=no --disable-warnings -rs
```

复现探针（已实际执行，均不访问外部服务）：

```powershell
.\.venv\Scripts\python.exe docs/claude-code-handoff/probes/rag_probe_2026_10_27.py
.\.venv\Scripts\python.exe docs/claude-code-handoff/probes/runtime_probe_2026_10_27.py
.\.venv\Scripts\python.exe docs/claude-code-handoff/probes/ui_eval_probe_2026_10_27.py
```

修复后应将这些反例转成业务回归测试，断言期望行为；不要删除探针或改 fixture 来保持现有结果。

## 3. 必须继续修复的行为

### A01 / P1：active revision 只更新了目录，检索仍会读旧版

- **位置**：`app/services/knowledge_store.py:539–548`；`app/services/knowledge_service.py:378–407`。
- **复现**：使用真实 KnowledgeStore 作业和 VectorStoreRetriever，同 source 先导入 OLD，再导入 NEW；两个 job 都 ready，active 指向 NEW，检索仍返回 `["OLD instructions", "NEW instructions"]`。
- **原因**：旧版本仅被元数据标 superseded；检索没有 active revision 可见性约束。保留旧向量用于回滚可以，但不能把它们作为当前知识候选。
- **验收**：更新、回滚、撤销、并发查询只得到对应时刻允许的 active revision；staging/failed/superseded 不进入候选、rerank、模型与引用。先约束候选范围，避免旧版占满 top-k 后再过滤导致错误 miss。

### A02 / P1：一批向量部分写入后失败，当前批残片漏回滚

- **位置**：`app/services/knowledge_store.py:480–499`。
- **复现**：fake add_documents 写入当前批第一行后抛错；job=failed，但真实检索适配器仍返回 `FAILED PARTIAL instructions`。
- **原因**：只有整批成功返回才 inserted.extend，失败清理仅使用 inserted，不包含当前批已写入的行。
- **验收**：以全部预生成候选 IDs 做幂等回滚，清理失败必须记录并可重试；每批/每行注入故障，新版本不可见，旧 active 仍可用。A01 的可见性隔离与本条清理都需要完成，不能互相替代。

### A03 / P1：strict_local 没有强制 provider，本地模式可触发远端 embedding

- **位置**：`app/services/vector_embedding_service.py:245–285`；`app/agent/aiops/runtime_tools.py:407`；诊断知识工具的全局检索器。
- **复现**：配置 provider=openai，并调用 create_embedding_service(strict_local=True)，用 fake factory 捕获到远程 provider 被选择；没有实际发 HTTP。两模式知识工具使用全局检索配置，local 运行可以走到此路径。
- **验收**：local 请求使用独立注入的本地检索器，或遇远程 provider 明确 unavailable；远端 factory/HTTP client 调用次数为 0。cloud 按自身配置独立工作，不改共享全局配置来切模式。

### A04 / P1：同名多实例可以被发现，却无法选定某实例

- **位置**：`web/src/view.ts:53–84`；`web/src/App.tsx:519–520`；`app/models/aiops.py:64–72`；`app/core/targets.py:129–150`。
- **复现**：输入 orders/i-a/r-a 与 orders/i-b/r-b，前端按 service 去重，仅留一张卡；点击后只发 `target_choice="orders"`，丢失实例/地域。后端按服务名再次匹配，仍 target_required；直接提交 i-a 又 not_found。
- **验收**：服务端签发稳定候选 ID 或完整可信目标键；UI 保留所有不同目标，提交后服务端重新校验资源目录并绑定准确实例。双模式 HTTP→UI→再次请求的流程均应结束歧义，不能只修去重键。

### A05 / P1：补全目标参数时向合法工具注入不接受的字段

- **位置**：`app/agent/aiops/runtime_tools.py:302–305`；`app/core/targets.py:231–236`。
- **复现**：绑定唯一 topic-a，合法调用 get_topic_info_by_name(topic_name="topic-a")。运行器补入 topic_id/topic，StructuredTool 报 `unexpected keyword argument 'topic_id'`。
- **验收**：仅补 schema 接受的参数，并在补全后做完整校验；合法 topic 成功、跨 topic/region 仍拒绝。不要为通过测试给实际工具任意增加 **kwargs。

### A06 / P1：证据数值、单位、目标与时间的核验仍可绕过

- **位置**：`app/core/claims.py:132–145、327–359`；`app/agent/aiops/quality.py:134、176–192`。
- **复现**：证据 latency.avg=12、unit=ms、target=svc-a，当前质量链仍接受以下文本为 supported：

```text
网络延迟 999ms
网络延迟 12%
网络延迟 12ms，svc-b在昨天的结果
```

- **原因**：时间单位数字被跳过，实际自由 claim 没有形成不可绕过的结构化观测约束。此前的中文 CPU99% 和单独 CPU→内存反例已经修复，本条是剩余的不同边界。
- **验收**：按结构化 metric/value/unit/target/time/原始字段定位核对；不能将所有 ms/秒数字当成查询时长忽略。数值观测由服务器渲染，不支持的自由描述降为假设或缺口，验收上述三例及正确对照。

### A07 / P1：已接入部分预算，日志参数和证据字节预算仍未生效

- **位置**：`app/agent/aiops/runtime_tools.py:228–270`；`app/core/budgets.py:310、466`。
- **复现**：设置 evidence_chars=10，工具仍收到 limit=999999，并保留约 200054 字符返回；预算快照 used=0、remaining=10。
- **验收**：调用前收敛参数与子调用上界；获取后预占/消费证据预算，超额数据采用受控原文存储与有界摘要；prompt 入模前再校验。测试实际 RunTools 与真实包装层，不只测试 BoundedLogParams/truncate helper。

### A08 / P1：Milvus 首次初始化失败后，同进程不会随依赖恢复

- **位置**：`app/services/vector_store_manager.py:141–143、187–192`。
- **复现**：fake connect 第一次失败、第二次本可成功；连续两次请求只调用 connect 一次，第二次直接重抛缓存的 VectorStoreUnavailable。
- **验收**：transient unavailable 在锁保护下有限退避重试并恢复；invalid_index 等永久不匹配保持明确状态。依赖先停后启，同进程无需手工 reset/重启；并发恢复不重复初始化。

## 4. 其他仍需收尾的功能

### A09 / P2：历史分页和前端续读仍不能保证读到终态

- `app/api/runs.py:216–253`：after_seq=1、limit=2，连续两条无 seq 的 evidence/tool_step 返回 truncated=true、next_after_seq=null，后面仍有状态与终态。
- `web/src/api.ts:246`、`App.tsx:229、314`：getRun 不传续页参数，打开历史只请求一次；断流后刷新列表，没有恢复当前 run 的事件。
- **验收**：所有持久事件统一单调序号，或采用独立位置游标；任意小分页无遗漏/重复读到终态；前端消费全部页，恢复只读已有 run，不能自动重发诊断 POST。

### A10 / P2：评测新格式无法被列表 API 读取

- `scripts/run_eval.py:287` 输出 `{manifest, rows}`；`app/api/runs.py:267–271` 仍只接受 list，跳过 dict。
- 临时目录放一份新格式、1 行成功结果，实际 endpoint 函数返回 `{"results":[]}`。
- **验收**：兼容旧 list 与新对象格式，返回 rows 和 manifest；面板可看结果与条件。测试覆盖真实生成器产物→API→前端，不只分别测各自数据模型。

### A11 / P2：在线取消传播了协程取消，但 runtime 标记与审计不一致

- `app/agent/aiops/cloud_multi.py:540–550` 对比本地取消清理：两专科工具进入等待后 aclose，runtime.cancelled 仍 false。
- **验收**：统一云端 runtime 清理、取消标记、审计与后续工具禁止调用。当前探针证明的是标记/清理不完整，**不是协程取消完全失效的证据**；同步 SDK/远端任务是否终止仍需单独记录真实边界。

### A12 / P2：模式恢复、FE-09 与能力探针仍未全部完成

- `App.tsx:501` 刷新按钮仍只在 modeError 出现；正常 configured=false 无刷新入口，和实施文档的描述不同。
- 模型/MCP readiness 仍不完整；health 中 ready 主要由配置形态推断，不能当作端点连接成功。
- IME 组合态判断已加入，不重复报旧缺陷；aria-live、≤1180px inspector 替代入口仍缺，真实 IME/读屏与当前构建的浏览器验收待补。
- 旧 BROWSER_CHECK 使用早期构建。后续已经修过计数、失败 revision 激活、IME 等，旧失败截图不能直接当作当前失败；旧成功也不能代替当前构建的完整验收。

### A13 / P2：部署和完整能力的剩余边界

- Dockerfile 已冻结安装、移除 npm fallback；entrypoint 已轮询/监督；脚本 LF 正常，Compose 端口与探针一致，持久卷已补齐。不要继续把这些列成未实施。
- `docker-compose.yml:182` 仍以 Milvus healthy 作为 app 冷启动硬依赖，与 entrypoint 的软依赖降级策略不一致。需明确“允许诊断先启动、RAG 后恢复”还是全栈强依赖，并按预期测试。
- 本轮 Docker daemon 已可用，但项目容器未运行；未完成镜像构建、信号传播、卷重建恢复或真实 Milvus 入库验证。
- 当前 eval/rag 只有 14 条 dev，无独立 blind；阈值在 dev 上拟合，满分不能作为盲测泛化结果。
- parent 元数据已存在，回答仍使用 child，完整代码/前提 parent 回填未闭环；DK-03 正文提取仍很短，需核对而非只验文件哈希。
- chat 历史已持久化恢复，但 `api/chat.py:45` 明确不把历史用于回答生成。可宣称“历史恢复”；多轮省略追问理解需要另接有界上下文并验证。
- retention、abandoned 恢复、真实用户/资源归属，以及知识依据对具体观测的交叉引用，仍属未完成项。共享会话 ID 校验不等于完整用户权限体系。

## 5. 可确认的进展与文档修订

本轮已经确认的修复包括：P0-01/02/03 原始缺陷、SSH/配置污染、CLS 原包装层错误来源透传、本地质量进入终态 trace、前端 state/sse/KB 接线、结构化引用事件、知识 job/revision API、历史文件存储、实际 descriptor 校验入口和 11 页快照规范化。

因此不建议回退本轮实现。按 A01–A08 先补核心边界，再补 A09–A13 的功能/真实环境验收，最后重跑固定快照的全套检查。

`IMPLEMENTATION_STATUS.md` 需要据行为收窄：RAG-03/07/08、BE-01/03/04/06/07、FE-08 等不能只因入口接上或单测存在而标完整；关于持久卷的“未做”和“已有”互相矛盾，当前实际上已有。`VALIDATION_RESULTS.md` 保留既有真实记录，再追加本轮反例及修复后的结果，不覆盖历史失败证据。

只有上述反例都不再出现，并补齐用户选择的真实 Milvus/双模式端到端/当前浏览器任务旅程与部署验收后，才能按原始完整范围宣告完成。若明确缩小为离线演示版本，应在 README/简历中保留其边界。
