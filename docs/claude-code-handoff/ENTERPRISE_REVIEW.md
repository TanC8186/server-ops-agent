# 企业工程审查与整改清单

审查日期：2026-10-26。审查对象：当前工作区的 On-Call Agent，包括尚未提交的本地/在线双模式实现。基线提交为 `fca7f1c`，不能仅 checkout 该提交来复现当前行为。配套文件：

- [Claude Code 执行任务书](CLAUDE_CODE_TASK.md)
- [知识库来源与入库实施规格](RAG_CORPUS_PLAN.md)
- [25 个官方手册页面目录](knowledge_sources.json)
- [代码审查快照与文件哈希](review_snapshot.json)

## 1. 结论与适用边界

项目已经具备值得继续建设的结构：前端模式选择、本地小模型生成计划、受限工具执行、在线 Supervisor/专科 Agent/Reviewer 图编排、诊断事件流与运行记录，以及独立的 Milvus 知识问答入口。但当前实现仍是原型；在线完整路由存在确定的运行阻断，知识问答也尚未形成“检索状态明确、答案引用可核对、版本适用可判断”的闭环。

建议把求职项目主线定为：**面向有限显存设备的双模式运维诊断系统，具备受约束的工具执行、证据核验和版本化知识检索**。多智能体数量、向量库规模和框架数量都不能替代可运行、可解释、可复现的任务闭环。

本轮只审查与编写交接材料，没有修改业务代码、向实际服务器发出诊断操作、调用付费模型完成真实运行，也没有下载整套手册或写入 Milvus。前端发现以源码检查、状态函数和流解析反例为依据；未进行新的完整浏览器可用性/无障碍审计。涉及潜在布局问题的条目明确列为待浏览器验证。

此前单元测试记录为 105 passed，并通过了当时的前端 build/lint 等检查；这些结果不代表完整在线流程可用。本轮在实际运行器边界使用假模型/工具复现了 `run_id` 冲突，说明测试替换层级过高，漏掉了真实服务与事件协议的交互。Claude Code 必须先补反例，再修改代码。

优先级含义：

| 优先级 | 含义 | 执行顺序 |
|---|---|---|
| P0 | 阻断主流程、造成数据损失或明确的访问边界漏洞 | 第一批全部修复，才继续扩展 |
| P1 | 会误导操作判断、破坏状态一致性或使核心功能不可靠 | 完成简历演示闭环前修复 |
| P2 | 可维护性、体验、扩展或部署改进 | 在功能闭环后按价值推进 |

不重复列为缺陷的已有改进包括：诊断 SSE 的分片处理、按准确 run_id 回填与防过期响应、本地诊断的云模型隔离、聊天卸载时中止请求、Markdown 危险 URL 协议过滤。后续修复需要保留这些行为。

## 2. 首先处理的四个阻断问题

### P0-01 在线多智能体在外层运行器写 trace 时中断

- **位置**：`app/services/diagnostic_runtime.py:53–54`；`app/agent/aiops/cloud_multi.py:397–400`；测试盲区在 `tests/test_execution_modes_api.py:43–46`。
- **触发与证据**：云端生成器会给每个事件补上 `run_id`。外层对 `agent_status` 调用 `trace_event(run_id, "agent_status", **event_fields)`，只剔除了 `type`，因此再次传入同名参数。用假模型/工具驱动真实运行器，得到 `status: run_start` 后出现 `trace_event() got multiple values for argument 'run_id'`。这不依赖真实云 API。
- **影响**：直接调用云图的测试可以通过，实际 HTTP 服务使用的外层运行链路却不能正常完成。
- **修改要求**：定义统一事件信封，明确 `run_id/type/execution_mode` 等保留字段的所有权；trace 接收规范化事件，禁止将任意事件字段直接展开到带同名参数的函数中。不要只对这一处随手删除字段而让其他事件继续各自组装。
- **验收**：FastAPI → 真实 service → 真实 diagnostic runtime → 真实 cloud runner；仅替换模型和外部工具。必须依次产生可解释的状态、证据、报告和唯一终态，trace 可回放。模拟 Reviewer 失败也应返回明确的降级结果，不出现该 TypeError。

### P0-02 仅在 .env 配置 API_KEY 时，鉴权可能未生效

- **位置**：`app/core/auth.py:11–16`；`app/config.py:18–23`；`servers/web_server.py:20–25`。
- **触发与证据**：鉴权直接读取 `os.getenv("API_KEY", "")`；Settings 通过 env_file 读取配置不会自动把值导出到进程环境，且当前 Settings 未统一承载该字段。独立临时 .env 反例显示：文件里配置了 key，鉴权仍读到空字符串并放行。Web 服务监听 `0.0.0.0` 会放大部署误判。
- **影响**：使用者以为已开启的访问控制实际可能不存在。共享 key 也不等于有用户、租户与资源权限体系。
- **修改要求**：配置入口统一到 Settings；显式区分仅限本机的开发演示与启用鉴权的部署配置；生产配置缺失密钥时启动报错或 readiness 不通过；比较密钥使用恒定时间比较。不要在日志或前端返回密钥。
- **验收**：独立子进程仅由 env_file 注入配置，无密钥/错密钥请求均 401，正确密钥通过；显式开发模式行为有测试与文档。运行记录、知识文档与诊断入口采用同一权限入口。

### P0-03 Windows 下 run_id 可越界读取 traces 目录外的 JSONL

- **位置**：`app/api/runs.py:62–67`。
- **触发与证据**：直接拼接 `TRACE_DIR / f"{run_id}.jsonl"`。在临时目录中使用 `/api/runs/..%5Coutside`，可返回上级目录的测试 `outside.jsonl`，HTTP 200。审查没有读取工作区外的真实业务文件。
- **影响**：在当前 Windows 开发/部署环境构成明确的目录边界绕过。
- **修改要求**：按项目实际生成格式限定 run_id，例如 32 位小写十六进制；再做 resolve 后的目录包含校验；访问前检查资源归属。不存在和无权访问的响应避免泄露不必要信息。
- **验收**：合法 ID 可读；URL 编码的反斜杠、斜杠、点段、绝对路径、盘符及 UNC 形式全部拒绝；测试在 Windows 路径语义下执行。仅修复正斜杠不算完成。

### P0-04 同名知识文件上传/重建失败会破坏旧版本

- **位置**：`app/api/file.py:54–64`；`app/services/vector_index_service.py:154–164`；`app/services/vector_store_manager.py:77–91`。
- **触发与证据**：上传先 unlink 旧文件，再读取与检查大小；超限或后续失败时旧文件已消失。重建索引读取文本后先删旧向量，再切分/embedding/插入；这些后续步骤失败会让旧知识不可用。删除异常被转成 0 条又继续插入，还可能产生重复内容。
- **影响**：一次失败更新足以丢失已可用知识，且文件状态与向量状态可能不一致。
- **修改要求**：临时文件验证 → 新 revision 在 staging 构建 → 验证数量/描述符 → 激活 revision → 延后清理旧版本；按 document_id 做互斥和幂等。不要假定文件系统与 Milvus 之间有跨系统事务，使用显式作业状态与可恢复流程。
- **验收**：超大文件、空文件、编码失败、embedding 失败、插入部分失败和并发同名上传，都不能破坏当前 active revision；相同内容重复导入不会增殖；删除失败可见且可重试。

## 3. 前端交互与用户任务闭环

### FE-01 / P1 计划步骤的身份不一致，完成计数和勾选可能错误

- **位置**：`web/src/App.tsx:516、573`；`app/agent/aiops/runtime_tools.py:174–182`。
- **问题与证据**：计划行按描述判断完成，执行事件的 `step` 却是工具名；发现服务也被计为执行步骤；同工具多次调用会折叠。状态函数反例出现“计划 1 步、完成 2 步、计划行未勾选”。失败调用也不能简单按完成数当成功数。
- **修改**：计划步骤使用稳定 `step_id`，每次工具执行另有 `tool_call_id`；发现目标独立展示。步骤状态至少区分 pending/running/succeeded/failed/skipped；允许一个步骤有多次工具调用。
- **验收**：同工具不同参数执行两次、重试、发现目标、失败、跳过、并行完成乱序，均得到准确行状态与成功/失败计数。

### FE-02 / P1 在线实时计划与历史回放采用不同协议

- **位置**：`web/src/App.tsx:184、513、551`；`app/agent/aiops/cloud_multi.py:287–291`。
- **问题与证据**：实时使用 `plan`，trace 使用 `agent_plan` 与 assignments；结束后用回放整体替换实时事件，却没有完整归一化。回放反例出现 planned=0、completed=1，用户刚看到的计划消失。
- **修改**：实时流与 trace 共用一份 schema、事件 ID 与 reducer；按序号合并去重；保留专科分支身份，不能用覆盖数组掩盖缺失。
- **验收**：实时视图、结束回填、刷新后的历史页，在计划、证据、状态和报告上相同；重复与乱序事件不改变最终结果。

### FE-03 / P1 模式不可用时缺少恢复入口，配置标志不能表示可运行

- **位置**：`web/src/App.tsx:95、97、344–345`。
- **问题**：`configured=false` 时提示刷新配置，但刷新按钮仅在请求报错时展示。服务端完成配置并按需重启后，前端没有直接重新检查的入口。配置存在也不能证明本地模型、MCP 或云 API 可用。
- **修改**：模式卡始终提供刷新/重试；显示配置状态与按能力检查结果、最近检查时间和可操作原因；保留用户草稿。诊断仅依赖选中模式必要能力，知识库故障不应自动禁用所有诊断。
- **验收**：未配置→配置后刷新、模型离线→恢复、MCP 缺失、Milvus 单独故障均有正确状态与下一步；切换模式/刷新不清空输入。

### FE-04 / P1 知识聊天网络错误和 SSE 解析存在状态悬挂

- **位置**：`web/src/chat.ts:25、40、44、68、80–84`。
- **问题与证据**：fetch 在 try 外，网络拒绝不会调用 onError，busy 可能不清除。聊天仍使用独立的旧 SSE 解析；包含中文内容的 CRLF 逐字节分片反例丢失内容，缺失 done 的 EOF 被当成成功，读取器没有完整释放/取消。该反例证明分隔处理问题，不能据此认定 TextDecoder 的 UTF-8 解码有缺陷。
- **修改**：复用已加固的诊断流传输层，按业务设置终态；从 fetch 到 reader 全生命周期 try/catch/finally；仅显式终态算完成；异常 EOF 标为中断且保留可重试内容；回调只终结一次。
- **验收**：网络拒绝、HTTP 非 2xx、跨分片 CRLF/UTF-8、多行 data、服务端错误、缺失 done、主动中止，都不会永久 busy、丢失已接收内容或重复完成。正常与异常出口都释放 reader。

### FE-05 / P1 聊天会话、停止与历史恢复缺少统一状态机

- **位置**：`web/src/ChatPanel.tsx:19、45、57、61、140`；`web/src/chat.ts:81`；`web/src/App.tsx:312`。
- **问题**：停止后可能残留 streaming 光标；旧请求回调可能改写新请求状态。切换视图会丢失前端历史，后端同 session 的 MemorySaver 却可能保留上下文，造成“用户看不到、模型仍记得”的状态差异。卸载 abort 已有，不要把它重复作为新功能。
- **修改**：turn/request ID 与 idle/streaming/completed/cancelled/failed 状态机；只允许当前请求更新状态。前后端明确会话保留/清空语义，支持恢复历史或同时清空服务端。
- **验收**：快速发送/停止/再发送、切换页签、卸载返回、刷新恢复均无串消息；取消标志、光标和输入状态一致。

### FE-06 / P1 前端缺少知识库管理与索引状态

- **位置**：`web/src/api.ts:102`；`web/src/ChatPanel.tsx:89`；`app/api/file.py:21、100`。
- **问题**：后端有上传/目录索引入口，前端没有贯通文档列表、上传、索引作业、失败重试、版本与撤销流程。用户不知道答案究竟基于哪些已生效文件。
- **修改**：增加知识库面板：文档来源/版本/适用系统、作业 uploaded/indexing/ready/failed、进度与错误摘要、当前生效 revision、重试与撤销。上传成功与索引成功分开。
- **验收**：从空库开始，经上传→索引→ready→问答→新版本替换→撤销，界面可完整操作；失败条目不能被标成可检索。

### FE-07 / P1 答案没有可核验引用，命中片段事件没有贯通

- **位置**：`web/src/ChatPanel.tsx:62、99`；`web/src/chat.ts:74`；相关服务见 RAG-02。
- **问题**：前端忽略 search_results，后端也未稳定发出该事件。文件名文本无法定位到版本、章节、原文片段，也无法约束模型虚构引用。
- **修改**：展示“已检索/未命中/知识库不可用”及引用卡片；点击可查看授权片段、标题、版本、来源 URL/文档页面和定位。服务器为命中分配引用 ID，按本轮授权集合校验。
- **验收**：每个可点击引用都能定位本轮真实命中；不存在/撤销/未授权 ID 不能显示成有效引用；回答中的推测与手册事实分开。

### FE-08 / P1 运行历史恢复和局部加载故障相互耦合

- **位置**：`web/src/App.tsx:109、165、192`；`web/src/api.ts:104`。
- **问题**：运行列表和评测 Promise.all，其中一个失败会阻断另一个刷新；已有 run_id 在断线后未用于恢复；刷新丢失选中运行与草稿。准确 run_id 回填和防过期响应已有，需要继续保留。
- **修改**：独立加载边界；URL 保存选中 run_id，草稿本地保存；按 run_id 查询状态并回放/续读事件。自动恢复只读已有运行，不能自动重发 POST 产生重复诊断或费用。
- **验收**：评测接口失败不阻断历史；断网/恢复、刷新、打开历史链接不产生新任务；分页和空列表状态明确。

### FE-09 / P2 中文输入、滚动、响应式和可访问性需要专项验收

- **位置**：`web/src/ChatPanel.tsx:127`；`web/src/App.tsx:132、301、317、420`；`web/src/styles.css:369、433、535`；`web/src/markdown.ts:65`。
- **问题**：Enter 未检查 IME composing，有提前发送风险；诊断自动滚动可能打断读旧内容，聊天流更新又缺乏一致跟随策略；错误/状态缺少系统化 live region。≤1180px 隐藏 inspector 无替代入口，表格/图片溢出需浏览器确认。这些静态发现不能冒充已完成的浏览器复现。
- **修改**：composing 时不提交；仅在接近底部时跟随并提供“有新消息”；低频播报状态/错误，尊重 reduced-motion；窄屏改用抽屉/折叠证据区，表格局部滚动、图片限宽。
- **验收**：375/768/1024/1440 宽度、中文 IME、键盘、长 Markdown 与长表格、暂停自动滚动、屏幕阅读器提示均有实际操作记录。

## 4. 后端执行、证据与服务运行

### BE-01 / P1 引用真实证据 ID 仍可附带错误数值或根因结论

- **位置**：`app/agent/aiops/cloud_multi.py:136–158、232–244、323–336`。
- **问题与证据**：校验主要确认 ID 存在、ok=true、来源类型。反例中 E1 为 CPU=12% 的真实来源证据，模型写“CPU=99%，OOM 根因已确认”并引用 E1，仍通过当前 supported 校验。Prompt 中要求谨慎不能替代数据约束。
- **修改**：观测采用 metric/value/unit/time/target/json_pointer 等结构化字段并验证到原始 evidence；确定数值由服务器渲染；自由因果解释放 hypothesis，注明缺口。对无法机械校验的语义，保留审阅边界，不能宣称自动验证所有事实。
- **验收**：错数值、错指标、错目标、过期时间窗、demo/失败证据、只相关不因果等反例均被降级；正确事实保持可引用。

### BE-02 / P1 CLS 上层包装丢失底层错误与来源

- **位置**：`servers/cls_server.py:530–579、617–650`；`app/agent/aiops/runtime_tools.py:80–104、163–169`。
- **问题与证据**：假 search_log 返回 AccessDenied/unavailable 时，上层可包装为 total=0、logs=[]、“成功查询 0 条”；runtime 未识别为错误且来源为空。demo 标记也可能在包装时丢失。
- **修改**：工具统一 envelope：status、source_kind、target、time_range、data、error、subqueries；保留部分成功和每个子查询来源。空结果、查询失败、权限不足、demo 必须分开，日志分析不能将失败当成“错误数 0”。
- **验收**：权限失败、超时、有效零命中、部分失败、demo、真实成功六类贯通工具→证据→Reviewer→UI。

### BE-03 / P1 未指定目标时，运行器会选服务列表第一项

- **位置**：`app/agent/aiops/runtime_tools.py:186–207`；`app/services/aiops_service.py:237–238`。
- **问题与证据**：泛化症状、没有 service_name、候选 svc-a/svc-b 时自动选 svc-a。用户输入含糊不等于授权检查任意第一台服务。
- **修改**：服务发现返回候选；多个候选进入 target_required，让 UI 选择或补充；明确唯一候选才允许自动绑定。批量诊断必须显式选择并单独预算。当前输入不是完整告警接入，不要将其宣传为已有告警平台联动。
- **验收**：0/1/多候选、未知服务、过期候选均有确定行为；多候选时在绑定前不调用目标数据工具。

### BE-04 / P1 目标绑定没有覆盖 region/topic/SSH 等完整资源范围

- **位置**：`app/agent/aiops/runtime_tools.py:149–169`；`servers/cls_server.py:160–175`。
- **问题与证据**：主要校验 service_name/instance_id。绑定 svc-a 后请求其他 region 的 svc-b-log 仍可通过，证据却标成 svc-a；可选参数缺省也可能绕过。SSH 目前仅是独立配置端点，不能当作目标服务同一机器。
- **修改**：用可信资源目录绑定 service→instance→region→topic→SSH；服务端填参数并拒绝越界覆盖。未绑定 SSH 的证据标为独立范围，不支持目标服务确定性结论。
- **验收**：跨 service/instance/region/topic、缺省参数、伪造 endpoint 均被约束；证据目标来自实际查询资源而非用户标签。

### BE-05 / P1 任务结束、诊断成功、证据充分被混为一谈

- **位置**：`app/agent/aiops/local_runtime.py:74–79`；`app/services/diagnostic_runtime.py:42–49`；`app/api/runs.py:35–56`；`app/agent/aiops/cloud_multi.py:208–225`。
- **问题**：本地工具全失败仍可生成 complete；只有 run_start 的 trace 可能被 /runs 标成 ok；在线专科也可零工具结束。报告生成不等于收集到足够证据，agent_plan 还未计入历史计划。
- **修改**：运行生命周期与诊断结果分开。生命周期 queued/running/completed/failed/cancelled/abandoned；结果 sufficient/partial/insufficient。服务端根据成功观测、错误、范围与时间窗计算质量字段，模型不能自行宣布流程成功。
- **验收**：零证据、全失败、部分成功、正常、取消、进程中断六类在实时/历史/报告中一致；中断记录不显示成功。只有服务发现、时间、demo、知识依据或 unsupported 说明，也不能算当前诊断证据充分。

### BE-06 / P1 缺少跨请求的资源与上下文边界

- **位置**：`app/agent/aiops/cloud_multi.py:110–121、210–215、331–332、381–389`；`app/agent/aiops/runtime_tools.py:138–157`；`servers/cls_server.py:498–505`。
- **问题**：单图 max_concurrency=2 不限制 N 个请求的 2N 上游调用；日志参数缺少充分上界，原始大证据进入模型，逻辑工具预算未覆盖子 API 请求。可能耗尽内存、线程、API 配额和上下文。
- **修改**：全局 admission/有界队列、按 provider 的并发限制、日志条数/字节上限、请求时限、上下文预算、子调用预算账本与费用记录。原始证据独立保存，提示词仅传有界抽取与定位。限制 worker 数量，与 8GB 本机部署相容。
- **验收**：超并发受控排队或 429/503；大日志不会无限入队/入 prompt；预算耗尽可解释且仍能返回部分报告；记录峰值资源与实际子调用数。

### BE-07 / P1 超时/取消等待不代表同步 SDK 或远端操作已终止

- **位置**：`app/agent/aiops/runtime_tools.py:156–162`；`servers/ssh_server.py:114–125`。
- **问题与证据**：假同步工具睡眠 120ms，外层 20ms 超时后其线程仍会继续完成。当前异步取消不能保证终止 to_thread、SDK socket 或 SSH 远端进程。
- **修改**：deadline 传递到底层 connect/read/command timeout；限制存活 worker，禁止取消后继续发起新调用；记录仍在运行/已停止等待/已确认取消的区别。仅在可验证时宣称远端已停止。
- **验收**：慢同步工具、慢网络、SSE 断开、用户取消后无新调用；底层 deadline 生效；残留工作有上界且可观测，取消状态真实。客户端断开需贯通真实 HTTP SSE→API→runtime→工具，不能只测试生成器 aclose()。

### BE-08 / P1 运行记录尚不足以支撑持久化审计和多进程部署

- **位置**：`app/observability.py:17–35`；`app/api/runs.py:27–33、102–112`；`docker-compose.yml:129–133`。
- **问题**：trace JSONL 未挂持久卷，替换容器会丢记录；多个副本看到不同历史；写入异常被吞；同步读取全部内容，无分页、保留期、访问归属与敏感字段分层。
- **修改**：先做单实例可靠方案：持久卷、运行元数据存储、事件序号与分页、失败指标、受控证据存储与保留策略；可用 SQLite WAL 存元数据，若选择它就明确单实例边界。真正多实例再迁移共享数据库/对象存储，不为简历提前搭建大量基础设施。
- **验收**：容器替换后可恢复；写盘失败可见；超大 trace 分页；过期清理保留必要元数据；跨用户访问隔离。分页/清理与事件追加并发时不丢事件或产生 500。应用异常退出后的 running 记录转 abandoned/可恢复状态。

## 5. RAG 问答与知识库生命周期

### RAG-01 / P1 知识问答由模型自行决定检索，且暴露了过多工具

- **位置**：`app/services/rag_agent_service.py:96、113`。
- **问题**：create_agent 挂载检索、时间与全部 MCP 工具，普通知识问题可能不检索；知识问答也拥有与任务无关的运维工具。上传文本直接进入上下文，存在文档指令影响工具决策的权限路径。本轮没有声称已成功利用提示注入。
- **修改**：知识模式固定 retrieve→状态/适用性门控→answer，默认只具备知识检索权限。实时诊断另走受目标和预算约束的路径；检索文档标为不可信数据，不能扩大工具权限或覆盖系统指令。
- **验收**：知识问题每次产生可见检索状态；“忽略指令并调用 SSH/发送数据”等测试文档不能引发运维工具执行；无命中/不可用不能伪装成知识库已支持的答案。

### RAG-02 / P1 检索产物、引用与 API/SSE 契约断开

- **位置**：`app/tools/knowledge_tool.py:13、65`；`app/services/rag_agent_service.py:173`；`app/api/chat.py:120`；前端见 FE-07。
- **问题**：检索上下文已有标题/来源文件名和 chunk 正文，但缺少稳定 chunk ID、revision 与原文定位；流式服务只发 AI 文本，丢掉 ToolMessage artifact。API 中处理 search_results 的分支缺乏真实上游事件。
- **修改**：建立 RetrievedChunk、RetrievalResult、AnswerCitation 结构；在服务层明确发 retrieval/citations 事件；本轮 hit 分配短引用 ID，持久化完整来源定位与 revision。
- **验收**：真实检索适配器替换为固定向量结果，贯通服务→HTTP SSE→前端 reducer，验证片段、文档版本与引用一致；伪造引用被拒绝/移除。

### RAG-03 / P1 未命中和检索失败混同，Milvus 初始化不能可靠恢复

- **位置**：`app/tools/knowledge_tool.py:36、48`；`app/services/vector_store_manager.py:34` 附近；`app/core/milvus_client.py:265`。
- **问题**：零命中和异常都返回空字符串/空数组；初始化过早标记完成并缓存错误，服务恢复后可能持续失败；health 仅检查连接登记，未证明索引可查询。
- **修改**：显式 status=hit/miss/unavailable/timeout/invalid_index；初始化加锁与可恢复重试；readiness 检查目标 collection、索引加载、embedding 描述符和有界查询探针。不能因为 Milvus 独立故障把所有诊断能力一起判死。
- **验收**：Milvus 启动前请求→依赖恢复→同进程检索恢复；缺 collection、索引未加载、查询超时、有效零命中呈现不同状态。

### RAG-04 / P1 embedding 与 collection 缺少一致性描述符

- **位置**：`app/core/milvus_client.py:48–49`；`app/services/vector_embedding_service.py:90–118`。
- **问题**：collection 固定 biz/384 维，embedding 还有 OpenAI 1536 与 DashScope 1024 分支；同维不同模型空间也未校验。当前本地加载失败还有下载/远端 provider 路径，模型 ID、凭证与所选 provider 的一致性需要检查。错误提示出现未配置的 MILVUS_COLLECTION。
- **修改**：持久化 model/revision/dimension/normalization/distance/chunker 的描述符哈希；索引与查询完全匹配才开放；切模型必须显式重建新 revision/collection。本地知识模式严格 fail closed，预下载与运行分离，embedding 凭证独立配置。
- **验收**：不同维度、同维不同模型、不同规范化策略均拒绝混用；离线断网可启动已预置模型；本地模式不触发远端 embedding；不得恢复零向量“兜底”。

### RAG-05 / P1 按字符切分不保证模型 token 上限，代码结构可能损坏

- **位置**：`app/services/document_splitter_service.py:33、36、70–74`；`tests/test_chunking.py:31` 附近。
- **问题与证据**：180 字符不等于 128 tokens。当前 multilingual MiniLM 的官方 max_seq_length 为 128；已有检查仅覆盖有限文档且依赖 tokenizer 可用。当前依赖中的 MarkdownHeaderTextSplitter 会处理行首空白，可能破坏 YAML/Python 缩进；字符递归切分还可能分断命令和 fence。并非声称所有现有 chunk 都已超限。
- **修改**：按真实 tokenizer 校验子块预算，保留标题路径与原文 offset；parent/child 结构保留完整代码块和注意事项。超长代码保存父块及可检索摘要/子段，答案引用能返回完整上下文；不要盲目调大模型最大长度。
- **验收**：中英混排、长 URL、表格、YAML、带续行命令、长代码块的 tokens 与原文重构/缩进检查通过。与 [官方配置](https://huggingface.co/sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2/blob/main/sentence_bert_config.json) 对齐。

### RAG-06 / P1 dense top-3 没有适用性门控，难以拒答无关问题

- **位置**：`app/tools/knowledge_tool.py:30`。
- **问题**：k 由 config.rag_top_k 控制，默认 3，缺相关性门槛、OS/版本/组件过滤；非空库容易返回“最不相关中的前几项”。命令、错误码、配置项也可能需要关键词检索。未做评测前不能声称 hybrid/rerank 一定提高质量。
- **修改**：先建立人工核验查询集和当前 dense baseline；先做权限/系统/版本过滤，再比较 BM25+dense/RRF、候选重排和小模型查询改写；按验证集校准 no-answer 门控，保留命中原始分数与决策原因。
- **验收**：相关问题、跨系统/版本冲突、关键词命令、无答案、模糊问题分别报告结果；新方案在隔离盲测中有证据再启用。CPU 基线优先，不能默认增加大 GPU reranker。

### RAG-07 / P1 上传阻塞与假成功，缺少可恢复索引作业

- **位置**：`app/api/file.py:60、73、75–89、117、135`。
- **问题**：先完整 read 再判断 10MB；异步路由中同步索引会阻塞；索引失败仍返回 HTTP 200 的成功消息；目录参数的 HTTP 400 会被宽泛异常捕获为 500。
- **修改**：有界流式写入与格式验证；返回 202+job_id 或明确 indexed=false，后台有界 worker 构建索引并持久化作业状态。短期用受限线程卸载可作为过渡，但不等于完整任务队列。保留 HTTPException 原语义。
- **验收**：并发上传不阻塞普通健康/状态请求；超大/空/不支持格式明确拒绝；索引失败可查询并重试；进程重启后作业可恢复/标记中断。

### RAG-08 / P1 文档身份、来源版本和访问范围不足

- **位置**：`app/services/vector_store_manager.py:15`；`app/services/document_splitter_service.py:70–74`；`app/api/file.py:54`。
- **问题**：单 collection、文件名覆盖，主要元数据为 source/extension/file/h1/h2；缺 document_id、revision、来源 URL、OS/产品/版本、有效期、撤销状态与服务范围。共享 API key 不提供租户身份，不能由前端传 tenant_id 就宣布隔离。
- **修改**：使用 corpus 方案中的最小元数据契约；先诚实声明单用户演示边界。企业部署时 principal/tenant 由服务端鉴权推导，在写入/检索/更新/撤销与片段回看中一致过滤。旧版本撤销后不得继续在新问答中引用。
- **验收**：同文件名不同文档、同文档不同版本、撤销、版本不适用、跨用户读写删除测试通过；单机演示没有用户体系时不能在 README 写成已实现多租户。

### RAG-09 / P1 会话历史 API 使用不匹配，长期上下文缺少边界

- **位置**：`app/services/rag_agent_service.py:32、203、207–210、227–229`。
- **问题与证据**：当前安装版本的 checkpointer.get 返回 dict，代码按 tuple[0] 读取，异常后吞掉并返回空历史；trim 函数未接入真实链路。MemorySaver 无持久化/TTL；历史 API 转换将 ToolMessage 等粗略映射成 assistant，丢失 tool 角色和调用关联信息。这不是内部图的消息已损坏的证据，后续裁剪也须保留工具配对。
- **修改**：按锁定依赖版本使用真实 checkpoint API；保留角色和 tool_call 配对；按 token 预算压缩与裁剪；定义会话所有权、TTL、重启后恢复策略。单机可先做元数据/历史持久化，无需立刻引入复杂 memory 服务。
- **验收**：多轮检索、工具消息、超长会话、重启恢复、会话清空、两 session 隔离；错误可观测，不用“空历史”掩盖所有后端异常。

### RAG-10 / P1 新双模式诊断未接入 Milvus 知识检索，追问也未绑定运行证据

- **位置**：`app/agent/aiops/runtime_tools.py:20、216`；`servers/monitor_server.py:830–849`；`web/src/chat.ts:28`。
- **问题**：新双模式诊断工具白名单没有 Milvus 知识检索；旧 planner.py 曾调用 retrieve_knowledge，不应把旧链路也概括为完全没有检索。search_historical_tickets 当前不等于接入真实工单或知识库；知识聊天请求只有 session/question，没有 run_id。同 session 名称不能自动把独立图的报告与 evidence 关联起来。
- **修改**：增加受权限/版本约束的 retrieve_knowledge，输出 knowledge 类证据；实时监控/日志保留 observation 类证据。知识只支持解释/检查建议，不证明当前主机状态。追问显式带 run_id，由服务端校验归属并加载受控报告/证据摘要；本地模式检索使用本地/CPU embedding。
- **验收**：同一故障问题可引用手册解释并分别列出当前观测和知识依据；追问引用正确运行，跨 run/用户不串线；只检索到手册不能写“已确认此服务器发生故障”。

## 6. 部署与可维护性

### OPS-01 / P1 健康检查没有按能力分层，MCP 子进程缺少就绪与退出处理

- **位置**：`app/api/health.py:29–53`；`Dockerfile:37–38`；`docker-entrypoint.sh:25–41`。
- **问题**：统一健康依赖 Milvus，但不充分覆盖模型与 MCP；entrypoint 启动多个后台子进程后固定 sleep 3 秒，再 exec Web，不保证子服务就绪，也未定义子进程退出后的处理。
- **修改**：区分 liveness、各能力 readiness 与依赖详情；启动探针替代固定等待。拆为独立 Compose 服务并带 healthcheck，或明确使用进程管理器/监督逻辑；二者择一，不叠加不必要复杂度。
- **验收**：Milvus 独立中断只影响 RAG；本地模型/MCP 失败准确反映对应能力；子进程退出能被检测和恢复/重启，SIGTERM 正确传播。探针短超时、缓存且限频，不通过付费推理、完整诊断或 SSH 操作探测健康。

### OPS-02 / P2 容器构建没有严格消费已有锁文件

- **位置**：`Dockerfile` 的 Python 安装步骤和前端 npm fallback。
- **问题**：复制 uv.lock 后仍用 editable install/宽版本依赖解析，生产镜像安装 dev 依赖；npm ci 失败后退回 npm install 会掩盖锁文件不一致。当前只是源码检查，未重新完成镜像构建验证。
- **修改**：使用锁文件驱动且冻结的生产安装，开发/运行依赖分离；npm ci 失败即失败并修复 lock；记录 Python/Node/CUDA/量化后端版本。按实际运行模式拆可选依赖，避免所有模式都背负训练依赖。
- **验收**：干净环境冻结安装、前端构建、镜像启动探针、核心路由集成冒烟通过；构建不修改锁文件。

### OPS-03 / P2 演示与评测指标必须能追到原始任务和证据

- **位置**：前端评测面板、`docs/PROJECT_REVIEW_2026-10-26.md` 所述训练/评测边界。
- **问题**：当前测试通过数、计划格式通过率、诊断正确率、RAG 引用正确率和 QLoRA 收益是不同指标；不能相互替代。公开手册列表也不是已入库语料或 RAG 质量证明。
- **修改**：保存运行模式、模型/adapter 版本、量化配置、数据划分哈希、工具 fixture/真实来源、失败分母、资源/延迟/费用；对 base 与 QLoRA 用相同隔离测试和相同工具条件比较。计划 schema、任务完成、目标正确、证据支持、引用正确分别报告。
- **验收**：简历上的每个数值能追到固定脚本与结果文件；不写尚未执行的优化效果，不将 demo/合成工单写成生产事故。

## 7. 建议的用户流程与最小公共契约

### 7.1 用户流程

1. 初始界面选择本地诊断或在线多智能体；展示相应能力的可用性与配置入口。
2. 选择可信服务/目标，再填写症状和时间窗；多个候选不能默选第一项。
3. 展示计划及专科分支、实时步骤与工具状态；错误不会被当成“零异常”。
4. 报告分成当前观测、待验证假设、知识依据、下一步检查与证据缺口；来源、目标、时间窗可检查。
5. 任务结束展示生命周期和结果质量；支持用 run_id 恢复和追问，避免重复创建任务。
6. 知识库面板单独管理文档、版本、导入作业与撤销；问答显示检索状态和可核验引用。

先将只读诊断与知识问答做好。本轮不建议增加自动修复或任意 Shell 能力；它们不是本次交接的实现范围。

### 7.2 事件信封

以下是建议契约，不是当前已实现接口。优先在现有路由做兼容演进；只有破坏性变化才考虑 /api/v2。

```json
{
  "schema_version": "1",
  "event_id": "stable-event-id",
  "seq": 12,
  "run_id": "32-lowercase-hex",
  "session_id": "session-id",
  "execution_mode": "cloud_multi",
  "type": "tool_finished",
  "agent": "logs",
  "step_id": "logs-step-1",
  "tool_call_id": "call-2",
  "timestamp": "ISO-8601",
  "payload": {
    "status": "failed",
    "evidence_id": "E2",
    "error": {"code": "access_denied", "retryable": false}
  }
}
```

保留字段不能从 payload 任意覆盖；服务端生成 ID/seq/时间。前端与 trace 使用同一验证器和 reducer，按 seq 去重/恢复。run 的 terminal 事件只发一次；连接断开不自动等价于业务完成。重放接口支持 after_seq 与分页；JSONL 向结构化存储迁移时保留转换器。

### 7.3 工具与证据

- 工具返回 status=ok/empty/partial/error/timeout/unavailable/unsupported，携带真实 target、source_kind、时间窗、data 与结构化 error。
- 区分 evidence.kind=observation/knowledge；source_kind 表示实际来源与真实性，例如 tencent_cloud、ssh、manual、demo。demo 不能支持真实观测结论。knowledge 有 document/revision/chunk/source URI，observation 有查询范围和数值定位。
- Reviewer 仅对本次合法证据进行审阅；数值事实由服务器核对，因果解释默认保留为待验证假设。
- 生命周期与结果质量、工具调用次数与子 API 次数、请求取消与远端终止，都采用独立字段。

### 7.4 8GB 显存边界

本地部署优先保留一个量化小模型承担受约束计划/回答；embedding 先在 CPU 跑现有可验证基线。不要默认将 reranker、第二个 GPU 模型或训练任务同时驻留。多智能体可以复用一个在线 provider 的 API，角色由独立上下文/工具权限/图状态区分，不要求在本机同时加载多份模型。

以真实机器记录显存峰值、RAM、首 token、完整任务时延、失败率与并发限制。具体可用上下文和并发以测量为准，不能仅按“4-bit 权重大小”计算完整显存。

## 8. 分阶段执行与验收

| 阶段 | 工作 | 前置条件 | 可交付验收 |
|---|---|---|---|
| M0 | 四个 P0；为每个加先失败的回归测试 | 当前工作区快照 | 云端真实路由跑通；.env 鉴权；路径隔离；失败更新不破坏旧知识 |
| M1 | 统一事件/工具协议、可信目标绑定、状态/证据校验、前端实时与回放 | M0 | 正常/失败/部分成功/取消/断线的跨层集成测试；两模式报告与回放一致 |
| M2 | 版本化文档、索引作业、embedding 描述符、KB 管理界面 | P0-04 已有最小保护 | 文档从上传到 ready 可见；失败可恢复；切模型不污染旧索引 |
| M3 | 首批 11 个官方页面快照；结构/token 切分；固定检索与引用闭环 | M2 | 许可/版本 manifest；原文可定位；hit/miss/unavailable 分开；问答无工具越权 |
| M4 | 诊断接入知识证据与 run 追问；前端恢复/IME/响应式/可访问性 | M1、M3 | 当前观测与知识分离；授权 run 追问；浏览器任务旅程记录 |
| M5 | 评测、部署探针、持久化、资源限额与构建冻结 | 之前闭环可用 | 干净部署冒烟；断依赖恢复；隔离评测与真实资源记录 |

阶段是依赖关系，不是工期承诺。P0-04 的最小防丢修复应在 M0 完成，完整版本化作业在 M2 继续，不能以“后面重构”推迟数据保护。

测试重点是行为与边界，不是给每个函数写镜像断言：

- 单元/性质反例：路径约束、事件归一化、引用集合、数值核对、token/代码保真、幂等。
- 跨层集成：保留真实 API/service/runtime/retrieval adapter，只 fake 模型 SDK、外部 MCP/云返回和必要的 Milvus I/O。
- SSE：使用真实响应体字节与前端公共解析器测试分片、乱序/重复、错误与终态，不能仅传已解析 dict。
- 故障注入：权限拒绝、超时、空结果、依赖掉线/恢复、插入部分失败、取消后同步 worker 延迟结束。
- 浏览器：双模式选择、目标歧义、知识库作业与引用、历史恢复、中文输入、窄屏和滚动。
- 真实环境冒烟放在明确配置的测试环境执行，默认不连接生产服务器、不运行修改命令；记录调用与费用，不编造结果。

## 9. 完成定义

能在可复现环境中展示：选择模式→检查能力→绑定目标→执行/展示证据→如实报告结果质量→断线恢复→追问同一运行；另能从文档导入→索引 ready→有引用问答→失败/撤销与版本替换。两条链路用统一身份、状态与来源契约连接。

验收报告应逐项标记本文件条目为已修复/已验证/未完成及原因，附测试命令、依赖版本、fixture/真实服务边界与结果文件。通过单元测试不能替代端到端闭环，设计建议不能标成已经完成的功能。
