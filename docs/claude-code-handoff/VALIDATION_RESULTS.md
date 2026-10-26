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
| 后端全量 pytest | `./.venv/Scripts/python.exe -m pytest tests -p no:cacheprovider -q -o addopts= --tb=line --disable-warnings -rs` | **589 passed, 1 skipped, 2 warnings，90.15s**（exit 0） |
| 前端单测 | `cd web && npm test` | **48 passed, 0 fail** |
| 前端构建 | `cd web && npm run build` | 通过，产物 `assets/index-vsa-hn5h.js` 320.57 kB / `index-Zmx7MErZ.css` 28.86 kB |
| 前端 lint | `cd web && npm run lint` | 通过（oxlint 无输出） |
| Python 静态检查 | `./.venv/Scripts/python.exe -m ruff check app servers scripts --select F821,F811,F841` | All checks passed（去掉过期的 F841 未用变量） |
| Compose 配置 | `docker compose config --quiet` | 通过（exit 0） |
| Docker daemon | `docker info --format '{{.ServerVersion}}'` | 29.8.2（可用） |
| skip | `tests/test_p0_run_paths.py:134` | 环境不允许创建符号链接，仍是 **未执行**，不是通过 |

收集总数从 528 → 589（+61），新增测试文件：
`test_acceptance_boundaries.py`(12)、`test_acceptance_targets_budget_paging.py`(12)、
`test_claim_boundaries.py`(17)、`test_parent_backfill.py`(5)、`test_runs_api.py` +5（abandoned/retention）；前端 +4。

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
