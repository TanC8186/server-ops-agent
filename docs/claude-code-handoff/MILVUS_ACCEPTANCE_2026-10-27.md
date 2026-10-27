# 真实 Milvus 端到端验收（2026-10-27）

对象：当前工作区。Milvus **真实运行**（`docker compose up -d etcd minio milvus`），
`milvusdb/milvus:v2.5.10` + `quay.io/coreos/etcd:v3.5.18` + `minio/minio:RELEASE.2023-03-20`。
embedding 是**真实本地 MiniLM**（`paraphrase-multilingual-MiniLM-L12-v2`，384 维，COSINE），
不是 fake store、不是内存向。

复跑命令：

```
.\ .venv\Scripts\python.exe scripts/acceptance_milvus_e2e.py
```

## 1. 语料与 collection

| 项 | 值 |
|---|---|
| core 文档 | 11（SD-01/02, LK-01, RH-01, DK-01/02/03, WS-01..04） |
| 索引结果 | **11/11 ready**，共 **951** chunks |
| collection | `biz` |
| 维度 / 度量 | 384 / **COSINE** |
| 索引 | `IVF_FLAT`, `nlist=128` |
| embedding 描述符哈希 | `sha256:589648f22e0d…` |
| 完成后实体数 | **953**（含后续 revision 测试写入） |
| `needs_review` 许可 | SD-02、LK-01、RH-01（保持未升级，见 BLIND_EVAL 审计节） |

证据：`eval/results/milvus-e2e-2026-10-27T*.json`。

## 2. 查询（真实 Milvus 检索）

6 条问题全部命中（每条 top-3），示例：

| 查询 | 命中 | top1 分数 | 定位 |
|---|---|---|---|
| 磁盘满了怎么排查 | 3 | 0.606 | `doc-314931ac42a5` L266-L267 |
| systemctl 重启服务 | 3 | 0.718 | `doc-4ca55974894e` L897-L899 |
| journalctl 查看日志 | 3 | 0.838 | `doc-8f6ba5b76ee0` L711-L713 |

## 3. active revision：更新与回滚

| 步骤 | 观测 |
|---|---|
| 同名文档先导 OLD，再导 NEW | 两个 job 都 `ready`，`active_revision` 指向 NEW |
| 检索 | **只返回 NEW_SENTINEL**，OLD 不可见（953 实体里旧向量仍在，只是不是 active） |
| `activate_revision(OLD)` | 返回 `active_revision=old` |
| 再检索 | **只返回 OLD_SENTINEL** |

即“保留旧向量用于回滚”与“旧版不作为当前知识”同时成立。

## 4. 真实部分写入失败清理

在**真实 Milvus** 上把 `add_documents` 包成“先写一行再抛错”：

| 项 | 观测 |
|---|---|
| 失败 job 状态 | `failed`，`error.code=cleanup_failed`（清理也失败时）或 `index_failed` |
| 残留可见 | **否**（`BROKEN_RESIDUE_QQQ` 检索不到） |
| 旧 active | **仍可检索**（`GOOD_BASELINE_ABC` 命中） |

重试路径：清掉残留后 `retry` → `ready`，且不产生重复实体。

## 5. 描述符错配

把 `config.embedding_model` 改成不存在的模型后探测状态：

```
status = invalid_index
reason = embedding 描述符与 collection 不一致（provider/model/normalization 变化）；请显式重建索引
```

**不是** `unavailable`，也**没有**静默复用旧向量空间。

## 6. 停机 → 重启 → 同进程恢复

| 阶段 | 观测 |
|---|---|
| 初始化成功 | `True`，`failure_kind=None` |
| 停掉 Milvus 后第 1 次调用 | 抛 `VectorStoreUnavailable`，`failure_kind=transient` |
| 仍在停机时第 2 次调用 | **重新尝试连接**（10.1s 超时后再次失败），不是直接抛缓存 |
| `docker start milvus-standalone` 后 | **同一个 manager 实例第 1 次重试即恢复**（`failure_kind=None`） |
| 数据持久化 | `collection_count()` 仍为 953（bind mount `./volumes/milvus`） |

无需手工 `reset()`、无需重启进程。

## 7. 顺带修掉的真实缺陷

**`Collection.num_entities` 在 Milvus 2.5 上刚写完返回 0**（`count(*)` 立刻是 953）。
影响：`_verify_descriptor()` 用 `collection_count()` 判断“collection 是否为空”，
旧实现会把**非空的** collection 判成空、于是给一个错配的向量空间**静默写入新描述符**，
后续所有检索都会在错误的度量/模型上工作。

修复：`milvus_client.collection_count()` 改为 `query(count(*))` 优先，
`num_entities` 仅作退路。实测从 `0` 变为 `953`。

## 8. 本轮没有验证的

- **多副本并发**：per-path trace 锁是进程内锁；Milvus 侧未做多客户端并发写压测。
- **大规模**：11 文档 / 953 chunks，没有做 10 万级容量与延迟曲线。
- **真实问答（LLM 参与）**：`answer_question` 的端到端只在单元测试与 blind 检索层验证，
  未在真实 Milvus + 真实模型上跑完整问答并核对引用支持率。
- **ACL/OS/版本/有效期过滤**：目前是归属校验（principal），**不是**按文档 ACL/版本
  在候选阶段过滤；这一点在计划里属未完成项。
