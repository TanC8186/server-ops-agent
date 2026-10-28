/**
 * REST 调用封装。
 *
 * SSE 传输与解析已统一到 `sse.ts`；这里不再保留旧版 `diagnose` /
 * `parseSseChunk` / `normalizeSseEvent`，避免出现两套事件形状。
 */

export type ExecutionMode = "local" | "cloud_multi";
export type RunStatus = "ok" | "error" | "running" | "cancelled";

export interface ExecutionModeInfo {
  id: ExecutionMode;
  label: string;
  description: string;
  configured: boolean;
  model: string;
}

export interface RunSummary {
  run_id: string;
  session_id: string | null;
  started_at: string | null;
  duration_ms: number | null;
  lifecycle?: string | null;
  quality?: string | null;
  /** 跑完但质量不足以支撑结论；由服务端按 quality 计算，不由前端猜。 */
  degraded?: boolean;
  status: RunStatus;
  steps: number;
  plan_steps: number;
  backend: "local" | "cloud";
  execution_mode?: ExecutionMode;
  target?: Record<string, unknown> | null;
  has_report?: boolean;
  severity: string;
  category: string;
  input_tokens: number;
  output_tokens: number;
}

/** trace 文件里的事件形状（仅用于运行回放的类型提示）。 */
export interface RunEvent {
  ts?: string;
  event: string;
  node?: string;
  step?: string;
  tool?: string;
  tools?: { name: string; args: Record<string, unknown> }[];
  action?: string;
  backend?: string;
  reason?: string;
  severity?: string;
  category?: string;
  steps?: unknown;
  input_tokens?: number | null;
  output_tokens?: number | null;
  result_chars?: number;
  result_preview?: string;
  duration_ms?: number;
  message?: string;
  run_id?: string;
  session_id?: string;
  execution_mode?: ExecutionMode;
  agent?: string;
  status?: string;
  response?: string;
  report?: string;
  seq?: number;
  event_id?: string;
  [key: string]: unknown;
}

export interface EvalRow {
  id: string;
  tool_f1?: number;
  tool_recall?: number;
  tool_precision?: number;
  task_success?: boolean;
  steps?: number;
  input_tokens?: number;
  output_tokens?: number;
  groundedness?: number;
  facts_matched?: number;
  facts_total?: number;
  error?: string;
}

export interface EvalResult {
  name: string;
  rows: EvalRow[];
  /**
   * 这次评测的运行条件（模型/embedding/场景/代码版本）。
   * 旧格式结果文件只有裸列表，服务端补 `null`——此时面板显示“旧格式”，
   * 不得拿默认值冒充条件。
   */
  manifest?: Record<string, unknown> | null;
  mean_tool_f1: number;
  success_rate: number;
  total_tokens: number;
}

/** 知识库文档（契约见 docs/claude-code-handoff/M2_API_CONTRACT.md §2.1）。 */
export interface KnowledgeDocument {
  document_id: string;
  title: string;
  source_uri?: string;
  source_kind?: string;
  active_revision?: string | null;
  status: string;
  chunks: number;
  updated_at?: string | null;
  pending_revision?: string | null;
}

export interface KnowledgeDocumentList {
  documents: KnowledgeDocument[];
  next_cursor?: string | null;
}

export interface KnowledgeJobProgress {
  chunks_total?: number;
  chunks_done?: number;
}

export interface KnowledgeJobError {
  code?: string;
  message?: string;
  retryable?: boolean;
}

export interface KnowledgeJob {
  job_id: string;
  document_id?: string;
  status: string;
  progress?: KnowledgeJobProgress;
  revision?: string | null;
  started_at?: string | null;
  finished_at?: string | null;
  error?: KnowledgeJobError | null;
}

export interface KnowledgeJobList {
  jobs: KnowledgeJob[];
}

/** 202 响应：已接收，但新版本还不能检索。 */
export interface KnowledgeUploadAccepted {
  job_id: string;
  document_id: string;
  status: string;
}

export interface KnowledgeRevision {
  revision: string;
  created_at?: string | null;
  status: string;
  chunks: number;
}

export interface KnowledgeRevisionList {
  revisions: KnowledgeRevision[];
  active_revision?: string | null;
}

/** 服务端会话历史的一条消息（assistant 带引用与检索状态）。 */
export interface ChatHistoryMessage {
  role: string;
  content: string;
  citations?: Record<string, unknown>[];
  retrieval_status?: string;
  outcome?: string;
}

export interface ChatSessionInfo {
  session_id: string;
  message_count: number;
  history: ChatHistoryMessage[];
}

export interface KnowledgeStatus {
  status: string;
  collection?: string;
  chunks?: number;
  embedding?: {
    provider?: string;
    model?: string;
    dimension?: number;
    normalization?: string;
    distance?: string;
    descriptor_hash?: string;
  };
  milvus?: { connected?: boolean; latency_ms?: number };
  documents?: { active?: number; pending?: number };
  reason?: string;
}

const ENV =
  (import.meta as unknown as { env?: Record<string, string | undefined> }).env ?? {};

export const API_BASE = ENV.VITE_API_BASE ?? "";
const API_KEY = ENV.VITE_API_KEY ?? "";

export function headers(): Record<string, string> {
  const result: Record<string, string> = { "Content-Type": "application/json" };
  if (API_KEY) result["X-API-Key"] = API_KEY;
  return result;
}

/** multipart 上传不能手写 Content-Type（boundary 由浏览器生成）。 */
function uploadHeaders(): Record<string, string> {
  const result = headers();
  delete result["Content-Type"];
  return result;
}

async function errorMessage(response: Response): Promise<string> {
  let detail = "";
  try {
    const body = (await response.json()) as { detail?: unknown };
    if (typeof body.detail === "string") detail = body.detail;
  } catch {
    // 代理层可能返回非 JSON 错误页
  }
  return detail || `${response.status} ${response.statusText}`;
}

async function request<T>(path: string, init: RequestInit): Promise<T> {
  const response = await fetch(`${API_BASE}${path}`, init);
  if (!response.ok) {
    throw new Error(await errorMessage(response));
  }
  if (response.status === 204) return undefined as T;
  return (await response.json()) as T;
}

async function getJson<T>(path: string, signal?: AbortSignal): Promise<T> {
  return request<T>(path, { headers: headers(), signal });
}

function query(params: Record<string, string | number | undefined>): string {
  const search = new URLSearchParams();
  for (const [key, value] of Object.entries(params)) {
    if (value !== undefined && value !== "") search.set(key, String(value));
  }
  const text = search.toString();
  return text ? `?${text}` : "";
}

/** 单次 run 读取的一页（A09）：两种游标互斥，截断时必有一个非空。 */
export interface RunEventPage {
  run_id: string;
  events: RunEvent[];
  truncated?: boolean;
  next_after_seq?: number | null;
  next_after_event?: number | null;
}

/**
 * 聊天流事件的一页（G5 断流续读）。
 *
 * 服务端契约：``{run_id, events, truncated, next_after_seq}``。聊天事件只有
 * seq 游标（事件由 ``/api/chat_stream`` 顺序产生，不存在无 seq 的边界），
 * 因此没有位置游标。``truncated=true`` 时必须带 ``next_after_seq`` 再读。
 */
export interface ChatEventPage {
  run_id: string;
  events: RunEvent[];
  truncated?: boolean;
  next_after_seq?: number | null;
}

export const api = {
  executionModes: () => getJson<{ modes: ExecutionModeInfo[] }>("/api/execution-modes"),
  listRuns: (limit = 20) => getJson<{ runs: RunSummary[] }>(`/api/runs${query({ limit })}`),
  /**
   * 读取一页 run 事件。
   *
   * A09：服务端在有 seq 时给 `next_after_seq`，无 seq 边界页给 `next_after_event`
   * （位置游标）。调用方必须按返回的游标继续翻页，否则事件数超过单页上限的
   * run 永远读不到终态。两个参数都可选，保持旧调用方式可用。
   */
  getRun: (runId: string, cursor?: { after_seq?: number | null; after_event?: number | null }) =>
    getJson<RunEventPage>(
      `/api/runs/${encodeURIComponent(runId)}${query({
        after_seq: cursor?.after_seq ?? undefined,
        after_event: cursor?.after_event ?? undefined,
      })}`,
    ),
  listEvalResults: () => getJson<{ results: EvalResult[] }>("/api/eval/results"),
  health: () => getJson<Record<string, unknown>>("/health"),

  // ---------- 知识库管理（M2） ----------

  knowledgeStatus: () => getJson<KnowledgeStatus>("/api/knowledge/status"),

  listKnowledgeDocuments: (limit = 20, cursor = "", status = "") =>
    getJson<KnowledgeDocumentList>(
      `/api/knowledge/documents${query({ limit, cursor, status })}`,
    ),

  /** 202 = 已排队；`job_id` 需要用 `getKnowledgeJob` 轮询。 */
  uploadKnowledgeDocument: (file: File) => {
    const form = new FormData();
    form.append("file", file);
    return request<KnowledgeUploadAccepted>("/api/knowledge/documents", {
      method: "POST",
      headers: uploadHeaders(),
      body: form,
    });
  },

  listKnowledgeJobs: (status = "", limit = 20) =>
    getJson<KnowledgeJobList>(`/api/knowledge/jobs${query({ status, limit })}`),

  getKnowledgeJob: (jobId: string) =>
    getJson<KnowledgeJob>(`/api/knowledge/jobs/${encodeURIComponent(jobId)}`),

  retryKnowledgeJob: (jobId: string) =>
    request<KnowledgeUploadAccepted>(
      `/api/knowledge/jobs/${encodeURIComponent(jobId)}/retry`,
      { method: "POST", headers: headers() },
    ),

  listKnowledgeRevisions: (documentId: string) =>
    getJson<KnowledgeRevisionList>(
      `/api/knowledge/documents/${encodeURIComponent(documentId)}/revisions`,
    ),

  activateKnowledgeRevision: (documentId: string, revision: string) =>
    request<{ active_revision?: string }>(
      `/api/knowledge/documents/${encodeURIComponent(documentId)}/activate`,
      { method: "POST", headers: headers(), body: JSON.stringify({ revision }) },
    ),

  deleteKnowledgeDocument: (documentId: string) =>
    request<{ deleted_chunks: number }>(
      `/api/knowledge/documents/${encodeURIComponent(documentId)}`,
      { method: "DELETE", headers: headers() },
    ),

  // ---------- 知识问答会话历史（RAG-09） ----------

  /**
   * 续读一次聊天流在 ``after_seq`` 之后的事件（G5）。
   *
   * 断流（EOF 没有 done）时前端用它补齐；服务端只读落盘记录，不会重新调用
   * 模型，也不会自动重跑。没有记录时返回 404，调用方据此区分“记录不存在”
   * 与“没有更多事件”。
   */
  chatStreamEvents: (runId: string, afterSeq = 0, sessionId = "", signal?: AbortSignal) =>
    getJson<ChatEventPage>(
      `/api/chat/events/${encodeURIComponent(runId)}${query({
        after_seq: afterSeq,
        // 服务端按**会话归属**校验续读权限：只有该会话的 owner 能读取。
        // 不传时部署模式会 403（见 /api/chat/events 的 docstring）。
        session_id: sessionId,
      })}`,
      // 续读必须能被"停止"打断：否则用户取消后，迟到的续读结果仍会被应用，
      // 并把刚清掉的 oncall.chat.pending 重新写回去。
      signal,
    ),

  /** 服务端保存的会话历史；带引用与检索状态，刷新后可回填。 */
  chatSession: (sessionId: string) =>
    getJson<ChatSessionInfo>(
      `/api/chat/session/${encodeURIComponent(sessionId)}`,
    ),

  clearChatSession: (sessionId: string) =>
    request<{ status?: string; message?: string }>("/api/chat/clear", {
      method: "POST",
      headers: headers(),
      body: JSON.stringify({ sessionId }),
    }),
};
