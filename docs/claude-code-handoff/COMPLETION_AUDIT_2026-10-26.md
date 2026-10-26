# Claude Code 整改完成状态复核

日期：2026-10-26。对象：当前工作区；不是最初的 Git 基线，也不是仅依据 Claude Code 的回复判断。原始范围见 [ENTERPRISE_REVIEW.md](ENTERPRISE_REVIEW.md)，续接指令见 [CLAUDE_CODE_RESUME.md](CLAUDE_CODE_RESUME.md)。

## 1. 结论

**任务尚未完成，当前不能通过整体验收。**

已有实质进展：原始三个 P0 的主要修复、上传防丢的最小保护、目标歧义处理、事件/工具/引用/预算基础模块、token 切分、新前端 reducer 和 SSE 解析器。但多项新模块只被测试调用，实际 API、运行器或页面仍沿用旧实现；知识库管理、版本作业、诊断知识联动和部署验收没有闭环。

当前又有一个启动级阻断：`servers/ssh_server.py` 使用 `read_env/read_env_int`，未导入它们，模块 import 即 NameError。完整后端测试在 collection 阶段失败。

| 阶段 | 完成判断 |
|---|---|
| M0 | P0-01/02/03 主要修复已实现；P0-04 部分保护已实现；全套测试受新 SSH 回归阻断 |
| M1 | 多项基础模块已写，部分已接；事实、来源、目标绑定、质量、预算、取消仍有缺口，前端未接新模块 |
| M2 | 只有部分上传/元数据能力；文档目录、作业、active revision、撤销和管理界面未实现 |
| M3 | 规范原始快照 3/11；未见 normalized/分片/完整导入及索引验证产物；问答实际入口仍走旧链路 |
| M4 | 未完成：知识与诊断联动、run_id 追问、前端完整任务旅程未闭环 |
| M5 | 未完成：冻结镜像、依赖恢复/进程监督、应用数据持久化和真实评测证据未验收 |

不按新增文件数或单元测试数换算“完成百分比”。这里的“主要修复已实现”也不表示真实云 API、SSH、Milvus 或浏览器端到端验证已通过。

## 2. Edit hook 停止的含义与当前文件

用户提供的 `EDIT_GUARD_STALE_INPUT` 表示 Edit 要匹配的旧字符串已经不在文件中；相同确定性失败提交两次后，`EDIT_LOOP_GUARD_STOP` 停止了该轮，避免重复编辑死循环。停止前已落盘的改动不会因此自动回滚。

当前 `servers/ssh_server.py` 的不完整状态：

- `:22–27` 仍导入并调用 `load_dotenv`。
- `:38–44` 已改用 `read_env/read_env_int`，但文件没有相应导入。
- 新建的 `servers/_config.py` 已提供这些函数，但三个 MCP server 的只读配置迁移尚未完整完成；`cls_server.py:15–20`、`monitor_server.py:22–27` 仍调用 `load_dotenv`。
- `servers/web_server.py` 当前作为独立脚本启动时会插入项目根到 sys.path；MCP 配置导入改造也需兼容现有 `python servers/xxx.py` 启动方式，不能只在 pytest 的项目根导入环境中工作。

应在 Claude Code **新一轮**先 Read 当前目标段，再选择小而唯一、逐字匹配的 old_string 修改一次。不要重发上轮旧 Edit，不要重置文件或关闭保护 hook 来绕过问题。

## 3. 本轮实际执行的检查

所有命令在本地当前工作区执行。未连接真实服务器、未调用付费模型、未连接 Milvus 完成入库、未做新的浏览器完整验收。本轮没有修改业务源码；前端 build 会更新构建产物，测试会产生正常缓存/临时文件。

| 检查 | 结果 | 解释 |
|---|---|---|
| `python -m pytest tests -q --tb=short --show-capture=no --disable-warnings` | **失败：2 个 collection errors** | test_eval_scoring、test_ssh_safety 均因 ssh_server 的 read_env 未定义，完整套件未能执行 |
| 排除 test_eval_scoring.py、test_ssh_safety.py、test_server_env_isolation.py 后运行其余 tests | **仍有 7 项失败、1 项跳过** | 这是子集结果，不能写为完整套件通过；失败名单见下 |
| 环境隔离的两个定向测试 | **2 failed** | server 导入失败；load_dotenv 扫描也失败，且扫描器有注释误报 |
| `python -m ruff check servers/ssh_server.py --select F821` | **失败：5 个 undefined-name 报错位置** | read_env/read_env_int 缺导入 |
| `npm run build` | **通过** | 能编译不代表页面使用了新状态/流模块 |
| `npm run lint` | **通过** | 静态前端 lint |
| `node --experimental-strip-types --test src/sse.test.ts src/state.test.ts` | **25 passed** | 新模块单元测试；未接入 App/chat，不能证明真实页面修复 |

本机 Node 为 v22.17.0；直接 `node --test ...ts` 不识别 .ts，本轮使用上表带 strip-types 的命令成功。后续应把可复现的 test script 与 Node 要求写入 package 配置/文档。

子集命令：

```powershell
.\.venv\Scripts\python.exe -m pytest tests --ignore=tests/test_eval_scoring.py --ignore=tests/test_ssh_safety.py --ignore=tests/test_server_env_isolation.py -q --tb=line --show-capture=no --disable-warnings
```

七项失败：

1. `tests/test_auth.py::test_config_dump_keeps_empty_secret_empty`
2. `tests/test_local_runtime.py::test_disk_tool_rejects_injection_at_entry[/; echo injected]`
3. `tests/test_local_runtime.py::test_disk_tool_rejects_injection_at_entry[$(id)]`
4. `tests/test_local_runtime.py::test_disk_tool_rejects_injection_at_entry[]`
5. `tests/test_local_runtime.py::test_disk_tool_rejects_injection_at_entry[C:/']`
6. `tests/test_p0_run_paths.py::test_run_eval_read_trace_rejects_traversal`
7. `tests/test_runtime_target_binding.py::test_ssh_evidence_is_marked_as_outside_target_scope`

后六项均在 SSH 模块 import 处遇到 NameError，不等于这六项所测业务断言分别已有独立缺陷。鉴权空配置测试显示得到打码的非空密钥，与 server import 的环境污染路径一致，但仍需在修复污染后重跑确认，不能仅据这一次失败就否定整个新鉴权设计。

环境隔离测试还存在测试自身的问题：`tests/test_server_env_isolation.py:180–201` 以字符串出现 `load_dotenv(` 为执行证据，连 `servers/_config.py` 说明文字里的历史示例也计入。应改为 AST 检查真实调用或其他可靠行为测试，不能通过随意改注释掩盖三个实际调用点。该文件包含子 pytest 的顺序回归测试，本轮只定向执行上述两个测试，没有声称该整文件已完整执行。

## 4. 原始 34 条逐项状态

状态采用“主要实现/部分实现/未实现”；凡新 helper 没接真实调用处，明确写未接入。没有真实环境验证的条目仍需验收。

### P0

| ID | 状态 | 当前证据与缺口 |
|---|---|---|
| P0-01 | 主要修复已实现 | `diagnostic_runtime.py:178–180、224–229` 已 normalize_event/trace_fields，避免 run_id 重复展开；新增 test_p0_cloud_pipeline 覆盖真实 API→service→runtime→graph、假外部边界；整体仍需在 SSH 回归消除后验收 |
| P0-02 | 主要修复已实现 | `core/auth.py:28–36、60–78` 统一 Settings、恒定时间比较、deployment 启动检查，`main.py:24` 接入；新增 env_file 子进程测试；环境污染回归尚未修完 |
| P0-03 | 主要修复已实现 | `api/runs.py:28–64` 限定 ID 并 resolve 校验；新增编码/盘符/UNC/链接测试；eval 相关测试当前被 SSH import 阻断 |
| P0-04 | 部分实现 | `api/file.py:76–108` 先验证/staging、失败恢复文件；`vector_index_service.py:253` 先插入、后删除旧向量，按预生成 ID 回滚；但上传读取 previous、activate、restore 在索引 source_lock 外，同名并发混合成功/失败仍有文件/向量不一致风险；无完整 revision 激活和崩溃恢复 |

### 前端

| ID | 状态 | 当前证据与缺口 |
|---|---|---|
| FE-01 | 新模块已写，未接入 | `state.ts` 有稳定 ID reducer，`App.tsx:516` 仍旧逻辑；`runtime_tools.py:240–251` 仍未发送 step_id/tool_call_id，新测试字段不等于真实协议 |
| FE-02 | 部分实现 | 后端开始保存入站 plan；App.replay 仍只读 steps/plan_steps、不读云 plan 字段，也不用新 reducer；实时/回放仍不一致 |
| FE-03 | 未实现 | `App.tsx:344` 刷新按钮仍只在 modeError 时出现；模式 API 仍仅查配置 |
| FE-04 | 新模块已写，未接入 | `sse.ts` 单元测试通过；`chat.ts:25` fetch 仍在 try 外、逐块 CRLF 替换、EOF 当成功、无 reader finally 清理 |
| FE-05 | 未实现 | ChatPanel 仍局部会话、无请求版本隔离；停止和跨视图历史一致性未改 |
| FE-06 | 未实现 | web/src 无文档/上传/索引作业管理面板；api.ts 未接文档管理接口 |
| FE-07 | 服务原语已写，未贯通 | 新引用 registry 和 knowledge_service 存在，但实际 chat API 未调用，ChatPanel 无引用来源卡片 |
| FE-08 | 部分实现 | 后端 runs 支持 after_seq/limit/truncated；前端未消费分页/截断，仍 Promise.all 耦合，无 URL/草稿/断流恢复 |
| FE-09 | 未实现，浏览器待验证 | IME composing guard、状态播报、窄屏 inspector 替代入口等未完成；本轮未冒充浏览器验收 |

另有新增跨层断点：后端已返回 `target_required`，但 `api.ts:210–216` 仍只认 complete/error，页面没有候选目标选择。该响应可能被旧 UI 当成未收到完成事件的断流。

### 后端

| ID | 状态 | 当前证据与缺口 |
|---|---|---|
| BE-01 | 部分实现，仍可绕过 | claims 原语存在；`quality.py:134` 未传结构化 observations，`:219–220` 仍渲染自由 claim、忽略 rendered。纯假数据反例：CPU=12 的证据仍可支持“内存 12%”；中文紧邻数值“CPU 使用率99%”未被当前数字正则识别，也可通过 |
| BE-02 | 契约已写，核心未修 | `tool_contract.py` 能识别保留下来的错误，但 `cls_server.py:530–579、617–654` 仍先把错误/demo 来源丢掉；只测试手工含 error 的字典没有覆盖此缺口 |
| BE-03 | 后端主链已实现 | targets 与两模式已处理歧义和 target_required；前端候选选择未接，因此用户流程未完成 |
| BE-04 | 部分实现，仍有越界 | `runtime_tools.py:46–49、254–257` 将 get_topic_info_by_name 当发现工具放行；`targets.py:180–182` 仅在 topics 非空时约束；假工具反例仍能跨 topic/region 读取并标成 svc-a |
| BE-05 | 部分实现 | 云 Reviewer 接上 quality helper；本地 complete 仍缺质量；`diagnostic_runtime.py:215–217` 终态 trace 不存 lifecycle/quality。假工具全 AccessDenied 后，本地历史仍可显示 ok/completed、quality=None |
| BE-06 | 原语已写，未接运行器 | AdmissionController、RequestBudget、日志上界和 prompt 截断仅在 budgets 模块/测试出现；实际 runner/runtime 未调用，不能宣称全局限流和预算生效 |
| BE-07 | 部分实现 | runtime 有 deadline/cancel 字段，但创建时未传 deadline、runner 未调用 cancel；SSH 固定 timeout，未形成端到端 deadline/同步 worker 取消边界 |
| BE-08 | 部分实现 | trace 增加脱敏、限额、失败计数/进程锁，run API 异步读取及限参数；无应用持久卷、持久元数据、retention、abandoned 恢复与访问归属；trace_envelope 未用于实际写入 |

上述后端反例使用纯假工具/数据，无真实监控、日志、SSH 或付费模型调用。

### RAG

| ID | 状态 | 当前证据与缺口 |
|---|---|---|
| RAG-01 | 新流水线已写，未接 API | `knowledge_service.answer_question` 固定 retrieve→gate→answer；`api/chat.py:11、38、96` 仍调用旧 RagAgentService，旧服务仍给自由 Agent 全部 MCP |
| RAG-02 | 结构已写，未贯通 | core/retrieval、splitter 有 chunk/revision/locator；实际 HTTP SSE 和前端尚未消费产物 |
| RAG-03 | 部分实现 | helper 区分五种状态；VectorStoreManager 仍提前置 initialized、缓存初始化错误、吞查询故障为空列表；新 adapter 回退后仍走该路径 |
| RAG-04 | 部分实现 | 本地 embedding 已 fail closed；descriptor/read/write/assert 仅内部与测试调用，未接建库/索引/查询；Milvus 固定 biz/384/L2，而 descriptor 声明 cosine、revision 空 |
| RAG-05 | 部分实现 | 新切分保留缩进、parent、行号与 token；tokenizer 缺失仍字符估算；`document_splitter_service.py:284–290` 超长单行代码在空 current 时绕过预算；检索未将 parent 回填回答 |
| RAG-06 | 门控原语存在，真实适配有缺陷 | `VectorStoreRetriever:315` 调用不存在的 similarity_search_with_score，回退旧检索后给 distance=0→similarity=1，候选全是最高分；默认 min_score=0，无适用范围过滤和隔离基准 |
| RAG-07 | 部分实现 | 有界读取、to_thread、indexed=false/202、目录 400 已改善；没有有界 worker、持久 job、进度/重试/重启恢复；202 是索引等待失败后返回，并非已排队的后台作业 |
| RAG-08 | 部分元数据已实现 | 有文档/版本/chunk/parent/locator；默认 document_id 仍按 basename 派生，同名不同目录碰撞；无 active revision/撤销/适用范围/ACL/有效期管理 |
| RAG-09 | 未实现 | `rag_agent_service.py:203` 仍把 checkpoint 字典当 tuple[0] 读取并吞异常；trim、角色关联、TTL/持久化未修 |
| RAG-10 | 未实现 | 双模式工具无 Milvus 检索；chat/API 没有 run_id/run_context；同 session 不代表诊断证据已共享 |

新知识服务另有实际超时缺陷：`knowledge_service.py:163–171` 使用 `with ThreadPoolExecutor`，future.result 超时后退出 with 时仍 shutdown(wait=True)，等待线程结束。现有测试只检查 status，不检查 wall time 和存活 worker。需修复并测真实耗时，不能以 timeout 字段存在判断超时有效。

### 部署与评测

| ID | 状态 | 当前证据与缺口 |
|---|---|---|
| OPS-01 | 部分实现 | Milvus 故障只降级 RAG，但能力检查仍不完整；entrypoint 仍 sleep 3、无 MCP 子进程监督 |
| OPS-02 | 未实现 | Dockerfile 仍 npm ci 失败回退 npm install；Python 非冻结 editable dev 安装；未完成镜像验收 |
| OPS-03 | 部分基础，未完成验收 | run_eval 改进部分事实匹配/记录；无完整模式/模型/adapter/量化/数据划分/fixture manifest 和本轮对照证据；评测导入又受 SSH 阻断 |

Compose app 仍只有模型缓存与 SSH key 等挂载，未持久化 uploads、traces、eval/results 和后续知识元数据。Milvus 自身有卷不能保证应用原文和运行记录在 app 容器替换后保留。

## 5. 公开语料与 M2 契约的实际状态

- `knowledge/raw` 规范快照只有 **SD-01、SD-02、LK-01 三个**；复核字节数与 SHA256 均匹配 `knowledge/manifests/import-2026-10-26.json`。
- `knowledge/_evidence` 有其他来源取证材料，但不能计作已完成规范导入。
- 未见 `knowledge/normalized`、`knowledge/notices`、分片 manifest、索引 descriptor/active revision、collection/chunk 数量与入库验证结果。
- manifest 中 raw_path 的相对根不统一；SD-02 的许可依据仍指 main，固定版本与许可记录需补齐。
- `M2_API_CONTRACT.md:3` 明确写的是待实现契约。当前没有约定的 knowledge/documents、jobs、import、activate/revoke 等路由，不能把该文档算作接口已实现。
- 原任务要求的 `IMPLEMENTATION_STATUS.md` 和 `VALIDATION_RESULTS.md` 均不存在。

## 6. 续接顺序

1. 新一轮 Read 当前文件，完成三个 MCP server 的只读配置迁移、SSH 导入和独立脚本入口兼容；修正环境隔离测试的误报。
2. 消除所有 collection errors，逐项解决失败，完整 pytest 通过后记录真实结果。不要保留本轮的 --ignore 作为“全套通过”命令。
3. 接上已写模块：统一真实事件 ID→后端→api.ts→App reducer；两条前端流接 sse.ts；接候选目标与质量/回放。
4. 修复证据/目标/预算/取消核心缺口；用真实包装层与运行器测试，而非继续只测 helper。
5. 接知识 API 到新流水线，修 adapter/score/超时/descriptor，完成版本化作业与 UI；补同名并发失败/依赖恢复测试。
6. 完成 11 页核心语料的规范处理与 staging 入库验收，再做 run 追问联动、部署和评测。
7. 交付逐项实施状态、实际测试结果及未完成原因。未跑真实环境/浏览器的部分明确保留为未验收。
