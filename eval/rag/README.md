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
