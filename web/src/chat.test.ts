/**
 * 对话流式传输测试（node:test，mock 全局 fetch）。
 *
 * 覆盖 FE-04 的对话侧反例：
 * - CRLF 与多字节字符被分块切开时内容不丢；
 * - EOF 没有 done 判为 truncated（不是成功）；
 * - fetch 拒绝走 onError，而不是抛到组件；
 * - retrieval / done 的 citations、removed_citations 被透传。
 */

import assert from "node:assert/strict";
import { afterEach, test } from "node:test";

import { chatStream, type ChatDone } from "./chat.ts";

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
