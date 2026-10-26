/**
 * 视图投影纯函数：把 SSE / REST 的原始负载收敛成组件直接渲染的形状。
 *
 * 这里不依赖 React、fetch 或 `import.meta`，因此可以被 `node --test`
 * 直接导入做单元测试：
 * - `target_required` 候选（service / instance / region）只做规范化与去重；
 * - 引用卡片只读取服务端字段，不在前端拼造缺失元数据；
 * - 检索状态与索引状态各自映射成明确的文案，`invalid_index` 不能被并入
 *   `unavailable`（否则用户会以为只是“没启动”，而实际需要重建索引）。
 */

export type StatusTone = "ok" | "warn" | "error" | "muted";

export interface StatusLabel {
  text: string;
  tone: StatusTone;
  detail: string;
}

export interface CitationCard {
  id: string;
  title: string;
  locator: string;
  sourceUri: string;
  snippet: string;
  score: number;
  documentId: string;
}

export interface TargetCandidate {
  service: string;
  instanceId: string;
  region: string;
}

function record(value: unknown): Record<string, unknown> | null {
  if (!value || typeof value !== "object" || Array.isArray(value)) return null;
  return value as Record<string, unknown>;
}

function text(value: unknown): string {
  if (typeof value === "string") return value;
  if (typeof value === "number" && Number.isFinite(value)) return String(value);
  return "";
}

function numberOr(value: unknown, fallback = 0): number {
  const parsed = typeof value === "number" ? value : Number(value);
  return Number.isFinite(parsed) ? parsed : fallback;
}

/** `target_required` 候选：只保留有服务名的条目，按服务名去重并保持服务端顺序。 */
export function targetCandidates(value: unknown): TargetCandidate[] {
  if (!Array.isArray(value)) return [];
  const seen = new Set<string>();
  const candidates: TargetCandidate[] = [];
  for (const item of value) {
    const row = record(item);
    if (!row) continue;
    const service = text(row.service) || text(row.service_name);
    if (!service || seen.has(service)) continue;
    seen.add(service);
    candidates.push({
      service,
      instanceId: text(row.instance_id) || text(row.instance),
      region: text(row.region) || text(row.region_code),
    });
  }
  return candidates;
}

export function candidateKey(candidate: TargetCandidate): string {
  return candidate.service;
}

/**
 * 用户点击候选卡片后重新发起诊断的请求体。
 * 只有显式传入非空选择时才带 `target_choice`——没有选择就等价于原请求，
 * 避免把“空串”当成一次目标绑定。
 */
export function targetRequestBody(
  base: Record<string, unknown>,
  targetChoice?: string | null,
): Record<string, unknown> {
  const choice = (targetChoice ?? "").trim();
  return choice ? { ...base, target_choice: choice } : { ...base };
}

/** done 事件 / retrieval.chunks 里的引用列表；缺失字段用空串，不编造来源。 */
export function citationCards(value: unknown): CitationCard[] {
  if (!Array.isArray(value)) return [];
  const cards: CitationCard[] = [];
  for (const item of value) {
    const row = record(item);
    if (!row) continue;
    cards.push({
      id: text(row.id),
      title: text(row.title) || text(row.document_id) || "未命名文档",
      locator: text(row.locator),
      sourceUri: text(row.source_uri),
      snippet: text(row.snippet),
      score: numberOr(row.score),
      documentId: text(row.document_id),
    });
  }
  return cards;
}

/** 检索状态：hit / miss / unavailable / timeout / invalid_index 各自独立。 */
export function retrievalLabel(status: unknown): StatusLabel {
  switch (text(status)) {
    case "hit":
      return { text: "已命中知识库", tone: "ok", detail: "" };
    case "miss":
      return { text: "知识库未命中", tone: "warn", detail: "没有找到可用依据，回答不能当作诊断结论。" };
    case "unavailable":
      return { text: "知识库不可用", tone: "error", detail: "检索依赖不可用，本次回答没有知识依据。" };
    case "timeout":
      return { text: "检索超时", tone: "error", detail: "检索超时，可稍后重试。" };
    case "invalid_index":
      return { text: "索引不匹配", tone: "error", detail: "索引维度或描述符与查询不一致，需要重建索引。" };
    default:
      return { text: "未检索", tone: "muted", detail: "" };
  }
}

/** 服务端移除的模型自带引用（不在本轮授权集合内）。 */
export function removedCitationsText(value: unknown): string {
  if (!Array.isArray(value)) return "";
  const ids = value.map(text).filter(Boolean);
  return ids.length ? `已移除未授权引用：${ids.join("、")}` : "";
}

export function chatOutcomeLabel(outcome: unknown): string {
  switch (text(outcome)) {
    case "answered":
      return "";
    case "insufficient_evidence":
      return "知识库证据不足";
    case "failed":
      return "回答生成失败";
    case "cancelled":
      return "已取消";
    default:
      return "";
  }
}

/** 知识库 collection 状态；invalid_index 必须与 unavailable 分开。 */
export function knowledgeStatusLabel(status: unknown): StatusLabel {
  switch (text(status)) {
    case "ready":
      return { text: "就绪", tone: "ok", detail: "" };
    case "degraded":
      return { text: "降级", tone: "warn", detail: "部分能力不可用，问答可能只能返回已有片段。" };
    case "unavailable":
      return { text: "不可用", tone: "error", detail: "检索依赖未连接，上传与问答都不可用。" };
    case "invalid_index":
      return { text: "索引不匹配（需重建）", tone: "error", detail: "索引维度或描述符与当前配置不一致，必须重建索引后才能检索。" };
    default:
      return { text: "未知", tone: "muted", detail: "" };
  }
}

/** 索引作业状态（202 + job_id 的轮询展示）。 */
export function jobStatusLabel(status: unknown): StatusLabel {
  switch (text(status)) {
    case "queued":
      return { text: "已排队", tone: "warn", detail: "已接收，尚未可检索。" };
    case "running":
      return { text: "索引中", tone: "warn", detail: "正在切分与向量化。" };
    case "ready":
      return { text: "已完成", tone: "ok", detail: "新版本可检索。" };
    case "failed":
      return { text: "失败", tone: "error", detail: "索引失败，旧版本仍可检索。" };
    case "cancelled":
      return { text: "已取消", tone: "muted", detail: "" };
    default:
      return { text: "未知", tone: "muted", detail: "" };
  }
}
