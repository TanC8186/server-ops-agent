/**
 * 诊断 SSE 解析（前端公共传输层）。
 *
 * 约束与已修复的行为一致：
 * - 保留原始缓冲区，跨网络分块的 CRLF / UTF-8 多字节字符不能丢事件；
 * - 只把显式 `complete` 当作完成；缺少终态的 EOF 视为中断；
 * - 任何异常都要能区分“用户取消”和“服务端/网络错误”；
 * - 执行模式不一致（本地模式收到云端事件）立即中止。
 *
 * 这里把“解析 + 状态机”抽成纯函数，方便用真实的字节分片测试；
 * fetch/ReadableStream 的驱动在 `diagnose()` 中。
 */

export type ExecutionMode = "local" | "cloud_multi";

export interface RunEvent {
  ts: string;
  event: string;
  type?: string;
  seq?: number;
  event_id?: string;
  schema_version?: string;
  run_id?: string;
  session_id?: string;
  execution_mode?: ExecutionMode;
  backend?: string;
  payload?: Record<string, unknown>;
  [key: string]: unknown;
}

export interface SseParserOptions {
  expectedMode?: ExecutionMode;
  /**
   * 终态事件名。诊断流用 `complete`/`error`（默认），知识问答流用 `done`/`error`
   * ——两者的事件形状不同，但“终态只能出现一次”的语义相同，所以由调用方声明，
   * 而不是在 `applyBlock` 里猜。默认值保持既有诊断行为不变。
   */
  terminalTypes?: readonly string[];
}

const DEFAULT_TERMINAL_TYPES: readonly string[] = ["complete", "error"];

export type ParseOutcome =
  | { kind: "event"; event: RunEvent }
  | { kind: "terminal"; event: RunEvent; outcome: "complete" | "error" }
  | { kind: "protocol-error"; message: string };

const LF = String.fromCharCode(10);
const CR = String.fromCharCode(13);

/** 从一段完整的 SSE 事件块（不含分隔符）解析出 data 负载。 */
export function parseSseBlock(block: string): RunEvent | null {
  const lines = block.split(/\r?\n/);
  const dataLines: string[] = [];
  let eventName = "";
  for (const line of lines) {
    if (line.startsWith("data:")) {
      dataLines.push(line.slice(5).replace(/^ /, ""));
    } else if (line.startsWith("event:")) {
      eventName = line.slice(6).trim();
    }
  }
  if (!dataLines.length) return null;
  const raw = dataLines.join(LF);
  if (!raw.trim()) return null;
  let payload: Record<string, unknown>;
  try {
    payload = JSON.parse(raw) as Record<string, unknown>;
  } catch {
    return null;
  }
  return normalizeSseEvent({ ...payload, type: payload.type ?? payload.event ?? eventName });
}

/**
 * 后端 SSE 用 `type`，trace 文件用 `event`。统一成一种结构，前端只处理一种。
 * `payload`（事件信封）里的字段会被提升，但不允许覆盖服务端保留字段。
 */
export function normalizeSseEvent(payload: Record<string, unknown>): RunEvent {
  const type = String(payload.type ?? payload.event ?? "unknown");
  const outer = payload.payload;
  const merged: Record<string, unknown> =
    outer && typeof outer === "object" && !Array.isArray(outer)
      ? { ...(outer as Record<string, unknown>), ...payload }
      : { ...payload };
  const base: RunEvent = {
    ...(merged as RunEvent),
    ts: String(payload.ts ?? new Date().toISOString()),
    event: type,
    type,
  };
  if (type === "step_complete" && base.step === undefined) {
    base.step = payload.current_step as string | undefined;
  }
  if ((type === "plan" || type === "replan") && base.steps === undefined) {
    base.steps = payload.plan_steps ?? payload.steps ?? payload.plan;
  }
  if (
    ["report", "complete", "final_response", "run_complete"].includes(type) &&
    base.result_preview === undefined
  ) {
    base.result_preview = (payload.report ?? payload.response ?? payload.result_preview) as
      | string
      | undefined;
  }
  return base;
}

/**
 * 增量 SSE 分帧器：吃进任意字节分块，吐出完整事件块。
 * 只用 `decoder.decode(value, { stream: true })`，因此被切开的 UTF-8 字节
 * 会留在解码器内部，不会产生替换字符。
 */
export class SseBuffer {
  private buffer = "";
  private readonly decoder = new TextDecoder();

  push(chunk: Uint8Array): string[] {
    this.buffer += this.decoder.decode(chunk, { stream: true });
    return this.drain(false);
  }

  /** 连接结束时调用：解码器残留字节 + 未以空行结尾的最后一块。 */
  flush(): string[] {
    this.buffer += this.decoder.decode();
    return this.drain(true);
  }

  private drain(final: boolean): string[] {
    const blocks: string[] = [];
    let index = this.buffer.search(/\r?\n\r?\n/);
    while (index !== -1) {
      const match = /\r?\n\r?\n/.exec(this.buffer.slice(index)) as RegExpExecArray;
      blocks.push(this.buffer.slice(0, index));
      this.buffer = this.buffer.slice(index + match[0].length);
      index = this.buffer.search(/\r?\n\r?\n/);
    }
    if (final && this.buffer.trim()) {
      blocks.push(this.buffer);
      this.buffer = "";
    }
    return blocks;
  }
}

export interface StreamState {
  terminalSeen: boolean;
  lastSeq: number;
  seenEventIds: Set<string>;
  /** 当前流的 run_id（来自事件信封）；续读与持久化都要用它。 */
  runId: string;
}

export function createStreamState(): StreamState {
  return { terminalSeen: false, lastSeq: 0, seenEventIds: new Set(), runId: "" };
}

/**
 * 去重 + 游标推进。`applyBlock` 与“非 SSE 来源的回放事件”（聊天续读拿到的
 * JSON 数组）共用这一份实现，避免两处去重规则漂移。
 * 返回 false 表示重复投递，调用方应丢弃。
 */
export function acceptEvent(event: RunEvent, state: StreamState): boolean {
  if (event.event_id) {
    if (state.seenEventIds.has(event.event_id)) return false;
    state.seenEventIds.add(event.event_id);
  }
  if (typeof event.seq === "number" && event.seq > state.lastSeq) state.lastSeq = event.seq;
  if (typeof event.run_id === "string" && event.run_id) state.runId = event.run_id;
  return true;
}

/**
 * 把事件块应用到状态机，返回应该交给 UI 的事件。
 * - 重复 event_id 被丢弃（重连/重放幂等）；
 * - 乱序 seq 被接受但不回退 lastSeq；
 * - 终态只处理一次。
 */
export function applyBlock(
  block: string,
  state: StreamState,
  options: SseParserOptions = {},
): ParseOutcome[] {
  const event = parseSseBlock(block);
  if (!event) return [];

  if (!acceptEvent(event, state)) return [];
  return applyEventToState(event, state, options);
}

/**
 * 对已解析的事件做模式校验 + 终态识别（不做去重，`acceptEvent` 已完成）。
 *
 * 聊天续读拿到的是 JSON 事件数组而不是 SSE 文本块，但“重复投递只处理一次、
 * 终态只出现一次”的规则必须与实时流完全一致，所以两条路径都收敛到这里。
 */
export function applyEventToState(
  event: RunEvent,
  state: StreamState,
  options: SseParserOptions = {},
): ParseOutcome[] {
  const outcomes: ParseOutcome[] = [];
  if (
    options.expectedMode &&
    event.execution_mode &&
    event.execution_mode !== options.expectedMode
  ) {
    outcomes.push({
      kind: "protocol-error",
      message: `执行模式与请求不一致（请求 ${options.expectedMode}，收到 ${event.execution_mode}），已停止显示本次运行。`,
    });
    return outcomes;
  }
  if (options.expectedMode === "local" && event.backend === "cloud") {
    outcomes.push({
      kind: "protocol-error",
      message: "本地模式收到云端执行事件，已停止本次运行。",
    });
    return outcomes;
  }

  const terminalTypes = options.terminalTypes ?? DEFAULT_TERMINAL_TYPES;
  if (terminalTypes.includes(event.event)) {
    if (state.terminalSeen) return outcomes;
    state.terminalSeen = true;
    outcomes.push({
      kind: "terminal",
      event,
      outcome: event.event === "error" ? "error" : "complete",
    });
    return outcomes;
  }
  outcomes.push({ kind: "event", event });
  return outcomes;
}

export type DiagnoseOutcome =
  | { status: "complete"; events: RunEvent[] }
  | { status: "error"; events: RunEvent[]; message: string }
  | { status: "cancelled"; events: RunEvent[] }
  | { status: "truncated"; events: RunEvent[]; message: string };

export interface DiagnoseHandlers {
  onEvent: (event: RunEvent) => void;
  onDone: () => void;
  onError: (message: string) => void;
}

/**
 * 消费一次 POST /api/aiops 的 SSE。
 *
 * 修复点：
 * - fetch 与 reader 全生命周期在 try/catch/finally 内，网络拒绝也会 onError；
 * - 回调只终结一次（终态或异常，不会既 onDone 又 onError）；
 * - 缺少终态的 EOF 报“中断”，已收到的事件仍然保留。
 */
export async function diagnose(
  url: string,
  body: unknown,
  headers: Record<string, string>,
  handlers: DiagnoseHandlers,
  signal?: AbortSignal,
  expectedMode?: ExecutionMode,
): Promise<DiagnoseOutcome> {
  const events: RunEvent[] = [];
  const state = createStreamState();
  let settled = false;
  let reader: ReadableStreamDefaultReader<Uint8Array> | undefined;
  let outcome: DiagnoseOutcome = {
    status: "truncated",
    events,
    message: "连接已中断，未收到诊断完成事件。",
  };

  const finish = (result: DiagnoseOutcome) => {
    outcome = result;
    if (settled) return;
    settled = true;
    if (result.status === "complete") handlers.onDone();
    else if (result.status === "cancelled") handlers.onError("本次诊断已取消。");
    else handlers.onError(result.message);
  };

  try {
    const response = await fetch(url, {
      method: "POST",
      headers,
      body: JSON.stringify(body),
      signal,
    });
    if (!response.ok || !response.body) {
      let detail = "";
      try {
        const parsed = (await response.json()) as { detail?: unknown };
        if (typeof parsed.detail === "string") detail = parsed.detail;
      } catch {
        // 代理层可能返回非 JSON 错误页
      }
      const message = detail || `诊断请求失败: ${response.status}`;
      finish({ status: "error", events, message });
      return outcome;
    }

    reader = response.body.getReader();
    const sse = new SseBuffer();
    let protocolError = "";
    for (;;) {
      const { done, value } = await reader.read();
      const blocks = done ? sse.flush() : sse.push(value as Uint8Array);
      for (const block of blocks) {
        for (const result of applyBlock(block, state, { expectedMode })) {
          if (result.kind === "protocol-error") {
            protocolError = result.message;
            break;
          }
          events.push(result.event);
          handlers.onEvent(result.event);
          if (result.kind === "terminal") {
            if (result.outcome === "complete") {
              finish({ status: "complete", events });
            } else {
              finish({
                status: "error",
                events,
                message: String(result.event.message ?? "诊断失败"),
              });
            }
            return outcome;
          }
        }
        if (protocolError) break;
      }
      if (protocolError) {
        await abortReader(reader, signal);
        finish({ status: "error", events, message: protocolError });
        return outcome;
      }
      if (done) break;
    }
    if (signal?.aborted) {
      finish({ status: "cancelled", events });
    } else {
      finish({
        status: "truncated",
        events,
        message: "连接已中断，未收到诊断完成事件。可在最近运行中查看已保存的记录。",
      });
    }
    return outcome;
  } catch (error) {
    if (signal?.aborted) {
      finish({ status: "cancelled", events });
      return outcome;
    }
    finish({ status: "error", events, message: (error as Error).message });
    return outcome;
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

async function abortReader(
  reader: ReadableStreamDefaultReader<Uint8Array>,
  signal?: AbortSignal,
): Promise<void> {
  try {
    await reader.cancel();
  } catch {
    // 已关闭
  }
  if (signal?.aborted) return;
}

export { CR };
