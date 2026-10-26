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
  /**
   * 服务端签发的稳定候选 ID（`tgt-<hex>`）。同名多实例必须用它绑定具体实例，
   * 只发服务名会丢掉 instance/region，后端只能再次回答 `target_required`。
   * 旧服务端不发 `target_id` 时为空串，此时退化为 service+instance+region 身份。
   */
  targetId: string;
  service: string;
  instanceId: string;
  region: string;
  topics: string[];
  sshHost: string;
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

function stringList(value: unknown): string[] {
  if (!Array.isArray(value)) return [];
  return value.map(text).filter((item) => item.length > 0);
}

/**
 * `target_required` 候选的**完整身份**。
 *
 * A04：只按服务名去重会让 `orders/i-a/r-a` 与 `orders/i-b/r-b` 塌成一张卡，
 * 点击后无法表达“是哪一个实例”。因此身份优先用服务端签发的 `target_id`，
 * 旧服务端没有 `target_id` 时退化为 service+instance+region。
 */
function candidateIdentity(candidate: TargetCandidate): string {
  if (candidate.targetId) return candidate.targetId;
  return [candidate.service, candidate.instanceId, candidate.region].join("\u001f");
}

/**
 * `target_required` 候选：只保留有服务名的条目，按**完整身份**去重并保持服务端顺序。
 * 同一服务名的不同实例/地域是不同的目标，必须各自保留。
 */
export function targetCandidates(value: unknown): TargetCandidate[] {
  if (!Array.isArray(value)) return [];
  const seen = new Set<string>();
  const candidates: TargetCandidate[] = [];
  for (const item of value) {
    const row = record(item);
    if (!row) continue;
    const service = text(row.service) || text(row.service_name);
    if (!service) continue;
    const candidate: TargetCandidate = {
      targetId: text(row.target_id) || text(row.targetId),
      service,
      instanceId: text(row.instance_id) || text(row.instance),
      region: text(row.region) || text(row.region_code),
      topics: stringList(row.topics),
      sshHost: text(row.ssh_host) || text(row.sshHost),
    };
    const identity = candidateIdentity(candidate);
    if (seen.has(identity)) continue;
    seen.add(identity);
    candidates.push(candidate);
  }
  return candidates;
}

/** 卡片身份即完整身份：同名不同实例的两张卡必须有不同 React key。 */
export function candidateKey(candidate: TargetCandidate): string {
  return candidateIdentity(candidate);
}

/**
 * 用户点击候选卡片后重新发起诊断的请求体。
 *
 * - 有服务端候选 ID 时只带 `target_candidate_id`（完整目标），服务端据此绑定
 *   具体实例；`target_choice` 仍在传入时保留，供旧服务端回退使用。
 * - 没有显式选择就等价于原请求，绝不把“空串”当成一次目标绑定。
 */
export function targetRequestBody(
  base: Record<string, unknown>,
  targetChoice?: string | null,
  candidateId?: string | null,
): Record<string, unknown> {
  const choice = (targetChoice ?? "").trim();
  const candidate = (candidateId ?? "").trim();
  const body: Record<string, unknown> = { ...base };
  if (choice) body.target_choice = choice;
  if (candidate) body.target_candidate_id = candidate;
  return body;
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

export interface EvalManifestSummary {
  /** true = 结果文件带 manifest（新格式）；false = 只有 rows 的旧格式。 */
  present: boolean;
  /** 面板直接渲染的一行文本，只包含 manifest 里真实存在的字段。 */
  text: string;
  /** 缺失或无法读取的字段，用于提示“未知”，绝不填占位数值。 */
  missing: string[];
}

function str(value: unknown): string {
  return typeof value === "string" ? value.trim() : "";
}

/**
 * 评测结果的运行条件摘要（A10）。
 *
 * 两份 eval JSON 只有在记录了模型/embedding/场景/代码版本时才能比较，
 * 所以面板要把 manifest 摆出来。这里**只读取服务端写入的字段**：
 * 缺什么就在 `missing` 里列出，不用默认值或猜测补全。
 */
export function evalManifestSummary(manifest: Record<string, unknown> | null | undefined): EvalManifestSummary {
  const row = record(manifest);
  if (!row) {
    return { present: false, text: "旧格式（无 manifest）", missing: [] };
  }
  const llm = record(row.llm);
  const embedding = record(row.embedding);
  const git = record(row.git);
  const scenarios = Array.isArray(row.scenarios) ? row.scenarios : [];

  const parts: string[] = [];
  const missing: string[] = [];

  const executionMode = str(row.execution_mode);
  if (executionMode) parts.push(`模式 ${executionMode}`);
  else missing.push("execution_mode");

  const provider = str(llm?.provider);
  const model = str(llm?.model);
  const localModel = str(llm?.local_llm_model);
  if (model) parts.push(`模型 ${provider ? `${provider}/` : ""}${model}`);
  else missing.push("llm.model");
  if (localModel) parts.push(`本地模型 ${localModel}`);

  const embeddingModel = str(embedding?.model);
  if (embeddingModel) parts.push(`embedding ${embeddingModel}`);
  else missing.push("embedding.model");

  if (scenarios.length) parts.push(`场景 ${scenarios.length} 个`);
  else missing.push("scenarios");

  const commit = str(git?.commit);
  const dirty = git?.dirty_worktree;
  if (commit) {
    const dirtyText =
      typeof dirty === "boolean" ? (dirty ? " · 工作区有未提交改动" : " · 工作区干净") : "";
    parts.push(`commit ${commit}${dirtyText}`);
  } else {
    missing.push("git.commit");
  }
  if (typeof dirty !== "boolean") missing.push("git.dirty_worktree");

  return { present: true, text: parts.length ? parts.join(" · ") : "manifest 为空", missing };
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

/**
 * 历史 run 的分页续读（A09）。
 *
 * 服务端一页最多返回固定条数，截断时给出两种游标之一：
 * - `next_after_seq`：本页边界事件带 seq 时的 seq 游标；
 * - `next_after_event`：无 seq 边界页的位置游标（跳过前 N 条）。
 *
 * 前端必须一直翻到 `truncated=false` 才能看到终态（`run_complete`）。这里把
 * “要不要继续翻”“下一次带什么游标”做成纯函数，便于单测，也避免 App 里各写一份。
 *
 * 安全上限：`MAX_PAGES` 防止服务端异常时无限翻页（正常 trace 远不会到）。
 */
export const MAX_RUN_PAGES = 50;

export interface RunPageShape {
  events?: unknown;
  truncated?: boolean;
  next_after_seq?: number | null;
  next_after_event?: number | null;
}

export interface RunCursor {
  after_seq?: number | null;
  after_event?: number | null;
}

/** 根据一页响应决定是否继续、以及下一页的游标；null 表示已读完。 */
export function nextRunCursor(page: RunPageShape, pagesRead: number): RunCursor | null {
  if (!page || page.truncated !== true) return null;
  if (pagesRead >= MAX_RUN_PAGES) return null;
  const afterEvent = page.next_after_event;
  if (typeof afterEvent === "number" && Number.isFinite(afterEvent) && afterEvent > 0) {
    return { after_event: afterEvent };
  }
  const afterSeq = page.next_after_seq;
  if (typeof afterSeq === "number" && Number.isFinite(afterSeq) && afterSeq >= 0) {
    return { after_seq: afterSeq };
  }
  // 截断却没有任何可用游标：说明服务端与前端契约不一致。继续翻只会重复同一页，
  // 因此停在这里（调用方应提示“记录被截断”），不能静默假装读完了。
  return null;
}

/** 合并多页事件；保留服务端顺序，不重排、不去重（服务端已保证无重复）。 */
export function appendRunEvents(
  collected: unknown[],
  page: RunPageShape,
): unknown[] {
  const events = Array.isArray(page?.events) ? page.events : [];
  return [...collected, ...events];
}

/** 一页是否“被截断且无法继续”——用于给用户明确提示。 */
export function isStuckPage(page: RunPageShape, pagesRead: number): boolean {
  return page?.truncated === true && nextRunCursor(page, pagesRead) === null;
}
