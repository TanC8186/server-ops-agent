/**
 * 对话流式传输测试（node:test，mock 全局 fetch）。
 *
 * 覆盖 FE-04 的对话侧反例：
 * - CRLF 与多字节字符被分块切开时内容不丢；
 * - EOF 没有 done 判为 truncated（不是成功）；
 * - fetch 拒绝走 onError，而不是抛到组件；
 * - retrieval / done 的 citations、removed_citations 被透传。
 *
 * G5 追加：
 * - 重复 `event_id` 只渲染一次（重连重放幂等）；
 * - 断流后按 run_id/lastSeq 续读，读到 done 正常收尾；
 * - 续读也失败时文案区分“已续读 N 条”与“未能续读”；
 * - 取消（abort）不会被当成失败；
 * - 会话 ID 与未完成流的持久化/恢复（纯函数 + 假 localStorage）。
 */

import assert from "node:assert/strict";
import { afterEach, test } from "node:test";

import {
  MAX_RESUME_ATTEMPTS,
  chatStream,
  mergeResumedAnswer,
  readPendingStream,
  resolveSessionId,
  resumeChatStream,
  sanitizeSessionInput,
  validSessionId,
  writePendingStream,
  type ChatDone,
  type StorageLike,
} from "./chat.ts";

const encoder = new TextEncoder();

function sse(payload: unknown): string {
  return `event: message\r\ndata: ${JSON.stringify(payload)}\r\n\r\n`;
}

function mockFetch(chunks: Uint8Array[], status = 200) {
  return async () => {
    let index = 0;
    const stream = new ReadableStream<Uint8Array>({
      pull(controller) {
        if (index >= chunks.length) {
          controller.close();
          return;
        }
        controller.enqueue(chunks[index]);
        index += 1;
      },
    });
    return {
      ok: status >= 200 && status < 300,
      status,
      body: status >= 200 && status < 300 ? stream : null,
      json: async () => ({}),
      text: async () => "",
    } as unknown as Response;
  };
}

const originalFetch = globalThis.fetch;

afterEach(() => {
  globalThis.fetch = originalFetch;
});

test("content 与 done 完整到达时返回 done 并透传引用", async () => {
  const raw = encoder.encode(
    sse({ type: "content", data: "磁盘" }) +
      sse({
        type: "retrieval",
        status: "hit",
        retrieval: { chunks: [{ chunk_id: "c1" }] },
      }) +
      sse({ type: "content", data: "满了先看 inode。" }) +
      sse({
        type: "done",
        data: {
          outcome: "answered",
          retrieval_status: "hit",
          content: "磁盘满了先看 inode。",
          citations: [{ id: "K1", title: "磁盘容量排查", locator: "L1-L2", score: 0.9 }],
          removed_citations: ["K9"],
        },
      }),
  );
  // 把分块边界切在一个多字节字符中间
  const split = 12;
  globalThis.fetch = mockFetch([raw.slice(0, split), raw.slice(split)]);

  const deltas: string[] = [];
  const retrievals: string[] = [];
  const dones: ChatDone[] = [];
  const outcome = await chatStream("s", "问题", {
    onDelta: (text) => deltas.push(text),
    onRetrieval: (retrieval) => retrievals.push(retrieval.status),
    onDone: (value) => { dones.push(value); },
    onError: (message) => assert.fail(`不应报错：${message}`),
  });

  assert.equal(outcome.status, "done");
  assert.deepEqual(deltas, ["磁盘", "满了先看 inode。"]);
  assert.deepEqual(retrievals, ["hit"]);
  assert.equal(dones.length, 1);
  const done = dones[0];
  assert.equal(done.outcome, "answered");
  assert.equal(done.retrievalStatus, "hit");
  assert.equal(done.citations.length, 1);
  assert.deepEqual(done.removedCitations, ["K9"]);
});

test("EOF 没有 done 视为 truncated，而不是成功", async () => {
  globalThis.fetch = mockFetch([encoder.encode(sse({ type: "content", data: "半截" }))]);

  const deltas: string[] = [];
  let truncated = "";
  let doneCalled = false;
  const outcome = await chatStream("s", "问题", {
    onDelta: (text) => deltas.push(text),
    onDone: () => { doneCalled = true; },
    onTruncated: (message) => { truncated = message; },
    onError: () => assert.fail("truncated 不应走 onError"),
  });

  assert.equal(outcome.status, "truncated");
  assert.deepEqual(deltas, ["半截"], "已收到的事件必须保留");
  assert.equal(doneCalled, false);
  assert.match(truncated, /连接中断/);
});

test("fetch 拒绝时走 onError，不向调用方抛出", async () => {
  const failingFetch = async (): Promise<Response> => {
    throw new TypeError("network down");
  };
  globalThis.fetch = failingFetch as unknown as typeof globalThis.fetch;

  let message = "";
  const outcome = await chatStream("s", "问题", {
    onDelta: () => {},
    onDone: () => assert.fail("不应成功"),
    onError: (value) => { message = value; },
  });

  assert.equal(outcome.status, "error");
  assert.match(message, /network down/);
});

test("error 事件是终态，之后不会再回调 done", async () => {
  globalThis.fetch = mockFetch([
    encoder.encode(sse({ type: "error", data: "模型未配置" }) + sse({ type: "done" })),
  ]);

  let doneCalled = false;
  let message = "";
  const outcome = await chatStream("s", "问题", {
    onDelta: () => {},
    onDone: () => { doneCalled = true; },
    onError: (value) => { message = value; },
  });

  assert.equal(outcome.status, "error");
  assert.equal(doneCalled, false);
  assert.match(message, /模型未配置/);
});

test("HTTP 错误状态返回 error 而不是 truncated", async () => {
  globalThis.fetch = mockFetch([], 500);

  let message = "";
  const outcome = await chatStream("s", "问题", {
    onDelta: () => {},
    onDone: () => assert.fail("不应成功"),
    onError: (value) => { message = value; },
  });

  assert.equal(outcome.status, "error");
  assert.match(message, /500/);
});

// ---------- G5：去重 / 断流续读 / 取消 ----------

const RUN_ID = "0123456789abcdef0123456789abcdef";

/** 按 URL 分发的 fetch：POST 流 + GET 续读共用同一个 mock。 */
function routedFetch(
  streamChunks: Uint8Array[],
  resumePages: Array<{ status?: number; body?: unknown }>,
  hooks: { onStreamRead?: (index: number) => void } = {},
) {
  const resumeCalls: string[] = [];
  const handler = async (input: string | URL | Request, init?: RequestInit) => {
    const url = String(input);
    if (url.includes("/api/chat/events/")) {
      resumeCalls.push(url);
      const page = resumePages[Math.min(resumeCalls.length - 1, resumePages.length - 1)];
      const status = page?.status ?? 200;
      if (status >= 300) {
        return {
          ok: false,
          status,
          statusText: "Not Found",
          body: null,
          json: async () => ({ detail: "run not found" }),
        } as unknown as Response;
      }
      return {
        ok: true,
        status: 200,
        body: null,
        json: async () => page?.body ?? {},
      } as unknown as Response;
    }
    void init;
    let index = 0;
    const stream = new ReadableStream<Uint8Array>({
      pull(controller) {
        if (index >= streamChunks.length) {
          controller.close();
          return;
        }
        controller.enqueue(streamChunks[index]);
        hooks.onStreamRead?.(index);
        index += 1;
      },
    });
    return {
      ok: true,
      status: 200,
      body: stream,
      json: async () => ({}),
    } as unknown as Response;
  };
  return handler as unknown as typeof globalThis.fetch;
}

const contentEvent = (seq: number, text: string, runId = RUN_ID) => ({
  type: "content",
  data: text,
  seq,
  event_id: `${runId}:${seq}`,
  run_id: runId,
});

const doneEvent = (seq: number, content: string, runId = RUN_ID) => ({
  type: "done",
  seq,
  event_id: `${runId}:${seq}`,
  run_id: runId,
  data: { outcome: "answered", retrieval_status: "hit", content, citations: [] },
});

test("G5 重复 event_id 只渲染一次（重连重放幂等）", async () => {
  const duplicate = sse(contentEvent(1, "磁盘"));
  globalThis.fetch = routedFetch(
    [encoder.encode(duplicate + duplicate + sse(doneEvent(2, "磁盘")))],
    [],
  );

  const deltas: string[] = [];
  const outcome = await chatStream("s", "问题", {
    onDelta: (text) => deltas.push(text),
    onDone: () => {},
    onError: (message) => assert.fail(`不应报错：${message}`),
  });

  assert.equal(outcome.status, "done");
  assert.deepEqual(deltas, ["磁盘"]);
});

test("G5 断流（EOF 无 done）先续读，读到 done 正常收尾", async () => {
  globalThis.fetch = routedFetch(
    [encoder.encode(sse(contentEvent(1, "磁盘")) + sse(contentEvent(2, "满了")))],
    [
      {
        body: {
          run_id: RUN_ID,
          events: [contentEvent(3, "先看 inode。"), doneEvent(4, "磁盘满了先看 inode。")],
          truncated: false,
          next_after_seq: null,
        },
      },
    ],
  );

  const deltas: string[] = [];
  const progress: { runId: string; lastSeq: number }[] = [];
  let done: ChatDone | null = null;
  const outcome = await chatStream("s", "问题", {
    onDelta: (text) => deltas.push(text),
    onProgress: (value) => progress.push(value),
    onDone: (value) => { done = value; },
    onError: (message) => assert.fail(`不应报错：${message}`),
  });

  assert.equal(outcome.status, "done");
  assert.deepEqual(deltas, ["磁盘", "满了", "先看 inode。"], "续读增量必须接在已收到内容之后");
  assert.ok(done);
  assert.equal((done as unknown as ChatDone).content, "磁盘满了先看 inode。");
  // 续读请求要带上 run_id 与最后应用的 seq，而不是从 0 重放。
  assert.equal(progress.at(-1)?.runId, RUN_ID);
  assert.ok(progress.every((item) => item.runId === RUN_ID));
});

test("G5 续读请求带 session_id（服务端按会话归属校验续读权限）", async () => {
  let seenUrl = "";
  globalThis.fetch = (async (input: string | URL | Request) => {
    const url = String(input);
    if (url.includes("/api/chat/events/")) {
      seenUrl = url;
      return {
        ok: true,
        status: 200,
        body: null,
        json: async () => ({
          run_id: RUN_ID,
          events: [contentEvent(2, "补")],
          truncated: false,
          next_after_seq: null,
        }),
      } as unknown as Response;
    }
    const stream = sse(contentEvent(1, "半截"));
    return {
      ok: true,
      status: 200,
      body: new ReadableStream<Uint8Array>({
        start(controller) {
          controller.enqueue(encoder.encode(stream));
          controller.close();
        },
      }),
    } as unknown as Response;
  }) as typeof fetch;

  await chatStream("session-xyz", "问题", {
    onDelta: () => {},
    onDone: () => {},
    onError: () => {},
  });

  assert.ok(seenUrl.includes("/api/chat/events/"), "断流后必须尝试续读");
  assert.ok(
    seenUrl.includes("session_id=session-xyz"),
    `续读请求必须带上会话 ID（否则部署模式 403）：${seenUrl}`,
  );
  assert.ok(seenUrl.includes("after_seq="), "续读必须带 seq 游标");
});

test("G5 续读也拿不到终态：文案区分“已续读 N 条”与“未能续读”", async () => {
  // 情况一：服务端有记录但没有 done -> 已续读 N 条
  globalThis.fetch = routedFetch(
    [encoder.encode(sse(contentEvent(1, "半截")))],
    [
      {
        body: {
          run_id: RUN_ID,
          events: [contentEvent(2, "更多")],
          truncated: false,
          next_after_seq: null,
        },
      },
    ],
  );
  let partial = "";
  const partialOutcome = await chatStream("s", "问题", {
    onDelta: () => {},
    onDone: () => assert.fail("没有 done 不应成功"),
    onTruncated: (message) => { partial = message; },
    onError: () => assert.fail("truncated 不应走 onError"),
  });
  assert.equal(partialOutcome.status, "truncated");
  assert.match(partial, /已续读 1 条/);

  // 情况二：服务端没有该次流的记录（404）-> 未能续读
  globalThis.fetch = routedFetch(
    [encoder.encode(sse(contentEvent(1, "半截")))],
    [{ status: 404 }],
  );
  let missing = "";
  const missingOutcome = await chatStream("s", "问题", {
    onDelta: () => {},
    onDone: () => assert.fail("没有 done 不应成功"),
    onTruncated: (message) => { missing = message; },
    onError: () => assert.fail("truncated 不应走 onError"),
  });
  assert.equal(missingOutcome.status, "truncated");
  assert.match(missing, /未能续读/);
  assert.doesNotMatch(missing, /已续读/);
});

test("G5 续读有界：截断页最多再请求 MAX_RESUME_ATTEMPTS 次", async () => {
  let calls = 0;
  globalThis.fetch = (async (url: string | URL | Request) => {
    calls += 1;
    void url;
    return {
      ok: true,
      status: 200,
      body: null,
      json: async () => ({
        run_id: RUN_ID,
        // 每页都有新内容但永远没有 done，且一直声称还有更多。
        events: [contentEvent(calls, `块${calls}`)],
        truncated: true,
        next_after_seq: calls,
      }),
    } as unknown as Response;
  }) as unknown as typeof globalThis.fetch;

  const outcome = await resumeChatStream(RUN_ID, 0, {
    onDelta: () => {},
    onDone: () => assert.fail("不应成功"),
    onError: () => {},
  });

  assert.equal(outcome.status, "partial");
  assert.equal(calls, MAX_RESUME_ATTEMPTS);
});

test("G5 取消：EOF 前 abort 返回 cancelled，不报失败", async () => {
  const controller = new AbortController();
  globalThis.fetch = routedFetch(
    [encoder.encode(sse(contentEvent(1, "半截")))],
    [],
    { onStreamRead: () => controller.abort() },
  );

  const outcome = await chatStream(
    "s",
    "问题",
    {
      onDelta: () => {},
      onDone: () => assert.fail("取消不应成功"),
      onError: () => assert.fail("取消不应走 onError"),
    },
    controller.signal,
  );

  assert.equal(outcome.status, "cancelled");
});

// ---------- G6 P1：网络异常续读 / 主动停止不恢复 ----------

test("G6 读到一半网络异常：按 run_id 续读并拿到终态", async () => {
  // 第一次 POST 先投递一条 content 事件，然后底层流报错（不是正常 EOF）。
  const failingStream = () => {
    let sent = false;
    return new ReadableStream<Uint8Array>({
      pull(controller) {
        if (!sent) {
          sent = true;
          controller.enqueue(encoder.encode(sse(contentEvent(1, "半截"))));
          return;
        }
        controller.error(new TypeError("network reset"));
      },
    });
  };

  const seen: string[] = [];
  globalThis.fetch = (async (input: string | URL | Request) => {
    const url = String(input);
    if (url.includes("/api/chat/events/")) {
      seen.push(url);
      return {
        ok: true,
        status: 200,
        body: null,
        json: async () => ({
          run_id: RUN_ID,
          events: [contentEvent(2, "补全"), doneEvent(3, "半截补全")],
          truncated: false,
          next_after_seq: null,
        }),
      } as unknown as Response;
    }
    return { ok: true, status: 200, body: failingStream() } as unknown as Response;
  }) as unknown as typeof globalThis.fetch;

  const deltas: string[] = [];
  let done: ChatDone | null = null;
  const outcome = await chatStream("s", "问题", {
    onDelta: (text) => deltas.push(text),
    onDone: (value) => { done = value; },
    onError: (message) => assert.fail(`应续读成功，不应报错：${message}`),
  });

  assert.equal(outcome.status, "done");
  assert.ok(seen.length >= 1, "网络异常后必须按已有 run_id 续读");
  assert.ok(seen[0].includes(`after_seq=1`), "续读要从中断的 seq 之后继续");
  assert.deepEqual(deltas, ["半截", "补全"]);
  assert.equal((done as unknown as ChatDone).content, "半截补全");
});

test("G6 首个续读请求传输失败：仍再试一次并拿到已落盘终态", async () => {
  // 一次瞬时 502 不该让"服务端已落盘终态"被丢掉：MAX_RESUME_ATTEMPTS 是
  // **请求次数**上限，传输失败也要占用一次尝试并继续重试。
  const calls: string[] = [];
  let resumeAttempts = 0;
  const stream = new ReadableStream<Uint8Array>({
    start(ctrl) {
      // 先投递一条增量，然后 EOF 且没有 done：这就是"读到一半断流"。
      ctrl.enqueue(encoder.encode(sse(contentEvent(1, "半截"))));
      ctrl.close();
    },
  });
  globalThis.fetch = (async (input: string | URL | Request) => {
    const url = String(input);
    calls.push(url);
    if (!url.includes("/api/chat/events/")) {
      return { ok: true, status: 200, body: stream, json: async () => ({}) } as unknown as Response;
    }
    resumeAttempts += 1;
    if (resumeAttempts === 1) {
      // 首个续读：代理层 502。
      return {
        ok: false, status: 502, statusText: "Bad Gateway", body: null,
        json: async () => ({ detail: "bad gateway" }),
      } as unknown as Response;
    }
    return {
      ok: true, status: 200, body: null,
      json: async () => ({
        run_id: RUN_ID,
        events: [contentEvent(2, "补上的"), doneEvent(3, "补上的回答")],
        truncated: false,
        next_after_seq: null,
      }),
    } as unknown as Response;
  }) as unknown as typeof globalThis.fetch;

  const outcome = await chatStream("s", "问题", {
    onDelta: () => {},
    onDone: () => {},
    onError: () => assert.fail("续读重试后应成功"),
    onTruncated: () => assert.fail("续读重试后不应报中断"),
  });

  assert.equal(outcome.status, "done");
  assert.equal(
    outcome.status === "done" ? outcome.done.content : "",
    "补上的回答",
  );
  assert.equal(resumeAttempts, 2, "首个续读失败后必须再试一次");
});

test("G6 主动停止：abort 后返回 cancelled，且不再发起续读", async () => {
  const controller = new AbortController();
  let resumeCalls = 0;
  const stream = new ReadableStream<Uint8Array>({
    start(ctrl) {
      ctrl.enqueue(encoder.encode(sse(contentEvent(1, "半截"))));
      // 用户看到第一个增量后点“停止”。
      controller.abort();
      ctrl.error(new DOMException("aborted", "AbortError"));
    },
  });
  globalThis.fetch = (async (input: string | URL | Request) => {
    const url = String(input);
    if (url.includes("/api/chat/events/")) {
      resumeCalls += 1;
      return { ok: true, status: 200, body: null, json: async () => ({}) } as unknown as Response;
    }
    return { ok: true, status: 200, body: stream } as unknown as Response;
  }) as unknown as typeof globalThis.fetch;

  const outcome = await chatStream("s", "问题", {
    onDelta: () => {},
    onDone: () => assert.fail("取消不应成功"),
    // 取消沿用既有 finish 行为（cancelled 也会走一次 onError 文案），
    // 这里只关心终态与“不续读”。
    onError: () => {},
  }, controller.signal);

  assert.equal(outcome.status, "cancelled");
  assert.equal(resumeCalls, 0, "主动取消不是断流，不能触发续读");
});

test("G5 会话 ID：URL 优先，其次 localStorage，非法值被拒绝", () => {
  const memory = new Map<string, string>();
  const storage: StorageLike = {
    getItem: (key) => memory.get(key) ?? null,
    setItem: (key, value) => { memory.set(key, value); },
    removeItem: (key) => { memory.delete(key); },
  };
  const generate = () => "web-generated1";

  assert.equal(validSessionId("web-abc.1_2"), "web-abc.1_2");
  assert.equal(validSessionId("-bad"), null);
  assert.equal(validSessionId("../etc/passwd"), null);
  assert.equal(validSessionId("a".repeat(129)), null);
  assert.equal(validSessionId(""), null);

  // 首次访问：生成并保存。
  const first = resolveSessionId({ search: "", storage, generate });
  assert.deepEqual(first, { sessionId: "web-generated1", source: "new" });
  // 刷新：沿用 localStorage 里的会话（不再每次换一个空历史会话）。
  const second = resolveSessionId({ search: "", storage, generate: () => "web-other" });
  assert.deepEqual(second, { sessionId: "web-generated1", source: "storage" });
  // ?session= 优先，并写回存储，分享链接打开后刷新不跳会话。
  const shared = resolveSessionId({ search: "?session=web-shared", storage, generate });
  assert.deepEqual(shared, { sessionId: "web-shared", source: "url" });
  assert.equal(
    resolveSessionId({ search: "", storage, generate }).sessionId,
    "web-shared",
  );
  // 非法 ?session= 被拒绝，退回到已存会话，不能把路径拼进请求。
  const rejected = resolveSessionId({ search: "?session=..%2F..%2Fsecret", storage, generate });
  assert.deepEqual(rejected, { sessionId: "web-shared", source: "storage" });
  // 存储不可用（隐私模式）仍能工作，只是不持久化。
  assert.deepEqual(
    resolveSessionId({ search: "", storage: null, generate: () => "web-nostore" }),
    { sessionId: "web-nostore", source: "new" },
  );
});

test("G6 会话 ID 输入框：字符被收敛而不是静默丢弃", () => {
  // 输入框以前只 trim，用户输入中文/空格等字符时 `persistSession` 直接拒绝，
  // 刷新后会话悄悄跳回去（历史"消失"）。这里锁定逐字符收敛的行为。
  assert.equal(sanitizeSessionInput("  会话1  "), "1");
  assert.equal(sanitizeSessionInput("web-abc 123"), "web-abc123");
  assert.equal(sanitizeSessionInput("../etc/passwd"), "etcpasswd");
  assert.equal(sanitizeSessionInput("__ok"), "ok");
  assert.equal(sanitizeSessionInput("a".repeat(200)).length, 128);
  // 收敛后一定能被持久化层接受（这是"看到的就是存下的"的最小保证）。
  const cleaned = sanitizeSessionInput("会话abc-1");
  assert.equal(validSessionId(cleaned), "abc-1");
});

test("G5 未完成流：按会话隔离，完成后清除", () => {
  const memory = new Map<string, string>();
  const storage: StorageLike = {
    getItem: (key) => memory.get(key) ?? null,
    setItem: (key, value) => { memory.set(key, value); },
    removeItem: (key) => { memory.delete(key); },
  };

  assert.equal(readPendingStream(storage, "s1"), null);
  writePendingStream(storage, { sessionId: "s1", runId: RUN_ID, lastSeq: 7 });
  assert.deepEqual(readPendingStream(storage, "s1"), {
    sessionId: "s1",
    runId: RUN_ID,
    lastSeq: 7,
  });
  // 另一个会话绝不能拿到这条位置（否则会续读别人的 run）。
  assert.equal(readPendingStream(storage, "s2"), null);
  // done 之后清除：下次打开不会再追加一遍。
  writePendingStream(storage, null);
  assert.equal(readPendingStream(storage, "s1"), null);
});

test("G5 tab 恢复：终态已落库时不重复追加回答", () => {
  const history = [
    { role: "user", content: "磁盘满了怎么办" },
    { role: "assistant", content: "先看 inode。", citations: [], retrieval_status: "hit" },
  ];
  const resume = {
    status: "done" as const,
    done: {
      outcome: "answered",
      retrievalStatus: "hit",
      content: "先看 inode。",
      citations: [{ id: "K1", title: "磁盘", score: 0.9 }],
      removedCitations: [],
      reason: "",
    },
    content: "先看 inode。",
    resumed: 2,
  };
  const merged = mergeResumedAnswer(history, resume);
  assert.equal(merged.append, false, "服务端历史已有同一回答，不能再追加一条");
  assert.equal(merged.complete, true);
  assert.equal(merged.notice, "");

  // 历史里没有这条回答（断流时服务端还没写）-> 追加，并带上引用。
  const fresh = mergeResumedAnswer([], resume);
  assert.equal(fresh.append, true);
  assert.equal(fresh.message?.role, "assistant");
  assert.equal(fresh.message?.citations?.length, 1);
  assert.match(fresh.notice, /已从服务端续读/);

  // 只有部分内容：追加但标记不完整，文案说明“未收到完成事件”。
  const partial = mergeResumedAnswer([], {
    status: "partial",
    message: "连接中断，已续读 2 条事件但未收到完成事件；本次回答不完整。",
    content: "半截回答",
    resumed: 2,
  });
  assert.equal(partial.append, true);
  assert.equal(partial.complete, false);
  assert.equal(partial.message?.content, "半截回答");

  // 续读失败 / 无内容：不追加空回答，只报告原因。
  const failed = mergeResumedAnswer(history, {
    status: "failed",
    message: "连接中断，未能续读（run not found）；本次回答不完整。",
    content: "",
    resumed: 0,
  });
  assert.equal(failed.append, false);
  assert.match(failed.notice, /未能续读/);
});
