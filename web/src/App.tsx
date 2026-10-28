import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  api,
  API_BASE,
  headers,
  type EvalResult,
  type ExecutionMode,
  type ExecutionModeInfo,
  type RunEvent as StoredRunEvent,
  type RunSummary,
} from "./api";
import {
  applyEvent,
  emptyLive,
  replay,
  summarize,
  type LiveState,
  type RunEvent,
  type StepStatus,
  type StepSummary,
} from "./state";
import { diagnose as diagnoseStream, type DiagnoseOutcome } from "./sse";
import {
  MAX_RUN_PAGES,
  appendRunEvents,
  boundTarget,
  candidateKey,
  evalManifestSummary,
  isStuckPage,
  nextRunCursor,
  shouldOfferModeReload,
  targetCandidates,
  targetRequestBody,
  type TargetCandidate,
} from "./view";
import { renderMarkdown } from "./markdown";
import {
  browserStorage,
  persistSession,
  resolveSessionId,
  sanitizeSessionInput,
  type StorageLike,
} from "./chat";
import ChatPanel from "./ChatPanel";
import KnowledgePanel from "./KnowledgePanel";
import "./styles.css";

const modeChoices: ExecutionModeInfo[] = [
  { id: "local", label: "本地端侧小模型", description: "使用本地模型执行诊断，模型不可用时直接提示，不会自动切换至在线 API。", configured: false, model: "" },
  { id: "cloud_multi", label: "在线 API 多智能体", description: "由在线模型协作分析症状、收集证据并审查结论，需要连接配置的在线 API。", configured: false, model: "" },
];

/** 用户点选的诊断目标：服务名用于展示/回退，服务端候选 ID 用于精确绑定。 */
interface ChosenTarget {
  service: string;
  targetId: string;
}

function modeLabel(mode: ExecutionMode | null | undefined) {
  return mode === "local" ? "本地端侧小模型" : mode === "cloud_multi" ? "在线 API 多智能体" : "历史运行（模式未记录）";
}

function lifecycleLabel(lifecycle: LiveState["lifecycle"]) {
  switch (lifecycle) {
    case "running":
      return "进行中";
    case "completed":
      return "已完成";
    case "failed":
      return "失败";
    case "cancelled":
      return "已取消";
    case "target_required":
      return "待选择目标";
    default:
      return "待命";
  }
}

/**
 * 新会话 ID。加一段随机后缀：同一毫秒内点两次“新会话”也必须得到不同 ID，
 * 否则第二次会把第一次的历史又读回来。字符集与 `validSessionId` 一致。
 */
function newSessionId() {
  return `web-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 8)}`;
}

/**
 * 首次渲染使用的会话 ID：`?session=` 优先，其次 localStorage 里上次的会话，
 * 都没有才新建。刷新不再每次换一个空历史会话（G5 tab 恢复的反例）。
 */
function initialSession(): { id: string; source: "url" | "storage" | "new" } {
  const resolved = resolveSessionId({
    search: typeof window === "undefined" ? "" : window.location.search,
    storage: browserStorage() as StorageLike | null,
    generate: newSessionId,
  });
  return { id: resolved.sessionId, source: resolved.source };
}

function fmtTime(value: string | null | undefined) {
  if (!value) return "—";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "—";
  return date.toLocaleTimeString("zh-CN", { hour12: false });
}

function fmtDuration(ms: number | null | undefined) {
  if (ms == null) return "—";
  if (ms < 1000) return `${Math.round(ms)} ms`;
  return `${(ms / 1000).toFixed(1)} s`;
}

function shortId(id: string) {
  return id.length > 10 ? `${id.slice(0, 6)}…${id.slice(-3)}` : id;
}

/** 运行列表里的时间戳可能是 ISO 字符串。 */
function runTime(run: RunSummary) {
  return fmtTime(run.started_at);
}

/**
 * 运行模式的后端来源：`LiveState` 只保留 executionMode，`backend` 从事件里推导。
 * 这是纯展示适配，不影响事件到状态的唯一实现（在 state.ts）。
 */
function deriveBackend(live: LiveState): "local" | "cloud" | null {
  if (live.executionMode === "local") return "local";
  if (live.executionMode === "cloud_multi") return "cloud";
  for (let index = live.events.length - 1; index >= 0; index -= 1) {
    const event = live.events[index];
    const backend = event.backend;
    if (backend === "local") return "local";
    if (backend === "cloud") return "cloud";
  }
  return null;
}

/** 选中 run 写入 URL（`?run=<id>`），刷新时按 URL 恢复。 */
function setRunParam(runId: string | null) {
  if (typeof window === "undefined") return;
  const url = new URL(window.location.href);
  if (runId) url.searchParams.set("run", runId);
  else url.searchParams.delete("run");
  window.history.replaceState({}, "", `${url.pathname}${url.search}${url.hash}`);
}

function initialRunParam(): string | null {
  if (typeof window === "undefined") return null;
  return new URLSearchParams(window.location.search).get("run");
}

/**
 * 把当前会话写进 URL（`?session=`）。
 *
 * 不写回的话，“新会话”之后刷新会按旧的 `?session=` 又回到上一个会话，
 * 用户刚清掉的对话看起来“自己回来了”。这里只替换 query，不产生历史记录。
 */
function setSessionParam(sessionId: string) {
  if (typeof window === "undefined") return;
  const url = new URL(window.location.href);
  if (url.searchParams.get("session") === sessionId) return;
  url.searchParams.set("session", sessionId);
  window.history.replaceState({}, "", `${url.pathname}${url.search}${url.hash}`);
}

/**
 * 读完整一次 run 的事件（A09）。
 *
 * 服务端单页有条数上限；必须按返回的游标循环到 `truncated=false`，否则事件数
 * 超过一页的运行在界面上永远看不到终态。`onPage` 每读完一页回调一次，让大
 * trace 能渐进渲染而不是等到全部读完。
 *
 * 只读：这里不发任何诊断 POST，也不会自动重跑。`stuck` 表示服务端截断却给不出
 * 可继续的游标（契约不一致），此时提示“内容不完整”，绝不假装已读全。
 */
async function collectRunEvents(
  runId: string,
  onPage?: (events: unknown[]) => void,
): Promise<{ events: unknown[]; stuck: boolean }> {
  let collected: unknown[] = [];
  let cursor: { after_seq?: number | null; after_event?: number | null } | undefined;
  let stuck = false;
  for (let page = 0; page < MAX_RUN_PAGES; page += 1) {
    const data = await api.getRun(runId, cursor);
    collected = appendRunEvents(collected, data);
    if (page === 0) onPage?.(collected);
    const next = nextRunCursor(data, page + 1);
    if (!next) {
      stuck = isStuckPage(data, page + 1);
      break;
    }
    cursor = next;
    if (!data.truncated) break;
  }
  return { events: collected, stuck };
}

export default function App() {
  const [sessionId, setSessionId] = useState(() => initialSession().id);
  const [runs, setRuns] = useState<RunSummary[]>([]);
  const [evals, setEvals] = useState<EvalResult[]>([]);
  const [live, setLive] = useState<LiveState>(() => emptyLive(sessionId));
  const [selected, setSelected] = useState<string | null>(null);
  const [selectedEvents, setSelectedEvents] = useState<StoredRunEvent[]>([]);
  const [notice, setNotice] = useState("");
  const [runsError, setRunsError] = useState("");
  const [evalsError, setEvalsError] = useState("");
  const [mode, setMode] = useState<"diagnose" | "chat" | "knowledge">("diagnose");
  const [executionMode, setExecutionMode] = useState<ExecutionMode | null>(null);
  const [modeInfos, setModeInfos] = useState<ExecutionModeInfo[]>(modeChoices);
  const [modesState, setModesState] = useState<"loading" | "loaded" | "error">("loading");
  const [modeError, setModeError] = useState("");
  // ≤1180px：右侧栏被隐藏，用这个开关把同样的内容展开在主区下方。
  const [inspectorOpen, setInspectorOpen] = useState(false);
  const [symptom, setSymptom] = useState("");
  const [serviceName, setServiceName] = useState("");
  const abortRef = useRef<AbortController | null>(null);
  const latestRunId = useRef<string | null>(null);
  const requestVersion = useRef(0);
  const selectedRunRef = useRef<string | null>(null);
  const streamEnd = useRef<HTMLDivElement | null>(null);
  const running = live.lifecycle === "running";
  const selectedModeInfo = modeInfos.find((item) => item.id === executionMode);
  const canStart = !!executionMode && modesState === "loaded" && !!selectedModeInfo?.configured && !!symptom.trim() && !running;
  // A12：配置读完但存在未配置模式时，仍然要能重新读取（不需要先制造一次错误）。
  // 判断逻辑放在 view.ts 的纯函数里，有单测覆盖，避免被改回“仅错误时可见”。
  const canReloadModes = shouldOfferModeReload({
    modesState,
    hasError: !!modeError,
    configured: modeInfos.map((item) => item.configured),
  });

  const loadModes = useCallback(async () => {
    try {
      const data = await api.executionModes();
      setModeInfos(modeChoices.map((choice) => data.modes.find((item) => item.id === choice.id) ?? choice));
      setModesState("loaded");
      setModeError("");
    } catch (error) {
      setModesState("error");
      setModeError(`无法读取执行模式配置：${(error as Error).message}`);
    }
  }, []);

  /**
   * 重新读取模式配置：只重置读取状态并重发 GET，绝不顺带发起诊断。
   * 无论当前是“读取失败”还是“读到了但没配置”都走这一个入口。
   */
  const reloadModes = useCallback(() => {
    setModesState("loading");
    setModeError("");
    void loadModes();
  }, [loadModes]);

  // 运行历史与评测各自独立加载：一个失败不能阻断另一个。
  const loadRuns = useCallback(async () => {
    try {
      const data = await api.listRuns();
      setRuns(data.runs);
      setRunsError("");
    } catch (error) {
      setRunsError(`无法加载运行记录：${(error as Error).message}。确认后端已启动（默认 http://127.0.0.1:9900）。`);
    }
  }, []);

  const loadEvals = useCallback(async () => {
    try {
      const data = await api.listEvalResults();
      setEvals(data.results);
      setEvalsError("");
    } catch (error) {
      setEvalsError(`无法加载评测结果：${(error as Error).message}`);
    }
  }, []);

  const refresh = useCallback(async () => {
    await Promise.all([loadRuns(), loadEvals()]);
  }, [loadRuns, loadEvals]);

  useEffect(() => {
    // eslint-disable-next-line react/set-state-in-effect -- Initial API reads update state after awaiting network responses.
    void refresh();
    void loadModes();
    return () => { abortRef.current?.abort(); };
  }, [refresh, loadModes]);

  // 会话 ID 变更即写入 localStorage 与 URL：刷新/重开标签页后沿用同一个会话，
  // ChatPanel 才能从服务端恢复历史。写入失败（隐私模式）不影响对话。
  useEffect(() => {
    persistSession(browserStorage() as StorageLike | null, sessionId);
    setSessionParam(sessionId);
  }, [sessionId]);

  useEffect(() => {
    if (live.lifecycle !== "running") return;
    streamEnd.current?.scrollIntoView({ behavior: "smooth", block: "end" });
  }, [live.events.length, live.lifecycle]);

  /** SSE 只带过程事件；完成后用 trace 补齐 token、耗时和节点明细。 */
  const backfill = useCallback(async (runId: string | null, version: number) => {
    await refresh();
    if (!runId) return;
    try {
      // A09：同样按游标读完全部页，否则长运行补齐时看不到终态。
      const collected = await collectRunEvents(runId);
      if (version !== requestVersion.current) return;
      setLive((prev) => {
        if (prev.runId !== runId) return prev;
        // 回放与实时共用 state.ts 的同一份 reducer。
        const saved = replay(prev.sessionId, collected.events as StoredRunEvent[]);
        return {
          ...saved,
          runId,
          executionMode: saved.executionMode ?? prev.executionMode,
          report: saved.report || prev.report,
          error: saved.error || prev.error,
        };
      });
    } catch {
      // trace 还没落盘时保持 SSE 结果
    }
  }, [refresh]);

  /** 处理 sse.ts `diagnose()` 的四种结局，truncated 不得当成成功。 */
  const applyOutcome = useCallback((outcome: DiagnoseOutcome, version: number) => {
    if (version !== requestVersion.current) return;
    switch (outcome.status) {
      case "complete":
        setLive((prev) => (prev.lifecycle === "running" ? { ...prev, lifecycle: "completed" } : prev));
        void backfill(latestRunId.current, version);
        break;
      case "error":
        setLive((prev) => ({
          ...prev,
          lifecycle: "failed",
          error: prev.error || outcome.message,
        }));
        void refresh();
        break;
      case "cancelled":
        setLive((prev) => ({ ...prev, lifecycle: "cancelled" }));
        setNotice("本次诊断已取消。");
        break;
      case "truncated":
        // 连接中断：保留已收到的事件，但本次运行不能标记为成功。
        // 若已经收到 target_required，则维持“待选择目标”，那不是失败。
        setLive((prev) =>
          prev.lifecycle === "target_required"
            ? prev
            : {
                ...prev,
                lifecycle: "failed",
                error: "连接中断，已收到的事件保留。可在最近运行中查看已保存的记录。",
              },
        );
        setNotice("连接中断，已收到的事件保留。");
        void refresh();
        break;
    }
  }, [backfill, refresh]);

  /**
   * 发起一次诊断。`choice` 只在用户点击候选卡片时传入，
   * 这是显式的第二次请求，不是自动重试。
   *
   * A04：同一个服务名可能对应多个实例，所以选择必须带上服务端签发的
   * `targetId`（请求里的 `target_candidate_id`）；只发服务名会丢掉实例/地域。
   */
  const start = useCallback(async (choice?: ChosenTarget) => {
    if (!executionMode) return;
    const text = symptom.trim();
    if (!text && !choice) return;
    const version = ++requestVersion.current;
    const id = sessionId || newSessionId();
    latestRunId.current = null;
    selectedRunRef.current = null;
    setSessionId(id);
    setMode("diagnose");
    setSelected(null);
    setSelectedEvents([]);
    setNotice("");
    setRunParam(null);
    setLive({ ...emptyLive(id), executionMode, lifecycle: "running" });

    const controller = new AbortController();
    abortRef.current = controller;

    // 候选选择与手填服务名冲突时，以用户刚点的选择为准：后端在两种字段都
    // 存在时优先 service_name，若只发旧值会让选择失效。
    const chosenService = choice?.service.trim() || serviceName.trim();
    const body = targetRequestBody(
      {
        session_id: id,
        execution_mode: executionMode,
        ...(text ? { symptom: text } : {}),
        ...(chosenService ? { service_name: chosenService } : {}),
      },
      choice?.service,
      choice?.targetId,
    );

    // 事件 -> 状态只有 state.ts 一份实现。
    const outcome = await diagnoseStream(
      `${API_BASE}/api/aiops`,
      body,
      headers(),
      {
        onEvent: (event) => {
          if (version !== requestVersion.current) return;
          if (event.run_id) latestRunId.current = String(event.run_id);
          setLive((prev) => applyEvent(prev, event));
        },
        onDone: () => {
          // 终态已由 outcome 处理，避免重复写入生命周期。
        },
        onError: () => {
          // 错误文案由 outcome 统一处理。
        },
      },
      controller.signal,
      executionMode,
    );
    applyOutcome(outcome, version);
    abortRef.current = null;
  }, [applyOutcome, executionMode, serviceName, sessionId, symptom]);

  const openRun = useCallback(async (runId: string, fromUrl = false) => {
    selectedRunRef.current = runId;
    setSelected(runId);
    setSelectedEvents([]);
    if (!fromUrl) setRunParam(runId);
    try {
      // A09：服务端一页有条数上限，必须按游标翻到 truncated=false，否则事件
      // 较多的运行在 UI 上永远读不到终态。这里循环消费全部页。
      const collected = await collectRunEvents(runId, (progress) => {
        if (selectedRunRef.current !== runId) return;
        // 边翻边渲染：大 trace 不必等所有页读完才显示。
        setSelectedEvents(progress as StoredRunEvent[]);
      });
      if (selectedRunRef.current !== runId) return;
      setSelectedEvents(collected.events as StoredRunEvent[]);
      setNotice(
        collected.stuck
          ? "该运行的事件超出服务端上限且无法继续翻页，显示的内容不完整。"
          : "",
      );
    } catch (error) {
      if (selectedRunRef.current !== runId) return;
      // 只读恢复失败：提示，但不自动新建诊断任务。
      selectedRunRef.current = null;
      setSelected(null);
      setSelectedEvents([]);
      setRunParam(null);
      setNotice(`读取 run 失败：${(error as Error).message}。已保留当前会话，不会新建诊断任务。`);
    }
  }, []);

  // 刷新后按 URL 恢复；失败只提示，不发起新诊断。
  useEffect(() => {
    const runId = initialRunParam();
    if (!runId) return;
    // eslint-disable-next-line react/set-state-in-effect -- 恢复 URL 指定的 run 是外部同步，状态在请求返回后更新。
    void openRun(runId, true);
  }, [openRun]);

  const current = useMemo<LiveState>(() => {
    if (!selected) return live;
    const saved = replay(live.sessionId, selectedEvents);
    const summary = runs.find((run) => run.run_id === selected);
    return {
      ...saved,
      runId: selected,
      executionMode:
        saved.executionMode
        ?? summary?.execution_mode
        ?? (summary?.backend === "local" ? "local" : summary?.backend === "cloud" ? "cloud_multi" : null),
    };
  }, [selected, selectedEvents, live, runs]);

  const summary = useMemo(() => summarize(current), [current]);
  const backend = useMemo(() => deriveBackend(current), [current]);
  const candidates = useMemo(() => targetCandidates(current.candidates), [current.candidates]);

  /**
   * A12：屏幕阅读器播报的唯一来源——当前生命周期 + 最新提示。
   * 刻意不拼事件日志：日志每追加一条都会重播，等于刷屏。
   */
  const announcement = useMemo(() => {
    const parts = [mode === "knowledge" ? "知识库管理" : lifecycleLabel(current.lifecycle)];
    if (notice) parts.push(notice);
    return `${parts.join("。")}。`;
  }, [mode, current.lifecycle, notice]);

  const newSession = () => {
    requestVersion.current++;
    latestRunId.current = null;
    selectedRunRef.current = null;
    const id = newSessionId();
    setSessionId(id);
    setLive(emptyLive(id));
    setSelected(null);
    setSelectedEvents([]);
    setNotice("");
    setRunParam(null);
    setMode("diagnose");
  };

  const heading =
    mode === "knowledge"
      ? "知识库管理"
      : mode === "chat"
        ? "知识库问答 · 在线 API"
        : selected
          ? "回放运行"
          : "现场诊断";

  const subheading =
    mode === "knowledge"
      ? "文档 · 索引作业 · 版本"
      : mode === "chat"
        ? "该功能使用在线模型，不属于本地模式"
        : selected
          ? `run ${shortId(selected)}`
          : `session ${shortId(live.sessionId)}`;

  return (
    <div className="shell">
      {/*
        A12：唯一的 polite live region。只播报“当前生命周期 + 最新提示”这两条
        摘要，不朗读事件日志（那会每次追加都刷屏），也不重复其他可见文本。
      */}
      <div role="status" aria-live="polite" aria-atomic="true" className="sr-only">
        {announcement}
      </div>
      <aside className="rail">
        <div className="brand">
          <h1>On-Call Agent</h1>
          <p>plan · execute · verify · replan</p>
        </div>

        <div className="rail-actions">
          <button className="btn" onClick={() => void start()} disabled={!canStart}>
            {running ? "诊断中…" : "开始诊断"}
          </button>
          <button className="btn ghost" disabled={running} onClick={newSession}>
            新会话
          </button>
        </div>

        <div className="session-id">session {sessionId}</div>

        <div className="runs">
          <InspectorHeading text={`最近运行 · ${runs.length}`} />
          {runsError && <p className="note error">{runsError}</p>}
          {!runsError && runs.length === 0 && (
            <p className="empty">
              选择执行模式并描述症状后开始诊断。运行结束后，可在这里查看证据与报告。
            </p>
          )}
          {runs.map((run) => (
            <button
              key={run.run_id}
              className={`run-item ${run.status === "error" ? "error" : ""}`}
              aria-current={selected === run.run_id}
              onClick={() => void openRun(run.run_id)}
            >
              <span className="run-time">{runTime(run)}</span>
              <span className="run-time">{fmtDuration(run.duration_ms)}</span>
              <span className="run-meta">
                {run.steps} 步 · {modeLabel(run.execution_mode)} ·{" "}
                {run.severity || "—"} · {run.category || "—"} ·{" "}
                {run.input_tokens + run.output_tokens} tok
              </span>
            </button>
          ))}
        </div>
      </aside>

      <main className="stage">
        <header className="stage-head">
          <div>
            <h2>{heading}</h2>
            <span className="sub">{subheading}</span>
          </div>
          <div className="mode-switch" role="group" aria-label="切换视图">
            <button
              aria-pressed={mode === "diagnose"}
              onClick={() => setMode("diagnose")}
            >
              诊断
            </button>
            <button
              aria-pressed={mode === "chat"}
              disabled={running || !selectedModeInfo?.configured || executionMode !== "cloud_multi"}
              onClick={() => setMode("chat")}
            >
              在线知识库问答
            </button>
            <button
              aria-pressed={mode === "knowledge"}
              disabled={running}
              onClick={() => setMode("knowledge")}
            >
              知识库
            </button>
          </div>
          <span className="sub">
            {mode === "knowledge" ? "管理视图" : lifecycleLabel(current.lifecycle)}
          </span>
          {/* A12：≤1180px 时右侧 inspector 被媒体查询隐藏，这个按钮是可达的替代入口。 */}
          <button
            type="button"
            className="btn ghost inspector-toggle"
            aria-expanded={inspectorOpen}
            aria-controls="run-inspector-inline"
            onClick={() => setInspectorOpen((open) => !open)}
          >
            {inspectorOpen ? "收起本次运行与评测信息" : "本次运行与评测信息"}
          </button>
        </header>

        {mode === "knowledge" ? (
          <KnowledgePanel />
        ) : mode === "chat" ? (
          <ChatPanel key={sessionId} sessionId={sessionId} />
        ) : (
          <>
            <div className="stream">
              {notice && <p className="note">{notice}</p>}
              {current.error && <p className="note error">{current.error}</p>}
              <section className="mode-chooser" aria-labelledby="execution-mode-title">
                <div className="section-intro">
                  <span className="eyebrow">选择诊断方式</span>
                  <h3 id="execution-mode-title">先选择模型，再开始排查</h3>
                  <p>每次运行固定使用所选模式。模式选择在诊断期间锁定。</p>
                </div>
                <div className="execution-mode-cards" role="group" aria-label="执行模式">
                  {modeInfos.map((item, index) => (
                    <button
                      key={item.id}
                      className={`execution-mode-card ${executionMode === item.id ? "selected" : ""}`}
                      type="button"
                      disabled={running}
                      aria-pressed={executionMode === item.id}
                      onClick={() => { setExecutionMode(item.id); setMode("diagnose"); }}
                    >
                      <div className="mode-card-top"><span className="mode-number">0{index + 1}</span><span className={`config-state ${modesState === "loaded" && item.configured ? "configured" : ""}`}>{modesState === "loading" ? "读取配置中" : modesState === "error" ? "配置未知" : item.configured ? "已配置" : "未配置"}</span></div>
                      <strong>{item.label}</strong>
                      <span className="mode-description">{item.description}</span>
                      <span className="model-name">{item.model || "模型待配置"}</span>
                      <span className="mode-selection">{executionMode === item.id ? "已选择 ✓" : "选择此模式 →"}</span>
                    </button>
                  ))}
                </div>
                <p className="note configuration-note">“已配置”仅表示已填写模型配置，不代表服务已通过连接、可用性或健康检查（服务端 readiness 也主要按配置形态推断）。</p>
                {modeError && (
                  <div className="mode-load-error">
                    <p className="note error">{modeError}</p>
                    <button className="btn ghost" disabled={running} onClick={reloadModes}>重新读取配置</button>
                  </div>
                )}
                {!modeError && canReloadModes && (
                  <div className="mode-load-error">
                    <p className="note">有模式尚未配置。完成服务端配置后可直接重新读取，不会自动开始诊断。</p>
                    <button className="btn ghost" disabled={running} onClick={reloadModes}>重新读取配置</button>
                  </div>
                )}
                {executionMode && modesState === "loaded" && !selectedModeInfo?.configured && <p className="note error">所选模式尚未配置，请完成服务端模型配置后重新读取。</p>}
              </section>
              {selected && <div className="replay-banner"><span>正在回放 {modeLabel(current.executionMode)} · run {shortId(selected)}</span><button className="btn ghost" onClick={() => { selectedRunRef.current = null; setSelected(null); setSelectedEvents([]); setRunParam(null); }}>返回当前会话</button></div>}
              {current.events.length > 0 && <p className="active-mode">本次运行：<strong>{modeLabel(current.executionMode)}</strong>{current.runId && <span> · {shortId(current.runId)}</span>}</p>}
              {!selected && current.plan.length === 0 && current.events.length === 0 && (
                <p className="note">
                  {executionMode ? "在下方描述故障症状，必要时指定服务，然后开始诊断。" : "本地模式适合本机模型诊断；在线模式提供多智能体协作和知识库问答。"}
                </p>
              )}

              {current.lifecycle === "target_required" && candidates.length > 0 && (
                <TargetChoice
                  candidates={candidates}
                  reason={current.error}
                  disabled={running || !executionMode}
                  onChoose={(candidate) => {
                    // 把选择同步到输入框，用户能看到本次目标；请求带服务端候选 ID。
                    setServiceName(candidate.service);
                    void start({ service: candidate.service, targetId: candidate.targetId });
                  }}
                />
              )}

              {current.plan.length > 0 && (
                <>
                  <InspectorHeading text="执行计划" />
                  <p className="plan-summary">
                    计划完成 {summary.completed}/{summary.total} 步
                    {summary.failed > 0 ? ` · 失败 ${summary.failed} 步` : ""}
                    {summary.running > 0 ? ` · 进行中 ${summary.running} 步` : ""}
                    {` · 工具调用 ${summary.toolCalls} 次`}
                    {` · 发现类调用 ${summary.discovery} 次（不计入计划）`}
                  </p>
                  <PlanSpine live={current} />
                </>
              )}

              {current.events.some(
                (e) =>
                  e.event === "tool_step" ||
                  e.event === "decision" ||
                  e.event === "planner_route" || e.event === "agent_status" || e.event === "status" || e.event === "step_complete",
              ) && (
                <>
                  <InspectorHeading text="执行记录" />
                  <EventLog events={current.events} />
                </>
              )}

              {current.report && (
                <>
                  <InspectorHeading text="诊断报告" />
                  <article
                    className="report"
                    dangerouslySetInnerHTML={{ __html: renderMarkdown(current.report) }}
                  />
                </>
              )}
              <div ref={streamEnd} />
            </div>

            <div className="compose">
              <div className="task-input-head"><strong>{executionMode ? modeLabel(executionMode) : "请先选择执行模式"}</strong><span>session {shortId(sessionId)}</span></div>
              <label htmlFor="symptom">故障症状</label>
              <textarea
                id="symptom"
                rows={3}
                value={symptom}
                disabled={!executionMode || running}
                placeholder="例如：订单服务近 10 分钟响应变慢，CPU 使用率持续超过 90%。"
                onChange={(event) => setSymptom(event.target.value)}
                maxLength={12000}
              />
              <div className="task-actions">
                <div className="service-input"><label htmlFor="service">服务名称（可选）</label>
                  <input id="service" value={serviceName} disabled={!executionMode || running} placeholder="例如 order-service" maxLength={200} onChange={(event) => setServiceName(event.target.value)} />
                </div>
                <button className="btn" onClick={() => void start()} disabled={!canStart}>{running ? "诊断中…" : "开始诊断"}</button>
                {running && <button className="btn ghost" onClick={() => abortRef.current?.abort()}>取消诊断</button>}
              </div>
              <details className="session-options"><summary>会话设置</summary><label htmlFor="session">会话 ID</label>
                <textarea
                  id="session"
                  rows={1}
                  value={sessionId}
                  disabled={running}
                  onChange={(event) => setSessionId(sanitizeSessionInput(event.target.value))}
                  maxLength={128}
                />
              </details>
            </div>
          </>
        )}

        {/*
          A12：≤1180px 时 aside.inspector 被隐藏，这里给出同样的内容作为可展开区块，
          不依赖 hover，键盘可聚焦；>1180px 由 CSS 整体隐藏，右侧栏行为不变。
        */}
        <section
          id="run-inspector-inline"
          className="inspector-inline"
          hidden={!inspectorOpen}
          aria-label="本次运行与评测信息"
        >
          <RunInspector
            current={current}
            summary={summary}
            backend={backend}
            evals={evals}
            evalsError={evalsError}
            variant="inline"
          />
        </section>
      </main>

      <aside className="inspector" id="run-inspector">
        <RunInspector
          current={current}
          summary={summary}
          backend={backend}
          evals={evals}
          evalsError={evalsError}
          variant="aside"
        />
      </aside>
    </div>
  );
}

/**
 * 右侧栏内容。宽屏是固定的 `aside.inspector`；≤1180px 时同一个组件也渲染在
 * 主区下方的可展开区块里（A12），所以内容只有这一份实现。
 */
function RunInspector({
  current,
  summary,
  backend,
  evals,
  evalsError,
  variant,
}: {
  current: LiveState;
  summary: StepSummary;
  backend: "local" | "cloud" | null;
  evals: EvalResult[];
  evalsError: string;
  /** 同一个组件渲染两次（宽屏 aside / 窄屏内联），`data-testid` 必须唯一。 */
  variant: "aside" | "inline";
}) {
  // 目标行只读服务端绑定结果；未绑定时明确写“未绑定”，不显示空行。
  const bound = boundTarget(current.target);
  return (
    <>
      <section>
        <h3>本次运行</h3>
        <dl className="kv">
          <dt>执行模式</dt><dd>{modeLabel(current.executionMode)}</dd>
          <dt>run ID</dt><dd>{current.runId ?? "—"}</dd>
          <dt>模型路径</dt>
          <dd>
            {backend ? (
              <span className={`badge ${backend}`}>
                {backend === "local" ? "本地模型" : "在线 API"}
              </span>
            ) : (
              "—"
            )}
          </dd>
          <dt>状态</dt><dd>{lifecycleLabel(current.lifecycle)}</dd>
          <dt>目标</dt>
          {/*
            同一个组件在宽屏 aside 与窄屏内联区块各渲染一次（A12），若两处都用
            `bound-target` 会出现重复 id。aside 保留契约要求的 testid，内联副本
            加后缀，端到端测试按可见的那一个断言即可。
          */}
          <dd data-testid={variant === "aside" ? "bound-target" : "bound-target-inline"}>
            {bound ? (
              <span className="bound-target">
                <strong>{bound.service}</strong>
                <span>实例 {bound.instanceId || "未声明"}</span>
                <span>地域 {bound.region || "未声明"}</span>
              </span>
            ) : (
              "未绑定"
            )}
          </dd>
          <dt>证据质量</dt><dd>{current.quality ?? "—"}</dd>
          <dt>步骤</dt>
          <dd>{summary.completed}/{summary.total}</dd>
          <dt>工具调用</dt><dd>{summary.toolCalls}</dd>
          <dt>发现调用</dt><dd>{summary.discovery}</dd>
          <dt>事件</dt>
          <dd>{current.events.length}</dd>
          <dt>tokens</dt>
          <dd>{sumTokens(current.events)}</dd>
        </dl>
      </section>

      <section>
        <h3>评测结果</h3>
        {evalsError && <p className="note error">{evalsError}</p>}
        {!evalsError && evals.length === 0 ? (
          <p className="empty">还没有评测输出。运行 `python scripts/run_eval.py --live`。</p>
        ) : (
          evals.slice(0, 4).map((result) => {
            const manifest = evalManifestSummary(result.manifest);
            return (
              <div key={result.name} style={{ marginBottom: 14 }}>
                <div className="sub" style={{ fontFamily: "var(--mono)", fontSize: 11, color: "var(--muted)" }}>
                  {result.name} · {result.rows?.length ?? 0} cases
                </div>
                {/* 运行条件：没有 manifest 的旧文件如实说明，不补默认值。 */}
                <p className={`eval-manifest${manifest.present ? "" : " legacy"}`}>
                  {manifest.text}
                  {manifest.present && manifest.missing.length > 0 && (
                    <span className="eval-manifest-missing">（缺少：{manifest.missing.join("、")}）</span>
                  )}
                </p>
                <table className="eval-table">
                  <thead>
                    <tr>
                      <th>指标</th>
                      <th>值</th>
                    </tr>
                  </thead>
                  <tbody>
                    <tr>
                      <td>tool F1</td>
                      <td className="num">{result.mean_tool_f1.toFixed(3)}</td>
                    </tr>
                    <tr>
                      <td>任务成功率</td>
                      <td className="num">{(result.success_rate * 100).toFixed(0)}%</td>
                    </tr>
                    <tr>
                      <td>总 tokens</td>
                      <td className="num">{result.total_tokens.toLocaleString()}</td>
                    </tr>
                  </tbody>
                </table>
              </div>
            );
          })
        )}
      </section>

      <section>
        <h3>这套系统在做什么</h3>
        <p className="note" style={{ fontSize: 12.5 }}>
          本地模式使用端侧模型完成排查，模型或工具失败会直接显示错误。在线模式通过多个智能体协作收集、分析和审查证据。运行记录保留每一步的工具结果，供后续复核。
        </p>
      </section>
    </>
  );
}

function InspectorHeading({ text }: { text: string }) {
  return <h3 style={{ margin: "14px 0 8px", fontSize: 11.5, fontWeight: 500, color: "var(--muted)" }}>{text}</h3>;
}

/** `target_required` 候选卡片：点击是一次显式选择，不会自动重试。 */
function TargetChoice({
  candidates,
  reason,
  disabled,
  onChoose,
}: {
  candidates: TargetCandidate[];
  reason: string;
  disabled: boolean;
  onChoose: (candidate: TargetCandidate) => void;
}) {
  return (
    <section className="target-choice" aria-labelledby="target-choice-title">
      <span className="eyebrow">需要选择诊断目标</span>
      <h3 id="target-choice-title">本次请求没有指定唯一的服务</h3>
      <p className="note">
        {reason || "多个候选服务匹配本次描述。"}为避免把其他服务的证据归到目标上，本次没有调用任何指标或日志查询工具。选择后重新发起一次诊断。
      </p>
      <div className="target-cards" role="group" aria-label="候选服务">
        {candidates.map((candidate, index) => (
          <button
            key={candidateKey(candidate)}
            type="button"
            className="target-card"
            disabled={disabled}
            /*
              G5：除了 aria-label，卡片还要能被断言到“具体是哪一个实例”。
              data-* 直接来自服务端候选字段（declared values），不参与样式，
              端到端测试据此确认点击的是 i-a 而不是同名服务的 i-b。
            */
            data-testid="target-card"
            data-target-id={candidate.targetId}
            data-instance-id={candidate.instanceId}
            data-region={candidate.region}
            data-index={index}
            aria-label={`选择目标 ${candidate.service} · 实例 ${candidate.instanceId || "未声明"} · 地域 ${candidate.region || "未声明"}`}
            onClick={() => onChoose(candidate)}
          >
            <strong>{candidate.service}</strong>
            <span className="target-instance">实例：{candidate.instanceId || "未声明"}</span>
            <span>地域：{candidate.region || "未声明"}</span>
            {candidate.topics.length > 0 && <span>日志主题：{candidate.topics.join("、")}</span>}
            <span className="target-choose">选择并重新诊断 →</span>
          </button>
        ))}
      </div>
    </section>
  );
}

function sumTokens(events: RunEvent[]) {
  const total = events.reduce(
    (sum, event) => sum + numeric(event.input_tokens) + numeric(event.output_tokens),
    0,
  );
  return total.toLocaleString();
}

function numeric(value: unknown): number {
  return typeof value === "number" && Number.isFinite(value) ? value : 0;
}

function asText(value: unknown): string {
  return typeof value === "string" ? value : typeof value === "number" ? String(value) : "";
}

function statusClass(status: StepStatus | undefined): string {
  if (status === "succeeded") return " done";
  if (status === "failed") return " failed";
  if (status === "running") return " active";
  return "";
}

function PlanSpine({ live }: { live: LiveState }) {
  return (
    <ol className="plan">
      {live.plan.map((step, index) => {
        const status = live.stepStatus[step.step_id];
        return (
          <li key={`${step.step_id}-${index}`} className={`plan-step${statusClass(status)}`}>
            <span className="step-text">{step.description}</span>
            {step.tool && <span className="tag">{step.tool}</span>}
            {status === "failed" && <span className="tag">失败</span>}
          </li>
        );
      })}
    </ol>
  );
}

function EventLog({ events }: { events: RunEvent[] }) {
  const rows = events.filter((event) =>
    ["planner_route", "tool_step", "step_complete", "agent_status", "status", "decision", "replan", "run_complete", "run_error", "complete", "error"].includes(
      event.event,
    ),
  );
  return (
    <div className="log">
      {rows.map((event, index) => {
        const tools = Array.isArray(event.tools)
          ? (event.tools as { name?: unknown }[]).map((tool) => asText(tool?.name)).filter(Boolean).join(", ")
          : "";
        const step = asText(event.step);
        let value = "";
        let kind = "log-row";
        if (event.event === "planner_route") {
          value = event.backend === "local" ? "使用本地模型" : `使用在线 API${event.reason ? ` · ${asText(event.reason)}` : ""}`;
          kind += " route";
        } else if (event.event === "tool_step" || event.event === "step_complete") {
          value = `${step ? `${step} · ` : ""}${tools || "工具执行完成"}${event.result_chars ? ` · ${asText(event.result_chars)} chars` : ""}`;
        } else if (event.event === "agent_status") {
          value = `${asText(event.agent) || "Agent"} · ${asText(event.status)}${event.message ? ` · ${asText(event.message)}` : ""}`;
          if (event.status === "error" || event.status === "failed") kind += " alert";
        } else if (event.event === "status") {
          value = asText(event.message) || "准备诊断";
        } else if (event.event === "decision") {
          value = `decision: ${asText(event.action) || "—"}`;
        } else if (event.event === "replan") {
          value = `replan: ${asText(event.reason) || "重写剩余计划"}`;
          kind += " alert";
        } else if (event.event === "run_error" || event.event === "error") {
          value = `失败：${asText(event.message)}`;
          kind += " alert";
        } else if (event.event === "run_complete" || event.event === "complete") {
          value = `完成 · ${fmtDuration(typeof event.duration_ms === "number" ? event.duration_ms : null)}`;
        }
        return (
          <div key={index} className={kind}>
            <span className="t">{fmtTime(typeof event.ts === "string" ? event.ts : null)}</span>
            <span className="k">{event.event}</span>
            <div className="v">{value}
              {(event.event === "tool_step" || event.event === "step_complete") && typeof event.result_preview === "string" && event.result_preview && <details className="tool-evidence"><summary>查看工具证据</summary><pre>{event.result_preview}</pre></details>}
            </div>
          </div>
        );
      })}
    </div>
  );
}
