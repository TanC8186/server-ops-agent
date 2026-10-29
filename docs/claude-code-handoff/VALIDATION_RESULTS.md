# 验证结果（实际执行）

本文件只记录**本轮在本地工作区真实执行过**的命令与输出摘要。没有执行过的项一律
写“未验证”，不把推断、测试替身或静态阅读写成已通过。

- 记录时间：2026-10-26（命令时间戳均为 UTC；本地文件时间为 UTC+08:00）。
- 工作区：`E:/On_Call_Agent/on_call_agent`，Windows 11，bash 为 Git Bash
  （GNU bash 5.3.15, x86_64-pc-cygwin）。
- Git：HEAD `fca7f1c`。写本文件前工作区有 119 项变更（48 项 modified、71 项
  untracked），写完本文件后为 120 项（48 项 modified、72 项 untracked，多出的
  untracked 就是本文件）。**因此本文件描述的是“当前工作区”，不是某个干净提交。**
- 解释器/工具版本：Python 3.13.5、pytest 9.1.1、pytest-asyncio 1.4.0、
  ruff 0.17.1；Node v22.17.0、npm 11.14.1、oxlint 1.89.0、tsc 6.0.3、
  vite 8.3.5。关键库：fastapi 0.136.3、pydantic 2.13.4、langchain 1.3.2、
  sentence-transformers 5.5.1、uvicorn 0.48.0。

## 0. 收尾（本轮最终一次，代码冻结后）

在另一个并行 agent 完成全部改动后补跑，作为本文件的最终快照：

| 命令 | 结果 | 时间 |
|---|---|---|
| `./.venv/Scripts/python.exe -m pytest tests -p no:cacheprovider --tb=no -q -o addopts=""` | **528 passed, 1 skipped**，75.49s | 2026-10-26 23:5x |
| `cd web && npm run build` | 通过（`dist/assets/index-Bs1GhrJz.js` 317 kB） | 同上 |
| `cd web && npm run lint` | 通过，**0 warning**（此前一条 `set-state-in-effect` 已修） | 同上 |
| `cd web && npm test` | **44 passed**（新增 step_complete 去重 4 条） | 同上 |
| `bash -n docker-entrypoint.sh` | 语法通过 | 同上 |
| `ruff check app/ servers/ scripts/ --select F821,F811,F841` | All checks passed | 同上 |
| `scripts/normalize_corpus.py` | 11/11 normalized；切分 951 chunks、**超预算 0** | 同上 |
| `scripts/eval_retrieval.py --k 3` | recall@3=1.0、MRR=1.0、no_answer=1.0（阈值 0.6315 **在 dev 集上拟合**） | 同上 |

仍未验证：真实 Milvus、Docker 构建、真实云 API 诊断质量、盲测集。见
[IMPLEMENTATION_STATUS.md](IMPLEMENTATION_STATUS.md) 第 5 节。

## 0.1 测量期间存在并发编辑（影响下列早期数据强度）

本轮测量与另一位 agent 的源码修改**同时进行**。证据：

- `app/agent/aiops/local_runtime.py` 的文件时间为 23:26:38（+08:00），落在本轮
  15:26:12Z 那次失败运行之后；`runtime_tools.py`（23:19:13）、
  `diagnostic_runtime.py`（23:16:11）、`tests/test_run_lifecycle_wiring.py`
  （23:21:28）同样在本轮开始后被改过。
- pytest 收集数在本轮内从 **522 → 525 → 528** 增长（15:12:21Z、15:25:33Z、
  15:39:18Z、15:41:34Z 各测一次），说明测试文件在测量过程中被新增。

所以下面的数字是“某个时刻的工作区快照结果”，不是对某一固定提交的验收。若要作为
验收证据，必须在代码冻结后重跑同一组命令并记录新的 commit。

## 1. 后端测试（pytest）

命令（任务指定形式，覆盖仓库 `addopts`）：

```bash
./.venv/Scripts/python.exe -m pytest tests -p no:cacheprovider --tb=no -q -o addopts=""
```

同一命令在不同时刻的结果：

| 时间（UTC） | 结果 | 退出码 | 耗时 |
|---|---|---|---|
| 15:00:35 起 | **521 passed, 1 skipped**（收集 522） | 0 | 114.73s |
| 15:07:09 起（加 `-rs`） | **521 passed, 1 skipped** | 0 | 81.68s |
| 15:24:05 起 | **1 failed, 523 passed, 1 skipped** | 1 | 84.21s |
| 15:28:20 起 | **524 passed, 1 skipped** | 0 | 86.36s |
| 15:29:51 起 | **524 passed, 1 skipped** | 0 | 78.62s |
| 15:34:33 起 | **527 passed, 1 skipped** | 0 | 82.70s |
| 15:40:05 起 | **527 passed, 1 skipped** | 0 | 75.58s |
| 15:43:27 起 | **527 passed, 1 skipped** | 0 | 78.14s |

不覆盖仓库 `addopts`、直接跑 `./.venv/Scripts/python.exe -m pytest tests -p no:cacheprovider --tb=no -q`（此时仓库 `pyproject.toml:120-124` 的 `-ra -q --strict-markers` 生效）的另外几次：

| 时间（UTC） | 结果 | 退出码 |
|---|---|---|
| 15:18:05 起 | **4 failed**（全部在 `tests/test_state_isolation.py`） | 1 |
| 15:19:31 起 / 15:21:49 起 | **524 passed, 1 skipped** | 0 |

**唯一失败项与波动原因**：

- 失败测试：`tests/test_state_isolation.py::test_run_tools_budget_and_target_are_per_instance`，
  断言位置 `tests/test_state_isolation.py:220`：
  `assert tools_b.cancelled is False` → 实际 `assert True is False`。
- 15:18:05 那次的失败集合是同一文件里 4 个用例：
  `test_concurrent_runs_do_not_share_evidence`、
  `test_concurrent_runs_do_not_share_event_state`、
  `test_run_tools_budget_and_target_are_per_instance`、
  `test_service_layer_is_stateless_between_runs`。
- 隔离复现（`pytest tests/test_state_isolation.py -q`）：15:21 连跑 3 次全绿，
  15:26:12Z 出现 **`..F.`（1 failed）**，同一断言；15:37:26–15:38:08 连跑 5 次全绿。
- 结论：这是一个**低频不稳定用例**，不是稳定失败，也不能写成稳定通过。最可能的
  机制是 `app/agent/aiops/local_runtime.py:197-203` 的“消费方关闭生成器即调用
  `_record_cancel`”路径与 `RunTools.cancel()` 之间存在时序竞争；但本轮测量期间该
  文件正被另一 agent 改写（见 §0），因此**不能把这次偶发归因为当前代码的确定缺陷**，
  必须在代码冻结后重跑才能定性。
- 跳过项：`tests/test_p0_run_paths.py:134`，原因（`-rs` 原文）
  “当前环境不允许创建符号链接”。该用例测的是 traces 目录内符号链接逃逸，
  **本轮因环境限制未执行**，不能算通过。
- 2 条 warning 均为 `app/models/aiops.py:55`、`:98` 的 Pydantic V2 弃用提示
  （class-based `config`），不影响结果。

限制：全部测试使用假 MCP 工具/临时目录/测试替身，未连接真实云 API、真实 SSH
主机或 Milvus。测试通过不等于真实服务链路通过。

## 2. 前端（`web/`）

### 2.1 构建

```bash
cd web && npm run build     # = tsc -b && vite build
```

| 时间（UTC） | 结果 | 退出码 |
|---|---|---|
| 15:02:41 | 通过（`tsc -b` 无报错，`✓ built in 394ms`） | 0 |
| 15:23:49 | 通过（`built in 362ms`） | 0 |
| 15:36:13 | 通过（`built in 485ms`） | 0 |
| 15:38–15:39（该次未单独打时间戳） | 通过（25 modules，`index-DlK1LCkZ.js` 316.59 kB / gzip 98.97 kB，`index-DWW_m4U_.css` 27.95 kB / gzip 5.32 kB，`built in 344ms`） | 0 |

限制：TypeScript 类型检查与打包通过，**不代表页面行为正确**；`web/dist/` 已被
`.gitignore` 忽略，构建产物不进入交付。

### 2.2 Lint

```bash
cd web && npm run lint      # = oxlint
```

| 时间（UTC） | 结果 | 退出码 |
|---|---|---|
| 15:02:48 / 15:10:42 / 15:36:17 | 通过，**1 条 warning** | 0 |

warning 原文位置：`web/src/ChatPanel.tsx:71:5`
`react(set-state-in-effect): Calling setState synchronously within an effect can trigger cascading renders`。
这是 warning 不是 error，退出码 0；它不是“零告警”，不要写成完全干净。

### 2.3 单元测试

```bash
cd web && npm test          # = node --experimental-strip-types --test src/sse.test.ts src/state.test.ts src/view.test.ts src/chat.test.ts
```

| 时间（UTC） | 结果 | 退出码 |
|---|---|---|
| 15:03:04 | **tests 40 / pass 40 / fail 0 / skipped 0**（355.3ms） | 0 |
| 15:12 前后 | 40 pass / 0 fail | 0 |
| 15:36:20 | **tests 40 / pass 40 / fail 0**（294.5ms） | 0 |

限制：这是 node:test + 类型剥离的纯逻辑测试（SSE 分片、state reducer、view、
chat），**不驱动浏览器、不验证真实 DOM 布局与渲染**。真实浏览器检查由另一位
agent 负责，见 `BROWSER_CHECK.md`：本轮 15:36Z 与 15:48:38Z 两次检查时该文件不
存在，15:48:51Z 再查时已出现（文件时间 23:48:35 +08:00，即 15:48Z 之后才创建）。
**本轮没有执行、也没有复核该文件的浏览器结论**，它的通过项不能算进本文件。

## 3. Python lint（ruff，F 规则）

```bash
./.venv/Scripts/python.exe -m ruff check servers/ app/ --select F
```

| 时间（UTC） | 结果 | 退出码 |
|---|---|---|
| 15:02:48 / 15:14:52 / 15:36:28 | **失败：21 errors（8×F401 + 13×F541）** | 1 |

逐条位置（`--output-format concise`）：

| 文件:行 | 规则 | 说明 |
|---|---|---|
| `app/agent/aiops/planner.py:10:31` | F401 | `typing.List` 未使用 |
| `app/agent/aiops/planner.py:11:36` | F401 | `langchain_core.prompts.ChatPromptTemplate` 未使用 |
| `app/agent/aiops/replanner.py:9:31` | F401 | `typing.List` 未使用 |
| `app/agent/aiops/replanner.py:10:36` | F401 | `langchain_core.prompts.ChatPromptTemplate` 未使用 |
| `app/core/retrieval.py:18:36` | F401 | `dataclasses.field` 未使用 |
| `app/core/targets.py:18:36` | F401 | `dataclasses.field` 未使用 |
| `app/models/aiops.py:6:30` | F401 | `typing.List` 未使用 |
| `servers/cls_server.py:11:32` | F401 | `datetime.timedelta` 未使用 |
| `servers/cls_server.py:66,78,83,86,88` | F541 ×5 | 无占位符的 f-string |
| `servers/monitor_server.py:116,128,133,136,138` | F541 ×5 | 无占位符的 f-string |
| `servers/ssh_server.py:142,147,151` | F541 ×3 | 无占位符的 f-string |

补充测量（同一时刻，15:14:52Z 与 15:36:28Z 各一次）：

```bash
./.venv/Scripts/python.exe -m ruff check servers/ app/ --select F821,F811,F841
# → All checks passed!  (exit 0)
```

即：**没有未定义名/重复定义/未使用变量**，21 条全部是未使用导入与多余 f 前缀，
ruff 标为 `[*]` 可自动修复。另：不加 `--select`、用默认配置检查时为
**301 errors（exit 1）**，其中多为 UP006 等风格规则；任务要求的只是 `--select F`，
本文件不把默认配置的 301 条算作本轮验收口径。

限制：ruff 是静态检查，不覆盖类型错误（pyright 未在本轮运行）。

## 4. 入口脚本与编排文件

```bash
bash -n docker-entrypoint.sh
```

| 时间（UTC） | 结果 | 退出码 |
|---|---|---|
| 15:02:48 / 15:13:37 / 15:36:28 | 语法检查通过（无输出） | 0 |

限制：`bash -n` 只解析语法，**不执行**脚本；校验用的是 Windows 上的 Git Bash
（5.3.15），不是容器内的 bash。Milvus 轮询、MCP 子进程拉起、端口就绪与监督循环
**未在容器内实际运行过**。

额外（本轮自行补做，不在任务清单内）：

```bash
docker compose config --quiet    # 15:09:15Z, exit 0
```

只证明 `docker-compose.yml` 能被 compose 解析，**不是镜像构建，也不是启动验证**。

## 5. 数据侧证据 A：原始快照 `knowledge/manifests/import-2026-10-26.json`

本轮只读取文件与磁盘快照做校验，**没有重跑抓取**。校验脚本（15:32:55Z）对每份
raw 文件重新计算 sha256 并比对 `bytes`：

| ID | 家族 | bytes | sha256 | license_status | HTTP | 本地校验 |
|---|---|---|---|---|---|---|
| SD-01 | systemd | 201971 | `f95e4bc82edf115aa4736ec9c9871cb1fd7f8c4cbe1b9be4f7790ab02c000d6f` | per_file_identifier_observed | 200 | sha256+bytes 一致 |
| SD-02 | systemd | 87375 | `75440570fb1f71859229ca88fed0a6764b2057c7dd90651701b3d022a5649722` | needs_review | 200 | 一致 |
| LK-01 | - | 24531 | `25b25737d41b604cdfac5fc6e17c2dd92ee02ef36e60780e0f4c819ff1590555` | needs_review | 200 | 一致 |
| RH-01 | rhel | 846928 | `71132ce799804c8cb8194aedd3473e68b073c6d6e89ab231031adf9e61483522` | needs_review | 200 | 一致 |
| DK-02 | docker | 547031 | `83b301919b714b5816014cfc54e5df34425db9d76ae0dff85ba86d5cf7d0ab88` | repository_or_publisher_license_observed | 200 | 一致 |
| DK-03 | docker | 430480 | `199c761cb8daac8a88feac10d101cbce169c1c70e867c447afab4a73a381d9ce` | repository_or_publisher_license_observed | 200 | 一致 |
| WS-01 | windows | 52937 | `21ff1cdd13413816166addffd15ce22be4e5448b8f1ec1c5e2d373606cc087d5` | repository_or_publisher_license_observed | 200 | 一致 |
| WS-02 | windows | 137503 | `e94e926c3e4d11b891e0e1d9ebaefb6cc6d35ad083813caae085893dcd6d6d86` | repository_or_publisher_license_observed | 200 | 一致 |
| WS-03 | windows | 97644 | `b8effbf6f3da2c3f63eea4268d80aed3a5af95ba751409e450f1417a113ef474` | repository_or_publisher_license_observed | 200 | 一致 |
| WS-04 | windows | 79534 | `ddf653d52d75d36974a8b9dc9e8d5515e970c59830dac29fa3f1e2e2ff3f19d8` | repository_or_publisher_license_observed | 200 | 一致 |
| DK-01 | docker | 519657 | `e61fe443722d783f758372702ff6f43003360b44211a83ccf3282ded64045a98` | repository_or_publisher_license_observed | 200 | 一致 |

汇总：**11/11 份 core 快照在磁盘上与 manifest 的 sha256、bytes 完全一致**；
全部记录 HTTP 200；总字节 3 025 591。许可状态分布：1 份
`per_file_identifier_observed`、7 份 `repository_or_publisher_license_observed`、
3 份 `needs_review`（SD-02、LK-01、RH-01）。

抓取时间与上游版本（来自 manifest 字段，非本轮实测）：

| ID | fetched_at_utc | upstream_version |
|---|---|---|
| SD-01 / SD-02 | 09:18:33Z / 09:18:55Z | 262 / 262 |
| LK-01 | 09:19:08Z | unversioned rolling docs |
| RH-01 | 14:05:54Z | RHEL 9 |
| DK-02 / DK-03 / DK-01 | 14:06:02Z / 14:06:08Z / 14:06:55Z | 滚动文档（按 Linux/Windows daemon、CLI 版本区分） |
| WS-01 / WS-02 / WS-03 / WS-04 | 14:06:10Z / 14:06:16Z / 14:06:27Z / 14:06:33Z | Windows Server 2022 为主 / PowerShell 7.6 ×3 |

限制：`rh-01` 的 `license_id` 为 `null`、`license_basis_url` 为空；`SD-02`、`LK-01`
也标 `needs_review`。**这 3 份只能作内部验证数据，人工核对许可前不得对外分发。**
sha256 只证明文件未被改动，不证明内容正确或完整（见 §7 的提取量问题）。

## 6. 数据侧证据 B：规范化产物 `knowledge/manifests/normalized-2026-10-26.json`

校验脚本时间 15:32:55Z，对每份 normalized 文件重算哈希与字符数：

| ID | document_id | revision | chars（非空白） | notice 文件存在 |
|---|---|---|---|---|
| SD-01 | `doc-453500c16e01` | `2ff1bc80444c1cf1` | 85699 | 是 |
| SD-02 | `doc-16a7cf3d627d` | `c1c8bc5b1cc8d426` | 35056 | 是 |
| LK-01 | `doc-f5bb3cdda892` | `13b22369aa6779c5` | 10079 | 是 |
| RH-01 | `doc-18841382b218` | `c6b9b4f1bce05ad2` | 3003 | 是 |
| DK-02 | `doc-c8abd05e882f` | `740282fceaddef72` | 10390 | 是 |
| DK-03 | `doc-36318887da0b` | `8f21a94b05d785fc` | 512 | 是 |
| WS-01 | `doc-e03e9a9e881e` | `7744319ea96babd4` | 2429 | 是 |
| WS-02 | `doc-308fdec0b57e` | `7140fe357666dec8` | 32698 | 是 |
| WS-03 | `doc-108f227f8ae7` | `afbe7e280880eb34` | 19756 | 是 |
| WS-04 | `doc-2c12877b0d64` | `9e21c9205a79e5cd` | 9853 | 是 |
| DK-01 | `doc-d145dfc281d3` | `b563a104a420e431` | 1892 | 是 |

汇总：**11/11 份 normalized 存在、哈希一致、`chars` 字段可复算、notice 文件齐全**，
`chars` 合计 211367；manifest 自身时间戳 `2026-10-26T14:54:32Z`。

两个必须写清的字段语义/环境差异：

1. `chars` 不是文件长度，而是**去空白字符数**（`scripts/normalize_corpus.py:284`
   `len(re.sub(r"\s+", "", markdown))`）。例如 SD-01 文件实际 102495 字符、`chars`
   为 85699。按“文件长度”复核会误判不一致。
2. normalized 的 `normalized_sha256` 是**按 LF 文本**计算的
   （`scripts/normalize_corpus.py:291` `content_bytes = markdown.encode("utf-8")`，
   `:317` 对该字节串求哈希；而 `:296` 的 `write_text` 在 Windows 上把 `\n` 落成
   CRLF）。本机 `git config core.autocrlf=true`，落盘文件确为 CRLF（SD-01 有 1568
   个 `\r\n`），直接对磁盘字节做 sha256 会得到 `SHA_MISMATCH`。**先做 CRLF→LF
   归一后 11/11 哈希全部一致**，即 manifest 与产物本身是自洽的，差异来自 Windows
   换行。该语义未写进 manifest 或文档，建议补一行说明。

## 7. 数据质量：正文提取量偏低（真实缺陷，仅记录不修）

把每份 raw 的字节数与规范化后的非空白字符数对照（15:37 前后用同一脚本计算）：

| ID | raw bytes | chars | 字节/字符 | 判读 |
|---|---|---|---|---|
| SD-01 | 201971 | 85699 | 2.4 | 正常 |
| SD-02 | 87375 | 35056 | 2.5 | 正常 |
| LK-01 | 24531 | 10079 | 2.4 | 正常 |
| WS-02 | 137503 | 32698 | 4.2 | 正常 |
| WS-03 | 97644 | 19756 | 4.9 | 正常 |
| WS-04 | 79534 | 9853 | 8.1 | 偏低 |
| WS-01 | 52937 | 2429 | 21.8 | 偏低 |
| DK-02 | 547031 | 10390 | 52.6 | 明显偏低 |
| RH-01 | 846928 | 3003 | 282.0 | 明显偏低 |
| DK-01 | 519657 | 1892 | 274.7 | 明显偏低 |
| DK-03 | 430480 | 512 | 840.8 | 最严重 |

补充核对（不重跑抓取，只读本地 raw 快照）：

- RH-01 去掉 `script/style/svg/nav/header/footer/aside/form/button/template` 后的
  `<main>` 可见文本约 3005 非空白字符，与 3003 基本一致——提取器没有大规模漏抓，
  是该页正文本身短（页面为 Nuxt 前端渲染，原始 HTML 里大量是脚本与导航）。
- DK-01 同理（可见文本约 2544，落在 1892 的量级）；其 normalized 末尾仍有
  `## Next steps`，结构完整。
- **DK-03 是明确异常**：normalized 只有 512 非空白字符，且末尾停在选项
  `-s, --sizeDisplay total file sizes` 之后没有内容；原始 HTML 中 `--type`、
  `Exit Status`、`Global options` 均为 0 次命中；标记之后只剩 9408 字节，其中
  包含 5 个 SVG 图标（整页共 249 个）与页脚 `</footer></body></html>`。也就是说
  **快照在“Display total file sizes”之后就没有正文了**，可能是抓取时页面未完整
  渲染，或该页本身是客户端补全。本轮按任务要求不重跑抓取，**无法判定是快照不
  完整还是页面结构如此**。

影响：DK-03（4 个 chunk）、DK-01（9 个 chunk）、RH-01（10 个 chunk）、DK-02
（45 个 chunk）在 951 个 chunk 中占比很小，而 SD-01 一页就有 347 个 chunk。这会
让 §8 的 recall@3 更容易“命中”，是检索指标偏乐观的结构性原因之一。

## 8. 离线检索基线 `eval/rag/results/offline-*.json`

本轮**只读取已有结果文件**，没有重跑抓取或评测（任务要求）。两份结果都由
`scripts/eval_retrieval.py`（内存 cosine + 本地 embedding
`sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2`，`EMBEDDING_PROVIDER=local`）
在 2026-10-26 晚生成：

| 文件 | generated_at_utc | k | docs | chunks | recall@3 | MRR | 无答案判定 | 阈值 |
|---|---|---|---|---|---|---|---|---|
| `eval/rag/results/offline-20261026-225755.json` | 14:57:55Z | 3 | 11 | 951 | 1.0 | 1.0 | **no_answer_precision = 0.0**（3 个无答案问题全部误判为可回答） | 0.35（固定经验值，无 `threshold_source`） |
| `eval/rag/results/offline-20261026-225925.json` | 14:59:25Z | 3 | 11 | 951 | 1.0 | 1.0 | **no_answer_precision = 1.0** | 0.6315，`threshold_source=fitted_on_dev_set(not a blind claim)` |

第二份的分离度（文件内 `separation` 字段）：

- `min_answerable_top_score = 0.6315`（q05）
- `max_unanswerable_top_score = 0.5699`（q14）
- `overlap = false`，间隔 **0.0616**

**阈值是在 dev 集上拟合的，不是盲测。** 结果文件自己写着
`fitted_on_dev_set(not a blind claim)`，`scripts/eval_retrieval.py:36-40` 的注释与
`:194` 的 `threshold_source` 赋值也说明默认阈值由 dev 集的最优切点得到。因此
0.6315 这个数字、以及 `no_answer_precision=1.0`，**都属于“在同一批题上调参后的
自评”**，不能作为泛化能力或线上阈值的证据。

逐题复核（从结果文件的 `rows` 重算，非新增执行）：

- 11 个可回答问题（q01–q11）`rank` 全为 1，`reciprocal_rank` 全为 1.0，故
  recall@3 = 11/11、MRR = 1.0 与文件汇总一致。
- 3 个无答案问题（q12 MySQL 复制延迟、q13 K8s Ingress 证书续期、q14 食堂几点开饭）
  top1 分数 0.5021 / 0.4866 / 0.5699，全部低于 0.6315，第二份文件据此判为
  “无答案”，与 `predicted_answerable=false` 一致。
- 第一份文件用 0.35 阈值，这三题全部被判为“可回答”，所以
  `no_answer_precision=0.0`。两份文件是同一评测脚本的两次参数选择，不是两次独立
  实验。

指标本身的限制（必须一起读）：

1. **dev 集只有 14 题**，其中 11 份文档**每份恰好 1 题**（q01–q11 与 11 个 source_id
   一一对应）。这种“一文档一题”的构造让 recall@3 很容易满分，**1.0 不代表真实
   运维问句上的召回**。
2. 阈值与指标在同一批 14 题上拟合又评估，无留出集、无盲测集。
3. 检索是**内存 cosine**，不是 Milvus 检索；不验证 collection 描述符、索引度量、
   写入原子性与 `num_entities`。结果文件 `note` 字段原文即
   “内存 cosine 检索，不是 Milvus 入库验证”。
4. 语料只有 11 份官方手册，且 §7 指出 4 份提取量偏低；chunk 分布严重不均
   （SD-01 347 个、DK-03 仅 4 个）。
5. 没有 rerank、没有 hybrid/RRF，也没有与 QLoRA/基座模型的对比数据；
   `scripts/run_eval.py --live` 需要真实凭证，**本轮未运行**。

## 9. 明确未验证（本轮没有能力或没有执行）

| 项 | 状态 | 依据 |
|---|---|---|
| 真实 Milvus | **未验证** | 两次 socket 探测（约 15:09Z 与 15:51:45Z）中 `127.0.0.1:19530` 与 `:9091` 均 `TimeoutError`（CLOSED）；15:45Z、15:51:45Z 两次 `docker ps` 均无 milvus 容器（grep 无命中）。`volumes/milvus/` 下有历史数据目录，但那是旧运行的残留，不能当成本轮验证 |
| Milvus 入库/索引/检索端到端 | **未验证** | 无 Milvus 实例；所有 RAG 测试用假 store/临时目录 |
| 真实云 API（LLM / 腾讯云监控等） | **未验证** | 未调用；测试使用测试替身与固定 fixture。未打印 `.env` 或任何密钥 |
| 真实 SSH 目标主机 | **未验证** | 无真实 SSH 连接；相关测试用假工具 |
| Docker 镜像构建 | **未验证** | 只跑了 `docker compose config --quiet`（exit 0，15:09:15Z），未 `docker build`、未启动容器 |
| 容器内 `docker-entrypoint.sh` 执行 | **未验证** | 只有 `bash -n` 语法检查；Windows Git Bash 下的解析不等于容器内运行 |
| 浏览器端到端 / 截图 / 响应式与 IME | **未验证（本轮）** | 由另一位 agent 负责，见 `BROWSER_CHECK.md`。本轮 15:36Z 与 15:48:38Z 检查时该文件不存在，15:48:51Z 已出现；**本文件没有复核它的任何结论**。`docs/screenshots/` 的 5 张旧截图看不到生成命令与时间，不能据此声称本轮验证 |
| pyright / 类型检查 | **未验证** | 本轮未运行；`npm run build` 里的 `tsc -b` 只覆盖前端 |
| `scripts/run_eval.py --live` | **未验证** | 需真实凭证，未运行 |
| 符号链接逃逸用例 | **未执行** | `tests/test_p0_run_paths.py:134` 环境不允许建符号链接 |
| 代码冻结后的最终验收 | **未验证** | 测量期间仍有并发编辑（§0），收集数 522→528 |

## 10. 失败/异常项汇总（保留，不隐藏）

| # | 项 | 现象 | 原因 | 状态 |
|---|---|---|---|---|
| 1 | `ruff check servers/ app/ --select F` | 21 errors（8 F401 + 13 F541） | 未使用的导入、无占位符 f-string；均可 `--fix` | **失败，未修**（本轮只写文档，不改代码） |
| 2 | 后端全量 pytest | 15:18Z 出现 4 failed、15:24Z 出现 1 failed，其余多次全绿 | `test_state_isolation.py` 偶发；`tools_b.cancelled` 被置 True 的时序竞争，测量期同文件正被改动 | **不稳定，未定性** |
| 3 | `tests/test_p0_run_paths.py:134` | 跳过 | Windows 环境不允许创建符号链接 | **未执行** |
| 4 | DK-03 语料 | 规范化后仅 512 非空白字符，末尾断在选项表中间 | 快照在 “Display total file sizes” 后无正文；是否抓取不全未重跑确认 | **数据质量缺陷，未修** |
| 5 | normalized manifest 哈希语义 | 直接对磁盘字节算 sha256 全部 mismatch | CRLF（`core.autocrlf=true`）vs manifest 的 LF 哈希；归一后 11/11 一致 | **文档缺口，建议在 manifest 或脚本注释写明** |
| 6 | 前端 lint | 1 条 warning | `ChatPanel.tsx:71` 在 effect 内同步 setState | **告警，非失败** |
| 7 | 检索基线指标 | recall@3=1.0 / MRR=1.0 / 无答案 precision=1.0 | 阈值在 dev 集上拟合、一文档一题、11 份小语料、内存检索 | **仅作内部基线，不得外推** |

## 11. 复现命令清单

```bash
# 后端全量测试（任务指定形式）
./.venv/Scripts/python.exe -m pytest tests -p no:cacheprovider --tb=no -q -o addopts=""

# 单独复现不稳定用例
./.venv/Scripts/python.exe -m pytest tests/test_state_isolation.py -p no:cacheprovider -q --tb=short

# 前端
cd web && npm run build && npm run lint && npm test

# Python lint（F 规则）与 shell 语法
./.venv/Scripts/python.exe -m ruff check servers/ app/ --select F
bash -n docker-entrypoint.sh

# 语料与基线（本轮只读，未重跑）
#   knowledge/manifests/import-2026-10-26.json
#   knowledge/manifests/normalized-2026-10-26.json
#   eval/rag/results/offline-20261026-225755.json
#   eval/rag/results/offline-20261026-225925.json
```

写回本文件前，最后一轮测量是 15:43:27Z–15:44:50Z：
`527 passed, 1 skipped`（exit 0），收集 528。**这个数字只对应那一刻的工作区。**

---

# 2026-10-27 第二轮整改验证（A01–A13）

本节**追加**在 2026-10-26 记录之后，不覆盖任何历史失败证据。上面的历史结论
仍然是当时的真实测量结果，不能因为本轮修复就被当成“当时也已通过”。

## V1. 本轮实测（2026-10-27，当前工作区 = 基线 `fca7f1c` + 两轮实现）

| 检查 | 命令 | 结果 |
|---|---|---|
| 后端全量 pytest | `./.venv/Scripts/python.exe -m pytest tests -p no:cacheprovider -q -o addopts= --tb=line --disable-warnings -rs` | **717 passed, 1 skipped, 2 warnings，103.21s**（exit 0） |
| 前端单测 | `cd web && npm test` | **53 passed, 0 fail** |
| 前端构建 | `cd web && npm run build` | 通过，产物 `assets/index-vsa-hn5h.js` 320.57 kB / `index-Zmx7MErZ.css` 28.86 kB |
| 前端 lint | `cd web && npm run lint` | 通过（oxlint 无输出） |
| Python 静态检查 | `./.venv/Scripts/python.exe -m ruff check app servers scripts --select F821,F811,F841` | All checks passed（去掉过期的 F841 未用变量） |
| Compose 配置 | `docker compose config --quiet` | 通过（exit 0） |
| Docker daemon | `docker info --format '{{.ServerVersion}}'` | 29.8.2（可用） |
| skip | `tests/test_p0_run_paths.py:134` | 环境不允许创建符号链接，仍是 **未执行**，不是通过 |

### V1.1 当前工作区复核（2026-10-27，代码未再修改）

- 再次执行 `npm test` 得到 **53 passed**；`npm run build` 与 `npm run lint` 通过；后端与三项离线探针结果见本节和 `NEXT_PHASE_PLAN_2026-10-27.md`。
- `docker compose config --quiet` 通过；实际 `docker build --progress=plain -t oncall-agent-acceptance .` 在解析 `docker/dockerfile:1.7` 时因无法从 `auth.docker.io` 获取 OAuth token 失败。该次没有产生可验收镜像，属于 Docker registry 网络/认证阻断，不能写成镜像构建通过或 Dockerfile 逻辑失败。
- 当前项目没有运行中的 Compose 容器；真实 Milvus、云 API、SSH 和当前构建浏览器旅程仍未进入真实依赖验收。

收集总数从 528 → 717（+189），新增测试文件：
`test_acceptance_boundaries.py`(12)、`test_acceptance_targets_budget_paging.py`(12)、
`test_claim_boundaries.py`(45)、`test_parent_backfill.py`(5)、`test_runs_api.py` +5（abandoned/retention）；前端 +4。

## V2. 三个离线探针（修复后，均不访问真实外部服务）

```powershell
.\.venv\Scripts\python.exe docs/claude-code-handoff/probes/rag_probe_2026_10_27.py
.\.venv\Scripts\python.exe docs/claude-code-handoff/probes/runtime_probe_2026_10_27.py
.\.venv\Scripts\python.exe docs/claude-code-handoff/probes/ui_eval_probe_2026_10_27.py
```

| 探针字段 | 修复前 | 修复后 |
|---|---|---|
| `superseded_revision_visible.retrieved_texts` | `["OLD instructions","NEW instructions"]` | `["NEW instructions"]`（`bug_reproduced=false`） |
| `failed_batch_visible.retrieved_texts` | 含 `FAILED PARTIAL instructions` | `["NEW instructions"]`（`bug_reproduced=false`） |
| `strict_local_selects_remote_factory.selected_factories` | `[{factory:remote}]` | `[]`（`bug_reproduced=false`） |
| `first_initialization_failure_is_sticky.actual_connect_attempts` | `1` | `2`（`bug_reproduced=false`） |
| `facts.cases[].accepted_as_supported` | `true,true,true` | `false,false,false` |
| `target_choice.candidates[].target_id` | 缺失 | `tgt-<hex>`，两实例不同 |
| `topic_and_budget.valid_topic_query` | `ok=false`，`unexpected keyword argument 'topic_id'` | `ok=true, error=""` |
| `topic_and_budget.log_query.executed_limit` | `999999` | `100` |
| `topic_and_budget.log_query.retained_data_chars` | `200054` | `2` |
| `topic_and_budget.log_query.evidence_budget` | `used=0, remaining=10` | `used=2, limit=10, remaining=8` |
| `cloud_cancel.runtime_cancelled_flag` | `false` | `true` |
| `trace_page` | `truncated=true, next_after_seq=null`（读不到终态） | 返回含 `status seq=2` 与 `run_complete`，`truncated=false` |
| `same_service_candidates.bug_reproduced` | `true`（2 进 1 出） | `false`（2 进 2 出，各带 target_id） |
| `new_eval_format.api_result` | `{"results":[]}` | 1 条结果 + `manifest`，`bug_reproduced=false` |

探针退出 0 只表示执行完成；上表右列是**执行完毕后的观测值**，不是“业务已验收”。

## V3. 每条反例对应的回归测试（真实业务代码 + 仅替换外部 I/O）

| 编号 | 测试文件 | 关键用例 |
|---|---|---|
| A01 | `test_acceptance_boundaries.py` | `test_superseded_revision_is_not_retrievable`、`test_rollback_makes_old_revision_visible_again`、`test_failed_revision_is_not_retrievable`、`test_active_revisions_snapshot_is_consistent_under_concurrency` |
| A02 | 同上 | `test_partial_batch_failure_rolls_back_every_candidate_id`、`test_failed_new_revision_keeps_old_active_intact`、`test_cleanup_failure_is_recorded_and_retry_succeeds` |
| A03 | 同上 | `test_strict_local_never_selects_remote_factory`、`test_strict_local_returns_no_remote_http_and_refuses_when_model_absent` |
| A04 | `test_acceptance_targets_budget_paging.py`、`web/src/view.test.ts` | `test_candidate_id_binds_exact_instance_and_region`、`test_stale_candidate_id_is_rejected_instead_of_silently_rebinding`、同服务双实例用例 |
| A05 | `test_acceptance_targets_budget_paging.py` | `test_valid_topic_query_succeeds_without_injecting_unknown_keys`、`test_topic_outside_binding_is_still_rejected` |
| A06 | `test_claim_boundaries.py` | 三个反例参数化降级 + `test_correct_latency_claim_stays_supported` |
| A07 | `test_acceptance_targets_budget_paging.py` | `test_log_limit_is_clamped_and_evidence_budget_is_consumed`、`test_budget_notes_describe_what_was_dropped` |
| A08 | `test_acceptance_boundaries.py` | `test_transient_initialization_failure_recovers_without_reset`、`test_permanent_invalid_index_is_not_retried`、`test_concurrent_recovery_initializes_only_once` |
| A09 | `test_acceptance_targets_budget_paging.py`、`test_runs_api.py` | `test_mixed_seq_page_always_reaches_terminal_event`、`test_paging_walks_all_events_without_gaps_or_duplicates` |
| A10 | `test_runs_api.py`、`web/src/view.test.ts` | 新格式 `{manifest, rows}` 端到端 + 旧裸列表兼容 |
| A11 | `test_cloud_multi.py`、`test_p0_cloud_pipeline.py` | 流关闭取消两专科 + 正常终态不被记成 `run_cancelled` |
| A12 | `web/src/view.test.ts`、`test_health_capabilities.py` | 候选身份/请求体、manifest 摘要、health 五类能力 |
| A13(parent) | `test_parent_backfill.py` | child 命中时携带 parent_id/parent_text、上下文用完整父段落、超长有界截断、旧索引降级 |

版本化用例使用**先写一行再抛错**的 fake（`RowWiseFailingVectors.fail_after_rows`），
不是“调用前直接抛错”的 fake —— 后者无法覆盖“批量插入已落部分行”的竞态。

## V4. 仍未进入“真实依赖验证”的范围（诚实标注）

1. **真实 Milvus**：本机 19530 未启动。`_revision in [...]` 预过滤表达式、
   `delete_by_ids` 语义、collection 度量读取全部只在 fake 上验证。
2. **真实 Docker 镜像/恢复**：daemon 可用但本轮未 `build`/`up`，没有镜像、
   信号传播、卷重建、Milvus 冷启动恢复的实测证据；只有 `compose config` 解析。
3. **真实浏览器旅程**：A04 候选点击、A12 aria-live/窄屏 inspector 只有纯逻辑
   单测；旧的 BROWSER_CHECK 使用早期构建，不能代表当前代码。
4. **真实云 API**：仍只有一次 cloud_multi 历史运行（模型返回非 JSON 导致角色失败）。
5. **真实远端任务取消**：A11 只证明 runtime 标记/清理/审计一致；同步 SDK / 远端
   任务是否真正终止仍未单独记录。
6. **独立盲测 / retention / abandoned / 权限 / parent 回填 / DK-03 正文质量**：
   仍属未完成（见 IMPLEMENTATION_STATUS §7）。

## V5. 与上一轮文档的矛盾修正

- 上轮写“持久卷未做”，实际 compose 已有四个命名卷；本轮如实记录为“已有”。
- 上轮 FE-09 记为“未实现”，本轮 `aria-live` 与窄屏 inspector 入口**已实现**，
  但真实浏览器/读屏器未验证，因此仍不能标“已验证”。
- 上轮把“入口接上”当成 RAG-03/07/08、BE-01/03/04/06/07 的完成依据；本轮按
  **行为**重写，并在 §5 表里区分“已实现 / 测试替身验证 / 真实依赖验证”。

## V6. 当前构建的浏览器验收（2026-10-27，A12 窄屏替代入口 / aria-live）

用 `npm run build` 后的产物起 `vite preview`，Playwright 以 1100×900 视口加载：

| 检查 | 观测 |
|---|---|
| `matchMedia('(max-width: 1180px)')` | `true` |
| 右侧 `aside.inspector` | `display: none`（媒体查询按设计隐藏） |
| `.inspector-toggle` | 存在、`display: flex`、可见、`aria-expanded="false"`、`aria-controls="run-inspector-inline"` |
| 真实点击后（300ms 后读取） | `aria-expanded="true"`、`.inspector-inline` 的 `hidden` 移除、`display: block`、按钮文案变为“收起本次运行与评测信息” |
| 展开内容 | 「本次运行」8 个字段 + 「评测结果」 + 「这套系统在做什么」全部渲染 |
| `[role="status"][aria-live="polite"]` | 存在，文本随状态更新（观测到“待命。”） |

截图：`docs/claude-code-handoff/screenshots/narrow-inspector-1100px-2026-10-27.png`

**边界（不得含糊）**：

- 本轮没有启动后端（`127.0.0.1:9900` 未起，页面显示 502），因此**没有**验证
  “真实诊断任务旅程”：两模式选择后真正跑一次、target_required 点选候选、
  引用卡片、错误/部分成功/取消态、刷新历史。
- 没有验证真实中文 IME 输入与真实读屏器播报（`aria-live` 只确认了 DOM 与
  文案存在，未确认读屏实际朗读）。
- 375 / 768 / 1024 / 1440 四档只测了 1100 与 1440 两个宽度。

## V7. 对抗复核发现的三项反驳与修复（2026-10-27）

一个独立复核子代理用自己的反例（不复用本仓库探针）逐条尝试**反驳**修复，发现三项
此前被我判为"已修复"的说法不成立。以下是证据与修复，全部可复跑。

### 7.1 A06 对真实 payload 无效（HIGH，已修）

复核用 `servers/monitor_server.py` 实际返回的形状：
`{"metric_name": ..., "data_points": [{"value": 12, "unit": "ms"}], "statistics": {...}}`
—— **没有顶层 `data.unit`**。原实现只读 `data.unit`，于是 `has_duration_evidence`
永远为 False，`999ms` 被当成查询窗口跳过。复核实测：`网络延迟 999ms` 与
`网络延迟 12%` **仍显示为 supported**（3 个原反例中的 2 个）。

修复：`evidence_value_pairs` 单位来源改为
`data_points[].unit` → 顶层 `data.unit` → 指标名推断（latency→ms、packet_loss→%、
cpu/memory/disk/usage→%）；并且 `statistics.count` 不再被当成读数。
新增 6 项回归（含 `999s`、`50分钟`、无单位 payload 按指标名推断、count 不算读数）。

### 7.2 A03 只修了工厂，没接线（MEDIUM，已修）

复核实测：`create_embedding_service(strict_local=True)` 正确，但 `grep` 显示
**生产代码没有任何地方传这个参数**；本地模式跑一次 `retrieve_knowledge`，
在 `EMBEDDING_PROVIDER=openai` 下真的调用了远端工厂与 `embed_query`。

修复：
- `vector_store_manager.create_local_vector_store_manager()` 提供**独立**实例，
  注入 `LocalOnlyEmbeddings`（只允许本地，失败即抛错）；
- `knowledge_wiring.knowledge_tools(execution_mode="local")` 注入该检索器；
- 不改全局配置、不复用全局单例（云端模式与管理页不受影响）。
新增 2 项回归（本地不调远端工厂 + 云端不被切换）。

### 7.3 A09 前端不分页（MEDIUM，已修）

复核实测：后端分页正确，但 `api.getRun` 不传游标、`App.tsx` 只请求一次，
因此 >2000 事件的运行在 UI 上永远读不到终态（复核构造 2601 条 trace 验证）。

修复：
- `api.getRun(runId, cursor)` 支持 `after_seq` / `after_event`；
- `view.ts` 新增纯函数 `nextRunCursor` / `isStuckPage` / `appendRunEvents`
  与 `MAX_RUN_PAGES` 上限；
- `App.tsx` 的 `openRun` 与 `backfill` 改为循环读完全部页；截断且无游标时
  明确提示"内容不完整"，绝不假装读全。
新增 3 项前端单测（游标优先级 / 页数上限 / 顺序追加）。

### 7.4 复核确认无问题的项

A01、A02、A04、A05、A07、A08、A10、A11 经复核独立复现为**已修复**；
A12/A13 确为部分完成且文档诚实，未发现夸大（尤其没有把 Docker 镜像或恢复
写成已验证）。

复核同时给出的两个非阻断观察，记录在此不隐藏：

- A07：预算耗尽后每次调用仍记一条固定 51 字符占位且不再预占，单次有界、
  总量受 `tool_calls` 限制，不是无界增长。
- A01：依赖真实 `langchain_milvus` 尊重 `expr`；若某适配器接受 `expr` 却忽略它，
  `pool=max(4k,k+8)` 的候选窗口可能仍被旧版占满。真实库行为正确。

## V8. 第二轮对抗复核发现的两项反驳与修复（2026-10-27）

第一个复核代理的问题修完后，第二个独立代理（`aa13bcba`）再次用**自造反例**尝试反驳，
结果：A03 确认已修（含两种 provider、预热的远端缓存不被复用、全局状态零变更），
但 **A06 与 A09 仍各有一处真实缺口**。

### 8.1 A06：duration 声明挂在非 duration 证据上仍被放过（已修）

反例（真实 CPU/流量 payload，无 ms 单位）：

- `CPU 使用率 999ms` → 旧实现判为“查询窗口”跳过 → **supported**
- `CPU 使用率 999秒` → 同上 → **supported**
- `CPU 使用率 12%，接口耗时 999ms` → **supported**
- `网络延迟 999ms` 对 `network_traffic_mbps` 证据 → **supported**

原因：`_verify_claim_value_pairs` 只要证据没有 duration 单位，就把**任何**时长数值
当作查询窗口跳过。

修复：先判语义再决定是否核验——
- 出现读数语义（耗时/延迟/响应时间/超时/latency…）→ 必须核验，证据没有时长单位即拒绝；
- 出现范围线索（最近/过去/近/last/past/within/期间…）→ 判为查询窗口，不核验；
- 其余按读数处理（保守：误放过读数会直接造成错误结论）。

新增 8 项回归（4 条应拒 + 3 条查询窗口应通过 + 2 条正确读数应通过）。

### 8.2 A09：seq→位置游标交接丢失跳过距离（已修）

反例：混合 trace（前 2 万条无 seq，边界有 seq）。第 1 页给 `next_after_seq`，
第 2 页按 seq 续读后返回 `next_after_event = len(selected)` —— **丢掉了已跳过的
20000 条**，客户端在偏移 20000 与 22000 之间反复读取。复核实测 60003 事件：
`pages=50 delivered=550000 uniq=42000 dup=508000 terminal=False stuck=True`。

修复：记录本次请求的 `skipped`（位置游标=传入值；seq 游标=切掉的前缀长度），
位置游标一律返回 `skipped + len(selected)` 的**绝对位置**。

修复后实测：
- 22 002 条混合 trace → 2 页、零重复、`run_complete` 可达；
- 60 001 条生产形态 trace → 4 页、唯一 60000、零重复、终态可达。

新增 1 项回归（seq→位置交接不得重复投递且必须读到终态）。

### 8.3 复核确认无问题

A03 本地模式：`store is global: False`、`_embedding_function` 为 `LocalOnlyEmbeddings`、
`embed_query` 抛错、远端记录器为空；`openai` 与 `dashscope` 两种 provider 均如此；
云端路径未被切换；全局单例与配置零变更。A09 规范要求的小分页/HTTP 回放/`getRun`
URL 断言全部通过。

### 8.4 仍存在、已知且未修的边界（诚实记录）

1. ~~**A06 中文数字**：`延迟 九百九十九毫秒` 无阿拉伯数字，抽不出数值对，会通过。~~ **已修**：新增中文数字解析（十/百/千/万），该例现已降级；仍不覆盖完整中文语法（如“两千零一十”之外的复杂写法）。
2. **A06 空 payload**：`data: {}` / `None` 时证据无任何数值对，任何声明都通过。
   该形态经 `RunTools` 不可达（`source=unspecified` 非 live 证据），影响有限。
3. **A06 单位换算**：`延迟 0.012s`（= 证据的 12ms）被拒，属**过严**而非漏放；
   不实现毫秒/秒换算以免引入新的误判面。
4. **A03 大小写**：`execution_mode` 精确匹配 `"local"`；调用方只传字面量，
   不可利用，但比较偏脆弱。

## V9. 第三轮对抗复核（终验）发现的两项与修复

终验代理确认 **A06 的 13 条要求用例与 A09 的 (a)–(d) 分页断言在两栈上均不可反驳**
（含 140 份真实 trace / 270 次分页回放，最坏 283 759 事件 21 页 < 50 页上限），
但报告了两处新的真实问题，均已修复。

### 9.1 A06 窗口词洗白（已修）

模型只要在句中任何位置补一个范围词，就能把任意时长洗成查询窗口：

- `CPU 使用率 999ms，最近10分钟` → 旧实现 supported
- `过去5分钟 CPU 使用率 8888ms 峰值` → supported
- `最近5分钟内出现过 999ms 的卡顿，CPU 使用率 12%` → supported
- `disk usage 999ms since deploy` → supported

原因：`_looks_like_query_window` 对**整句**搜索范围线索。

修复：范围线索必须**紧贴数值前 6 个字符内**；数值本身带读数语义（延迟/耗时/
响应时间/超时/latency）时一律按读数核验。新增 4 项回归。

### 9.2 A06 服务端核算行渲染了错误数值（已修）

`rendered` 兜底用了 `claim_numbers(text)[0]`，于是：

- `CPU 最近10分钟使用率 12%` → 渲染 `cpu_usage_percent=10`（10 是窗口长度）
- `过去5秒内 CPU 使用率 12%` → 渲染 `=5`
- `日志中出现了上游超时` → 渲染 `metric=0`

即"服务端确定性核算"本身给出了证据中不存在的数字——正是本模块要杜绝的行为。

修复：新增 `_first_supported_reading()`，只挑**确实被证据支持**的读数用于渲染；
证据没有任何数值读数时渲染 `（无数值读数）`，不再编造 `metric=0`。
新增 2 项回归。

### 9.3 仍未修的已知边界（完整记录，不隐藏）

A06：

1. **科学计数法** `延迟 1.2e1ms` 解析为 1.2 并与证据 1.2 匹配。
2. **跨族 other 单位** `网络流量 12.5gb` 对 `mbps` 证据按数值通过。
3. **容差** `12.0000001%`、`13.012ms` 落在 `_ABS_TOL=0.05` 内。
4. **全角单位** `网络延迟 １２ｍｓ` 单位丢失，退化为纯数值匹配。
5. **无单位声明** `CPU 使用率 12` 按设计只核对数值。

A09（均为 writer 不可达的损坏 trace）：

6. 重复/非单调 `seq` → 可能跳过事件（真实 writer 的 seq 是单调计数器，140 份
   真实 trace 中 0 例）。
7. 全部 `seq == 0` → 首页重复，50 页封顶后 `stuck=true`。
8. `limit` 越界：边界无 seq 且剩余 ≤ `MAX_EVENT_LIMIT` 时最多返回 20000 条。
9. 32 MB 截断标记 `trace_truncated`：整份 trace 没有终态事件，UI 会显示不完整
   （分页本身不会卡住）。

这些属于"已知且如实标注"，不是"已修复"。

## V10. 依赖升级计划（2026-10-27，本轮未改动任何版本）

本节只记录**实测到的弃用信号**与修复草图，不修改 `pyproject.toml` /
`uv.lock` / `web/package.json`。当前实测版本：`pydantic 2.13.4`、
`pymilvus 2.6.15`、`ruff 0.17.1`、`node v22.17.0`。

### 10.1 PydanticDeprecatedSince20：class-based `config`

实测（`python -W always -c "import app.main"`）出现 2 条：

```
PydanticDeprecatedSince20: Support for class-based `config` is deprecated,
use ConfigDict instead. Deprecated in Pydantic V2.0 to be removed in V3.0.
  -> app/models/aiops.py
```

同一模式还出现在 `app/models/document.py`（导入路径没走到，但代码存在）。
影响：Pydantic V3 会移除 `class Config`，届时这些模型直接报错，而不是 warning。

**Owner-fix 草图**（模型层负责人，1 个 PR 可收）：

```python
# 旧
class Foo(BaseModel):
    class Config:
        populate_by_name = True

# 新
from pydantic import ConfigDict

class Foo(BaseModel):
    model_config = ConfigDict(populate_by_name=True)
```

验收：`python -W error::DeprecationWarning -c "import app.main"` 无
`PydanticDeprecatedSince20`；621+ 后端用例全绿（本轮为 635 passed）。
注意 `model_config` 的键名与 `class Config` 的属性名并不总是一一对应
（`allow_population_by_field_name` → `populate_by_name`），逐个核对。

### 10.2 PyMilvus 2.6 → 3.1：ORM 风格连接与 `utility.*` 弃用

实测导入期出现：

```
PyMilvusDeprecationWarning: connections.connect ... will be removed in PyMilvus 3.1
```

本仓库的弃用面（`app/core/milvus_client.py`）：

| 位置 | 现用 API | 3.1 之后 |
| --- | --- | --- |
| `milvus_client.py:80` | `connections.connect(alias="default", ...)` | `MilvusClient(uri=..., token=...)` |
| `milvus_client.py:145` | `utility.has_collection(name)` | `client.has_collection(name)` |
| `milvus_client.py:224-225` | `utility.load_state(name)` | `client.get_load_state(...)` |
| `milvus_client.py:285` | `Collection.num_entities`（已是 count(*) 退路） | `client.query(..., output_fields=["count(*)"])` |
| `Collection.query` / 全局 alias 下的增删改查 | ORM 风格 | `client.search` / `client.query` / `client.upsert` |

**Owner-fix 草图**（检索/向量库负责人，需真实 Milvus 验收）：

1. 先**不换版本**，只在 `milvus_client.py` 内部引入一层 `_client()` 工厂，把
   `connections.connect` 换成 `MilvusClient`，其余调用点暂时保留；
2. 逐个方法迁移：`has_collection` → `has_collection`、`load_state` →
   `get_load_state`、`num_entities` → `query(count(*))`，每个都保留旧分支到
   2.6 兼容期结束；
3. 用 G1.2 的真实 Milvus staging 脚本跑一轮导入/检索/恢复，**不能只用 fake
   store 的离线用例证明迁移成功**；
4. 最后才升 `pymilvus>=3.1` 并删掉兼容分支。

风险：`langchain-milvus 0.3.3` 仍依赖 `pymilvus` 的 ORM 接口，升级前必须先确认
它已发版支持 `MilvusClient`，否则会出现"我们迁了、上游没迁"的混合状态。

### 10.3 Ruff：顶层 lint 设置迁移

`ruff 0.17.1` 每次运行都打印：

```
warning: The top-level linter settings are deprecated in favour of their
counterparts in the `lint` section.
  - 'ignore' -> 'lint.ignore'
  - 'select' -> 'lint.select'
  - 'isort' -> 'lint.isort'
  - 'per-file-ignores' -> 'lint.per-file-ignores'
```

影响：目前只是 warning，未来 Ruff 小版本会直接忽略顶层键——那时 lint 规则会
**静默失效**（最危险的一种回归：CI 变绿但什么都没检查）。

**Owner-fix 草图**（构建/CI 负责人，纯配置改动）：

```toml
# pyproject.toml：把顶层的 select/ignore/isort/per-file-ignores
# 原样移动到 [tool.ruff.lint] / [tool.ruff.lint.isort]
[tool.ruff.lint]
select = [...]
ignore = [...]
```
验收：`ruff check` 无 deprecation warning，且 `ruff check --show-settings`
在迁移前后输出的规则集合完全一致（否则等于偷偷改了规则）。

### 10.4 前端测试：`node --experimental-strip-types`

`web/package.json` 的 `test` 脚本是：

```
node --experimental-strip-types --test src/sse.test.ts src/state.test.ts src/view.test.ts src/chat.test.ts
```

风险：
- 该 flag 在 Node 22 是实验特性，Node 23+ 已默认启用类型剥离，flag 变成噪音；
  未来版本可能直接拒绝未知的 `--experimental-*` flag。
- 它只做**类型擦除**，不做类型检查；`npm run lint`/`tsc` 不跑到测试文件时
  `.test.ts` 里的类型错误不会被发现。
- 只能用 Node 执行，vitest/jest 生态的覆盖率、watch、mock 能力都拿不到。

**Owner-fix 草图**（前端负责人）：

1. 近期：把 flag 收进一个 `scripts/test.sh` / npm script 变量，Node 版本判断后
   决定是否附加，避免每个 CI job 各写一份；
2. 中期：迁移到 `vitest`（原生 TS、可复用现有 `node:test` 断言风格），
   `test` 脚本改为 `vitest run`，并保留一份 `test:node` 作为对照；
3. 迁移验收标准：同一批 53 个前端用例在 vitest 下全绿，且 `npm run build`
   与 `npm run lint` 不退步。

### 10.5 本轮结论

- 以上四项**均未修改版本或配置**，只记录信号与修复路径。
- 三项（10.1/10.2/10.3）是"现在能跑、未来会断"的定时炸弹；10.4 是"现在能跑、
  但覆盖面比看起来窄"的隐性缺口。
- 真正的升级顺序建议：10.3（纯配置、零风险）→ 10.1（模型层，1 个 PR）→
  10.4（前端测试基建）→ 10.2（最后做，必须配真实 Milvus 验收）。

---

# V11. 第三轮：真实依赖闭环（2026-10-27）

上两轮（V1–V10）的结果全部是**测试替身验证**。本轮第一次拿到真实依赖证据。
分三档记录，不混写。

## V11.1 真实依赖验证（新增）

| 项 | 证据文件 | 关键结果 |
|---|---|---|
| 真实 Milvus 端到端 | `MILVUS_ACCEPTANCE_2026-10-27.md` + `eval/results/milvus-e2e-*.json` | 11 文档 / 953 chunks / COSINE 384；更新只返 active、回滚恢复、真实部分写入失败零残留、描述符错配 `invalid_index`、停机→重启同进程恢复 |
| Docker 镜像与 Compose | `DOCKER_ACCEPTANCE_2026-10-27.md` | 镜像 `sha256:7788efe30239…`；全栈 4 容器 healthy；401/200 鉴权；SIGTERM 退出码 0；杀 MCP 子进程后容器退出并按 `unless-stopped` 重启；4 个命名卷 + Milvus bind mount 重建后数据保留 |
| 真实云模型 | `CLOUD_SMOKE_2026-10-27.md` | 真实 LLM 跑通 cloud_multi（34.5s），`completed/sufficient`，观测数值可回证据；JSON 围栏/前缀/截断全部可恢复 |
| 当前构建浏览器 | `BROWSER_JOURNEY_2026-10-27.md` + `screenshots/` | 五档视口无横向溢出；替代入口/aria-live 正常；知识库面板连真实 Milvus；合成 IME 事件序列 |

## V11.2 本轮实测数字

| 检查 | 结果 |
|---|---|
| 后端 pytest | **717 passed, 1 skipped**（103.21s） |
| 前端 | **53 passed**；build / lint / tsc 通过 |
| ruff `F821,F811,F841` | 通过 |
| `docker compose config --quiet` | 通过 |
| 三个离线探针 | 全部 `bug_reproduced=false` |

## V11.3 本轮修掉的真实缺陷（都由真实依赖暴露，测试替身没抓到）

| 缺陷 | 暴露场景 | 影响 | 修复 |
|---|---|---|---|
| `web/package-lock.json` 有缺 `version` 的条目 | `docker build` 的 `npm ci` | 镜像**必然构建失败**，而本地 `npm install` 容忍 | 从官方 registry 重新生成 lock |
| `Collection.num_entities` 在 Milvus 2.5 刚写入后返回 0 | 真实 Milvus descriptor 校验 | 非空 collection 被当空 → 给错配向量空间**静默写描述符** | `collection_count()` 改 `count(*)` 优先 |
| `compose up app` 复用 2026-06 的陈旧镜像 | 真实 compose 启动 | 把旧代码当成“刚验收”；容器里没有 `capabilities`、没有 `principal.py` | 显式 retag + `--force-recreate`，并以镜像内文件核对 |
| `json.loads(model_reply)` 过脆 | 真实模型返回围栏/前缀/截断 | Reviewer 与 logs 各失败一次，报告退化为“没有观测” | `_extract_json()` + 按 finish_reason 区分的单次重试 |
| `statistics.count` / `total` 不被当事实 | 真实报告写“样本数 3”“日志仅 1 条” | 正确描述被判成“数值不存在”进缺口 | 以 `count` 单位收录为可引用事实（duration/percent 仍匹配不上） |

## V11.4 仍属未验证（与上轮相比的增量，不含全部历史项）

1. **SSH 只读冒烟**：未执行（无我获授权的 staging 主机；不伪造证据）。
2. **同名实例的浏览器点击**：容器内 MCP 是 stub，无同名双实例目录；仅有 HTTP/单测覆盖。
3. **作业失败/重试的浏览器操作**：容器里没有失败作业，`hasRetry=false`。
4. **断流续读 / tab 恢复 / 引用卡片**：浏览器侧未操作。
5. **真实中文 IME 进程与读屏器**：只有合成事件。
6. **ACL/OS/版本/有效期在候选阶段过滤**：目前只有 principal 归属，无文档级 ACL。
7. **多副本部署下的 trace 并发**：进程内锁，跨进程无互斥（子代理已如实标注）。
8. **费用**：网关无单价，未估算。

## V11.5 简历可写 / 不可写

**可写**（都有可复跑证据）：

- 双模式诊断 + 版本化 RAG（真实 Milvus v2.5.10，11 文档 / 953 chunks，COSINE 384）。
- 检索质量：77 条独立 blind 集，Recall@3 **0.983** / MRR **0.928** / nDCG@5 **0.945**（n=58），
  无答案误答 **0/19**；并给出“dev 拟合阈值不可泛化”的阈值-召回曲线。
- 真实 LLM 全链路（Supervisor→metrics/logs→Reviewer），观测数值由服务端按证据渲染。
- Docker 多阶段镜像可构建、全栈可启动、SIGTERM/子进程崩溃/卷重建均有实测。

**不可写**：

- “RAG 准确率 98.3%”——那是 Recall@5，且在 11 篇小语料上；没有完整问答正确率。
- “端到端回答率 50%”——那是 dev 阈值 0.6315 的结果，属阈值问题，不是模型能力。
- “支持多用户权限体系”——目前是单租户 + principal 归属检查。
- “SSH 已联调”“取消能终止远端生成”——两者都未验证。

# V12. 第四轮：G5 质量与生产化收尾（2026-10-28）

本轮完整结果、分母与限制单独记在
[G5_RESULTS_2026-10-28.md](G5_RESULTS_2026-10-28.md)，这里只留“本次实测了什么”的索引，
不复制数字（避免两处漂移）。**V11 及以上的历史记录保留，未覆盖、未删除。**

## V12.1 本轮实测（2026-10-28，工作区 HEAD `ed13769` + 未提交改动）

| 检查 | 结果 |
|---|---|
| 后端 pytest | **809 passed, 1 skipped, 1 warning**（164.42s） |
| 前端 | **70 passed**；`tsc -b` / `oxlint` / `vite build` 通过 |
| 真实 Milvus 校准 | 80 行 calibration、0 失败；全局 0.5135，分域 docker 0.606 / systemd 0.5676 / windows 0.5487 |
| blind 对照 | 用 0.5135 跑 frozen blind：Recall@5 0.9828，漏答 1/58，无答案误答 5/19 |
| 真实 LLM 生成评测 | 12 条，失败 0；引用支持 16/16（4 条 undecided），judge 6/8，确定性覆盖 4/9，无答案误答 1/3 |
| 检索前 ACL/OS/版本过滤 | 真实 Milvus 复合 expr 可执行；收窄 allowlist 只剩授权文档，空 allowlist 返回空 |
| 浏览器 | 同名双实例点选绑定 `ins-staging-b-0002`；真实引用卡片；SSE 断流按 seq 续读；刷新恢复；失败作业点重试到 ready |
| 镜像 | `sha256:d93d673ee7d2…` **10.7 GB**（原 18.9 GB），当前工作区重建，容器 healthy |
| 并发/长时间 | 4 进程 × 500 条 trace 无丢失/无半行；retention 扫描窗口饥饿已修；并发 retry 幂等 |

## V12.2 本轮修掉的真实缺陷（此前记录里没有的）

| 缺陷 | 暴露场景 | 修复 |
|---|---|---|
| `.venv-train/` 未进 `.dockerignore` | 真实 `docker build` | 构建上下文 5.33 GB → 18.5 MB，镜像 18.9 → 10.7 GB |
| `fastmcp 2.14.x` 的 `@mcp.tool()` 返回不可调用的 `FunctionTool` | `uv sync --frozen` 后三个 MCP server 的 9 条测试失败 | 锁文件升级到 fastmcp 3.4.0（装饰器返回原函数），23 处调用点不改 |
| `retry_job` 对已有 queued/running 重试不返回也不拒绝 | 8 线程并发 retry 实测产生 8 个新 job | 返回已有作业（`duplicate=true`） |
| `purge_expired_traces` 先 `[:200]` 再判 mtime | 250 新鲜 + 50 过期，连续 3 轮 deleted=0 | 上限改为“删除数”，按 mtime 升序遍历 |
| chat 事件不落盘，断流无法续读 | 浏览器模拟断流 | 新增 `app/services/chat_events.py` + `GET /api/chat/events/{run_id}?after_seq=`；前端按 `run_id+seq` 续读 |
| 生成层“模型自己拒答”被记成误答 | 真实 LLM 评测 | 文本层识别并**单列** `model_refusals_in_answered`，不改变门控统计 |

## V12.3 V11 中被本轮推翻/更新的表述

- V11.4 第 7 条“多副本部署下的 trace 并发：进程内锁，跨进程无互斥”
  → **已加 `<run_id>.jsonl.lock` 跨进程独占锁**（msvcrt/flock），有界等待 + 超时可见；
  同机 4 进程 × 500 条验证通过。POSIX 分支仍只有静态审查（本机是 Windows）。
- V11.4 第 6 条“ACL/OS/版本/有效期在候选阶段过滤：未实现”
  → **已实现**并在真实 Milvus 上验证复合表达式；诊断路径（`app/agent/aiops/*`）仍未接 principal。
- V11.4 第 4 条“断流续读 / tab 恢复 / 引用卡片：浏览器侧未操作”
  → 本轮在真实浏览器里完成，截图见 `screenshots/g5-*.png`。
- V11.4 第 3 条“作业失败/重试的浏览器操作” → 本轮完成（停 Milvus 制造 `cleanup_failed` → 点重试 → ready）。
- V11.4 第 2 条“同名实例的浏览器点击” → 本轮完成（staging 双实例目录 + 点第 3 张卡绑定 `ins-staging-b-0002`）。

仍未推翻的：SSH 只读联调未执行；真实中文 IME 进程与读屏器未验证；CUDA torch 未替换；
PyMilvus ORM 迁移未做；生成式评测只 12/80 条。

---

# V13. 第五轮：G6 权限与聊天流一致性（2026-10-28）

本轮只修具体的业务一致性问题；历史 V1–V12 的记录**原样保留**（不覆盖、不删除），
被本轮推翻或补全的表述在 §V13.3 显式列出。

## V13.1 本轮实测

| 项 | 结果 |
|---|---|
| 后端 pytest | **849 passed, 1 skipped, 1 warning**（94.27 s） |
| 前端 | **74 passed**；`tsc -b` / `oxlint` / `vite build` 通过 |
| 真实镜像 | `sha256:14417223cdbf…`，10 680 483 616 B（10.7 GB），当前工作区重建 |
| 运行配置 | `AUTH_MODE=deployment` + `API_KEY`（值不记录）；无 key 401、错 key 401、对 key 200 |
| RAG readiness | 同一时刻 `/health`：`status=healthy`（liveness）+ `milvus=connected` + `capabilities.rag=ready` |
| 知识作业 ACL（容器内） | 另一把 key：`/jobs` 为空、`/jobs/{foreign}` 404、`/jobs/{foreign}/retry` 404 且 store 作业数不变；自己上传后列表/详情/重试正常 |
| 聊天 run 拆分（容器内） | 同一诊断 run 两次追问 → 两个 chat run，文件 `seq` 各从 1 连续；跨 session 续读 403 且原文件字节数不变 |
| 评测快照一致性 | 真实 `calibration-20261027T191726Z.json` 与当前题集/manifest/descriptor/store 比对：**0 差异** |

## V13.2 本轮修掉的真实缺陷

| 缺陷 | 暴露方式 | 修复 |
|---|---|---|
| `list_documents` 先分页后过滤 | 阅读代码 + 新增翻页用例 | 过滤下沉到 store，先过滤再 status/cursor/limit，`next_cursor` 取过滤后页尾 |
| 作业路由无 principal 边界 | 容器内用第二把 key 复验 | 三个路由解析 key；单条不可见统一 404；retry 先鉴权再建作业 |
| 请求 `run_id` 当聊天事件文件名 | 单测 + 容器内两次追问 | 每次请求生成 `chat_run_id`，请求 `run_id` 只用于追问上下文 |
| 续读不校验事件归属 | 跨 session 复读 | `read_after(..., session_id=)`，不符抛 404；校验前置于去重 |
| `purge_expired()` 无调用点 | 全仓 grep 无调用 | 接入 lifespan 启动清理（不新增后台线程） |
| 网络异常不续读 | 前端新增用例 | catch 分支复用 EOF 的同一 `settleResume` |
| "停止"留下待恢复位置 | 前端新增用例 | abort 后清 `CHAT_PENDING_KEY` |
| 诊断路径知识检索无 principal | 新测试 `test_diagnosis_knowledge_principal.py` | owner 全链路传播；`need_principal_scope` 只在真需要时启用 |
| `except TypeError` 后无条件重查 | 新测试（内部 TypeError 必须失败） | 调用前签名判断；真实错误保持 `RetrievalUnavailable` |

## V13.3 对既有记录的修正

- V12 表里"后端 809 passed"是**上一轮**的数字；本轮为 **849 passed, 1 skipped**（新增 40 条）。
- V12 表里"前端 70 passed"；本轮 **72 passed**。
- G5 结果表中"诊断路径的知识检索仍未接 principal"**本轮已修**（旧 LangChain 工具路径除外）。
- G5 结果表中"chat 事件 TTL 无调用点"本轮已修；`chat_events` 的跨进程并发仍依赖
  `O_APPEND` 小写入原子性，未加跨进程锁（不变）。
- README 中"容器 healthy 即全栈可用"的旧读法已显式拆成 liveness 与 `capabilities`。

历史事实（当时的镜像 digest、当时的时间点、当时的 809/70）不回改也不删除。

## V13.4 对抗复核后追加修复的越权（同一轮内）

六维复核 + 逐条反驳（35 条原始发现 → 10 条确认）暴露出五处本轮新代码引入或放大的越权，
已在同一轮内修掉并加测试：

| 越权 | 后果 | 修复 |
|---|---|---|
| 读写共用 `_enforce_document`（只看 owner 侧表） | "有 ACL、无 owner"的文档对陌生 principal 的 activate/delete 放行 → 跨 principal 删除 | 读写拆分：读 `document_visible_to`（404）、写 `document_writable_by`（owner 优先，无 owner 才认 ACL 成员；403） |
| retry 只校验"可见" | 只读 ACL 成员能把 active revision 回滚到旧版本 | retry 要求写权限，不可见时 404 |
| 同名 source_uri 上传无归属校验 | 他人文档的 active revision 被替换成注入内容 | 上传/导入前预检，非法 → 409 |
| 删除后遗留作业重新可见 | 删除动作把 404 变 200，响应含 `source_uri`/错误信息 | 文档不存在时作业一律不可见 |
| claim 在 submit 之后 | claim 失败仍返回 202，上传者在自己的列表/详情里看不到刚上传的文档 | claim 提前到提交之前，失败即 409；ACL 补充留在提交之后 |

另外两处"文档照代码"的修正：`need_principal_scope` 简化为"有身份就启用"（报告 §6 已同步）；
`store_document_ids` 校验让"同一 embedder 换整批文档"能被识别。

---

# V14. 第六轮：G7 上传归属一致性、评测快照与发布收尾（2026-10-29）

身份：base `ed13769` / code `de9d36e` / HEAD `b27f9f4` / 本轮提交 `024aad3`。
历史 V1–V13 记录原样保留；本节只引用当前提交与当前产物。

## V14.1 本轮实测

| 项 | 结果 |
|---|---|
| 后端 pytest | **874 passed, 1 skipped, 1 warning**（102.06 s） |
| 前端 | **74 passed**；`tsc -b` / `oxlint` / `vite build` 通过 |
| 标准 Dockerfile 构建 | **成功**（仓库原始 `Dockerfile`，30 步，`--no-cache`） |
| 镜像 | `sha256:19b5710e2cfd…`，10 681 408 761 B，`revision=b27f9f4…`，`source=github.com/TanC8186/On_Call_Agent` |
| Compose | app / etcd / MinIO / Milvus 四服务 healthy；attu 因本机 8000 被无关进程占用未起 |
| 鉴权 | 无 key 401 / 错 key 401 / 对 key 200（deployment） |
| 能力分层 | liveness=healthy、milvus=connected、rag=ready、**mcp=degraded（未首次加载）**、diagnosis_local=unconfigured |
| 评测快照 | 真实 profile 对真实 store：0 mismatch / 0 evidence missing；store 清空后 4 条硬差异 |

## V14.2 本轮修掉的真实缺陷

| 缺陷 | 后果 | 修复 |
|---|---|---|
| metadata 校验晚于 owner 写入 | 非法 `valid_from` 返回 400 却留下幽灵 owner；同名上传永久 409 且 API 无法自愈（DELETE 404） | 校验前移到 claim 之前 |
| 失败上传私有化共享文档 | 一次 400 请求即可把"无 owner 共享文档"变成上传者私有 | 同上；claim 不再发生 |
| 提交失败不回滚 owner | 向量库故障后 owner 残留，同名重传被拒 | claim 记录 `owner_before`，只在本次新建时回滚；新增条件删除 `release_owner_if` |
| 读取谓词"ACL 非空即忽略 owner"（含 4 处调用点抑制） | ACL seal 失败时上传者 202 成功却看不到自己的文档/作业/检索结果 | 谓词改 `owner OR ACL`，四处调用点无条件读 owner |
| ACL seal 静默失败 | 部分成功被包装成完全成功 | 响应带 `acl_sealed=false` |
| 快照校验对空集合短路 | profile 13 份 vs 当前 0 份 → 0 条硬差异，静默复用旧阈值 | 空集合判硬差异；descriptor 空集合同理 |
| `consistency` 被无条件覆写 | `mismatch_ignored_explicit_threshold` 与 partial 被吞成 ok | 改 if/elif/else |
| `_store_evidence` UnboundLocalError | 损坏 store 抛未初始化变量，不是明确失败 | 结构化返回 `store_read_error`；main 明确报错 return 1 |
| 取样命名仍是 smoke | kind/前缀/help 继续用"冒烟"语义 | 统一 `_sampled` / `-sampled` / "抽样" |

## V14.3 与历史记录的差异

- G6 报告写"镜像用临时 `.g6-local.Dockerfile` + `--network host` 构建"——**本轮已用仓库原始 `Dockerfile` 重新构建成功**，临时变体不再参与，且已删除。
- G6 报告的身份表述（"未提交"/"dirty"）已统一为 base/code/HEAD 三字段。
- 本轮**未执行 `--pull`**：`registry-1.docker.io` 直连超时，基础镜像用本机已有副本。不得写成 `--pull --no-cache` 构建通过。
- 本轮未执行双模式对比（P2），也未改动任何生成层数字。

---

# V15. 第八轮（G8，2026-10-29）

## V15.1 实测数字

| 项 | 结果 |
|---|---|
| 后端测试 | 900 passed, 1 skipped（94.26 s） |
| 前端测试 | 75 passed（chat 18 / sse 15 / state 21 / view 21） |
| tsc / oxlint / build | 全过 |
| 镜像 | `oncall-agent:g8-final` = `sha256:6a7ff2b7…`，10 671 703 570 B |
| revision 标签 | `63d1a41cd8bec0c17e87c7abfeb86aad40d26dca` = 最终 HEAD `63d1a41` |
| Compose | 四个核心服务 healthy；attu 未启动（端口被无关进程占用） |
| 鉴权 | 无 key 401 / 错 key 401 / 对 key 200 |

双模式同题对比（固定 18 条，sha256 `89b54d10…`）：

| 指标 | local | cloud_multi |
|---|---:|---:|
| complete | 18/18 | 18/18 |
| 延迟 p50 / p95 | 3.768 s / 6.189 s | 17.889 s / 39.008 s |
| 输入 / 输出 token 均值 | 1047.8 / 231.3 | 5964.1 / 1338.9 |
| 工具调用成功率 | 0.355 | 0.279 |
| 计划工具召回 / F1 | 0.875 / 0.533 | 1.000 / 0.160 |

工具调用成功率两边都低，原因是真实云账户侧错误
（CLS 未开通、指标命名空间无效、实例不可用），不是模型或代码问题。

## V15.2 本轮修掉的真实缺陷

| 缺陷 | 危害 | 修法 |
|---|---|---|
| `.dockerignore` 裸 glob 不跨目录 | **未加密 RSA 私钥真实存在于镜像层**（`/app/servers/AgentTest.pem`），293 个 `.pyc` 一并入镜像 | 敏感模式全部 `**/` 前缀 + 补 ppk/id_rsa/p12/pfx；新增 24 条测试（在旧文件上失败 6 条） |
| Supervisor JSON Schema 回显 | 约 1/3 云端运行直接失败；通用重试指令只是让模型再抄一次 schema | `_looks_like_schema_echo` 识别 + 专门纠正指令；实测 5/8 → 8/8 |
| compose `app:` 无 `image:` | `up --force-recreate` 复用 `on_call_agent-app:latest`，验收跑的是旧代码 | `image: ${APP_IMAGE:-on_call_agent-app:latest}`，验收用 `APP_IMAGE` 钉 digest |
| 评测 gold 用不可执行工具 | 4/5 场景 gold `search_log` 在唯一目标上必被拒绝（无 topic） | 改用 `search_service_logs`；`validate_eval.py` 抓不到这类问题 |
| 本地 severity 越界分支缺文案 | 用户看不出是否偷偷切云端 | 统一为"本地模式已停止，不会调用在线模型"，前端 `localFailureNote` 只对 local 生效 |
| README 写"失败时自动回退云端" | 与 `allow_cloud_fallback=False` 直接冲突 | 删除该表述并补"为什么同时支持本地与云端" |

## V15.3 与历史记录的差异

- G7 文档里"`servers/AgentTest.pem` 已被 `.dockerignore` 排除"的结论**是错的**：
  本轮用探针镜像实测确认它在构建上下文里，且镜像层里真实存在该文件。已在
  `G8_RESULTS` §2.1 更正。
- 双模式对比的口径修过一次：初版把发现工具 `list_all_services` 算进计划 F1 的
  分母，而它在两条链路里都是**规划之前**就执行的绑定步骤、永远不会出现在 plan
  事件里，导致云端被系统性扣分。改为计划指标只比"计划里能出现的工具"、发现调用
  单独统计后，云端计划召回从 0.50 变为 1.000、F1 从 0.152 变为 0.160（本地
  相应从 0.889/0.665 变为 0.875/0.533）。这一修正对结论方向有实质影响，故保留说明。
- 本轮**未执行 `--pull`**（registry 直连超时），构建走构建期代理参数，
  不写成可复现 CI 证据。
- `training/RESULTS.md` 的 v9/v11 数字本轮未重跑，未采用任何新的"模型能力"结论。
