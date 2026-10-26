# 运维知识库来源与入库实施规格

核验日期：2026-10-26。配套目录：[knowledge_sources.json](knowledge_sources.json)。

本轮已通过联网浏览核验 **9 类官方来源、25 个手册页面/入口**，其中核心 11、扩展 9、可选 5。此交付是来源清单和实施规格；尚未批量下载原始手册、生成向量或写入 Milvus。不得将来源数量写成已入库文档数或检索质量指标。

## 1. 已核验的公开来源

| 来源 | 建议页面/章节 | 覆盖场景 | 入库范围与限制 |
|---|---|---|---|
| Red Hat RHEL 9 | [性能监控工具概览](https://docs.redhat.com/en/documentation/red_hat_enterprise_linux/9/html/monitoring_and_managing_system_status_and_performance/overview-of-performance-monitoring-options)、[文件系统管理](https://docs.redhat.com/en/documentation/red_hat_enterprise_linux/9/html/managing_file_systems/index) | CPU、内存、I/O、存储 | 文件系统页面是手册入口，只扩展所选章节，不能把目录当作正文；版本为 RHEL 9 |
| systemd | [systemctl](https://www.freedesktop.org/software/systemd/man/latest/systemctl.html)、[journalctl](https://www.freedesktop.org/software/systemd/man/latest/journalctl.html)、[资源控制](https://www.freedesktop.org/software/systemd/man/latest/systemd.resource-control.html) | 服务失败、日志时间窗、cgroup 限制 | 当前滚动页显示 262；需匹配目标主机实际版本，不能把新选项用于旧系统 |
| Linux Kernel | [内存管理概念](https://docs.kernel.org/admin-guide/mm/concepts.html)、[v6.6 PSI](https://www.kernel.org/doc/html/v6.6/accounting/psi.html) | cache/reclaim/OOM、资源压力 | 内核文档是机制资料，不能单独证明当前机器发生了 OOM；PSI 还取决于内核配置 |
| NGINX | [入门手册](https://nginx.org/en/docs/beginners_guide.html)、[代理模块](https://nginx.org/en/docs/http/ngx_http_proxy_module.html) | 代理、上游、502/504、超时 | 保留 directive 的版本与商业版条件；手册示例不自动成为允许执行的命令 |
| Docker | [日志](https://docs.docker.com/engine/logging/)、[资源限制](https://docs.docker.com/engine/containers/resource_constraints/)、[container inspect](https://docs.docker.com/reference/cli/docker/container/inspect/) | 容器日志、状态、OOM、CPU/内存限制 | 区分 Linux 与 Windows daemon；保留 logging driver 与 cgroup 前提 |
| Kubernetes | [Pod 排障](https://kubernetes.io/docs/tasks/debug/debug-application/debug-pods/)、[Service 排障](https://kubernetes.io/docs/tasks/debug/debug-application/debug-service/)、[DNS 排障](https://kubernetes.io/docs/tasks/administer-cluster/dns-debugging-resolution/) | 集群工作负载、Service/DNS | 可选扩展：当前项目没有完整 K8s 工具能力，不要因此宣称能自动排查集群；按集群 minor 冻结版本 |
| Prometheus | [PromQL 基础](https://prometheus.io/docs/prometheus/latest/querying/basics/)、[告警规则](https://prometheus.io/docs/prometheus/latest/configuration/alerting_rules/)、[告警概览](https://prometheus.io/docs/alerting/latest/overview/)、[Node Exporter](https://prometheus.io/docs/guides/node-exporter/) | 指标、标签、时间窗、告警状态 | 教程版本与目标版本分开；文档描述的告警不是系统实际摄入的告警 |
| Microsoft Learn | [Windows Server 性能手册](https://learn.microsoft.com/en-us/windows-server/administration/performance-tuning/)、[Get-WinEvent](https://learn.microsoft.com/en-us/powershell/module/microsoft.powershell.diagnostics/get-winevent?view=powershell-7.6)、[Get-Counter](https://learn.microsoft.com/en-us/powershell/module/microsoft.powershell.diagnostics/get-counter?view=powershell-7.6)、[Get-Service](https://learn.microsoft.com/en-us/powershell/module/microsoft.powershell.management/get-service?view=powershell-7.6) | Windows 事件、服务、性能采样 | 当前远端配置可能是 Windows，这部分应在核心集；PowerShell 7.6 与 Windows PowerShell 5.1 必须区分 |
| PostgreSQL 17 | [统计视图](https://www.postgresql.org/docs/17/monitoring-stats.html)、[磁盘使用与 Disk Full Failure](https://www.postgresql.org/docs/17/diskusage.html) | 数据库活动与容量 | 可选扩展，固定 17 版本；不要据此假设项目已有数据库取数工具 |

页面通常包含诊断、配置和修改操作。应分段标注只读查询、配置变更和高风险维护，并保留前置条件。不要仅靠“手册来自官方”就把所有命令加入 Agent 白名单。

## 2. 许可与版本记录

目录记录的是本轮看到的来源证据，不是对所有衍生材料的许可保证。

- Docker docs 与 Prometheus docs 仓库的 LICENSE 标为 Apache-2.0：[Docker](https://github.com/docker/docs/blob/main/LICENSE)、[Prometheus](https://github.com/prometheus/docs/blob/main/LICENSE)。
- Kubernetes website、PowerShell 文档与 Windows Server 文档仓库标为 CC-BY-4.0：[Kubernetes](https://github.com/kubernetes/website/blob/main/LICENSE)、[PowerShell](https://github.com/MicrosoftDocs/PowerShell-Docs/blob/main/LICENSE.md)、[Windows Server](https://github.com/MicrosoftDocs/windowsserverdocs/blob/main/LICENSE)。
- PostgreSQL 官方许可明确涵盖软件及文档：[许可说明](https://www.postgresql.org/about/licence/)。
- systemctl 的源文件有 `LGPL-2.1-or-later` 标记：[源文件](https://github.com/systemd/systemd/blob/main/man/systemctl.xml)。不能把一个文件的许可自动套到所有 systemd/内核/第三方页面。
- 其余条目在目录标记 `needs_review`，由实施时核对具体页面/文件及其 notices。保留原版权与署名，不把项目 MIT 许可覆盖到外部手册。

每个来源导入时保存 canonical URL、最终跳转 URL、抓取时间、上游版本/commit、原始文件哈希、许可依据 URL 与本地 notice。`latest`、`main` 是定位入口，不是不可变版本。失效链接不能静默换成搜索引擎摘要；本轮 PostgreSQL 的旧 `disk-full.html` 已确认为 404，替代的是 `diskusage.html` 对应章节。

## 3. 推荐的最小知识库组成

第一批使用目录中 `ingestion_stage=core` 的 11 个页面，覆盖 RHEL 性能、systemctl/journalctl、Linux 内存、Docker 三页、Windows 四页。之后按实际部署组件扩展 9 页；Kubernetes 和 PostgreSQL 的 5 页留到工具能力与环境明确后。

建议另写 6–10 篇项目自己的中文 runbook，围绕 CPU 高、OOM、磁盘容量/ inode、服务不可用、日志缺失、权限不足、健康对照组织。它们属于维护者编写的操作规程，必须有审核人/版本/适用范围/来源引用，不能标为真实历史工单或原厂官方内容。

原文用于证据核对；中文别名、术语表或译文用于检索辅助，并与原文绑定。不要只保存 AI 摘要，也不要将译文作为另一个独立权威来源重复计权。

## 4. 入库流水线

```text
来源目录 → 有界抓取 → 原始快照与许可 → 正文提取 → 结构解析
         → token 计数与 parent/child 切分 → 元数据与幂等校验
         → staging revision 向量化 → 验证 → 激活 revision → 检索可见
```

建议存储布局：

```text
knowledge/
  sources.json
  raw/<source_id>/<snapshot_hash>.html
  normalized/<document_id>/<revision>.md
  manifests/<import_job_id>.json
  notices/<source_family>/...
eval/rag/
  dev.jsonl
  blind.jsonl
  corpus_manifest.json
```

实现约束：

1. 默认只抓目录中的明确页面；手册入口另列允许章节。设置域名白名单、超时、大小、内容类型、速率和最多页数，不做全站递归。若未来接收用户 URL，需要防 SSRF：校验 DNS 与每次跳转，拒绝 loopback、私网、link-local、云 metadata 等内部目标。
2. 正文提取去除导航、侧栏、搜索、聊天挂件、重复 footer。保留标题层级、代码块、表格、Note/Warning、版本标记和章节锚点；拒绝登录墙/错误页/只有目录的页面进入 ready。
3. HTML 优先；有公开原始 Markdown/RST/XML 时，可从固定 commit 导入并核对渲染语义。PDF 作为补充解析格式，不能把目录、页眉和正文混在一起。
4. 先校验到临时文件。先完成新 revision 的所有分块和写入，再切换 active revision；失败保留旧版本。不要依赖 Milvus 的跨多条写入事务来实现此行为，应由元数据层管理版本可见性。
5. 文档/版本/分片有稳定 ID，同内容重复导入不重复增量；同文档并发更新采用 revision 冲突检测或锁。修改记录支持回滚、撤回和完整清理。
6. 小规模先使用有界后台 worker 与持久化 job/metadata 表，复用 Milvus，不必为了“企业级”立刻引入 Celery、Kafka 或 K8s。需要多进程/多实例时再迁移共享任务队列和 PostgreSQL 元数据。

## 5. 文档与向量契约

至少需要以下字段：

```json
{
  "document_id": "stable-document-id",
  "revision": "content-or-revision-hash",
  "chunk_id": "stable-child-id",
  "parent_id": "stable-section-id",
  "source_id": "SD-02",
  "source_uri": "https://official.example/manual#section",
  "source_snapshot_hash": "sha256:...",
  "title": "...",
  "heading_path": ["...", "..."],
  "language": "en",
  "os_family": "linux",
  "product": "systemd",
  "product_version": "explicit-version-or-unknown",
  "service_scope": [],
  "environment_scope": [],
  "tenant_id": "server-derived-principal-scope",
  "visibility": "public",
  "status": "active",
  "valid_from": "...",
  "valid_until": null,
  "reviewed_at": "...",
  "risk_level": "read_only|change|destructive|mixed",
  "license_id": "...",
  "line_start": 1,
  "line_end": 20,
  "embedding_descriptor_hash": "sha256:..."
}
```

`tenant_id/visibility` 来自服务端身份，不接受客户端自报越权。公开手册与企业私有 runbook 可以在统一检索接口汇合，但应用 ACL 不能只依靠模型提示词。权限约束前推到向量/关键词查询和候选阶段，未授权片段不能进入 reranker、模型或答案日志。需要能删除所有 revision 与对应缓存，而不是只在 UI 隐藏。

Embedding descriptor 保存 provider、模型、revision、维度、normalization、distance metric、tokenizer、最大长度和前缀配置。Collection 绑定 descriptor hash；不同模型即使维度相同也不能混入同一向量空间。切换 provider 创建新版本索引并验证，不能自动 drop 旧库。

## 6. 分块与 8GB 资源约束

当前模型 `paraphrase-multilingual-MiniLM-L12-v2` 的官方配置是 `max_seq_length=128`，输出 384 维，见 [模型配置](https://huggingface.co/sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2/blob/main/sentence_bert_config.json) 与 [模型卡](https://huggingface.co/sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2)。180 字符不是普遍有效的 128-token 保证。

先保留 CPU embedding 基线并读取真实 tokenizer/token budget；不要让小模型推理与大型 reranker 同时占 8GB 显存。建议小 child 用于检索，完整 parent 保留前提、命令、说明和警告作为回答上下文。child 的真实编码（含特殊 token 与前缀）必须不超过模型窗口；较长代码块可分成带位置的小检索片段，但原始 parent 中代码必须字节级保留。不要强行把所有命令压成 128 tokens 的孤立操作。

候选 retrieval 参数如 dense top-20、BM25 top-20、RRF 后取若干 parent，仅是待调试的初始配置。先测基线，再决定 hybrid/rerank；分数门限须在当前 metric 与验证集上校准。没有通用的“0.7 就可信”阈值。

## 7. 回答流程与引用

```text
身份/问题/环境 → 分类（知识问题或实时调查）
知识问题 → 服务端推导 ACL/OS/版本/有效期条件 → 带过滤的候选检索 → relevance gate
         → 有证据则生成带引用的回答 → 引用白名单/支持性校验
         → 无命中、不可用、超时分别返回明确结果
实时调查 → 受限诊断运行器 → 实时 evidence
```

知识问答不应默认拥有所有 MCP 工具。文档只是数据，不能修改工具权限、要求外传或注入系统指令。知识证据与实时观测应有不同类型：

- `evidence.kind=knowledge`：某版本手册描述的行为或操作经验。
- `evidence.kind=observation`：本次目标实例、时间窗和工具返回的观测。

`source_kind` 另表示实际来源与真实性标记，例如 tencent_cloud、ssh、manual 或 demo；不能把 demo 包装为真实 observation。实施时在一份共享 schema 中固定字段含义。

系统可以回答“手册建议检查 X”，但不能把手册中的“OOM 可能导致 Y”写成“当前服务已经因 OOM 发生 Y”。

建议回答协议（概念契约，实施时保持现有接口兼容或引入 `/api/v2`）：

```json
{
  "answer_id": "...",
  "session_id": "...",
  "run_id": null,
  "retrieval_status": "hit|miss|unavailable|timeout|invalid_index",
  "outcome": "answered|insufficient_evidence|failed|cancelled",
  "answer_markdown": "... [K1]",
  "citations": [
    {"id":"K1", "document_id":"...", "revision":"...", "chunk_id":"...",
     "source_uri":"...#section", "title":"...", "locator":"...",
     "snippet":"...", "score":0.0}
  ]
}
```

引用 ID 由服务器对本次检索结果分配，不能信任模型自带 URL。UI 支持查看原文、版本、片段和定位；无引用不显示“基于知识库”。检索得分不是回答正确率。

在线 multi-agent 可增加知识专科或给日志专科一个受 ACL 限制的检索工具，但保留 knowledge 类型，不绕开统一证据边界。诊断后追问显式绑定 `run_id`，先检查访问权限，再加载该次报告和证据摘要。严格本地模式以后接 RAG 时，embedding 必须使用本地已准备模型，依赖缺失返回 unavailable，不能偷偷下载或回退远程。

## 8. 检索与问答验收

第一轮建议建立约 60 条人工审阅问题作为小型验收起点，不据此宣称生产泛化。按原始来源/事件族分组，开发集与盲测集隔离。至少包括：

- 中文问题检索英文手册；精确错误码、参数名和命令。
- 相同问题不同 OS/组件版本；未知版本时给出限定或澄清。
- 无答案问题、过期文档、矛盾文档、相似但不支持结论的片段。
- 多文档联合解释；代码/YAML/续行命令和警告不丢失。
- 带提示注入的文档不改变权限，不触发工具操作。
- 断 Milvus、embedding 故障、索引模型不匹配、并发更新和撤回。
- 公开手册和私有 runbook 的权限隔离；被撤回内容不能再被引用。

记录 gold document/revision/chunk、允许的事实、必要前提和禁止断言。对比 dense 基线、hybrid、可选 rerank，报告 Recall@k、MRR/nDCG、引用支持准确率、无答案误答率、端到端 p50/p95、RSS/VRAM 与失败分母。验收门槛由先行基线和部署目标确定，不能先编造提升百分比。

所有查询与回答可追溯到 corpus manifest、embedding descriptor、retriever 配置与代码版本。当前五条诊断评测不能替代这些 RAG 评测。
