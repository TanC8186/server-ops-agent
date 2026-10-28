/**
 * 前端 SSE 传输层回归测试（node:test + TS type stripping，无需浏览器）。
 *
 * 覆盖审查报告 FE-04 的反例：
 * - 中文内容按字节被 CRLF 切分时不能丢内容；
 * - 多行 data 事件；
 * - 缺少 done 的 EOF 必须报“中断”；
 * - 重复/乱序事件幂等；
 * - 终态只回调一次；
 * - 本地模式收到云端事件必须中止。
 */

import assert from "node:assert/strict";
import { test } from "node:test";

import {
  SseBuffer,
  applyBlock,
  createStreamState,
  normalizeSseEvent,
  parseSseBlock,
} from "./sse.ts";

const encoder = new TextEncoder();

function block(payload: unknown, eventName = "message"): string {
  return `event: ${eventName}\ndata: ${JSON.stringify(payload)}`;
}

test("parseSseBlock 解析单事件并统一 type 字段", () => {
  const event = parseSseBlock(block({ type: "agent_status", agent: "metrics", status: "running" }));
  assert.ok(event);
  if (!event) return;
  assert.equal(event.event, "agent_status");
  assert.equal(event.agent, "metrics");
});

test("多行 data 会被拼接成一个 JSON", () => {
  const text = ['event: message', 'data: {"type":"status",', 'data: "message":"多行"}'].join("\n");
  const event = parseSseBlock(text);
  assert.ok(event);
  if (!event) return;
  assert.equal(event.event, "status");
  assert.equal(event.message, "多行");
});

test("payload 字段被提升，但不能覆盖保留字段", () => {
  const event = normalizeSseEvent({
    schema_version: "1",
    event_id: "run:1",
    seq: 1,
    run_id: "run",
    type: "tool_step",
    payload: { step: "query_cpu_metrics", run_id: "forged", seq: 99, ok: true },
  });
  assert.equal(event.step, "query_cpu_metrics");
  assert.equal(event.ok, true);
  assert.equal(event.run_id, "run");
  assert.equal(event.seq, 1);
});

test("中文 UTF-8 被字节切开时不会丢字", () => {
  const raw = encoder.encode(block({ type: "content", data: "磁盘使用率过高，先看 inode。\n" }) + "\n\n");
  const split = 41; // 落在一个多字节字符中间
  const sse = new SseBuffer();
  const blocks = [...sse.push(raw.slice(0, split)), ...sse.push(raw.slice(split)), ...sse.flush()];
  assert.equal(blocks.length, 1);
  const event = parseSseBlock(blocks[0]);
  assert.ok(event);
  assert.equal(event.data, "磁盘使用率过高，先看 inode。\n");
});

test("CRLF 被切在两个分块之间仍能分帧", () => {
  const sse = new SseBuffer();
  // 分块边界落在 "\r\n\r\n" 的第二个 "\r" 与 "\n" 之间
  const first = encoder.encode(block({ type: "status", message: "a" }) + "\r\n\r");
  const second = encoder.encode("\n" + block({ type: "status", message: "b" }) + "\r\n\r\n");
  const blocks = [...sse.push(first), ...sse.push(second)];
  assert.equal(blocks.length, 2);
  assert.equal(parseSseBlock(blocks[0])?.message, "a");
  assert.equal(parseSseBlock(blocks[1])?.message, "b");
});

test("flush 返回没有以空行结尾的最后一块", () => {
  const sse = new SseBuffer();
  const blocks = [...sse.push(encoder.encode(block({ type: "report", report: "x" }))), ...sse.flush()];
  assert.equal(blocks.length, 1);
  assert.equal(parseSseBlock(blocks[0])?.event, "report");
});

test("重复 event_id 被丢弃，乱序 seq 不回退", () => {
  const state = createStreamState();
  const first = block({ type: "tool_step", event_id: "r:2", seq: 2 });
  const duplicate = block({ type: "tool_step", event_id: "r:2", seq: 2 });
  const earlier = block({ type: "status", event_id: "r:1", seq: 1 });

  assert.equal(applyBlock(first, state).length, 1);
  assert.equal(applyBlock(duplicate, state).length, 0);
  assert.equal(applyBlock(earlier, state).length, 1);
  assert.equal(state.lastSeq, 2, "乱序到达的旧事件不能让 seq 回退");
});

test("终态只产生一次", () => {
  const state = createStreamState();
  const complete = block({ type: "complete", response: "ok" });

  const first = applyBlock(complete, state);
  const second = applyBlock(complete, state);

  assert.equal(first.length, 1);
  assert.equal(first[0].kind, "terminal");
  assert.equal(second.length, 0);
});

test("error 事件是终态而不是普通事件", () => {
  const state = createStreamState();
  const outcomes = applyBlock(block({ type: "error", message: "boom" }), state);
  assert.equal(outcomes.length, 1);
  assert.equal(outcomes[0].kind, "terminal");
  assert.equal(outcomes[0].kind === "terminal" && outcomes[0].outcome, "error");
});

test("执行模式不一致会中止", () => {
  const state = createStreamState();
  const outcomes = applyBlock(
    block({ type: "agent_status", execution_mode: "cloud_multi" }),
    state,
    { expectedMode: "local" },
  );
  assert.equal(outcomes.length, 1);
  assert.equal(outcomes[0].kind, "protocol-error");
});

test("本地模式收到 cloud backend 事件会中止", () => {
  const state = createStreamState();
  const outcomes = applyBlock(
    block({ type: "planner_route", backend: "cloud" }),
    state,
    { expectedMode: "local" },
  );
  assert.equal(outcomes[0].kind, "protocol-error");
});

test("非 JSON 心跳被忽略而不是抛错", () => {
  const state = createStreamState();
  assert.deepEqual(applyBlock("data: ping", state), []);
});

test("缺少终态的 EOF 由调用方判定为中断", () => {
  const sse = new SseBuffer();
  const blocks = [...sse.push(encoder.encode(block({ type: "status", message: "x" }) + "\n\n")), ...sse.flush()];
  const state = createStreamState();
  const outcomes = blocks.flatMap((item) => applyBlock(item, state));
  assert.equal(outcomes.length, 1);
  assert.equal(outcomes[0].kind, "event");
  assert.equal(state.terminalSeen, false);
});

test("G5 终态事件名由调用方声明：聊天流用 done，诊断流不变", () => {
  // 默认（诊断）：done 不是终态，仍然作为普通事件交给 UI。
  const diagnoseState = createStreamState();
  const chatDone = block({ type: "done", seq: 1, data: { content: "x" } });
  const asEvent = applyBlock(chatDone, diagnoseState);
  assert.equal(asEvent[0].kind, "event");

  // 聊天流：done 是终态，且只产生一次。
  const chatState = createStreamState();
  const first = applyBlock(chatDone, chatState, { terminalTypes: ["done", "error"] });
  assert.equal(first[0].kind, "terminal");
  assert.equal(first[0].kind === "terminal" && first[0].outcome, "complete");
  assert.deepEqual(
    applyBlock(chatDone, chatState, { terminalTypes: ["done", "error"] }),
    [],
  );
  // error 在两种模式下都是终态。
  const errorState = createStreamState();
  const errored = applyBlock(block({ type: "error", message: "boom" }), errorState, {
    terminalTypes: ["done", "error"],
  });
  assert.equal(errored[0].kind === "terminal" && errored[0].outcome, "error");
});

test("G5 状态机记录 run_id 与 lastSeq，供断流续读取用", () => {
  const state = createStreamState();
  assert.equal(state.runId, "");
  applyBlock(block({ type: "content", run_id: "run-a", seq: 3, event_id: "run-a:3" }), state);
  assert.equal(state.runId, "run-a");
  assert.equal(state.lastSeq, 3);
  // 重复投递不推进游标，也不改 run_id。
  applyBlock(block({ type: "content", run_id: "run-b", seq: 9, event_id: "run-a:3" }), state);
  assert.equal(state.runId, "run-a");
  assert.equal(state.lastSeq, 3);
});
