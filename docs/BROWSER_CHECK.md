# 浏览器任务旅程验收（FE-01..FE-09 / target_required / M2 知识库面板）

日期：2026-10-26。对象：本工作区当前构建产物 `web/dist`（`index.html` 引用
`assets/index-DlK1LCkZ.js`，构建时间 22:43）。本文只记录真实浏览器操作与命令输出；
凡未执行的一律列在“未验证”章节。

## 1. 结论

知识库管理面板、执行模式卡片、诊断流与回放都能真实渲染，Milvus 不可用时返回明确的
`unavailable` 状态而不是白屏或 500。但本轮**不能**把“页面能打开”记成“任务旅程通过”：

- 诊断要先手工启动三个 MCP 服务才可能产生一次完整运行；MCP 缺失时页面只显示
  `ExceptionGroup: unhandled errors in a TaskGroup (1 sub-exception)`，无法定位原因。
- 一次真实 cloud_multi 运行能生成报告，但 `quality=insufficient`，metrics 与 reviewer
  两个角色都因模型返回非 JSON 而失败，报告没有“当前观测”与“待验证假设”。
- 工具调用计数与真实调用次数不一致（3 次真实调用显示为 6 次 / 发现调用 3 次），
  已用 Node 重放同一条 trace 复现。
- 知识库面板可上传、可看到“已排队，索引中”提示与失败作业；但索引失败的 revision
  仍可通过 activate 接口变成 active，文档列表显示 `status=active`，违反“失败条目不能
  被标成可检索”的验收要求（FE-06）。
- 中文输入法、屏幕阅读器、窄屏证据区等 FE-09 项：IME 只用合成键盘事件验证，
  未用真实中文输入法；inspector 在 ≤1180px 直接 `display: none` 且没有替代入口；
  页面没有任何 `aria-live` 区域。

## 2. 环境与方法

| 项目 | 值 |
| --- | --- |
| 后端 | `AUTH_MODE=dev ./.venv/Scripts/python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 9911` |
| 前端 | 同一后端的静态产物（`/` 返回 `web/dist/index.html`），非 `npm run dev` |
| 浏览器 | Playwright MCP（Chromium），视口 1440×900 / 1024×800 / 768×900 / 390×844 / 375×812 |
| 鉴权 | `AUTH_MODE=dev`、未配置 API Key，`/health` 返回 `authentication_required:false` |
| Milvus | 未启动（`localhost:19530` 连接被拒） |
| MCP | 8003/8004/8005 初始**未启动**；为跑通一次真实诊断，本轮临时手工启动了 `servers/cls_server.py`、`servers/monitor_server.py`、`servers/ssh_server.py`，结束时已一并停止 |
| 数据 | 上传与对话会写 `data/`（git 忽略）。本轮产生的 `data/` 全部由本次操作创建，结束时已删除 |

启动后 `/health` 返回 200，`data.capabilities` 为：`diagnosis_local=unconfigured`
（未配置 `LOCAL_LLM_BASE_URL`）、`diagnosis_cloud=ready`、`rag=unavailable`
（Milvus 连接异常）。`GET /api/execution-modes` 返回：local `configured:false`
（model `qwen-ops`），cloud_multi `configured:true`（model `DeepSeek-V4.1-Flash-1017`）。

## 3. 逐步记录

### 步骤 1：首屏与执行模式卡片（FE-03 展示部分）

- **操作**：`browser_navigate http://127.0.0.1:9911/`，读取快照与网络请求。
- **期望**：两张模式卡片渲染；`未配置/已配置` 来自 `/api/execution-modes`。
- **实际**：两张卡片渲染，编号 01「本地端侧」显示“未配置”、模型 `qwen-ops`；
  02「在线多智能体」显示“已配置”、模型 `DeepSeek-V4.1-Flash-1017`。网络面板记录
  `GET /api/execution-modes => 200`。卡片下方有“已配置仅表示已填写模型配置，
  不代表服务已通过连接或健康检查”的说明。默认未选模式，症状输入框与“开始诊断”均
  `disabled`（DOM 读取 `disabled:true`）。
- **截图**：`docs/screenshots/browser-01-home.png`
- **判定**：通过（展示与数据来源一致）。

### 步骤 2：知识库管理视图（M2 / FE-06）

- **操作**：点击“知识库”切换视图。
- **期望**：状态区、文档列表、作业列表都渲染；Milvus 未启动时显示 unavailable，
  不是白屏或 500。
- **实际**：状态区显示“不可用 · 检索依赖未连接，上传与问答都不可用”，并附带
  `向量库不可用: ... Fail connecting to server on localhost:19530`；kv 表显示
  `collection=biz`、`片段数=0`、`文档 active 0 · pending 0`、嵌入
  `local/sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2 · 384 维`、
  `Milvus 未连接 · 0 ms`。文档区显示空状态文案，作业区显示“还没有索引作业”。
  网络请求 `GET /api/knowledge/status`、`/documents?limit=20`、`/jobs?limit=20`
  均为 200，无 500，页面无白屏。
- **截图**：`docs/screenshots/browser-02-knowledge.png`
- **额外观测（性能）**：后端刚启动后，第一次 `GET /api/knowledge/status` 会同步重试
  Milvus 连接，日志显示 23:02:18 开始连接、23:02:28 失败。该窗口内 `curl --max-time 5`
  对 `/api/knowledge/status` 超时（exit 28，无响应体）；把超时放宽到 60s 后，同一路径
  0.118s 返回 200。即首个知识库请求会悬挂约 10 秒。（另有一次启动过程中的 `/health`
  也无响应，同属进程未就绪，不计入本条。）
- **判定**：渲染通过；首个请求阻塞 10s 记为缺陷 D6。

### 步骤 3：上传小 .md 与索引作业（M2 / FE-06）

- **操作**：造 fixture `# 测试\n\n磁盘满时先看 df -h。`，通过文件选择器上传。
- **期望**：出现“已排队/索引中”的作业提示；202 不能被当成索引成功。
- **实际**：`POST /api/knowledge/documents => 202`；页面出现 notice
  “已排队，索引中。完成前新版本不可检索。”，随后约 1.5s 轮询命中失败，页面同时出现
  `索引失败：向量库不可用: ...`，作业卡徽章为“失败 / 0/1 片段”。文档表新增一行
  `测试 doc-94f5518a1204`、状态 `pending`、chunks 0、`— → rev-b9a2f1a5c48b4564`；
  “最近作业”出现 `job-11a41b72937c ... failed` 与“重试”按钮。后端日志对应
  `knowledge_store:499 索引作业失败: job-11a41b72937c`。
  未能截到 1.5s 内徽章仍为“已排队”的瞬时画面；截图记录的是 notice 仍在、徽章已变“失败”的状态。
- **截图**：`docs/screenshots/browser-03-upload-job.png`
- **判定**：上传→排队→失败可见，通过；但失败后 notice 未清除（见 D4）。

### 步骤 4：回到诊断视图，症状为空时按钮禁用

- **操作**：切回“诊断”，选“在线多智能体”，不填症状，读取按钮 DOM；再选“本地端侧”确认
  未配置模式同样禁用；最后输入症状观察按钮变为可点。
- **期望**：不填症状时“开始诊断”禁用。
- **实际**：选 cloud 模式、症状为空时 `{disabled:true, hasDisabledAttr:true}`；
  选 local（未配置）且已填症状时仍为 `disabled:true`；输入“订单服务近 10 分钟响应变慢，
  CPU 使用率持续超过 90%。”后变为 `disabled:false`。
- **截图**：`docs/screenshots/browser-04-start-disabled.png`
- **判定**：通过。

### 步骤 5：MCP 未启动时点击“开始诊断”

- **操作**：在 cloud_multi + 已填症状下点击“开始诊断”（此时 8003/8004/8005 未监听）。
- **期望**：失败要能说明原因，且不能把失败显示成完成。
- **实际**：约 6 秒后状态变为“失败”，正文与执行记录均为
  `在线多 Agent 流程失败: ExceptionGroup: unhandled errors in a TaskGroup (1 sub-exception)`；
  trace 落 `agent_error` / `runtime_error_event` / `run_error`，`lifecycle=failed`。
  用独立命令复现同一异常：
  `get_mcp_tools_cached(force_refresh=True)` → `EXC TYPE ExceptionGroup`，
  子异常 `ConnectError All connection attempts failed`。页面没有展示底层 `ConnectError`，
  也没有提示“MCP 服务未启动”。
- **截图**：`docs/screenshots/browser-05-diagnosis-failed.png`
- **判定**：失败状态正确；错误文案不可操作，记为缺陷 D1。

### 步骤 6：启动 MCP 后重新诊断（一次完整运行）

- **操作**：手工启动 `servers/cls_server.py`(8003)、`monitor_server.py`(8004)、
  `ssh_server.py`(8005)，端口均返回 406（streamable-http 对普通 GET 的正常响应）；
  重新点击“开始诊断”（同一症状，服务名留空）。
- **期望**：计划、工具、报告依次渲染；失败项要可见。
- **实际**：运行 `b21fd84d673c42388f83dc271aa801f1`，23:10:59 开始、33.4s 结束，
  状态“已完成”，证据质量 `insufficient`。计划 2 步：metrics 步骤无失败标记、logs 步骤
  标“失败”，摘要为“计划完成 1/2 步 · 失败 1 步 · 工具调用 6 次 · 发现类调用 3 次
  （不计入计划）”。执行记录里每次工具调用都渲染成两条（`tool_step` 与 `step_complete`），
  实际只有 `list_all_services`、`analyze_log_pattern`、`search_historical_tickets` 三次调用。
  报告可见“当前观测/待验证假设/知识依据/证据缺口/后续检查建议/原始证据索引”。
- **截图**：`docs/screenshots/browser-06-diagnosis-report.png`
- **判定**：旅程能走完；计数不准（D2）、角色失败（D12）、证据不足（环境 + 模型）分别记录。

### 步骤 7：在线知识库问答（知识库不可用时）

- **操作**：选 cloud 模式后切到“在线知识库问答”，提问“磁盘满了应该先做哪几步？”。
- **期望**：Milvus 缺失时明确说明不可用，不崩溃。
- **实际**：`POST /api/chat_stream => 200`；回答气泡显示“知识库当前不可用。”、
  “知识库不可用 · 向量库不可用: ...”、最后一行“回答生成失败”（服务端
  `outcome=failed`）。无未捕获异常。
- **截图**：`docs/screenshots/browser-07-chat-unavailable.png`
- **判定**：降级路径可见；末行“回答生成失败”与上一句重复，属文案冗余（不单列缺陷）。

### 步骤 8：回放 target_required 运行

- **操作**：`browser_navigate http://127.0.0.1:9911/?run=3373ea1fdbe246009da01cd28edaf46f`。
- **期望**：回放显示“待选择目标”与候选卡片，可据以继续。
- **实际**：标题“回放运行”，状态“待选择目标”，候选卡片渲染 `svc-a`/`svc-b`（含实例与地域）；
  但两张卡片都是 `disabled:true`，因为回放未带执行模式、页面要求先选模式；卡片文案仍是
  “选择并重新诊断 →”。候选说明文字出现缺标点拼接：
  “请选择本次诊断的目标为避免把其他服务的证据归到目标上…”。该运行 inspector 显示
  `工具调用 0 / 发现调用 3 / 事件 8`，而 trace 里只有一次 `list_all_services` 证据。
- **截图**：`docs/screenshots/browser-09-target-required-replay.png`
- **判定**：渲染通过；卡片禁用原因未说明（D5）、文案拼接（D7）、计数重复（D2）。

### 步骤 9：中文输入法（IME）与键盘

- **操作**：在问答输入框写入“磁盘满了”，对 textarea 派发 `keydown Enter` 且
  `isComposing=true` 的合成事件，读取 `defaultPrevented`，再读对话列表。
- **期望**：输入法组合期间按 Enter 不应发送。
- **实际**：`defaultPrevented=true`，随后对话列表新增用户消息“磁盘满了”，并触发了一次
  `POST /api/chat_stream`。即 Enter 处理未判断组合态。
- **截图**：无单独截图（同 `browser-07-chat-unavailable.png` 的对话面板）。
- **判定**：失败，缺陷 D8。**注意**：这是合成键盘事件，不是真实中文输入法；真实 IME
  行为未验证。

### 步骤 10：响应式与可访问性（FE-09）

- **操作**：依次设 1440×900、1024×800、768×900、390×844、375×812，读取
  `aside.inspector` 计算样式、横向溢出、`aria-live`/`role` 数量。
- **期望**：窄屏有证据 inspector 的替代入口；无横向溢出；状态/错误有 live region。
- **实际**：
  - 1440：inspector `display:flex`，正常显示。
  - 1024 与 768：inspector `display:none`；用选择器查 `[aria-label*=证据]`、
    `.inspector-toggle`、`details.inspector` 均不存在，即**无替代入口**。768 下
    `railWidth=232`，`bodyScrollWidth=768`（不溢出）。
  - 390 与 375：`horizontalOverflow:false`（375 下 `scrollWidth=360`）；375 下 rail
    高度 341px（`max-height:42vh`）。
  - 全页 `[aria-live]` 数量 0、`[role=status]/[role=alert]` 数量 0；构建产物 JS 中
    检索 `aria-live` 无命中。
- **截图**：`docs/screenshots/browser-10-768-no-inspector.png`、
  `docs/screenshots/browser-11-1024.png`、`docs/screenshots/browser-08-narrow-390.png`、
  `docs/screenshots/browser-13-375-home.png`
- **判定**：无横向溢出通过；替代入口（D5）与 live region（D9）未通过。

### 步骤 11：知识库版本/撤销（FE-06 验收点）

- **操作**：在失败文档上点“版本”，看到 `rev-b9a2f1a5c48b4564 staged 0 片段` 与
  “切换到此版本”；用 API 调用同一动作：
  `POST /api/knowledge/documents/doc-94f5518a1204/activate {"revision":"rev-b9a2f1a5c48b4564"}`。
- **期望**：索引失败的 revision 不能被标成可检索；activate 应拒绝或至少保持
  `status != active`。
- **实际**：接口返回 200
  `{"document_id":"doc-94f5518a1204","active_revision":"rev-b9a2f1a5c48b4564","job_id":null,"status":"active"}`；
  该 revision 的作业是 `failed`、`chunks=0`。随后 `GET /api/knowledge/documents` 返回
  `status=active, chunks=0, active_revision=rev-b9a2...`；`GET /api/knowledge/status`
  的 `documents.active` 从 0 变 1。页面对应行变“active 0 rev-b9a2…”，
  但“刷新状态”不会重新拉取已展开的版本列表，列表仍显示 `staged`（我这次是用 API
  激活，不是点页面按钮）。删除该文档时 `DELETE /api/knowledge/documents/{id}`
  返回 503 `删除向量失败，文档未删除: 向量库不可用…`，元数据保持不变（符合预期）。
- **截图**：`docs/screenshots/browser-12-failed-revision-activated.png`
- **判定**：失败，缺陷 D3（FE-06 验收未满足）；另有 D4（刷新不覆盖版本列表）。

## 4. 缺陷清单（只记录，未改源码）

| 编号 | 严重度 | 位置 | 现象与证据 |
| --- | --- | --- | --- |
| D1 | P1 | `app/agent/aiops/cloud_multi.py:519` | MCP 不可用时 SSE 只给 `ExceptionGroup: unhandled errors in a TaskGroup`，子异常 `ConnectError` 被吞；页面无法定位“MCP 未启动”。独立复现输出 `EXC TYPE ExceptionGroup` / `SUB ConnectError All connection attempts failed`。 |
| D2 | P1 | `web/src/state.ts:220`（`tool_step` 与 `step_complete` 都追加一条 ToolCall，未按 `tool_call_id` 去重）；`app/agent/aiops/runtime_tools.py:279-285` | 一次工具执行在 SSE 上发两条事件（`tool_step` 与带 `tool`/`evidence_id` 的 `step_complete`）；trace 里则是三条（另一条不带 `event_id` 的原始 `tool_step`）。reducer 每条都当新调用且不按 `tool_call_id` 去重。真实调用 3 次（`evidence` 事件数），而运行结束后（已按 trace 回填）页面显示“工具调用 6 次 / 发现调用 3 次”。Node 重放：只喂带 `event_id` 的事件得 4/2，整条 trace 得 6/3。实时阶段的计数未单独测量，上述 6/3 是回填后的实测值。 |
| D3 | P1 | `app/services/knowledge_store.py:732`（activate_revision 不校验目标 revision 的 `chunks>0` 或来源作业状态）；`:676`（文档 status 只看 active_revision 是否存在） | 索引失败（job failed、chunks=0）的 revision 可被 activate 成 active，文档列表与状态计数显示为 active。M2 契约 §2.4 未显式禁止激活任意已有 revision，但 FE-06 验收要求“失败条目不能被标成可检索”，当前实现违反该条。 |
| D4 | P2 | `web/src/KnowledgePanel.tsx:102-105`（失败只 setError 不 setNotice）；`:240` 的“刷新状态”只重载 status/documents/jobs | ① 失败后“已排队，索引中”notice 与“索引失败”错误同时留在页面；② 点“刷新状态”不重新拉取已展开的版本列表，激活/作业完成后版本状态可能过期。 |
| D5 | P2 | `web/src/styles.css:433-436`；`web/src/App.tsx:690-705`（`TargetChoice` 的 `disabled` 未解释） | ≤1180px `aside.inspector` 直接 `display:none`，无抽屉/折叠替代入口（选择器搜索为空），窄屏看不到 run ID、证据质量、token 等。回放 target_required 时候选卡片全 `disabled`，但不说明“需先选执行模式”。 |
| D6 | P2 | `app/services/knowledge_store.py:908-937`（`probe_status` 同步调 `_ensure_initialized`）；`app/core/milvus_client.py:84`（`timeout=config.milvus_timeout/1000`，默认 10000ms） | 进程启动后首次 `/api/knowledge/status` 阻塞约 10s（日志 23:02:18 连接→23:02:28 失败），期间请求无响应（`curl --max-time 5` exit 28）。首次失败后 `_init_error` 被缓存，后续请求 0.002s 返回，故只影响首请求（以及后端启动时的 Milvus 连接）。 |
| D7 | P3 | `web/src/App.tsx:696` | 候选说明缺标点拼接：“…请选择本次诊断的目标为避免把其他服务的证据归到目标上…”。 |
| D8 | P2 | `web/src/ChatPanel.tsx:238` | Enter 发送未判断 `isComposing`。合成 `isComposing=true` 的 keydown 仍导致发送并发出 `/api/chat_stream` 请求。 |
| D9 | P2 | `web/src/*.tsx` 全局 | 无 `aria-live`/`role=status`/`role=alert`（DOM 计数 0，构建产物无命中）；状态变化与错误不会播报。 |
| D10 | P3 | `app/main.py:94-106`；`web/dist/index.html:5` | `index.html` 引用 `/favicon.svg`，但后端只挂载 `/assets` 与 `/`，`GET /favicon.svg` 返回 404（`web/dist/favicon.svg` 文件存在）。浏览器控制台唯一错误即此项。 |
| D11 | P3 | `app/main.py:28` | 启动日志打印“监听地址: http://0.0.0.0:9900”，实际由命令行绑定 127.0.0.1:9911，日志与会话诊断时容易误导。 |
| D12 | P1 | `app/agent/aiops/cloud_multi.py:137`（`json.loads(_response_text(response))`） | 真实运行中 metrics 与 reviewer 两个角色都得到 `JSONDecodeError: Expecting value: line 1 column 1 (char 0)`，reviewer 失败后报告“当前观测/待验证假设”为空；运行仍标 `completed`（`quality=insufficient`）。页面在报告顶部显示“Reviewer 失败”。 |

## 5. 未验证（环境或方法所限）

- **Milvus 相关全部能力**：知识库检索、索引作业 `ready`、引用卡片、版本真正切换后的可检索性、
  删除向量——本机 Milvus 未启动，只验证了 `unavailable`/`failed` 分支。属环境缺 Milvus。
- **真实工具结果**：`analyze_log_pattern` 返回“account is abnormal（CLS service is
  unregistered）”，`search_historical_tickets` 返回 error；没有真实云监控/日志数据可核对，
  报告的证据质量因此为 `insufficient`。
- **local 模式运行**：`LOCAL_LLM_BASE_URL` 未配置，只验证了卡片“未配置”与按钮禁用；
  本地模型推理、本地 planner 未跑。
- **真实中文输入法**：D8 只用合成 `isComposing` 事件验证，未接真实 IME（搜狗/微软拼音）。
- **屏幕阅读器**：只统计 DOM 中 live region 数量，未用 NVDA/JAWS 实听。
- **长 Markdown、长表格、图片溢出**：本轮报告与知识表格较短，未专门构造超长内容验证
  `styles.css` 的表格/图片限宽。
- **<375px 宽度**：只到 375，未测更窄。
- **自动滚动打断阅读 / “有新消息”提示**：未构造长事件流验证。
- **分页、断网恢复、URL 深链错误路径**：仅验证 `?run=<id>` 正常回放，未验证断网与非法 id。
- **AUTH_MODE=deployment / 携带 API Key 的前端路径**：仅 dev 模式。
- **`npm run build` / `npm run test` / 后端 pytest**：本任务未重跑，页面用的是既有 `web/dist`。

## 6. 进程与清理

- 后端（实际监听 PID 38220）与临时启动的三个 MCP 服务（47628/11180/14116）均已停止。
  停后 `netstat` 在 9911/8003/8004/8005 上无 LISTENING，`curl` 均返回 000（连接失败）。
- 本轮产生的 `data/`（`data/knowledge/store.json`、一份 revision 内容、两个
  `data/chat/*.json`）全部由本次上传/对话创建，且被 `.gitignore:83` 忽略；结束时已删除。
  未改动 `web/src`、`app/` 源码，未删除或改写 `knowledge/` 与 `logs/traces/` 的既有记录。
- 本轮诊断新增两条 trace（`logs/traces/b21fd84d….jsonl` 成功运行、`7b24cc20….jsonl` MCP 缺失失败运行），
  属运行记录本身，按仓库约定保留。`3373ea1f….jsonl` 是会话开始前已存在的 target_required 记录，
  本轮只读取回放，未修改。

## 7. 需要主 Agent 处理

1. D2/D3 属于“失败被算成成功/可检索”的验收红线，需前端 `state.ts` 与后端
   `knowledge_store.activate_revision` 两侧修正后重跑本节步骤 6、11。
2. D1/D12 的错误可读性与模型结构化输出解析需要后端负责人评估是否纳入本轮。
3. D5/D9 属 FE-09 专项，需要前端负责人决定窄屏证据入口与 live region 方案。

## 8. 命令与真实输出（节选）

```text
# 后端启动后的健康检查
GET /health => 200
{"status":"healthy","auth":{"mode":"dev",...},"milvus":{"status":"disconnected"},...}

# 模式与知识库接口
GET /api/execution-modes => 200
local configured=false / cloud_multi configured=true
GET /api/knowledge/status => 200 {"status":"unavailable",...}
GET /api/knowledge/documents?limit=20 => 200 {"documents":[],"next_cursor":null}
GET /api/knowledge/jobs?limit=20 => 200 {"jobs":[]}

# 上传
POST /api/knowledge/documents => 202
GET /api/knowledge/jobs/job-11a41b72937c => 200 status=failed

# MCP 缺失根因（独立进程）
EXC TYPE ExceptionGroup
EXC MSG unhandled errors in a TaskGroup (1 sub-exception)
  SUB ConnectError All connection attempts failed

# 计数重放（node --experimental-strip-types）
envelope-only: {"total":2,"completed":1,"failed":1,"toolCalls":4,"discovery":2}
full trace:     {"total":2,"completed":1,"failed":1,"toolCalls":6,"discovery":3}
evidence events (真实调用): ['list_all_services','analyze_log_pattern','search_historical_tickets']

# 失败 revision 激活
POST /api/knowledge/documents/doc-94f5518a1204/activate => 200
{"document_id":"doc-94f5518a1204","active_revision":"rev-b9a2f1a5c48b4564","status":"active"}
GET /api/knowledge/documents => status=active, chunks=0
DELETE /api/knowledge/documents/doc-94f5518a1204 => 503 删除向量失败，文档未删除

# 端口释放
netstat -ano | grep LISTENING => 9911/8003/8004/8005 无条目
curl http://127.0.0.1:9911/ => 000（连接失败）
```
