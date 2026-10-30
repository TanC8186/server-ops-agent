# AutoDL 官方语料与 RAG 入库报告（2026-10-30）

## 1. 结论

**AutoDL 官方文档已真实抓取、规范化并写入 Milvus。** 9 个官方来源全部成功，
9 篇文档、183 个 chunk 进入知识库，`target_kind=ssh` 过滤在真实 Milvus 上生效。

状态：`VERIFIED`（抓取/规范化/入库/过滤均有实际输出佐证）。

## 2. 抓取（9/9）

来源全部来自任务书 §3.1 的固定清单，不递归、不接受用户输入 URL：

| ID | 页面 | 字节 |
|---|---|---:|
| AD-01 | `https://www.autodl.com/docs/env/` | 50 202 |
| AD-02 | `.../docs/quick_start/` | 48 443 |
| AD-03 | `.../docs/image/` | 47 058 |
| AD-04 | `.../docs/instance_data/` | 47 795 |
| AD-05 | `https://api.autodl.com/docs/gpu/` | 55 517 |
| AD-06 | `.../docs/scp/` | 49 152 |
| AD-07 | `.../docs/qa/` | 54 211 |
| AD-08 | `.../docs/fs/` | 50 519 |
| AD-09 | `https://api.autodl.com/docs/instance_pro_api/` | 83 550 |

复现：

```powershell
python scripts/fetch_corpus.py --only AD-01   # 逐个抓，或
python scripts/fetch_corpus.py                # 抓全部 core 阶段来源
```

## 3. 修复的三个代码阻塞

### 3.1 抓取白名单不含 autodl.com（P0）

`scripts/fetch_corpus.py:49-58` 的 `ALLOWED_HOSTS` 只有 8 个域名，无 AutoDL。
任何 AutoDL URL 都会被 `:110-111` 拒绝（`域名不在白名单`）。

修：加入 `www.autodl.com` 与 `api.autodl.com`。原有的**重定向目标校验**保留不变
（`:129-131` 会校验 `final_url` 的 host 仍在白名单内）。

### 3.2 manifest 路径硬编码旧日期（P0）

`scripts/normalize_corpus.py:243` 写死 `import-2026-10-26.json`，而
`fetch_corpus` 写的是 `import-<UTC日期>.json`。于是**新抓的快照永远读不到**，
normalize 反复处理同一份旧清单——抓取看起来成功、语料却不变。

修：改为 显式参数 > `AIOPS_CORPUS_MANIFEST` 环境变量 > 目录内最新
`import-*.json`。已验证自动选中当天 manifest 并读到 11 条历史条目。

### 3.3 正文长度阈值误判真实页面（P1）

`MIN_BODY_CHARS = 400` 把 AD-03 判成 `needs_review`。实测该页正文去空白后
388 字，内容是完整的"镜像保存/加载/更换/共享"说明，并非空壳。

这条规则的本意是**拦截 JS 渲染后只剩几十字导航的空壳**，不是要求每页写成长文。
改为 200，足以区分"空壳"与"短而完整"。改后 AD-03 正常入库。

## 4. 正文抽取

从 `<article>` 容器提取，丢弃 `script`/`style`/`nav`/`footer`/`aside`。
这是必须的：直接对整页去标签，得到的大半是**侧边栏导航**
（"容器实例 概要 JupyterLab SSH 升降配置…"），不是知识。

验证样例（AD-01 归一化结果）：

```
# 概述¶
实例的系统盘与数据盘一般为本地SSD磁盘…重要的数据请定期备份到文件存储或本地
## 容器实例是什么?¶
容器实例是使用Docker技术进行资源划分与隔离的Container…
## 实例GPU/CPU/内存的分配¶
在AutoDL租用GPU的方式为：选择物理主机…
```

## 5. 入库结果

产物目录（任务书 §3.3 要求）：

```
knowledge/sources/autodl/
  raw/         9 个 HTML
  normalized/  9 个 Markdown
  notices/     9 个许可说明
  manifest.json
```

每个 chunk 携带的 metadata：

```json
{
  "product": "autodl",
  "os_family": "linux",
  "target_kind": "ssh",
  "source_kind": "manual",
  "product_version": "<记录值>",
  "source_uri": "https://www.autodl.com/docs/..."
}
```

入库命令：

```powershell
python scripts/ingest_autodl_corpus.py --dry-run   # 先看
python scripts/ingest_autodl_corpus.py             # 真正入库
```

结果：**9/9 作业 ready**，183 个 chunk，总正文 16 031 字符。

## 6. 修复的两个"看起来做了、实际没生效"缺陷

### 6.1 target_kind 加进白名单却没落盘（P0）

`METADATA_TEXT_FIELDS` 加了 `target_kind` / `source_kind`，但文档记录构造处
（`knowledge_store.py:606` 附近）只写了 `os_family` / `product` /
`product_version`。结果 snapshot 里 `target_kind` **永远是 None**，过滤条件
永远匹配不上。

实测：入库后查 snapshot，`product=autodl` 正确、`target_kind=None`。

修：新建文档时写入、已存在文档时 `setdefault` 补齐，两处都改。

### 6.2 没有 principal 时过滤被静默绕过（P0）

`VectorStoreRetriever._visible()` 只在 `principal_id` 非空时才应用 filters，
否则直接返回全部 active revision。

实测：`filters=RetrievalFilters(target_kind="cloud")` 仍然返回 autodl 的
ssh 文档——**过滤功能等于不存在**。

修：新增 `KnowledgeStore.filtered_revisions()`（只按元数据过滤、不做 ACL），
有 filters 而无 principal 时走它。给了条件就必须生效。

### 6.3 顺带：delete_document 未清理已完成作业

`delete_document` 只把 `running` 改成 `cancelled`，`ready` 的作业原地保留。
重新导入同一文件时 `submit_document` 命中 `_find_job_locked` 直接返回，
**文档记录永不重建**。实测出现过 Milvus 有行、store.json 里 0 篇文档的状态。

修：删除文档时把该文档所有 `ready`/`failed` 作业一并取消。

## 7. 过滤验收（真实 Milvus）

| 过滤条件 | 命中 | 结果 |
|---|---:|---|
| `product=autodl, target_kind=ssh` | 3 | 实例中的目录 / SCP远程拷贝 |
| `target_kind=cloud` | **0** | 正确排除全部 autodl 文档 |
| `product=autodl` | 3 | 同上 |
| 无过滤 | 3 | 含历史语料候选 |

`target_kind=cloud` 返回 0 条，是过滤真正生效的关键证据——修复前它和
`target_kind=ssh` 返回完全相同的 3 条。

## 8. 未完成

1. **许可状态全部 `needs_review`**：9 个 AutoDL 页面均未找到显式许可声明，
   仅作为平台操作说明引用，未复制大段原文。
2. **两套 store 并存**：本机 `data/knowledge/store.json` 与容器命名卷
   `oncall-data` 是两份独立数据。本轮在本机完成入库验证后，又在容器内重新
   执行了一次入库（语料通过 `docker cp` 送入）。这两份**没有自动同步机制**，
   属于已知限制。
3. **manifest 的 document_id 与 RAG document_id 不是同一个值**：前者是内容
   sha 派生，后者由 `document_id_for(source_uri)` 生成。追溯需要靠
   `source_uri` 关联，不能直接对 ID。
