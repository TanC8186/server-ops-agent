/**
 * 前端 reducer 回归测试（node:test，无需浏览器）。
 *
 * 覆盖 FE-01 / FE-02 的反例：
 * - 同工具不同参数执行两次 -> 完成计数准确，且计划行状态正确；
 * - 计划行不会因为工具名相同而折叠；
 * - 发现服务不计入计划完成数；
 * - 实时 `plan` 与历史 `agent_plan` 得到同一种计划；
 * - 重复事件 / 乱序事件不改变终态；
 * - 失败调用不会被当成成功。
 */

import assert from "node:assert/strict";
import { test } from "node:test";

import {
  applyEvent,
  emptyLive,
  isTargetBound,
  normalizeTraceEvent,
  planFromAssignments,
  replay,
  summarize,
  toPlanSteps,
} from "./state.ts";

const planEvent = {
  event: "plan",
  seq: 1,
  steps: [
    { step_id: "s1", kind: "tool", tool: "query_cpu_metrics", description: "查询 CPU" },
    { step_id: "s2", kind: "tool", tool: "query_memory_metrics", description: "查询内存" },
  ],
};

const toolEvent = (stepId: string, seq: number, status = "ok", tool = "query_cpu_metrics") => ({
  event: "tool_step",
  seq,
  step_id: stepId,
  tool_call_id: `call-${seq}`,
  tool,
  agent: "metrics",
  status,
});

test("计划步骤使用稳定 step_id，而不是描述或工具名", () => {
  const live = applyEvent(emptyLive(), planEvent);
  assert.deepEqual(live.plan.map((step) => step.step_id), ["s1", "s2"]);
});

test("同一工具不同参数执行两次仍然只完成一个步骤", () => {
  let live = applyEvent(emptyLive(), planEvent);
  live = applyEvent(live, toolEvent("s1", 2));
  live = applyEvent(live, toolEvent("s1", 3));

  const summary = summarize(live);
  assert.equal(summary.total, 2);
  assert.equal(summary.completed, 1);
  assert.equal(summary.toolCalls, 2);
  assert.equal(live.stepStatus.s1, "succeeded");
  assert.equal(live.stepStatus.s2, "pending");
});

test("失败调用把步骤标为 failed，不是 succeeded", () => {
  let live = applyEvent(emptyLive(), planEvent);
  live = applyEvent(live, toolEvent("s1", 2, "error"));

  assert.equal(live.stepStatus.s1, "failed");
  assert.equal(summarize(live).completed, 0);
  assert.equal(summarize(live).failed, 1);
});

test("发现服务不计入计划完成数", () => {
  let live = applyEvent(emptyLive(), planEvent);
  live = applyEvent(live, {
    event: "tool_step",
    seq: 2,
    tool: "list_all_services",
    agent: "discovery",
    status: "ok",
  });

  assert.equal(live.discoveryCalls.length, 1);
  assert.equal(live.toolCalls.length, 0);
  assert.equal(summarize(live).completed, 0);
  assert.equal(summarize(live).discovery, 1);
});

test("实时 plan 与历史 agent_plan 得到同一种计划", () => {
  const live = applyEvent(emptyLive(), planEvent);
  const historical = applyEvent(emptyLive(), {
    event: "agent_plan",
    seq: 1,
    assignments: [
      { agent: "metrics", focus: "查询 CPU" },
      { agent: "logs", focus: "查询内存" },
    ],
  });

  assert.equal(live.plan.length, 2);
  assert.equal(historical.plan.length, 2);
  assert.deepEqual(
    historical.plan.map((step) => step.agent),
    ["metrics", "logs"],
  );
  assert.deepEqual(planFromAssignments(undefined), []);
});

test("agent_status 驱动步骤 running -> succeeded", () => {
  let live = applyEvent(emptyLive(), {
    event: "agent_plan",
    seq: 1,
    assignments: [{ agent: "metrics", focus: "查 CPU" }],
  });
  const stepId = live.plan[0].step_id;
  live = applyEvent(live, { event: "agent_status", seq: 2, agent: "metrics", status: "running" });
  assert.equal(live.stepStatus[stepId], "running");

  live = applyEvent(live, { event: "agent_status", seq: 3, agent: "metrics", status: "completed" });
  assert.equal(live.stepStatus[stepId], "succeeded");
});

test("重复事件不会重复计数", () => {
  let live = applyEvent(emptyLive(), { ...planEvent, event_id: "r:1" });
  live = applyEvent(live, { ...toolEvent("s1", 2), event_id: "r:2" });
  const duplicate = applyEvent(live, { ...toolEvent("s1", 2), event_id: "r:2" });

  assert.equal(summarize(duplicate).toolCalls, 1);
  assert.equal(duplicate.events.length, live.events.length);
});

test("乱序事件不会让 lastSeq 回退", () => {
  let live = applyEvent(emptyLive(), { ...toolEvent("s1", 5), event_id: "r:5" });
  live = applyEvent(live, { ...toolEvent("s1", 2), event_id: "r:2" });
  assert.equal(live.lastSeq, 5);
});

test("回放与实时得到相同终态", () => {
  const events = [
    planEvent,
    toolEvent("s1", 2),
    {
      event: "report",
      seq: 3,
      report: "# 报告\n\n内容",
    },
    {
      event: "complete",
      seq: 4,
      response: "# 报告\n\n内容",
      status: "completed",
      lifecycle: "completed",
      quality: "partial",
    },
  ];
  const live = events.reduce((state, event, index) => applyEvent(state, event, index), emptyLive());
  const replayed = replay("s", events);

  assert.equal(replayed.lifecycle, "completed");
  assert.equal(replayed.quality, "partial");
  assert.equal(replayed.report, live.report);
  assert.deepEqual(
    replayed.plan.map((step) => step.step_id),
    live.plan.map((step) => step.step_id),
  );
  assert.equal(summarize(replayed).completed, 1);
});

test("历史 run_complete 归一化为 complete 并保留报告", () => {
  const normalized = normalizeTraceEvent({
    event: "run_complete",
    result_preview: "# 旧报告",
  });
  assert.equal(normalized.event, "complete");
  assert.equal(normalized.response, "# 旧报告");
});

test("target_required 是独立状态而不是失败", () => {
  const live = applyEvent(emptyLive(), {
    event: "target_required",
    seq: 1,
    candidates: [{ service: "svc-a" }, { service: "svc-b" }],
    reason: "多个候选",
  });
  assert.equal(live.lifecycle, "target_required");
  assert.equal(live.candidates.length, 2);
});

test("字符串计划步骤会得到稳定 ID", () => {
  const steps = toPlanSteps(["检查 CPU", "检查内存"]);
  assert.deepEqual(steps.map((step) => step.step_id), ["step-1", "step-2"]);
});

test("status=target_required 的 complete 也保留候选，不丢失选择卡片", () => {
  const live = applyEvent(emptyLive(), {
    event: "complete",
    status: "target_required",
    quality: "insufficient",
    reason: "未指定目标服务，且有多个候选",
    candidates: [{ service: "svc-a" }, { service: "svc-b" }],
    response: "# 需要选择诊断目标",
  });
  assert.equal(live.lifecycle, "target_required");
  assert.equal(live.candidates.length, 2);
  assert.equal(live.quality, "insufficient");
  assert.match(live.error, /未指定目标服务/);
});

test("回放补齐 trace 时 target_required 不会被 run_complete 抹掉", () => {
  const live = replay("s", [
    {
      event: "target_required",
      seq: 1,
      candidates: [{ service: "svc-a" }],
      reason: "多个候选",
    },
    // trace 的终态记录只有 status=completed
    { event: "run_complete", seq: 2, response: "# 需要选择诊断目标" },
  ]);
  assert.equal(live.lifecycle, "target_required");
  assert.equal(live.candidates.length, 1);
});


/**
 * D2 回归：同一次工具调用同时产生 tool_step 与 step_complete。
 * 旧实现把两者都当新调用，3 次真实调用显示成 6 次。
 */
test("step_complete 不新增工具调用，只更新状态", () => {
  let live = emptyLive("s");
  live = applyEvent(live, planEvent as never, 0);
  live = applyEvent(
    live,
    { event: "tool_step", seq: 2, step_id: "s1", tool_call_id: "run-c1",
      tool: "query_cpu_metrics", status: "ok" } as never,
    1,
  );
  live = applyEvent(
    live,
    { event: "step_complete", seq: 3, step_id: "s1", tool_call_id: "run-c1",
      current_step: "query_cpu_metrics", tool: "query_cpu_metrics",
      status: "ok" } as never,
    2,
  );

  assert.equal(live.toolCalls.length, 1, "一次调用只能计一次");
  assert.equal(live.toolCalls[0].status, "ok");
  assert.equal(summarize(live).completed, 1);
});

test("step_complete 能把调用从 ok 改成后续失败", () => {
  let live = emptyLive("s");
  live = applyEvent(live, planEvent as never, 0);
  live = applyEvent(
    live,
    { event: "tool_step", seq: 2, step_id: "s1", tool_call_id: "c9",
      tool: "query_cpu_metrics", status: "ok" } as never,
    1,
  );
  live = applyEvent(
    live,
    { event: "step_complete", seq: 3, step_id: "s1", tool_call_id: "c9",
      status: "error" } as never,
    2,
  );

  assert.equal(live.toolCalls.length, 1);
  assert.equal(live.toolCalls[0].status, "error");
  assert.equal(live.stepStatus["s1"], "failed");
});

test("发现类调用的 step_complete 也不重复计数", () => {
  let live = emptyLive("s");
  live = applyEvent(
    live,
    { event: "tool_step", seq: 1, tool_call_id: "d1",
      tool: "list_all_services", status: "ok" } as never,
    0,
  );
  live = applyEvent(
    live,
    { event: "step_complete", seq: 2, tool_call_id: "d1",
      current_step: "list_all_services", status: "ok" } as never,
    1,
  );

  assert.equal(live.discoveryCalls.length, 1);
});

test("重复投递的 tool_step（重连）按 tool_call_id 去重", () => {
  let live = emptyLive("s");
  const event = { event: "tool_step", seq: 5, tool_call_id: "dup-1",
                  tool: "query_cpu_metrics", status: "ok" };
  live = applyEvent(live, event as never, 0);
  live = applyEvent(live, event as never, 1);

  assert.equal(live.toolCalls.length, 1);
});

const boundTarget = {
  service: "orders",
  instance_id: "i-a",
  region: "r-a",
  topics: ["t-a"],
  ssh_host: "",
  ssh_in_scope: false,
};

test("G5 实时路径：status/target_bound 把 target 写入 LiveState", () => {
  const live = applyEvent(emptyLive("s"), {
    event: "status",
    stage: "target_bound",
    message: "本次诊断目标：orders",
    service_name: "orders",
    target: boundTarget,
    seq: 5,
  } as never, 0);

  assert.deepEqual(live.target, boundTarget, "后端绑定成功必须反映到 UI 状态");
  assert.equal(isTargetBound({ event: "status", stage: "target_bound" }), true);
});

test("G5 回放路径：trace 里同一扁平结构也能填出 target", () => {
  const live = replay("s", [
    { event: "status", stage: "target_bound", target: boundTarget, seq: 5, service_name: "orders" },
    { event: "run_complete", seq: 6, response: "# 报告" },
  ] as never);

  assert.deepEqual(live.target, boundTarget);
  assert.equal(live.lifecycle, "completed");
});

test("G5：旧服务端的 type=target_bound 与缺字段的绑定仍可识别", () => {
  const legacy = applyEvent(emptyLive("s"), {
    event: "target_bound",
    target: { service: "svc-a" },
  } as never, 0);
  assert.deepEqual(legacy.target, { service: "svc-a" });

  // 只有 service_name：保留服务名，实例/地域不编造。
  const partial = applyEvent(emptyLive("s"), {
    event: "status",
    stage: "target_bound",
    service_name: "svc-b",
  } as never, 0);
  assert.deepEqual(partial.target, { service: "svc-b" });

  // 无关的 status 事件不能清掉或伪造 target。
  const untouched = applyEvent(emptyLive("s"), {
    event: "status",
    stage: "run_start",
    message: "开始",
  } as never, 0);
  assert.equal(untouched.target, null);
  assert.equal(isTargetBound({ event: "status", stage: "run_start" }), false);
});
