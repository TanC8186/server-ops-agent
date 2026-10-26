# M2/M4 API 契约（前后端共同实现，勿各自发明）

本文件是**待实现契约**，不是已实现接口。任何实现方都必须按这里的字段名与状态值
对齐；变更先从本文件改起。

日期：2026-10-26。相关审查条目：RAG-02/04/07/08/09、FE-06/07/08、BE-08、M2/M4。

## 0. 通用约定

- 所有 `/api` 路由沿用现有 `X-API-Key` 鉴权入口（`app.core.auth.require_api_key`）。
- 错误统一 `{"detail": "<可读原因>"}`，并带 HTTP 状态码；不要在 200 里塞 `success:false`
  来表达失败（上传“已存文件但索引失败”除外，见 §2.2）。
- 时间：ISO-8601 带时区；`*_ms` 字段是整数毫秒。
- 分页：`limit`（1..100）+ `cursor`（不透明字符串）；响应带 `next_cursor`（可为 null）。
- 所有返回都不得包含 API Key、语料正文之外的私有路径。

## 1. 事件信封（SSE / trace / 回放共用）

服务端生成、不允许 payload 覆盖：

```json
{
  "schema_version": "1",
  "event_id": "<run_id>:<seq>",
  "seq": 12,
  "run_id": "<32 lowercase hex>",
  "session_id": "...",
  "execution_mode": "local|cloud_multi",
  "type": "tool_finished",
  "ts": "2026-10-26T12:00:00+08:00",
  "payload": { "...": "业务字段" }
}
```

平铺兼容：业务字段同时出现在顶层（旧 reducer 直接读 `event.step`），但
`schema_version/event_id/seq/run_id/session_id/execution_mode/type/ts` 只由服务端写。

前端：`web/src/sse.ts` 的 `SseBuffer` + `applyBlock` 是唯一解析入口，按 `event_id`
去重、按 `seq` 记录进度、终态只接受一次。

### 1.1 关键事件类型

| type | 必带 payload | 说明 |
|---|---|---|
| `status` | `stage`, `message` | 通用进度 |
| `target_required` | `candidates[]`, `reason` | 多候选，等待 UI 选择；不是失败 |
| `plan` | `steps[]`, `assignments[]` | 步骤含稳定 `step_id` |
| `tool_step` | `step_id`, `tool_call_id`, `status`, `evidence_id`, `source_kind` | 每次工具调用一条 |
| `report` | `report` | Markdown 报告 |
| `complete` | `response`, `lifecycle`, `quality`, `status` | 唯一终态之一 |
| `error` | `message`, `fatal` | 唯一终态之一 |

`lifecycle ∈ queued|running|completed|failed|cancelled|abandoned`；
`quality ∈ sufficient|partial|insufficient`；`status` 是兼容字段
（`completed|degraded|target_required`），由 lifecycle+quality 推导。

## 2. 知识库管理（M2）

### 2.1 文档列表

`GET /api/knowledge/documents?limit=20&cursor=&status=active`

```json
{
  "documents": [
    {
      "document_id": "doc-0123456789ab",
      "title": "磁盘容量排查",
      "source_uri": "https://example.invalid/manual#disk",
      "source_kind": "manual",
      "active_revision": "rev-abc123",
      "status": "active",
      "chunks": 42,
      "updated_at": "2026-10-26T12:00:00+08:00",
      "embedding_descriptor_hash": "sha256:...",
      "os_family": "linux",
      "product": "systemd",
      "product_version": "262",
      "license_id": "CC-BY-4.0",
      "pending_revision": "rev-def456"
    }
  ],
  "next_cursor": null
}
```

### 2.2 上传 / 导入

`POST /api/knowledge/documents` (multipart, field `file`) 或 `POST /api/knowledge/import`
(JSON: `{source_id | url}`，仅白名单目录内)。

- 202 + `{"job_id": "...", "document_id": "...", "status": "queued"}`：已接收，**未**可检索。
- 400：校验失败（超限/格式/编码/空/保留设备名），**旧版本不受影响**。
- 同一 `document_id` 的并发导入必须串行化；重复导入同内容幂等（返回已有 job）。

### 2.3 作业状态

`GET /api/knowledge/jobs/{job_id}`

```json
{
  "job_id": "job-...",
  "document_id": "doc-...",
  "status": "queued|running|ready|failed|cancelled",
  "progress": {"chunks_total": 42, "chunks_done": 12},
  "revision": "rev-abc123",
  "started_at": "...", "finished_at": null,
  "error": {"code": "embedding_failed", "message": "...", "retryable": true}
}
```

`GET /api/knowledge/jobs?status=failed&limit=20` 列出最近作业。

`POST /api/knowledge/jobs/{job_id}/retry` → 202 新 job_id（旧 job 保留为历史）。

### 2.4 版本与撤销

- `GET /api/knowledge/documents/{document_id}/revisions` → `{"revisions":[{"revision","created_at","status":"active|superseded|staged","chunks"}],"active_revision":...}`
- `POST /api/knowledge/documents/{document_id}/activate` `{"revision": "..."}` → 200，切换到已有 revision（用于回滚）。
- `DELETE /api/knowledge/documents/{document_id}` → 200 `{"deleted_chunks": n}`，删除全部 revision 与向量；之后问答不得再引用。

### 2.5 collection 描述符

`GET /api/knowledge/status`

```json
{
  "status": "ready|degraded|unavailable|invalid_index",
  "collection": "biz",
  "chunks": 1234,
  "embedding": {"provider": "local", "model": "...", "dimension": 384,
                 "normalization": "l2", "distance": "cosine",
                 "descriptor_hash": "..."},
  "milvus": {"connected": true, "latency_ms": 3},
  "documents": {"active": 11, "pending": 1},
  "reason": ""
}
```

`invalid_index` 必须是独立状态（维度/描述符不一致），不能并入 unavailable。

## 3. 知识问答（M2/M4）

`POST /api/chat_stream`（兼容现有路径）请求体：

```json
{"Id": "session-1", "Question": "...", "run_id": "可选：追问某次诊断"}
```

事件（复用 §1 信封，`type` 取值）：

```json
{"type":"retrieval","status":"hit|miss|unavailable|timeout|invalid_index",
 "reason":"","retrieval":{"chunks":[{"chunk_id","document_id","revision","title",
 "heading_path":[],"source_uri","locator","score","source_kind","snippet"}]}}
{"type":"content","data":"增量 Markdown"}
{"type":"done","outcome":"answered|insufficient_evidence|failed|cancelled",
 "retrieval_status":"hit|miss|unavailable|timeout|invalid_index",
 "content":"完整回答","citations":[{"id":"K1","chunk_id":"","document_id":"",
 "revision":"","title":"","heading_path":[],"source_uri":"","locator":"","snippet":"","score":0.0}],
 "removed_citations":["K9"],"run_context":{...},"tool_calls":[]}
```

- 引用 ID 由服务端为本轮命中分配；模型自带的 `[K#]` 若不在授权集合内会被移除并出现在 `removed_citations`。
- `run_id` 追问：服务端校验该 run 的归属（同 session 或同 principal），命中时在 `done.run_context`
  返回 `{"run_id","target","time_window","observations":[...],"hypotheses":[...],"gaps":[...],"quality"}`；
  未命中返回 `run_context: null` 并在 `reason` 说明，不得串到别的 run。
- 知识模式**没有**运维 MCP 工具权限；`tool_calls` 恒为空数组（保留字段便于将来显式授权）。

## 4. 运行历史（BE-08 / FE-08）

`GET /api/runs?limit=20&cursor=` → 每项：

```json
{"run_id":"...","session_id":"...","execution_mode":"local|cloud_multi",
 "started_at":"...","duration_ms":1234,"lifecycle":"completed","quality":"partial",
 "status":"ok|error|cancelled|abandoned","steps":3,"plan_steps":2,
 "target":{"service":"svc-a","region":"..."},"has_report":true,
 "input_tokens":0,"output_tokens":0}
```

`GET /api/runs/{run_id}?after_seq=0&limit=2000` → `{run_id, events[], truncated, next_after_seq}`。

- 只读恢复：前端**不得**因为“找不到 run”而自动 POST 新诊断。
- 写入失败通过 `/health` 的 `data.capabilities.tracing` 暴露（见 §5）。

## 5. 健康与能力

`GET /health`（保持 `{code,message,data}` 外壳）：

```json
{"code":200,"message":"服务运行正常","data":{
 "service":"OnCallAgent","version":"...","status":"healthy",
 "auth":{"mode":"dev","api_key_configured":false,"authentication_required":false},
 "capabilities":{
   "diagnosis_local":{"status":"unconfigured|ready|degraded","reason":""},
   "diagnosis_cloud":{"status":"...","reason":""},
   "rag":{"status":"unavailable","reason":"milvus not connected"},
   "tracing":{"status":"ready","writes":120,"failures":0}
 },
 "milvus":{"status":"disconnected","message":"..."}}}
```

- Milvus 故障只把 `capabilities.rag` 标记为不可用，**不得**把整体 `status` 变成 unhealthy/503。
- 任何能力字段都不得包含密钥。

## 6. 前端消费规则

- 计划步骤用 `step_id` 标识，工具调用用 `tool_call_id`；一个步骤可以有多次调用。
  完成计数只看步骤终态；`list_all_services`（发现）不计入计划完成数。
- 实时流与回放共用 `applyEvent`/`replay`（`web/src/state.ts`）。
- 运行列表与评测结果**独立加载**，一个失败不阻断另一个。
- 选中 run 写入 URL（`?run=<id>`）；刷新按 URL 恢复；恢复失败提示，不新建任务。
- 目标歧义：`target_required` 渲染候选卡片，点击后带 `target_choice` 重新发起**一次**诊断
  （用户显式动作，不是自动重试）。
