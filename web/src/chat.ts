/**
 * 对话（RAG）接口：与 AIOps 诊断共用 `sse.ts` 的传输层。
 *
 * 修复点（FE-04 / G5）：
 * - 分帧交给 `SseBuffer`，网络分块切开 CRLF 或 UTF-8 多字节字符时不会丢事件；
 * - 去重与游标推进走 `sse.ts` 的 `StreamState`（`event_id` 去重、`lastSeq` 只进
 *   不退），实时流与续读共用同一份状态机；
 * - `fetch` 放进 `try`，网络拒绝也会回调 `onError`；
 * - EOF 前没有 `done` **不再直接判定失败**：先按 `run_id` + `lastSeq` 有界续读
 *   （最多 2 次请求），读到 `done` 就正常收尾；只有续读也拿不到终态才报
 *   truncated，并区分“已续读 N 条”与“未能续读”；
 * - reader 在 `finally` 里 `cancel()+releaseLock()`。
 *
 * 会话与未完成流的持久化也在这里：它们决定“刷新/重开标签页后能不能接着看”，
 * 因此必须是纯函数或只依赖传入的 `StorageLike`，可以在 node 下直接用假存储测试。
 */

import { API_BASE, api, headers, type ChatEventPage } from "./api.ts";
import {
  SseBuffer,
  acceptEvent,
  applyEventToState,
  createStreamState,
  normalizeSseEvent,
  parseSseBlock,
  type RunEvent,
  type StreamState,
} from "./sse.ts";

export interface ChatRetrieval {
  status: string;
  reason: string;
  chunks: Record<string, unknown>[];
}

export interface ChatDone {
  outcome: string;
  retrievalStatus: string;
  content: string;
  citations: Record<string, unknown>[];
  removedCitations: string[];
  reason: string;
}

/** 当前流的位置（run_id + 最后应用的 seq），用于断流续读与 tab 恢复。 */
export interface ChatProgress {
  runId: string;
  lastSeq: number;
}

export interface ChatHandlers {
  onDelta: (text: string) => void;
  onRetrieval?: (retrieval: ChatRetrieval) => void;
  onEvent?: (event: RunEvent) => void;
  /** 每条被接受的事件之后回调，带 run_id/seq（不含重复投递）。 */
  onProgress?: (progress: ChatProgress) => void;
  onDone: (done: ChatDone) => void;
  onError: (message: string) => void;
  /** EOF 没有 done 且续读也没拿到终态时调用；未提供时降级为 onError。 */
  onTruncated?: (message: string) => void;
}

export type ChatOutcome =
  | { status: "done"; done: ChatDone }
  | { status: "error"; message: string }
  | { status: "cancelled" }
  | { status: "truncated"; message: string };

/** 续读结果：`content` 是续读期间收到的增量拼接（tab 恢复用它重建回答）。 */
export type ChatResumeOutcome =
  | { status: "done"; done: ChatDone; content: string; resumed: number }
  | { status: "error"; message: string; content: string; resumed: number }
  | { status: "cancelled"; content: string; resumed: number }
  | { status: "partial"; message: string; content: string; resumed: number }
  | { status: "failed"; message: string; content: string; resumed: number };

const TRUNCATED_MESSAGE = "连接中断，已收到的事件保留；本次回答不完整。";

/**
 * 聊天流的终态事件名。诊断用 `complete`，聊天用 `done`；`error` 两者都有。
 * 由调用方声明，避免 `applyBlock` 猜事件类型。
 */
const CHAT_TERMINAL_TYPES = ["done", "error"] as const;

/** 续读最多 2 次请求：一次拿不到就再试一次，再多只是拖长“已中断”的确认时间。 */
export const MAX_RESUME_ATTEMPTS = 2;

function asRecord(value: unknown): Record<string, unknown> | null {
  return value && typeof value === "object" && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : null;
}

function asRecords(value: unknown): Record<string, unknown>[] {
  if (!Array.isArray(value)) return [];
  return value.filter(
    (item): item is Record<string, unknown> =>
      !!item && typeof item === "object" && !Array.isArray(item),
  );
}

function asStrings(value: unknown): string[] {
  if (!Array.isArray(value)) return [];
  return value.map((item) => String(item)).filter(Boolean);
}

function toDone(payload: Record<string, unknown>): ChatDone {
  const body = asRecord(payload.data) ?? payload;
  return {
    outcome: String(body.outcome ?? ""),
    retrievalStatus: String(body.retrieval_status ?? ""),
    content: String(body.content ?? ""),
    citations: asRecords(body.citations),
    removedCitations: asStrings(body.removed_citations),
    reason: String(body.reason ?? ""),
  };
}

function toRetrieval(event: RunEvent): ChatRetrieval {
  const retrieval = asRecord(event.retrieval);
  return {
    status: String(event.status ?? ""),
    reason: String(event.reason ?? ""),
    chunks: asRecords(retrieval?.chunks),
  };
}

function errorMessageOf(event: RunEvent): string {
  const body = asRecord(event.data);
  return String((body?.message ?? event.data ?? event.message) || "对话失败");
}

interface RunContext {
  state: StreamState;
  done: ChatDone | null;
  error: string;
}

type EventResult = "done" | "error" | "continue" | "duplicate";

/**
 * 把一条事件应用到状态机并分发给 UI。实时块与续读的 JSON 事件共用这一份，
 * 两处不会出现“一边去重、一边不去重”的偏差。
 *
 * `onContent` 只给续读用：它需要把增量拼起来重建回答，而不是再渲染一遍。
 */
function applyChatEvent(
  event: RunEvent,
  ctx: RunContext,
  handlers: ChatHandlers,
  onContent?: (text: string) => void,
): EventResult {
  // 去重与游标推进在 acceptEvent 里；重复投递既不渲染也不推进 lastSeq。
  if (!acceptEvent(event, ctx.state)) return "duplicate";
  const outcomes = applyEventToState(event, ctx.state, { terminalTypes: CHAT_TERMINAL_TYPES });
  if (!outcomes.length) return "duplicate";
  let result: EventResult = "continue";
  for (const outcome of outcomes) {
    if (outcome.kind === "protocol-error") {
      // 聊天流没有执行模式约束，走到这里说明协议实现变了：如实报错，不吞掉。
      ctx.error = outcome.message;
      result = "error";
      break;
    }
    if (outcome.kind === "terminal") {
      if (outcome.outcome === "error") {
        ctx.error = errorMessageOf(outcome.event);
        result = "error";
      } else {
        ctx.done = toDone(outcome.event as unknown as Record<string, unknown>);
        result = "done";
      }
      break;
    }
    const current = outcome.event;
    const type = String(current.event ?? current.type ?? "");
    if (type === "content") {
      const text = String(current.data ?? "");
      if (text) {
        handlers.onDelta(text);
        onContent?.(text);
      }
    } else if (type === "retrieval") {
      handlers.onRetrieval?.(toRetrieval(current));
      handlers.onEvent?.(current);
    } else {
      handlers.onEvent?.(current);
    }
  }
  handlers.onProgress?.({ runId: ctx.state.runId, lastSeq: ctx.state.lastSeq });
  return result;
}

/**
 * 按 `run_id` + `after_seq` 从服务端续读聊天事件（G5）。
 *
 * 服务端只读落盘记录，不会重新调用模型。`afterSeq` 语义：
 * - 同一次会话里断流：传 `state.lastSeq`，只补没渲染过的增量；
 * - 刷新后恢复：传 `0`，从头重放以重建完整回答（已渲染的部分没有落盘）。
 *
 * `truncated` 时跟随 `next_after_seq` 继续，但总请求数不超过
 * `MAX_RESUME_ATTEMPTS`——续读是补救手段，不能变成无限轮询。
 */
export async function resumeChatStream(
  runId: string,
  afterSeq: number,
  handlers: ChatHandlers,
  signal?: AbortSignal,
  state: StreamState = createStreamState(),
  sessionId = "",
): Promise<ChatResumeOutcome> {
  let cursor = Number.isFinite(afterSeq) && afterSeq > 0 ? Math.floor(afterSeq) : 0;
  let resumed = 0;
  let content = "";
  let lastError = "";
  const ctx: RunContext = { state, done: null, error: "" };

  for (let attempt = 0; attempt < MAX_RESUME_ATTEMPTS; attempt += 1) {
    if (signal?.aborted) return { status: "cancelled", content, resumed };
    let page: ChatEventPage;
    try {
      page = await api.chatStreamEvents(runId, cursor, sessionId, signal);
    } catch (error) {
      // 传输层失败也占用一次尝试：续读本来就是"一次拿不到再试一次"的补救手段，
      // 首个请求被代理 502 掉就用完额度会把"服务端已落盘终态"白白丢掉。
      lastError = (error as Error).message || "读取失败";
      continue;
    }
    const events = Array.isArray(page.events) ? page.events : [];
    for (const raw of events) {
      const record = asRecord(raw);
      if (!record) continue;
      const result = applyChatEvent(normalizeSseEvent(record), ctx, handlers, (text) => {
        content += text;
      });
      if (result === "done" && ctx.done) {
        return { status: "done", done: ctx.done, content, resumed };
      }
      if (result === "error") {
        return { status: "error", message: ctx.error, content, resumed };
      }
      if (result !== "duplicate") resumed += 1;
    }
    const next = page.next_after_seq;
    if (
      page.truncated === true &&
      typeof next === "number" &&
      Number.isFinite(next) &&
      next > cursor
    ) {
      cursor = next;
      continue;
    }
    break;
  }

  if (signal?.aborted) return { status: "cancelled", content, resumed };
  if (resumed > 0) {
    return {
      status: "partial",
      message: `连接中断，已续读 ${resumed} 条事件但未收到完成事件；本次回答不完整。`,
      content,
      resumed,
    };
  }
  return {
    status: "failed",
    message: `连接中断，未能续读（${lastError || "服务端没有该次流的记录"}）；本次回答不完整。`,
    content,
    resumed,
  };
}

/**
 * POST /api/chat_stream。
 * 后端事件：`retrieval` / `content` / `tool_call` / `search_results` / `done` / `error`。
 * 回调只会终结一次：done、error、truncated、cancelled 四者互斥。
 *
 * 网络异常（`fetch` 抛出、读取中途断流）与 EOF 无 `done` 是同一类问题：
 * 服务端可能已经把终态落盘了。因此只要手里已有 `run_id`，就走与 EOF 完全
 * 相同的续读路径（`MAX_RESUME_ATTEMPTS` 内部已限次），而不是直接报错。
 */
export async function chatStream(
  sessionId: string,
  question: string,
  handlers: ChatHandlers,
  signal?: AbortSignal,
): Promise<ChatOutcome> {
  let reader: ReadableStreamDefaultReader<Uint8Array> | undefined;
  let settled = false;
  const ctx: RunContext = { state: createStreamState(), done: null, error: "" };

  const finish = (outcome: ChatOutcome): ChatOutcome => {
    if (settled) return outcome;
    settled = true;
    if (outcome.status === "done") handlers.onDone(outcome.done);
    else if (outcome.status === "truncated") {
      if (handlers.onTruncated) handlers.onTruncated(outcome.message);
      else handlers.onError(outcome.message);
    } else if (outcome.status === "error") handlers.onError(outcome.message);
    else handlers.onError("本次对话已取消。");
    return outcome;
  };

  /** EOF 与网络异常共用的续读收尾：同一份 status -> outcome 映射，不漏分支。 */
  const settleResume = async (state: StreamState): Promise<ChatOutcome> => {
    const resumed = await resumeChatStream(
      state.runId, state.lastSeq, handlers, signal, state, sessionId,
    );
    switch (resumed.status) {
      case "done":
        return { status: "done", done: resumed.done };
      case "error":
        return { status: "error", message: resumed.message };
      case "cancelled":
        return { status: "cancelled" };
      default:
        return { status: "truncated", message: resumed.message };
    }
  };

  try {
    const response = await fetch(`${API_BASE}/api/chat_stream`, {
      method: "POST",
      headers: headers(),
      body: JSON.stringify({ Id: sessionId, Question: question }),
      signal,
    });
    if (!response.ok || !response.body) {
      let detail = "";
      try {
        const body = (await response.json()) as { detail?: unknown };
        if (typeof body.detail === "string") detail = body.detail;
      } catch {
        // 非 JSON 错误页
      }
      return finish({
        status: "error",
        message: detail || `对话请求失败: ${response.status}`,
      });
    }

    reader = response.body.getReader();
    const sse = new SseBuffer();

    for (;;) {
      const { done: eof, value } = await reader.read();
      const blocks = eof ? sse.flush() : sse.push(value as Uint8Array);
      for (const block of blocks) {
        const event = parseSseBlock(block);
        if (!event) continue;
        const result = applyChatEvent(event, ctx, handlers);
        if (result === "done" && ctx.done) return finish({ status: "done", done: ctx.done });
        if (result === "error") return finish({ status: "error", message: ctx.error });
      }
      if (eof) break;
    }

    if (signal?.aborted) return finish({ status: "cancelled" });

    // 断流：EOF 没有 done。先按 run_id/lastSeq 续读，读到终态就正常收尾。
    if (!ctx.state.runId) {
      return finish({ status: "truncated", message: TRUNCATED_MESSAGE });
    }
    return finish(await settleResume(ctx.state));
  } catch (error) {
    if (signal?.aborted) return finish({ status: "cancelled" });
    // 网络异常但已经拿到 run_id：服务端可能已经落盘终态，走与 EOF 相同的
    // 有界续读；没有 run_id 才没有可续读的东西，直接报错。
    if (ctx.state.runId) {
      try {
        return finish(await settleResume(ctx.state));
      } catch {
        return finish({ status: "error", message: (error as Error).message });
      }
    }
    return finish({ status: "error", message: (error as Error).message });
  } finally {
    if (reader) {
      try {
        await reader.cancel();
      } catch {
        // 连接已关闭时取消可能失败
      }
      reader.releaseLock();
    }
  }
}

// ── 会话与未完成流的持久化（tab 关闭/重开一致性） ──────────────────

/** localStorage 的最小接口；测试用普通对象实现，不依赖 jsdom。 */
export interface StorageLike {
  getItem: (key: string) => string | null;
  setItem: (key: string, value: string) => void;
  removeItem?: (key: string) => void;
}

/** 会话 ID 的持久化键（带版本后缀，形状变化时不会读到旧格式）。 */
export const CHAT_SESSION_KEY = "oncall.chat.session";
export const CHAT_SESSION_VERSION = 1;
/** 未完成流的位置：刷新后据此续读。 */
export const CHAT_PENDING_KEY = "oncall.chat.pending";

/**
 * 合法会话 ID：字母数字开头，只含 `A-Za-z0-9._-`，最长 128。
 *
 * `?session=` 来自 URL，是外部输入。服务端会把会话 ID 清洗成文件名，但前端在
 * 拼进 URL/请求前就拒绝非法字符，能避免“看起来切换了会话，实际读的是另一个
 * 文件”这种难以诊断的错位。
 */
export function validSessionId(value: unknown): string | null {
  if (typeof value !== "string") return null;
  const text = value.trim();
  return /^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$/.test(text) ? text : null;
}

/**
 * 把用户输入的会话 ID 规整成持久化层能接受的形式。
 *
 * 服务端会把会话 ID 清洗成文件名，但前端一旦**静默拒绝**用户输入的 ID
 * （`validSessionId` 返回 null），`persistSession`/`readPendingStream` 就都
 * 不生效，刷新后会话会悄悄跳回去——用户看到的是“聊天记录消失”。
 * 所以输入框用这个函数逐字符收敛（而不是事后拒绝），保证"看到的就是存下的"。
 */
export function sanitizeSessionInput(value: unknown): string {
  if (typeof value !== "string") return "";
  const cleaned = value.trim().replace(/[^A-Za-z0-9._-]/g, "").slice(0, 128);
  // 首字符必须是字母或数字：首字符非法时直接丢掉它，其余保留。
  return cleaned.replace(/^[._-]+/, "").slice(0, 128);
}

export interface ResolvedSession {
  sessionId: string;
  source: "url" | "storage" | "new";
}

/**
 * 决定本次页面使用哪个会话 ID：
 * 1. `?session=`（分享/调试入口）优先，但必须合法；
 * 2. 否则用 localStorage 里上次的 ID；
 * 3. 首次访问才生成并写入。
 *
 * 解析结果一律写回 localStorage：分享链接打开后继续沿用同一个会话，刷新不会
 * 跳到另一个“空历史”的会话。
 */
export function resolveSessionId(options: {
  search?: string;
  storage?: StorageLike | null;
  generate: () => string;
}): ResolvedSession {
  const storage = options.storage ?? null;
  const fromUrl = validSessionId(
    new URLSearchParams(options.search ?? "").get("session") ?? "",
  );
  if (fromUrl) {
    persistSession(storage, fromUrl);
    return { sessionId: fromUrl, source: "url" };
  }
  const stored = validSessionId(readStoredSession(storage));
  if (stored) return { sessionId: stored, source: "storage" };
  const generated = options.generate();
  persistSession(storage, generated);
  return { sessionId: generated, source: "new" };
}

function readStoredSession(storage: StorageLike | null): string | null {
  if (!storage) return null;
  try {
    const raw = storage.getItem(CHAT_SESSION_KEY);
    if (!raw) return null;
    const parsed = JSON.parse(raw) as { version?: unknown; sessionId?: unknown };
    if (Number(parsed?.version) !== CHAT_SESSION_VERSION) return null;
    return typeof parsed.sessionId === "string" ? parsed.sessionId : null;
  } catch {
    // 存储被禁用/内容损坏：当作没有记录，继续用新会话。
    return null;
  }
}

export function persistSession(storage: StorageLike | null, sessionId: string): void {
  if (!storage) return;
  const valid = validSessionId(sessionId);
  if (!valid) return;
  try {
    storage.setItem(
      CHAT_SESSION_KEY,
      JSON.stringify({ version: CHAT_SESSION_VERSION, sessionId: valid }),
    );
  } catch {
    // 隐私模式/配额满：持久化失败不影响本次对话。
  }
}

export interface PendingStream {
  sessionId: string;
  runId: string;
  lastSeq: number;
}

/** 读取某次未完成流的位置；会话不匹配视为无记录（绝不串会话续读）。 */
export function readPendingStream(
  storage: StorageLike | null,
  sessionId: string,
): PendingStream | null {
  if (!storage || !sessionId) return null;
  try {
    const raw = storage.getItem(CHAT_PENDING_KEY);
    if (!raw) return null;
    const parsed = JSON.parse(raw) as Record<string, unknown>;
    const pendingSession = validSessionId(parsed.sessionId);
    const runId = typeof parsed.runId === "string" ? parsed.runId : "";
    const lastSeq = Number(parsed.lastSeq ?? 0);
    if (pendingSession !== sessionId || !runId) return null;
    return {
      sessionId: pendingSession,
      runId,
      lastSeq: Number.isFinite(lastSeq) && lastSeq > 0 ? Math.floor(lastSeq) : 0,
    };
  } catch {
    return null;
  }
}

export function writePendingStream(
  storage: StorageLike | null,
  pending: PendingStream | null,
): void {
  if (!storage) return;
  try {
    if (!pending || !pending.runId) {
      storage.removeItem?.(CHAT_PENDING_KEY);
      return;
    }
    storage.setItem(
      CHAT_PENDING_KEY,
      JSON.stringify({
        version: CHAT_SESSION_VERSION,
        sessionId: pending.sessionId,
        runId: pending.runId,
        lastSeq: pending.lastSeq,
      }),
    );
  } catch {
    // 同上：持久化失败只影响“能否续读”，不影响正在进行的对话。
  }
}

/** 浏览器环境下可用的 localStorage（SSR/测试返回 null）。 */
export function browserStorage(): StorageLike | null {
  if (typeof window === "undefined") return null;
  try {
    return window.localStorage;
  } catch {
    return null;
  }
}

/** 服务端历史消息（只声明恢复逻辑需要的字段，避免与 api.ts 循环依赖）。 */
export interface HistoryMessage {
  role: string;
  content: string;
  citations?: Record<string, unknown>[];
  retrieval_status?: string;
  outcome?: string;
}

export interface ResumedAnswer {
  /** 是否要追加一条 assistant 消息；false 表示服务端历史里已经有它。 */
  append: boolean;
  message: HistoryMessage | null;
  /** 给用户看的续读说明；“”表示不需要提示。 */
  notice: string;
  /** 续读到的内容是否完整（收到 done）。 */
  complete: boolean;
}

/**
 * 合并“服务端历史 + 未完成流续读结果”（G5 tab 恢复）。
 *
 * 为什么需要去重：``/api/chat_stream`` 只有在拿到 ``done`` 时才把这一轮写进
 * 历史。因此断流时浏览器缺的那条回答，可能在用户刷新前已经由服务端补写完成
 * ——继续合并会把同一个回答显示两次。这里按内容判断：
 *
 * - `done`：历史最后一条 assistant 与终态内容相同 -> 已经落库，不再追加；
 * - `partial`：历史最后一条 assistant 以续读内容结尾 -> 历史更完整，不追加；
 * - `failed` / 没有续读内容：不追加（不能凭空造一条空回答）。
 *
 * 纯函数：输入历史与续读结果，输出“要不要追加”，可在 node 下直接测。
 */
export function mergeResumedAnswer(
  history: HistoryMessage[],
  resume: ChatResumeOutcome | null,
): ResumedAnswer {
  if (!resume || !resume.content) {
    return {
      append: false,
      message: null,
      notice: resume?.status === "failed" ? resume.message : "",
      complete: false,
    };
  }
  const last = history.length ? history[history.length - 1] : null;
  const lastAnswer = last && last.role === "assistant" ? String(last.content ?? "").trim() : "";
  const content = resume.content;

  if (resume.status === "done") {
    if (lastAnswer && lastAnswer === content.trim()) {
      return { append: false, message: null, notice: "", complete: true };
    }
    return {
      append: true,
      message: {
        role: "assistant",
        content: resume.done.content || content,
        citations: resume.done.citations,
        retrieval_status: resume.done.retrievalStatus,
        outcome: resume.done.outcome,
      },
      notice: `已从服务端续读并补齐本次回答（${resume.resumed} 条事件）。`,
      complete: true,
    };
  }
  if (resume.status === "partial") {
    if (lastAnswer && lastAnswer.endsWith(content.trim())) {
      return { append: false, message: null, notice: "", complete: false };
    }
    return {
      append: true,
      message: { role: "assistant", content, outcome: "" },
      notice: resume.message,
      complete: false,
    };
  }
  if (resume.status === "error") {
    return { append: false, message: null, notice: resume.message, complete: false };
  }
  // cancelled / failed：没有新增内容，只报告（cancelled 没有 message 字段）。
  return {
    append: false,
    message: null,
    notice: resume.status === "cancelled" ? "上次回答已取消，未续读。" : resume.message,
    complete: false,
  };
}
