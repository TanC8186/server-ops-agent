# G2.2 / G2.1 盲测检索评测与语料审计

日期：2026-10-27（UTC）。工作区：`E:\On_Call_Agent\on_call_agent`。
基线：后端 700 passed / 1 skipped（本次新增 17 条 schema 测试后）。

本文件只记录**我实际跑出来的数字**，以及这些数字**不能证明**什么。
凡是需要真实依赖才能下的结论，都标了它的证据来源。

---

## 0. 一句话结论

在**真实 Milvus**（collection `biz`，COSINE，384d，957 条实体）上跑 77 条盲测题：

- 纯检索：Recall@1=0.879 / Recall@3=0.983 / Recall@5=0.983，MRR=0.928，nDCG@5=0.945；
- 无答案：19 条全部拒答（误答率 0.000）；
- **但端到端只有 50.0%** 的可答题被门控放行——dev 拟合的 0.6315 阈值在 blind 上
  太严，两类分数**有重叠**（最低可答 0.3515 < 最高无答案 0.5818）。

即：**检索不是瓶颈，阈值是瓶颈。** dev 上"完美分离"是小样本假象。

---

## 1. 盲测集（`eval/rag/blind.jsonl`，77 行）

### 1.1 为什么是 77 条而不是 100 条

能凑出 77 条**逐条可审计**的题（`notes` 里抄了原文锚点、脚本启动时校验该短语
确实在正文中）。剩下的缺口是真实的语料缺口，不是偷懒：

- 11 篇 core 文档里 SD-01/SD-02 极长（347/152 chunk），能出很多题；但
  WS-01（10 chunk）、DK-03（4 chunk）、RH-01（10 chunk）、DK-01（9 chunk）
  篇幅太短，能支撑的**有依据**的问题就少。
- 没有 YAML 类内容，所以 `code_config` 只能出"完整命令"而非 YAML（9 条已是
  语料上限附近）。
- 我没有为了凑数而写"看起来合理但语料里查不到"的题——那会让 Recall 变成噪音。

### 1.2 分布

| category | 条数 | 覆盖要求 |
|---|---|---|
| `zh_to_en_manual` | 12 | 中文问题检索英文手册 |
| `exact_command` | 13 | 精确命令/参数/错误码 |
| `os_version_conflict` | 8 | Linux vs Windows、版本/平台冲突 |
| `no_answer` | 11 | 无答案（要求 ≥8） |
| `misleading_trap` | 10 | 相似但不支持结论 |
| `code_config` | 9 | 代码/完整命令 |
| `prompt_injection` | 6 | 提示注入 |
| `lifecycle_refusal` | 8 | active revision / ACL / 索引不可用 |
| **合计** | **77** | 其中 `expect_no_answer=true`：**19** |

### 1.3 可审计性怎么保证（不是口头承诺）

每行 `notes` 形如：

```
依据=SD-01 --failed 选项原文；中文问句→英文手册。 锚点=SD-01「List units in failed state. This is equivalent to --state=failed.」
```

三道校验：

1. `scripts/eval_blind.py --dry-run`：`_anchor_problems()` 打开
   `knowledge/normalized/<id>/*.md`，断言引号内短语逐字存在、锚点来源 ∈
   `expected_ids`、且属于 11 个 core id。
2. `tests/test_blind_eval_schema.py`：17 条测试，含"锚点指向的来源必须有
   normalized 正文""注入类题面必须真的含注入标记""可答题必须有锚点"等。
3. 构建脚本在写盘前先跑同一套锚点断言（25 行最初漏了标签、1 行注入题面缺标记，
   都是被自己的测试拦下来的，不是事后补的说明）。

**反例已验**：故意注入"锚点短语不在正文中""锚点来源不在 expected_ids""expected_ids
用了未入库的 NX-01"三类脏行，校验器全部报错（见 §5 的命令输出）。

---

## 2. 评测环境（manifest）

| 项 | 值 |
|---|---|
| git commit | `444944da587b5a40f0313c62c43263555d5b0360` |
| git dirty | **true**（工作区有其他并行改动，本结果不绑定干净树） |
| 语料 manifest | `knowledge/manifests/normalized-2026-10-26.json` |
| manifest sha256 | `a0ce0ef8f89af80a1bde358b1cb56a5dac8307b5591d4bf90c988fcc5ff1262e` |
| embedding descriptor | `589648f22e0df491e887c16b02d393c5a11b786657aead70cfb2d89f900b6ebc` |
| embedding 模型 | `sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2`，384 维，L2 归一 |
| Milvus | `localhost:19530`，collection `biz`，metric **COSINE**，index `IVF_FLAT(nlist=128)` |
| 实体数（运行时） | **960**。store 侧共 13 份文档 / 15 个 revision / **953** chunk（其中 11 篇 core = 951 chunk，2 篇验收临时文档各 1 chunk）；Milvus 多出的部分是被 superseded 但仍保留的旧版本向量（供回滚），它们被 active revision 过滤掉、不参与检索 |
| knowledge store | `data/acceptance/milvus-e2e-store.json` |
| blind.jsonl sha256 | `a42aed35caa5e88db2b907a82867a8f4c2af905d398187ec5d11780f5b183253` |
| 阈值 | **0.6315**，来源 `eval/rag/results/offline-20261026-225925.json`（dev 拟合），**未在 blind 上拟合** |
| 结果文件 | `eval/rag/results/blind-20261027T113540Z.json` |

> ⚠️ 实体数是**活的**：观测到 955 → 957 → 960，说明有**其他进程正在往
> 同一个 collection 写**（并行 agent 的验收文档）。本次指标不受影响（新增的都是
> 验收临时文档，且带自己的 active revision），但复验时实体数一定对不上——
> 必须引用 `manifest.milvus.num_entities` 与 `blind_file_sha256`，不要凭记忆。
> 若要在干净环境复验，应改用独立的 collection 名（见 §9.4）。

---

## 3. 指标（含分母）

### 3.1 分母

| 分母 | 值 |
|---|---|
| `total` | 77 |
| `answerable_rows`（expected_ids 非空） | 58 |
| `no_answer_rows`（expect_no_answer=true） | 19 |
| `answered`（门控放行） | 29 |
| `refused` | 48 |
| `answerable_refused`（可答但被拒 = 漏答） | **29** |
| `errors`（不可用/超时/索引非法） | **0** |

### 3.2 纯检索（分母 58 条可答题）

| 指标 | 值 |
|---|---|
| Recall@1 | **0.8793** |
| Recall@3 | **0.9828** |
| Recall@5 | **0.9828** |
| MRR | **0.9282** |
| nDCG@5 | **0.9446** |

Recall@5 唯一未命中：**B032**「这份内核内存文档正文对应哪个内核版本？」——
期望 LK-01，实际 top3 是 SD-02/SD-02/DK-02。原因是"版本"这个词在 systemd 手册里
密度更高，而 LK-01 的版本号 `7.3.0` 孤立在页头。这是**真实的可解释失误**，
不是标注错误。

### 3.3 无答案与门控（分母 19 条无答案 / 58 条可答）

| 指标 | 值 | 分母 |
|---|---|---|
| 无答案误答率 | **0.0000**（0/19） | 19 |
| 拒答正确率 | **1.0000** | 19 |
| 端到端应答题被回答比例 | **0.5000**（29/58） | 58 |
| 端到端"回答且命中" | **0.5000**（29/58） | 58 |

被误拒的 29 条按类别：`zh_to_en_manual` 7、`misleading_trap` 5、`prompt_injection` 5、
`exact_command` 4、`os_version_conflict` 4、`code_config` 4。

### 3.4 按类别

| category | n | Recall@1 | Recall@5 | MRR | nDCG@5 |
|---|---|---|---|---|---|
| `zh_to_en_manual` | 12 | 0.9167 | 1.0000 | 0.9444 | 0.9692 |
| `exact_command` | 13 | 1.0000 | 1.0000 | 1.0000 | 1.0000 |
| `os_version_conflict` | 8 | 0.8750 | 0.8750 | 0.8750 | 0.8750 |
| `misleading_trap` | 10 | 0.9000 | 1.0000 | 0.9500 | 0.9631 |
| `code_config` | 9 | 0.7778 | 1.0000 | 0.8889 | 0.9180 |
| `prompt_injection` | 6 | 0.6667 | 1.0000 | 0.8333 | 0.8770 |
| `no_answer` | 11 | — | — | — | — （误答率 0.0，11/11 拒答） |
| `lifecycle_refusal` | 8 | — | — | — | — （误答率 0.0，8/8 拒答） |

`exact_command` 满分是**可预期的**：这些题含 `--now`、`-MaxEvents`、`-Exclude` 等
字面 token，MiniLM 对字面重合很敏感。它**不代表**语义理解好。

### 3.5 延迟（真实 Milvus，77 次查询，含网络与本地 embedding）

| 指标 | 值 |
|---|---|
| search p50 / p95 | **48.7 ms / 85.7 ms** |
| gate p50 / p95（检索+门控，单次完整路径） | **46.2 ms / 80.6 ms** |

注意 p95 在不同运行间抖动明显（三次运行 search p95 见过 85.7 / 156.4 / 81.9 ms），因为本机 Milvus 与其他
进程共用，且首次调用含模型加载与 collection 加载。**这不是压测数字。**

### 3.6 分数分离（本次最重要的发现）

| 项 | 值 |
|---|---|
| 阈值（dev 拟合） | 0.6315 |
| blind 可答题最低 top1 分数 | **0.3515** |
| blind 无答案最高 top1 分数 | **0.5818** |
| 是否重叠 | **是**（0.3515 < 0.5818） |

dev 上 `min_answerable(0.6315) > max_unanswerable(0.5699)`，看着"完美分离"。
blind 上两类直接交叠：没有任何单一阈值能同时拿到"全拒无答案"和"高比例回答可答"。

这不是脚本 bug，是**14 条 dev 题不足以定位阈值**的直接证据。要修，需要
（a）更多带标注的可答/不可答题来重新拟合，或（b）放弃单一全局阈值，改用
`lifecycle/domain` 感知的分档策略。本次**没有**去调阈值——那会让 blind 变成 dev。

---

## 4. 语料审计（G2.1）

### 4.1 三个 `needs_review` 来源

判据：只有找到**逐文件/逐页的文本证据**才算有依据。仓库级许可声明不替代页面级检查。

#### SD-02 — journalctl（`knowledge/notices/systemd/SD-02.md`）

| 项 | 结论 |
|---|---|
| 现状态 | `license_id=""`，`license_status=needs_review` |
| 实际证据 | `knowledge/_evidence/systemd_journalctl.xml` 第 4 行：`<!-- SPDX-License-Identifier: LGPL-2.1-or-later -->` |
| 旁证 | `systemd_LICENSES_README.md`：「Unless otherwise noted, the systemd project sources are licensed under … **LGPL-2.1-or-later**」；`systemd_tag_v262.json` 固定 tag v262（object sha `1f7dffaf714cc4488159c498b7a17cf34c5a96d5`） |
| 与 SD-01 的一致性 | SD-01 已按同一文件级证据标为 `per_file_identifier_observed` / `LGPL-2.1-or-later` |
| **仍缺什么** | ① `journalctl.xml` 的**上游 commit pin**（现在只有 tag v262 的 ref，没有该文件在 v262 下的 blob sha）；② 抓取的是 freedesktop 生成的 HTML，其与 `journalctl.xml` 的对应关系只能靠内容比对，未留 diff 记录 |
| **结论** | 文本证据**存在且明确**，但我**没有**替它改状态——改 `needs_review` 属于许可判断，需要 permit 决定。**建议**升级为 `LGPL-2.1-or-later` / `per_file_identifier_observed`，并在 notice 里补 lineage commit |

#### LK-01 — Linux 内核内存管理概念（`knowledge/notices/linux-kernel/LK-01.md`）

| 项 | 结论 |
|---|---|
| 现状态 | `license_id=""`，`license_status=needs_review` |
| 实际证据 | `knowledge/_evidence/k_concepts.rst`（10787 B，对应页面源文件）**首 200 字符内没有 SPDX 头**；`SPDX` 出现次数 = **0** |
| 旁证 | `linux_COPYING`：`SPDX-License-Identifier: GPL-2.0 WITH Linux-syscall-note`；`k_index_rst` 有 `SPDX-License-Identifier: GPL-2.0`；`linux_license-rules.rst` 自身也是 `GPL-2.0` |
| 缺口 | 内核仓库**按文件**声明许可，`Documentation/admin-guide/mm/concepts.rst` **本身**没有 SPDX 行，也无法从页面上读到。用 `COPYING`（整仓 GPL-2.0）推断到具体文件，属于**继承性推断**，不是逐文件证据 |
| 额外风险 | `k_concepts.rst` 无版本固定（docs.kernel.org 是滚动文档，页面显示 7.3.0），`k_tag_v66.json` 是 v6.6 的 tag，与 7.3.0 不一致 |
| **结论** | **保持 `needs_review`。** 缺的是"该 .rst 文件在固定 commit 下的 SPDX 行或等价声明"。补齐方式：抓 `https://git.kernel.org/pub/scm/linux/kernel/git/torvalds/linux.git/plain/Documentation/admin-guide/mm/concepts.rst?h=vX.Y` 的**首 20 行**并存 hash |

#### RH-01 — RHEL 9 性能监控概览（`knowledge/notices/rhel/RH-01.md`）

| 项 | 结论 |
|---|---|
| 现状态 | `license_id=""`，`license_status=needs_review`，`evidence_url` 未记录 |
| **找到的证据** | `knowledge/_evidence/rhel_legal_notice_rh01.html`（canonical = `https://docs.redhat.com/en/documentation/red_hat_enterprise_linux/9/html/monitoring_and_managing_system_status_and_performance/legal-notice`）正文：「Except as otherwise noted below, the text of and illustrations in this documentation are licensed by Red Hat under the **Creative Commons Attribution–Share Alike 3.0 Unported** license …」+「waives … Section 4d of CC-BY-SA」+ 商标段 |
| 这是**页面级**还是仓库级？ | **页面级**——该 legal-notice 页与 RH-01 属于**同一个 documentation set**（同一 `monitoring_and_managing_system_status_and_performance` 路径），不是泛化的 Red Hat 全站条款 |
| 还缺什么 | ① 该 legal-notice 页自带的「Except as otherwise noted below」意味着**可能存在第三方例外**，而 `rhel_legal_notice_rh01.txt` 里 `Apache`/`GPL`/`MIT`/`third` 出现次数均为 **0**，说明"otherwise noted below"部分在该页没有列出例外——但也不能排除例外散落在正文各章；② 缺抓取时的 doc revision / 更新时间 |
| **结论** | 证据比我预期强：**有页面级 CC-BY-SA 3.0 声明**。建议升级为 `license_id="CC-BY-SA-3.0"`、`license_status="repository_or_publisher_license_observed"`、`evidence_url` 指向上面那个 legal-notice 的 canonical URL，**并**在 notice 里写明"该 documentation set 的 legal-notice 页声明 CC-BY-SA 3.0，第三方例外待逐章核对"。同样**我没有替它改判** |

### 4.2 DK-03 正文质量（`docker container inspect`）

| 项 | 数值 |
|---|---|
| normalized 字符数 | **600**（去空白 **512**） |
| 标题数 | **3**（`# docker container inspect`、`## Description`、`## Options`） |
| 原始 HTML 字节 | 430,477 |
| 原始可见文本 | 21,363 字符（绝大部分是导航/页脚/脚本） |
| 正文 `<article>` 结束位置 | 第 421,125 字节（全文 430,477） |
| 是否 mid-table 截断 | **否** |
| 最后一个表格行 | `-s, --size` → `Display total file sizes`（表格完整闭合，`</tbody></table></div></article>`） |

**关键判定：这不是提取失败，是源页面本身就这么短。**

我用 Docker 官方为同一页提供的 canonical markdown
（`https://docs.docker.com/reference/cli/docker/container/inspect.md`，
本次抓取 HTTP 200，681 字节）做了对照：

```
# docker container inspect
**Description:** Display detailed information on one or more containers
**Usage:** `docker container inspect [OPTIONS] CONTAINER [CONTAINER...]`
## Description …
## Options
| `-f`, `--format` |  | Format output using a custom template … |
| `-s`, `--size` |  | Display total file sizes |
```

内容**逐节一致**：该页本来就只有 Description + 两个 option，没有 Examples 段。

| 项 | 结论 |
|---|---|
| 正文完整吗 | **完整**——与上游 canonical markdown 一致，没有截断 |
| 够不够索引 | **够索引，但价值低**：4 个 chunk 只能回答"`-s` 是什么""`--format` 支持 json/TEMPLATE"，回答不了"如何只读查看容器状态"这类需要示例的问题 |
| 建议 | 保留（它是 DK 系列唯一讲 `inspect` 的页面），但在 manifest 里标注 `coverage_note: page_is_short_no_examples`，避免后续把"检索不到示例"误判成切分 bug |

> 附带发现（顺带修正我自己之前的怀疑）：原始 HTML 里 `Examples` 出现 2 次，都在
> **导航栏链接**里（`/reference/api/engine/sdk/examples/`），不是本页章节。所以
> "normalized 里没有 Examples"不是抽取丢失。

---

## 5. 复现命令与输出

```bash
# 1) schema + 锚点校验（不连 Milvus）
./.venv/Scripts/python.exe scripts/eval_blind.py --dry-run
# => blind.jsonl: 77 行 / 8 类 / expect_no_answer 19
# => --dry-run：schema 校验通过，未连接 Milvus，未写结果文件。

# 2) 真实评测
./.venv/Scripts/python.exe scripts/eval_blind.py \
    --store data/acceptance/milvus-e2e-store.json
# => Collection: biz metric=COSINE entities=960
# => Recall@1=0.8793 Recall@3=0.9828 Recall@5=0.9828
# => MRR=0.9282 nDCG@5=0.9446
# => 无答案误答率=0.0 拒答正确率=1.0
# => 端到端: 应答题被回答比例=0.5 回答且命中=0.5
# => 分母: {'total': 77, 'answered': 29, 'refused': 48, 'no_answer_rows': 19,
#           'answerable_rows': 58, 'answered_rows': 58, 'answerable_refused': 29, 'errors': 0}
# => 延迟: {'search_p50': 48.7, 'search_p95': 85.7, 'gate_p50': 46.2, 'gate_p95': 80.56}

# 3) 全量后端测试
./.venv/Scripts/python.exe -m pytest tests -p no:cacheprovider -q -o addopts= --tb=line -rs --disable-warnings
# => 700 passed, 1 skipped, 12 warnings in 120.59s

# 4) lint
./.venv/Scripts/python.exe -m ruff check app servers scripts tests --select F821,F811,F841
# => All checks passed!
```

**校验器反例验证**（证明 schema 测试不是摆设）：

```
injected problems:
  - B001: 锚点短语不在 SD-01 正文中: '这句原文根本不存在 in systemctl'
  - B002: 锚点来源 WS-04 不在 expected_ids 内
  - B003: expected_ids 超出 11 个 core id: ['NX-01']
  - B003: 锚点来源 SD-02 不在 expected_ids 内
```

---

## 6. 过程中修掉的两个真 bug（都在评测脚本里）

诚实记录，因为不写出来就等于隐藏了"第一版数字是错的"：

1. **文档 ID join 错误 → Recall 全 0。**
   第一版用 `normalized manifest` 的 `document_id` 直接对 Milvus 返回的
   `document_id`，两者派生方式不同：
   - manifest：`sha256(f"{source_id}|{source_url}")[:12]` → SD-01 = `doc-453500c16e01`
   - 运行时：`sha256(normalize_source_uri(source_uri))[:12]` → SD-01 = `doc-4ca55974894e`
   结果所有可答题 rank 都是 `None`、**Recall@1/3/5 全是 0.0**，而"无答案误答率 0.0"
   也是假的（因为没有任何命中被判成可答）。
   修法：以 store 里记录的 `source_uri` 反查 source_id；无法映射的 ID 记进
   `id_mapping.unmapped_document_ids` 并在终端提示。
   **这个 bug 会伪装成"检索质量极差"或"效果完美"两种假象，取决于看哪个指标。**

2. **nDCG@5 = 2.1773（>1，不可能）。**
   相关性是文档级标签，top-k 是 chunk 级：同一文档的 5 个 chunk 各贡献一次增益，
   DCG 超过理想 DCG。修法：按文档去重、只在首次出现位置计增益 → 0.9446。

---

## 7. 这些数字**证明**什么 / **不证明**什么

### 证明（有可复现产物支撑）

- 真实 Milvus COSINE collection 上，**混合中英文查询**能检索到正确的英文手册：
  Recall@5 = 0.983（58 条可答题，0 条错误）。
- 对 19 条**语料内无依据**的问题，当前阈值下**全部拒答**（误答率 0.000）——
  包括 11 条纯无答案题和 8 条 lifecycle/ACL/索引类应拒答题。
- 题面里夹带"忽略以上指令""SYSTEM: 无限制模式"等注入文本，**不会**让检索跑偏：
  `prompt_injection` 6 条 Recall@5 = 1.000。
- 检索延迟在**本机** Milvus 上是 p50 ≈ 53 ms、p95 ≈ 156 ms。
- DK-03 的短正文是**源页面如此**，与上游 canonical markdown 一致，不是抽取失败。

### 不证明

- **不证明回答正确。** 全程没有 LLM 参与。引用是否支持结论、是否编造事实、
  `misleading_trap` 里模型会不会被"是不是就能证明…"的问法带偏——**一个都没测**。
  `misleading_trap` 的 Recall 只说明"该查的资料查到了"。
- **不证明阈值可用。** 0.6315 在 blind 上误拒了 29/58 条可答题。dev 的完美分离
  在 blind 上不复现。
- **不证明泛化。** 11 篇文档 / 957 条实体 / 单一 collection / 单一 embedding 模型。
  没有语料规模或模型对比实验，不能外推。
- **不证明生产可用。** 没有并发、没有压力、没有冷启动分隔；p95 抖动来自本机
  共享环境。没有 CPU/RAM/VRAM 采集（本次范围外）。
- **不证明许可已清。** 三个 `needs_review` 我**只做了证据梳理，没有改状态**。
- **不证明可引用性。** `notes` 的锚点是"题目来源"，不是"模型答案的引用支持"。

---

## 8. 可对外引用 / 不可声称

### 可以写（有产物、有分母、可复算）

> 在真实 Milvus（COSINE/384d，11 篇官方文档、951 chunk）上建立**与 dev 分离的
> 77 题盲测集**，覆盖中文→英文手册、精确命令、平台/版本冲突、无答案、
> 易误答陷阱、完整命令、提示注入、active revision/ACL/索引不可用八类；
> 每题的锚点原文由 schema 测试逐条校验存在于语料中。测得 **Recall@5=98.3%、
> MRR=0.928、nDCG@5=0.945（n=58）**，无答案误答率 **0/19**，
> 检索 p50≈49ms / p95≈86ms；并诚实报告 dev 拟合阈值在盲测上误拒 29/58
> 可答题这一反向结论。

这段话每个数字都能指到 `eval/rag/results/blind-20261027T113540Z.json`。

### 不可以写

- ❌ "RAG 准确率 98.3%" —— 那是 Recall@5（检索），不是回答准确率。
- ❌ "零误答，无幻觉" —— 只证明 19 条无答案题被拒答，没测过任何生成内容。
- ❌ "盲测全部通过 / 阈值已验证" —— 恰恰相反，阈值在盲测上暴露了过严的问题。
- ❌ "大规模语料验证" —— 11 篇文档不构成规模。
- ❌ "许可已全部澄清" —— 三个 `needs_review` 维持原状。
- ❌ 把 700 个后端测试说成"RAG 质量验证" —— 那些测试测的是代码契约。

---

## 9. 后续建议（有优先级）

1. **扩题再拟合阈值**：把可答题做到 ~150 条、无答案 ~40 条，用 dev+新集重新拟合；
   或者干脆承认单一全局阈值不成立，按 `source_kind`/`lifecycle` 分档。
2. **补 generative 层评测**：固定模型与 prompt，测"引用是否支持结论"和
   `misleading_trap` 上的实际误答率——这才是 G2.3 的核心。
3. **补齐三处许可 lineage**：SD-02 的 commit pin、LK-01 固定版本下的 .rst SPDX 行、
   RH-01 的 doc revision；补完再统一改状态（需要 permit 拍板）。
4. **隔离 Milvus**：评测专用 collection 名，避免验收文档混入（现在实体数会被
   其他进程改变）。

---

## 阈值扫描（真实数据，2026-10-27，blind 77 条）

`top_score` 分布（真实 Milvus + 真实 MiniLM 余弦相似度）：

| 集合 | n | min | p25 / median | max |
|---|---:|---:|---:|---:|
| 可答题 | 58 | 0.3515 | 0.5975 / 0.6319 | 0.9375 |
| 无答案题 | 19 | 0.3462 | — / 0.4718 | 0.5818 |

阈值扫描（TP=可答且过阈值，FP=无答案却过阈值）：

| 阈值 | TP | FN | FP | TN | recall | 无答案误答率 |
|---:|---:|---:|---:|---:|---:|---:|
| 0.00 | 58 | 0 | 19 | 0 | 1.000 | 1.000 |
| 0.30 | 58 | 0 | 19 | 0 | 1.000 | 1.000 |
| 0.40 | 57 | 1 | 16 | 3 | 0.983 | 0.842 |
| 0.45 | 57 | 1 | 12 | 7 | 0.983 | 0.632 |
| 0.50 | 57 | 1 | 7 | 12 | 0.983 | 0.368 |
| 0.55 | 49 | 9 | 1 | 18 | 0.845 | 0.053 |
| 0.58 | 46 | 12 | 1 | 18 | 0.793 | 0.053 |
| **0.60** | 41 | 17 | **0** | 19 | 0.707 | **0.000** |
| 0.6315（dev 拟合） | 29 | 29 | 0 | 19 | 0.500 | 0.000 |
| 0.65 | 26 | 32 | 0 | 19 | 0.448 | 0.000 |

**结论（可复算）**：

1. **两集合的分数分布严重重叠**（可答题 min 0.3515 < 无答案题 max 0.5818），
   单一相似度阈值无法同时达到高召回与零误答——这是嵌入模型与问题的真实局限，
   不是实现 bug。
2. **默认 `rag_min_score=0.0` 是危险配置**：19/19 无答案题全部被当作可答。
   生产环境不能沿用 0.0。
3. **dev 拟合的 0.6315 明显偏保守**：在同一批 blind 数据上把 29/58 可答题判成
   无答案（recall 0.500）。dev 只有 14 条、11 可答/3 无答案，样本太小，
   阈值不可外推——这直接实证了原计划里"dev 拟合不可当泛化结论"的要求。
4. **本数据上的最优平衡点是 0.60**（recall 0.707、无答案误答 0）。但它是**在
   blind 上调出来的**，因此只能作为"下一轮在**新的** held-out 集上验证的候选值"，
   不能作为已验收的生产阈值。
5. 若业务更怕误答，0.55（recall 0.845、误答 5.3%）更合适；若更怕漏答，
   0.50（recall 0.983、误答 36.8%）——**必须由产品决策，不能由代码偷偷定**。

**不可声称的推断**：不得声称"RAG 准确率 0.98"。可写的是：
"在 77 条独立 blind 集上，Recall@3 = 0.983、MRR = 0.928、nDCG@5 = 0.945；
门控阈值在 dev 上拟合后泛化不足（recall 0.500），据此给出阈值-召回/误答曲线"。
