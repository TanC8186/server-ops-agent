# eval/rag — 检索评测集与结果

本目录存放 RAG 检索的两个数据集和它们的测量脚本。**两个集合用途不同，数字不能互换**。

| 文件 | 行数 | 用途 | 阈值 |
|---|---|---|---|
| `dev.jsonl` | 14 | 开发/调试：拟阈值、看排序 | 在它自己身上拟合 |
| `blind.jsonl` | 77 | 盲测：只用来报告，不许回头调 | 沿用 dev 拟合值 |

## dev 与 blind 的划分（诚实说明）

`dev.jsonl` 的 14 条问题是先写出来的，`no-answer` 阈值（0.6315）也是在它上面
按"分开两类的最优切点"拟合的（见 `scripts/eval_retrieval.py` 的
`_fit_threshold`）。因此：

- **dev 上的 Recall@3=1.0 / MRR=1.0 / no-answer precision=1.0 不是泛化结论。**
  11 条可答题的满分只说明"切分+embedding+检索契约能跑通"，且这 14 条被反复看过。
- `blind.jsonl` 的题在写的时候只对照 `knowledge/normalized/` 的语料原文，
  `notes` 里逐条抄录了一句锚点原文（`锚点=<SOURCE-ID>「…」`），
  由 `tests/test_blind_eval_schema.py` 和 `scripts/eval_blind.py --dry-run`
  校验该短语**确实存在于对应文档正文中**。写题时没有拿检索结果排序做任何调整。
- blind 集**没有**参与阈值拟合。`scripts/eval_blind.py` 会读 dev 结果文件里的
  `threshold`，并在产物里写 `fitted_on_blind: false`；若拿不到 >0 的阈值它直接
  报错退出，而不是退化成 0（0 会让门控形同虚设，把误答率写成 0 这种假结论）。

## blind.jsonl 的构成

格式（每行一个 JSON 对象）：

```json
{"id":"B001","question":"…","expected_ids":["SD-01"],
 "expect_no_answer":false,"category":"zh_to_en_manual","notes":"… 锚点=SD-01「…」"}
```

`expected_ids` 只能是 11 个 core 来源之一：`SD-01 SD-02 LK-01 RH-01 DK-01 DK-02
DK-03 WS-01 WS-02 WS-03 WS-04`。

| category | 条数 | 说明 |
|---|---|---|
| `zh_to_en_manual` | 12 | 中文问题检索英文手册 |
| `exact_command` | 13 | 精确命令/参数/错误码 |
| `os_version_conflict` | 8 | Linux vs Windows、版本/平台冲突 |
| `no_answer` | 11 | 语料中无依据 |
| `misleading_trap` | 10 | 相似但不支持结论（易误答） |
| `code_config` | 9 | 代码 / 完整命令 |
| `prompt_injection` | 6 | 题面含"忽略以上指令"类注入 |
| `lifecycle_refusal` | 8 | active revision / ACL / 索引不可用 → 期望拒答 |
| **合计** | **77** | 其中 `expect_no_answer=true`：**19** 条 |

## 测量脚本

### `scripts/eval_retrieval.py`（离线基线，dev）

在内存里按 manifest 重新切分 + 本地 embedding + cosine 检索，不碰 Milvus。
用于"Milvus 不在时也能给出可复现证据"，产 `eval/rag/results/offline-*.json`。

### `scripts/eval_blind.py`（盲测，真实 Milvus）

走真实的 `VectorStoreRetriever` → 真实 Milvus collection，并额外跑一次
`knowledge_service._search`（检索+门控，与 `answer_question` 内部同一条路径）。

```bash
# 只校验 schema 与锚点，不连 Milvus
./.venv/Scripts/python.exe scripts/eval_blind.py --dry-run

# 真实评测（需 localhost:19530 有 Milvus 且已导入 11 个 core 文档）
./.venv/Scripts/python.exe scripts/eval_blind.py \
    --store data/acceptance/milvus-e2e-store.json
```

产物 `eval/rag/results/blind-<UTC>.json` 含 `manifest`（git commit、dirty、
语料 manifest hash、embedding descriptor hash、collection/metric/实体数、
阈值来源）与 `denominators`。

## 指标口径

- **Recall@k / MRR / nDCG@5**：分母是 `expected_ids` 非空的可答题（58 条）。
  逐 chunk 判定相关性（chunk 所属文档 ∈ expected 即相关）；nDCG 按**文档去重后的
  首次出现位置**计增益，否则同一文档的多个 chunk 会把 DCG 累加到 >1。
- **无答案误答率**：分母是 `expect_no_answer=true` 的 19 条。检索+门控仍返回
  高于阈值的命中即一次误答。
- **端到端应答题被回答比例**：可答题里门控放行的比例。它与 Recall@k 的落差
  就是门控的代价——只看 Recall 会把"检索到了但没敢答"藏起来。
- 任一行出错（不可用/超时/索引非法）单独计数进 `errors`，不静默丢弃。

## 已知口径限制

1. **这是检索质量，不是回答质量。** 没有 LLM 参与，所以这里**不测**引用是否
   支持结论、是否编造事实。`misleading_trap` 行的 `expected_ids` 标的是"该答的
   资料在哪"，不是"模型是否会被引导答错"。
2. **单机、单 collection、953–957 条实体、11 篇文档。** 语料规模很小，
   指标不随语料规模缩放；不能推断大语料下的表现。
3. **阈值来自 14 条 dev 题**，样本极小。blind 上 `min_answerable_top_score=0.3515`
   低于 `max_no_answer_top_score=0.5818`，说明**两类在 blind 上有重叠**——
   dev 的完美分离是小样本假象。这正是本次盲测最重要的发现之一。
4. **文档 ID 映射需要 store 文件。** manifest 的 `document_id` 与运行时
   `document_id_for(source_uri)` 的派生方式不同（前者 `source_id|url`，后者只用
   url），脚本以 store 里记录的 `source_uri` 为准反查，并把无法映射的 ID 记进
   `id_mapping`。

## G5：calibration 集与分域 gate（2026-10-28 追加）

上面前 3 条限制指向同一件事：**这批语料不存在一个干净的全局阈值**。dev 的
0.6315 在 blind 上误拒 29/58 可答题；阈值 0 又让 19/19 无答案题被误答；blind
的分数分布本身重叠（可答 min 0.3515 < 无答案 max 0.5818）。G5 因此新增一套
独立的 calibration 集与分域 gate，**blind 保持 frozen 不动**。

### `calibrate-dev2.jsonl`（80 行，阈值在这里拟合）

| 项 | 值 |
|---|---|
| 行数 | 80（可答 56 / 无答案 24，可答占比 0.700） |
| 类别 | 沿用 blind 的 8 类，每类 8–14 条 |
| 追加字段 | `os_family` / `product` / `product_version`（取自 normalized manifest）、`reference_points`（参考答案要点，供事实正确率判定） |
| 锚点 | 可答题每行 `notes` 含 `锚点=<SOURCE-ID>「原文片段」`，片段逐字存在于 `knowledge/normalized/<ID>/*.md` |
| 无答案行 | `expected_ids=[]`，`category` 为 `no_answer` / `lifecycle_refusal`，notes 写明"语料 11 篇正文均无该主题" |

校验器与 blind 共用 `scripts/eval_blind.py` 的 `_ANCHOR_PATTERN` /
`_anchor_problems`（没有第三份实现）；calibration 额外要求：

- 域字段必须与 manifest 一致（product 写错会让 gate 用到别家产品的阈值）；
- 可答题必须有非空 `reference_points`；
- 可答题占比必须落在 0.60–0.80。

```bash
./.venv/Scripts/python.exe scripts/eval_blind.py --set eval/rag/calibrate-dev2.jsonl --dry-run
```

`--set` 跑 blind 以外的题集时，产物改叫 `calibration-retrieval-<UTC>.json`，
`kind`/`report_label` 都是 `calibration`，并在 payload 里带
"本文件是 calibration 集，不能作为盲测结论复述"。判据是**内容 sha256**
而不是文件名：把 blind 复制改名后仍然会被认成 blind。

### `scripts/eval_calibration.py`（分域 gate 拟合）

```bash
# 只校验题集
./.venv/Scripts/python.exe scripts/eval_calibration.py --dry-run

# 真实 Milvus 上拟合
./.venv/Scripts/python.exe scripts/eval_calibration.py \
    --store data/acceptance/milvus-e2e-store.json
```

产物 `eval/rag/results/calibration-<UTC>.json` 的关键结构：

```json
"gate_profile": {
  "mode": "domain", "global": 0.5135,
  "by_category": {"exact_command": 0.6382, "...": 0.0},
  "by_product": {"docker": 0.606, "systemd": 0.5676, "...": 0.0},
  "fallback": "global",
  "fitted_on": {"dataset": "eval/rag/calibrate-dev2.jsonl", "sha256": "...",
                "rows": 80, "answerable_rows": 56, "no_answer_rows": 24,
                "fitted_on_blind": false,
                "store_evidence": {"corpus_manifest_sha256": "...",
                                   "embedding_descriptor_sha256": "...",
                                   "embedding_descriptor_hash": "...",
                                   "knowledge_store_file": "data/acceptance/milvus-e2e-store.json",
                                   "knowledge_store_sha256": "...",
                                   "store_document_descriptor_hashes": ["sha256:..."]}}
}
```

`fitted_on.store_evidence` 是 G6 补的快照证据：阈值是在哪份语料 manifest、
哪个 embedding descriptor、哪个 store 上拟合的。`eval_generation.py` 读 profile
时会拿它和当前磁盘/当前 store 逐项比对，对不上就停止（`gate.consistency` 记录
比对结果），避免换 collection 后静默复用旧阈值。

同时给每个桶的 `rows` / `answerable_samples` / `no_answer_samples` /
`tp/fn/fp/tn` / `recall` / `false_answer_rate` / `threshold` / `fallback` /
`fallback_reason`。

口径与限制：

- 判定规则 `top_score >= threshold` 记为 answered，与 `knowledge_service._gate`
  同向。切点选 **balanced accuracy** 最大者，平局取**更高**阈值——误答代价
  高于漏答，这个取舍写在产物的 `tie_break` 字段里。
- 桶内可答题样本 < `--min-bucket-samples`（默认 8）或缺一个类别时**回退全局**，
  写 `fallback: true` + `fallback_reason`，不假装拟合过。
- **`by_category` 只是诊断桶**：category 是评测标注，运行时没有这个值，所以
  每个 category 桶标 `runtime_selectable: false`。**`by_product` 才是生产可读的
  维度**（标 `runtime_selectable: true`），前提是候选过滤已经把产品确定下来。
- 无答案行没有产品归属，product 桶的负样本取自**全局无答案行**，逐桶标
  `negatives_source: "global_no_answer_rows"`；这不是"该产品域内的误答率"。
- 传 `--set eval/rag/blind.jsonl` 会**直接退出**（不是在 blind 上拟合）；
  产物的 `gate_profile.fitted_on.fitted_on_blind` 恒为 `false`。
- 产物里的 `in_sample` 是**样本内**指标（阈值就在同一批数据上拟合），不能当泛化结论。

### `scripts/eval_generation.py`（真实 LLM 生成层）

```bash
# 只校验题集 + SYSTEM_PROMPT/_build_messages 组装，不连 Milvus、不调模型
./.venv/Scripts/python.exe scripts/eval_generation.py --dry-run

# 抽样生成评测（此处 3 条；默认 12 条）
./.venv/Scripts/python.exe scripts/eval_generation.py \
    --limit 3 --store data/acceptance/milvus-e2e-store.json
```

走 `knowledge_service.answer_question` 完整路径（同检索、同门控、同
`SYSTEM_PROMPT`、同 `_build_messages`、同服务端引用消毒），只把模型换成
`.env` 里的真实 `LLM_*`。产物 `eval/rag/results/generation-<UTC>.json` 三层指标
**分别报告，不合成"问答准确率"**。产物的 `kind` 固定为
`generation_eval_llm_sampled`（不按 `--limit` 分档）；历史产物文件名与它们自己的
旧 `kind` 保留不动，以维持 provenance。读取 `gate_profile` 前会核对题集 sha256、
语料 manifest、embedding descriptor 与当前 store 的 descriptor hash，不一致直接
停止（见 `gate.consistency`）。

| 指标 | 分母 | 说明 |
|---|---|---|
| `citation_support_rate` | 答案里**被使用**的引用数 | 确定性 token 检查（引用所在句的数值/命令/路径是否出现在该片段正文）；抽不到 token 记 `undecided_no_token`，不算支持也不算不支持 |
| `removed_citations_count` | — | 服务端消毒移除的伪造引用，单列，不进分母 |
| `factual_correct_rate` | 可答题数 | `deterministic_mean_coverage`（reference_points 覆盖率）与 `judge_correct_rate`（第二次 LLM 判定）**双轨并列** |
| `no_answer_false_answer_rate` | `expect_no_answer=true` 行数 | 门控放行且 `outcome=answered` 才算误答；**模型自己在答案里明确说没有依据**（`looks_like_refusal`，且答案里没有代码片段）的单独记在 `model_refusals_in_answered`，既不算误答也不算门控的功劳 |
| `gate_answered` / `model_refusals_in_answered` | 同上分母 | 前者=门控放行条数，后者=其中被模型自己拒答的条数；两者与 `false_answers` 的关系是 `gate_answered = model_refusals_in_answered + false_answers` |

`--limit` 默认 12，按 category 轮转取样（确定性），保证每个类别都有代表、
无答案行不会被挤掉。失败（超时/网关过滤/解析失败）进 `failure_denominators`，
不丢弃；judge 的 token 用量单独记 `judge_tokens`。

## G5 真实运行结果（2026-10-28，本机 Milvus `biz`）

**实体数在本次会话期间变过**：现有产物里记到的是 **960 / 963 / 1005 / 1008 / 1010**
五种（同一台机器上还有并行 agent 在跑验收导入，往同一个 `biz` collection 追加文档）。
两次完整校准（`165119Z` 记 1005、`191726Z` 记 1008）逐行比对：**79/80 行的
`top_score` 与 `top_doc_ids` 完全相同，只有 C067 第 4 名多出一个未映射文档**
（`doc-834421b82dba`）——说明追加的文档**确实可能**挤进 top-5，所以"换个 collection
重跑得到同样的数"不能当默认假设。两次的全局阈值都是 0.5135，C067 又落在阈值之外，
因此结论不变；但引用产物时必须按它**自己**记的 `milvus.num_entities` 读，不要凭记忆。

| 产物 | 关键数字 |
|---|---|
| `calibration-20261027T165119Z.json`（1005 实体） | calibration sha256 `247b0891…75f3c`；全局阈值 **0.5135**（TP 52 / FN 4 / FP 2 / TN 22，recall 0.9286、误答率 2/24=0.0833、balanced accuracy 0.9226）；样本内（product+global）recall 51/56=0.9107、误答 2/24=0.0833；失败 0/80 |
| `calibration-retrieval-20261027T165238Z.json`（1005 实体） | 同一 threshold 0.5135 走完检索+gate：Recall@1 0.9107 / Recall@3 0.9821 / Recall@5 1.0，无答案误答 2/24=0.0833，可答被拒 4/56 |
| `blind-20261027T165340Z.json`（1005 实体，**对照，不是新结论**） | 把 calibration 的 0.5135 直接套到 frozen blind：无答案误答 5/19=0.2632（阈值 0 时是 19/19），可答被拒 1/58；Recall@5 0.9828 不变 |
| `generation-20261027T165427Z.json`（1005 实体） | 模型 `DeepSeek-V4.1-Flash-1017`（anthropic 兼容网关），3 条（可答 2 / 无答 1）；引用支持率 1/1=1.0（另 1 条 `undecided_no_token` 不计入分母），事实确定性覆盖 2/2=1.0、judge 2/2=1.0，无答案误答 0/1；失败 0/3；judge tokens 1086 |

多次抽样生成评测的引用条数在 1–3 之间波动（模型措辞不同），说明**引用条数不是稳定量**，
引用支持率的分母每次都要按当次产物读，不能跨次比较。

抽样生成评测用的阈值来自 `gate_profile`（C039 → docker 0.606、C012 → windows 0.5487、
C071 → global 0.5135）。第一次跑时读到的是同目录的 `calibration-retrieval-*.json`
（没有 `gate_profile`），阈值静默退化成 `config.rag_min_score=0`，无答案行 C071
因此被"回答"。已修：loader 只认带 `gate_profile` 的产物，跳过原因写进
`gate.profile_skipped`，并有回归测试锁定。

注意 calibration 的分数分布：可答 min 0.3961 / 无答案 max 0.5493 —— **仍然重叠**，
全局阈值在 calibration 上已经是"两害相权"，不是干净切点。分域桶（docker 0.606、
systemd 0.5676、windows 0.5487、exact_command 0.6382）把误答压到 2/24，但这是
样本内结果；是否泛化只能等新的独立集（不是 blind）验证。

**把 calibration 阈值搬到 blind 的后果值得单独记住**：误答率从 100%（阈值 0）降到
26.3%（5/19），代价是 58 条可答题里只有 1 条被误拒。这说明阈值方向是对的，但
26.3% 的误答率也说明**单个全局阈值不够**——这正是分域 profile 存在的理由，
而不是"再调一格就好了"。这份 blind 产物只是对照，blind 本身没有被用来选任何参数。

### G5 新增限制

1. **by_category 不能直接用于生产**（运行时没有 category）；真要按类别分域，
   得先有一个可靠的问题分类器，并单独评测它的准确率。
2. **product 桶的负样本来自全局**，产品域内的真实误答率尚未测到——需要
   每个产品各自的"语料里没有依据"的题。
3. **抽样生成评测这次只有 3 条**，且包含 1 条 prompt_injection；这是抽样评测而非结论。
   全量 12 条能覆盖 8 个类别，但 judge 调用会让耗时与 token 明显上升。
4. **blind 未被触碰**（sha256 `a42aed35…3253`，`tests/test_calibration_eval.py`
   逐字节校验）。任何"在 blind 上再调一次阈值"的做法都会被这条测试拦住。
5. **calibration 与 blind 主题高度重合**（同一批 11 篇文档、同一批主题）：它能
   用来拟合，但**不能证明泛化**。真正的泛化证据要等新语料上的新题集。
6. **collection 实体数在本次会话里变过多次**（960 / 963 / 1005 / 1008 / 1010；
   并行验收往同一 `biz` collection 导入）。两次完整校准有 79/80 行分数逐条相同，
   但 C067 的 top-5 里确实多出过一个未映射文档——所以不能假设"再跑一次数不变"。
   要产出可复现的数字，需要独占 collection 或固定快照。
