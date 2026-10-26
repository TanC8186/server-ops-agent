/**
 * 对话（RAG）接口：与 AIOps 诊断共用 `sse.ts` 的传输层。
 *
 * 修复点（FE-04）：
 * - 分帧交给 `SseBuffer`，不再逐块 `split(CRLF)`——CRLF 或 UTF-8 多字节字符
 *   被网络分块切开时不会丢事件；
 * - `fetch` 放进 `try`，网络拒绝也会回调 `onError`；
 * - EOF 前没有 `done` 视为 truncated，绝不当作成功；
 * - reader 在 `finally` 里 `cancel()+releaseLock()`；
 * - 支持 `retrieval` / `content` / `done` 与 `citations`、`removed_citations`。
 */

import { API_BASE, headers } from "./api.ts";
import { SseBuffer, parseSseBlock, type RunEvent } from "./sse.ts";

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

export interface ChatHandlers {
  onDelta: (text: string) => void;
  onRetrieval?: (retrieval: ChatRetrieval) => void;
  onEvent?: (event: RunEvent) => void;
  onDone: (done: ChatDone) => void;
  onError: (message: string) => void;
  /** EOF 没有 done 时调用；未提供时降级为 onError。 */
  onTruncated?: (message: string) => void;
}

export type ChatOutcome =
  | { status: "done"; done: ChatDone }
  | { status: "error"; message: string }
  | { status: "cancelled" }
  | { status: "truncated"; message: string };

const TRUNCATED_MESSAGE = "连接中断，已收到的事件保留；本次回答不完整。";

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

/**
 * POST /api/chat_stream。
 * 后端事件：`retrieval` / `content` / `tool_call` / `search_results` / `done` / `error`。
 * 回调只会终结一次：done、error、truncated、cancelled 四者互斥。
 */
export async function chatStream(
  sessionId: string,
  question: string,
  handlers: ChatHandlers,
  signal?: AbortSignal,
): Promise<ChatOutcome> {
  let reader: ReadableStreamDefaultReader<Uint8Array> | undefined;
  let settled = false;

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
    let done: ChatDone | null = null;

    for (;;) {
      const { done: eof, value } = await reader.read();
      const blocks = eof ? sse.flush() : sse.push(value as Uint8Array);
      for (const block of blocks) {
        const event = parseSseBlock(block);
        if (!event) continue;
        const type = String(event.event ?? event.type ?? "");
        if (type === "content") {
          const text = String(event.data ?? "");
          if (text) handlers.onDelta(text);
        } else if (type === "retrieval") {
          handlers.onRetrieval?.(toRetrieval(event));
          handlers.onEvent?.(event);
        } else if (type === "done") {
          done = toDone(event as unknown as Record<string, unknown>);
        } else if (type === "error") {
          const body = asRecord(event.data);
          const message = String(
            (body?.message ?? event.data ?? event.message) || "对话失败",
          );
          return finish({ status: "error", message });
        } else {
          handlers.onEvent?.(event);
        }
      }
      if (done) return finish({ status: "done", done });
      if (eof) break;
    }

    if (signal?.aborted) return finish({ status: "cancelled" });
    return finish({ status: "truncated", message: TRUNCATED_MESSAGE });
  } catch (error) {
    if (signal?.aborted) return finish({ status: "cancelled" });
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
