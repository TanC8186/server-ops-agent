/**
 * 前端状态归一化（实时流与回放共用）。
 *
 * 审查 FE-01 / FE-02 的两个反例：
 * - 计划行按描述判断完成、执行事件用工具名 => “计划 1 步、完成 2 步、计划行未勾选”；
 * - 实时用 `plan`、trace 用 `agent_plan`，结束后整体替换事件导致计划消失。
 *
 * 这里把两边的形状收敛到同一个 reducer：
 * - 步骤身份是 `step_id`（缺失时按稳定规则生成，绝不使用工具名当步骤名）；
 * - 工具调用是独立的 `tool_call_id`，一个步骤可以多次调用；
 * - 发现类调用（list_all_services 等）单独统计，不计入计划完成数；
 * - 事件的 `seq`/`event_id` 用于去重与合并，回放与实时同序。
 */

export type ExecutionMode = "local" | "cloud_multi";

export interface PlanStep {
  step_id: string;
  description: string;
  tool?: string;
  kind?: "tool" | "reason";
  agent?: string;
}

export type StepStatus = "pending" | "running" | "succeeded" | "failed" | "skipped";

export interface ToolCall {
  tool_call_id: string;
  step_id: string | null;
  tool: string;
  agent: string;
  status: string;
  evidence_id?: string;
  seq?: number;
}

export interface RunEvent {
  event: string;
  ts?: string;
  seq?: number;
  event_id?: string;
  run_id?: string;
  session_id?: string;
  execution_mode?: ExecutionMode;
  backend?: string;
  step_id?: string;
  tool_call_id?: string;
  payload?: Record<string, unknown>;
  [key: string]: unknown;
}

export interface LiveState {
  sessionId: string;
  runId: string | null;
  executionMode: ExecutionMode | null;
  lifecycle: "idle" | "running" | "completed" | "failed" | "cancelled" | "target_required";
  quality: "sufficient" | "partial" | "insufficient" | null;
  plan: PlanStep[];
  stepStatus: Record<string, StepStatus>;
  toolCalls: ToolCall[];
  discoveryCalls: ToolCall[];
  target: Record<string, unknown> | null;
  candidates: Record<string, unknown>[];
  report: string;
  error: string;
  events: RunEvent[];
  seenEventIds: Set<string>;
  lastSeq: number;
}

export const emptyLive = (sessionId = ""): LiveState => ({
  sessionId,
  runId: null,
  executionMode: null,
  lifecycle: "idle",
  quality: null,
  plan: [],
  stepStatus: {},
  toolCalls: [],
  discoveryCalls: [],
  target: null,
  candidates: [],
  report: "",
  error: "",
  events: [],
  seenEventIds: new Set(),
  lastSeq: 0,
});

const DISCOVERY_TOOLS = new Set([
  "list_all_services",
  "get_current_time",
  "get_current_timestamp",
  "get_region_code_by_name",
  "search_topic_by_service_name",
  "get_topic_info_by_name",
]);

const TERMINAL = new Set(["complete", "error"]);

function eventType(event: RunEvent): string {
  return String(event.event ?? event.type ?? "unknown");
}

/** 计划步骤：必须有稳定 ID；缺失时用「序号 + 摘要」生成，重复执行也不会变。 */
export function toPlanSteps(raw: unknown, agent?: string): PlanStep[] {
  if (!Array.isArray(raw)) return [];
  return raw.map((item, index) => {
    if (typeof item === "string") {
      return {
        step_id: `${agent ?? "step"}-${index + 1}`,
        description: item,
        agent,
        kind: "reason",
      };
    }
    const record = (item ?? {}) as Record<string, unknown>;
    const description = String(record.description ?? record.focus ?? record.tool ?? `步骤 ${index + 1}`);
    return {
      step_id: String(record.step_id ?? record.id ?? `${agent ?? "step"}-${index + 1}`),
      description,
      tool: record.tool ? String(record.tool) : undefined,
      kind: record.kind === "reason" ? "reason" : record.tool ? "tool" : undefined,
      agent: record.agent ? String(record.agent) : agent,
    };
  });
}

/** 从 agent_plan 的 assignments 生成计划（历史 trace 只有这一种形状）。 */
export function planFromAssignments(assignments: unknown): PlanStep[] {
  if (!Array.isArray(assignments)) return [];
  const steps: PlanStep[] = [];
  asRecords(assignments).forEach((assignment, index) => {
    const agent = String(assignment.agent ?? "agent");
    steps.push({
      step_id: `${agent}-assignment-${index + 1}`,
      description: String(assignment.focus ?? `${agent} 专科调查`),
      agent,
      kind: "reason",
    });
  });
  return steps;
}

function asRecords(items: unknown[]): Record<string, unknown>[] {
  return items.filter((item): item is Record<string, unknown> =>
    !!item && typeof item === "object" && !Array.isArray(item));
}

function stepIdForTool(event: RunEvent): string | null {
  if (event.step_id) return String(event.step_id);
  return null;
}

function callIdFor(event: RunEvent, index: number): string {
  if (event.tool_call_id) return String(event.tool_call_id);
  return `call-${event.seq ?? index}-${String(event.evidence_id ?? index)}`;
}

function statusOfCall(status: string): StepStatus {
  return status === "ok" || status === "empty" || status === "partial"
    ? "succeeded"
    : status === "running" || status === "queued"
      ? "running"
      : "failed";
}

/** 追加调用；同一个 tool_call_id（重放/重连重复投递）只保留一条。 */
function appendCall(calls: ToolCall[], call: ToolCall): ToolCall[] {
  if (calls.some((item) => item.tool_call_id === call.tool_call_id)) return calls;
  return [...calls, call];
}

/**
 * step_complete 合并到对应调用：优先按 tool_call_id，其次按 evidence_id。
 * 找不到对应调用时原样返回——step_complete 不是新调用。
 */
function mergeCallStatus(
  calls: ToolCall[],
  event: RunEvent,
  status: string,
): ToolCall[] {
  const callId = event.tool_call_id ? String(event.tool_call_id) : "";
  const evidenceId = event.evidence_id ? String(event.evidence_id) : "";
  if (!callId && !evidenceId) return calls;
  let changed = false;
  const next = calls.map((item) => {
    const matches =
      (callId && item.tool_call_id === callId) ||
      (!callId && evidenceId && item.evidence_id === evidenceId);
    if (!matches) return item;
    changed = true;
    return { ...item, status };
  });
  return changed ? next : calls;
}

/**
 * 纯 reducer：同一事件重复 apply 结果不变（幂等），乱序不会让状态回退。
 */
export function applyEvent(prev: LiveState, event: RunEvent, index = 0): LiveState {
  const type = eventType(event);
  if (event.event_id && prev.seenEventIds.has(event.event_id)) return prev;

  const next: LiveState = {
    ...prev,
    events: [...prev.events, event],
    stepStatus: { ...prev.stepStatus },
    seenEventIds: event.event_id ? new Set(prev.seenEventIds).add(event.event_id) : prev.seenEventIds,
    lastSeq: typeof event.seq === "number" && event.seq > prev.lastSeq ? event.seq : prev.lastSeq,
  };
  if (event.run_id) next.runId = String(event.run_id);
  if (event.session_id) next.sessionId = String(event.session_id);
  if (event.execution_mode) next.executionMode = event.execution_mode;

  if (type === "plan") {
    const steps = toPlanSteps(event.steps ?? event.plan_steps ?? event.plan);
    if (steps.length) {
      next.plan = steps;
      for (const step of steps) {
        if (!next.stepStatus[step.step_id]) next.stepStatus[step.step_id] = "pending";
      }
    }
  } else if (type === "agent_plan") {
    const steps = planFromAssignments(event.assignments);
    if (steps.length) {
      next.plan = steps;
      for (const step of steps) {
        if (!next.stepStatus[step.step_id]) next.stepStatus[step.step_id] = "pending";
      }
    }
  } else if (type === "target_required") {
    next.lifecycle = "target_required";
    next.candidates = asRecords((event.candidates as unknown[]) ?? []);
    next.error = String(event.reason ?? "");
  } else if (type === "target_bound") {
    next.target = (event.target as Record<string, unknown>) ?? {
      service: event.service_name,
    };
  } else if (type === "agent_status") {
    const agent = String(event.agent ?? "");
    const status = String(event.status ?? "");
    if (status === "error" || status === "failed") {
      next.error = String(event.message ?? next.error);
    }
    for (const step of next.plan) {
      if (step.agent === agent && next.stepStatus[step.step_id] === "pending") {
        next.stepStatus[step.step_id] = "running";
      }
      if (
        step.agent === agent &&
        (status === "completed" || status === "degraded") &&
        next.stepStatus[step.step_id] === "running"
      ) {
        next.stepStatus[step.step_id] = "succeeded";
      }
    }
  } else if (type === "tool_step") {
    // 只有 tool_step 代表一次真实工具调用。后端在同一次调用后还会发
    // step_complete（带同一个 tool_call_id/evidence_id），旧实现把两者都追加，
    // 于是 3 次真实调用在 UI 上显示成 6 次（BE 的 D2 反例）。
    const tool = String(event.tool ?? (event.tools as { name?: string }[] | undefined)?.[0]?.name ?? event.current_step ?? "");
    const call: ToolCall = {
      tool_call_id: callIdFor(event, index),
      step_id: stepIdForTool(event),
      tool,
      agent: String(event.agent ?? ""),
      status: String(event.status ?? (event.ok === false ? "error" : "ok")),
      evidence_id: event.evidence_id ? String(event.evidence_id) : undefined,
      seq: event.seq,
    };
    if (DISCOVERY_TOOLS.has(tool)) {
      next.discoveryCalls = appendCall(prev.discoveryCalls, call);
    } else {
      next.toolCalls = appendCall(prev.toolCalls, call);
      if (call.step_id && next.stepStatus[call.step_id]) {
        next.stepStatus[call.step_id] = statusOfCall(call.status);
      }
    }
  } else if (type === "step_complete") {
    // 步骤完成事件只更新步骤状态；如果它带的是某次调用的终态，就合并到那条
    // 调用上（不新增一条）。没有 step_id 时按 agent 归属升级计划步骤。
    const stepId = stepIdForTool(event);
    const status = String(event.status ?? (event.ok === false ? "error" : "ok"));
    if (stepId && next.stepStatus[stepId]) {
      next.stepStatus[stepId] = statusOfCall(status);
    }
    next.toolCalls = mergeCallStatus(prev.toolCalls, event, status);
    next.discoveryCalls = mergeCallStatus(prev.discoveryCalls, event, status);
  } else if (type === "report") {
    const text = String(event.report ?? event.result_preview ?? "");
    if (text) next.report = text;
  } else if (type === "complete") {
    const text = String(event.response ?? event.result_preview ?? "");
    if (text) next.report = text;
    const status = String(event.status ?? "");
    // trace 里 target_required 事件与 run_complete 都会落盘，而 run_complete
    // 只带 status=completed。已经处于“待选择目标”时不能被后来的完成记录抹掉，
    // 否则回放/补齐后选择卡片会消失。
    next.lifecycle =
      status === "target_required" || prev.lifecycle === "target_required"
        ? "target_required"
        : "completed";
    // 后端也允许只发一个 status=target_required 的 complete：候选必须保留，
    // 否则前端无法渲染选择卡片（target_required 事件与它等价）。
    if (status === "target_required") {
      const listed = asRecords((event.candidates as unknown[]) ?? []);
      if (listed.length) next.candidates = listed;
      next.error = String(event.reason ?? next.error);
    }
    const quality = event.quality as LiveState["quality"] | undefined;
    next.quality = quality ?? null;
  } else if (type === "error") {
    next.lifecycle = "failed";
    next.error = String(event.message ?? "诊断失败");
  }
  return next;
}

/** 回放：与实时完全同一份 reducer。 */
export function replay(sessionId: string, events: RunEvent[]): LiveState {
  let live = emptyLive(sessionId);
  events.forEach((event, index) => {
    live = applyEvent(live, normalizeTraceEvent(event), index);
  });
  return live;
}

/** trace 文件里的形状 -> 统一形状。 */
export function normalizeTraceEvent(event: RunEvent): RunEvent {
  const type = String(event.event ?? event.type ?? "unknown");
  const normalized: RunEvent = { ...event, event: type };
  if (type === "plan" && normalized.steps === undefined) {
    normalized.steps = (event as { plan_steps?: unknown }).plan_steps;
  }
  if (type === "report" && normalized.report === undefined) {
    normalized.report = (event as { result_preview?: string }).result_preview;
  }
  if (type === "run_complete") {
    return {
      ...normalized,
      event: "complete",
      status: "completed",
      response: (event as { response?: string }).response ?? liveReport(event),
    };
  }
  if (type === "run_error") {
    return { ...normalized, event: "error", message: (event as { message?: string }).message };
  }
  return normalized;
}

function liveReport(event: RunEvent): string {
  return String((event as { result_preview?: string }).result_preview ?? "");
}

export interface StepSummary {
  total: number;
  completed: number;
  failed: number;
  running: number;
  pending: number;
  toolCalls: number;
  discovery: number;
}

/**
 * 计划完成度：只看计划步骤的状态；工具调用次数单列。
 * 这样“同工具调用两次”“一次失败重试”“发现服务”都不会污染完成计数。
 */
export function summarize(live: LiveState): StepSummary {
  let completed = 0;
  let failed = 0;
  let running = 0;
  let pending = 0;
  for (const step of live.plan) {
    switch (live.stepStatus[step.step_id]) {
      case "succeeded":
        completed += 1;
        break;
      case "failed":
        failed += 1;
        break;
      case "running":
        running += 1;
        break;
      default:
        pending += 1;
    }
  }
  return {
    total: live.plan.length,
    completed,
    failed,
    running,
    pending,
    toolCalls: live.toolCalls.length,
    discovery: live.discoveryCalls.length,
  };
}

export { TERMINAL };
