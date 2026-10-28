import { useCallback, useEffect, useRef, useState, type MouseEvent } from "react";
import { api, type ChatHistoryMessage } from "./api";
import {
  browserStorage,
  chatStream,
  mergeResumedAnswer,
  readPendingStream,
  resumeChatStream,
  writePendingStream,
  type ChatDone,
  type ChatProgress,
  type StorageLike,
} from "./chat";
import { renderMarkdown } from "./markdown";
import {
  chatOutcomeLabel,
  citationAnchorId,
  citationCards,
  linkCitationMarks,
  removedCitationsText,
  retrievalLabel,
  safeSourceUri,
  type CitationCard,
} from "./view";

interface Turn {
  id: number;
  role: "user" | "assistant";
  text: string;
  streaming?: boolean;
  toolCalls?: string[];
  citations?: CitationCard[];
  retrieval?: { status: string; reason: string };
  removed?: string;
  outcome?: string;
  truncated?: boolean;
}

interface Props {
  sessionId: string;
}

/** 服务端历史 -> 本地 Turn（引用与检索状态都来自服务端，不重新推断）。 */
function historyToTurns(history: ChatHistoryMessage[]): Turn[] {
  const turns: Turn[] = [];
  let id = 1;
  for (const message of history) {
    if (message.role === "user") {
      turns.push({ id: id++, role: "user", text: message.content });
      continue;
    }
    turns.push({
      id: id++,
      role: "assistant",
      text: message.content,
      citations: citationCards(message.citations),
      retrieval: message.retrieval_status
        ? { status: message.retrieval_status, reason: "" }
        : undefined,
      outcome: message.outcome,
    });
  }
  return turns;
}

/** RAG 对话面板：与诊断共用 session，回答走 /api/chat_stream。 */
export default function ChatPanel({ sessionId }: Props) {
  const [turns, setTurns] = useState<Turn[]>([]);
  const [draft, setDraft] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [usingServerHistory, setUsingServerHistory] = useState(false);
  const nextId = useRef(1);
  const abortRef = useRef<AbortController | null>(null);
  const requestVersion = useRef(0);
  const listEnd = useRef<HTMLDivElement | null>(null);
  // localStorage 在隐私模式下可能抛异常；`browserStorage()` 已经吞掉并返回 null，
  // 之后所有持久化调用都按“可选能力”处理（失败不影响对话）。
  // 用 state 初始化（而不是读 ref.current）：渲染期间不访问 ref，只取一次实例。
  const [storage] = useState<StorageLike | null>(() => browserStorage());

  useEffect(() => () => { abortRef.current?.abort(); }, []);

  // 会话切换/刷新时从服务端回填历史；失败只提示，不覆盖本地已有对话。
  // 清空动作放进异步回调里：effect 内同步 setState 会触发级联渲染（lint 警告），
  // 而这里本来就要等网络结果，先清后填没有可观察差异。
  useEffect(() => {
    const version = ++requestVersion.current;
    let cancelled = false;
    void (async () => {
      setTurns([]);
      setError("");
      let restored: Turn[] = [];
      let history: ChatHistoryMessage[] = [];
      try {
        const info = await api.chatSession(sessionId);
        if (cancelled || version !== requestVersion.current) return;
        history = info.history ?? [];
        restored = historyToTurns(history);
        if (restored.length) {
          setTurns(restored);
          nextId.current = restored.length + 1;
          setUsingServerHistory(true);
        }
      } catch {
        // 历史读取失败不影响继续提问；不要把它显示成问答错误。
        if (!cancelled && version === requestVersion.current) setUsingServerHistory(false);
      }
      if (cancelled || version !== requestVersion.current) return;

      // G5 tab 恢复：上次关闭页面时有一次未完成的流，就按记录的 run_id 续读。
      // 只补“服务端确实有记录”的增量；拿不到终态就如实说明，不编造完整回答。
      const pending = readPendingStream(storage, sessionId);
      if (!pending) return;
      const resumed = await resumeChatStream(
        pending.runId,
        0,
        {
          // 恢复阶段不逐条渲染增量：合并逻辑一次性给出完整回答，避免半截闪烁。
          onDelta: () => {},
          onDone: () => {},
          onError: () => {},
        },
        undefined,
        undefined,
        sessionId,
      );
      if (cancelled || version !== requestVersion.current) return;
      // 续读成功（拿到终态或明确的部分内容）后清掉位置，避免下次打开把同一段
      // 内容再追加一遍；只有“读不到服务端记录”才保留，等下次网络恢复再试。
      if (resumed.status !== "failed") {
        writePendingStream(storage, null);
      }
      const merged = mergeResumedAnswer(history, resumed);
      if (merged.append && merged.message) {
        const turn = historyToTurns([merged.message])[0];
        setTurns((prev) => [
          ...prev,
          {
            ...turn,
            id: nextId.current++,
            truncated: !merged.complete,
          },
        ]);
      }
      // 服务端没有该次流的记录（已过期/从未落盘）时保持安静：历史已经显示了，
      // 启动就弹一条“未能续读”只会制造噪音。有内容的部分续读才提示。
      if (merged.notice && resumed.status !== "failed") setError(merged.notice);
    })();
    return () => { cancelled = true; };
  }, [sessionId, storage]);

  useEffect(() => {
    listEnd.current?.scrollIntoView({ behavior: "smooth", block: "end" });
  }, [turns.length, busy]);

  const send = useCallback(async () => {
    const question = draft.trim();
    if (!question || busy) return;

    const userTurn: Turn = { id: nextId.current++, role: "user", text: question };
    const assistantId = nextId.current++;
    setTurns((prev) => [
      ...prev,
      userTurn,
      { id: assistantId, role: "assistant", text: "", streaming: true, toolCalls: [] },
    ]);
    setDraft("");
    setBusy(true);
    setError("");

    const controller = new AbortController();
    abortRef.current = controller;
    const version = ++requestVersion.current;

    const patch = (update: (turn: Turn) => Turn) => {
      // 请求版本隔离：旧请求的迟到回调不能改新一轮的对话。
      if (version !== requestVersion.current) return;
      setTurns((prev) =>
        prev.map((turn) => (turn.id === assistantId ? update(turn) : turn)),
      );
    };

    await chatStream(
      sessionId,
      question,
      {
        onDelta: (text) => patch((turn) => ({ ...turn, text: turn.text + text })),
        onRetrieval: (retrieval) =>
          patch((turn) => ({
            ...turn,
            retrieval: { status: retrieval.status, reason: retrieval.reason },
          })),
        onEvent: (event) => {
          if (event.event !== "tool_call") return;
          const label = typeof event.message === "string" ? event.message : "";
          patch((turn) => ({
            ...turn,
            toolCalls: [...(turn.toolCalls ?? []), label],
          }));
        },
        /*
          G5：把 run_id/lastSeq 落到 localStorage。断流或用户直接关标签页时，
          下次打开据此续读；没有它就只能从头重放（依赖服务端仍保留记录）。
        */
        onProgress: (progress: ChatProgress) => {
          if (version !== requestVersion.current) return;
          writePendingStream(storage, progress.runId ? { sessionId, ...progress } : null);
        },
        onDone: (done: ChatDone) => {
          const cards = citationCards(done.citations);
          if (version !== requestVersion.current) return;
          writePendingStream(storage, null);
          patch((turn) => ({
            ...turn,
            // done 自带完整回答：流式增量缺失时用它补齐。
            text: turn.text || done.content,
            streaming: false,
            citations: cards,
            outcome: done.outcome,
            removed: removedCitationsText(done.removedCitations),
            retrieval: done.retrievalStatus
              ? { status: done.retrievalStatus, reason: turn.retrieval?.reason ?? "" }
              : turn.retrieval,
          }));
          setBusy(false);
        },
        onTruncated: (message) => {
          // 续读也没拿到终态：保留位置记录，用户下次打开还能再试一次。
          if (version !== requestVersion.current) return;
          patch((turn) => ({ ...turn, streaming: false, truncated: true }));
          setError(message);
          setBusy(false);
        },
        onError: (message) => {
          // 版本不符说明这已经不是"当前这一轮"：既不能清 busy（那是新请求的
          // 状态），也不能把旧错误显示在新回答上。
          if (version !== requestVersion.current) return;
          patch((turn) => ({ ...turn, streaming: false }));
          setError(message);
          setBusy(false);
        },
      },
      controller.signal,
    );
    // 只清自己的控制器：旧请求因续读而晚返回时，这里若无条件写 null，会把
    // 后发请求的 AbortController 清掉，导致"停止"对新请求失效。
    if (abortRef.current === controller) abortRef.current = null;
  }, [draft, busy, sessionId, storage]);

  const clearSession = useCallback(async () => {
    abortRef.current?.abort();
    requestVersion.current += 1;
    writePendingStream(storage, null);
    setTurns([]);
    setError("");
    try {
      await api.clearChatSession(sessionId);
    } catch (clearError) {
      setError(`清空会话失败：${(clearError as Error).message}`);
    }
  }, [sessionId, storage]);

  return (
    <div className="chat">
      <div className="chat-log">
        {turns.length === 0 && (
          <p className="note">
            基于运维知识库的问答。上传的 runbook 会被切分、向量化，回答时检索 top-k 片段；
            回答附带的引用都来自本轮检索，未授权的引用会被服务端移除。
            同一浏览器只支持一个活动聊天标签页；多标签页会共享续读位置，后发的流会覆盖它。
          </p>
        )}
        {usingServerHistory && turns.length > 0 && (
          <p className="note">已恢复服务端保存的会话历史（刷新后仍可查看引用与检索状态）。</p>
        )}
        {turns.map((turn) => (
          <article key={turn.id} className={`turn ${turn.role}`}>
            <span className="turn-role">{turn.role === "user" ? "你" : "Agent"}</span>
            {turn.role === "assistant" ? (
              <AssistantBody turn={turn} />
            ) : (
              <p className="turn-body">{turn.text}</p>
            )}
            {turn.streaming && <span className="caret" aria-label="生成中" />}
            {!!turn.toolCalls?.length && (
              <ul className="tool-trail">
                {turn.toolCalls.map((call, index) => (
                  <li key={index}>{call}</li>
                ))}
              </ul>
            )}
            {turn.retrieval && <RetrievalNote status={turn.retrieval.status} reason={turn.retrieval.reason} />}
            {turn.outcome && chatOutcomeLabel(turn.outcome) && (
              <p className="note error">{chatOutcomeLabel(turn.outcome)}</p>
            )}
            {turn.truncated && <p className="note error">连接中断，已收到的内容保留，回答可能不完整。</p>}
            {turn.removed && <p className="note">{turn.removed}</p>}
            {!!turn.citations?.length && <CitationList citations={turn.citations} />}
          </article>
        ))}
        {error && <p className="note error">{error}</p>}
        <div ref={listEnd} />
      </div>

      <div className="compose">
        <label htmlFor="question">问题</label>
        <div className="compose-row">
          <textarea
            id="question"
            rows={2}
            value={draft}
            placeholder="例如：磁盘满了应该先做哪几步？"
            onChange={(event) => setDraft(event.target.value)}
            onKeyDown={(event) => {
              // 中文/日文输入法组合期间按 Enter 是“确认候选词”，不是发送。
              // React 的 keydown 不保证带 isComposing，两种信号都要看。
              const composing =
                event.nativeEvent.isComposing || (event as { isComposing?: boolean }).isComposing;
              if (composing || event.keyCode === 229) return;
              if (event.key === "Enter" && !event.shiftKey) {
                event.preventDefault();
                void send();
              }
            }}
          />
          <button className="btn" onClick={() => void send()} disabled={busy || !draft.trim()}>
            {busy ? "回答中…" : "发送"}
          </button>
          {busy && (
            <button
              className="btn ghost"
              onClick={() => {
                abortRef.current?.abort();
                // 主动停止 = 用户不再要这次回答：清掉待续读位置，否则下次打开
                // 会把“主动取消”当成断流，自动把已落盘的内容续回来。
                writePendingStream(storage, null);
                setBusy(false);
              }}
            >
              停止
            </button>
          )}
          {!busy && turns.length > 0 && (
            <button className="btn ghost" onClick={() => void clearSession()}>
              清空历史
            </button>
          )}
        </div>
      </div>
    </div>
  );
}

function RetrievalNote({ status, reason }: { status: string; reason: string }) {
  const label = retrievalLabel(status);
  if (label.tone === "muted") return null;
  return (
    <p className={`retrieval-note ${label.tone}`}>
      {label.text}
      {reason ? ` · ${reason}` : label.detail ? ` · ${label.detail}` : ""}
    </p>
  );
}

/**
 * 回答正文：Markdown 渲染 + `[K#]` 锚点。
 *
 * 点击由容器级 onClick 处理（`href="#citation-K1"` 只是无 JS 时的退路）：
 * 这样不会把模型输出拼进内联事件，也不需要 `javascript:` 链接。只在同一 turn
 * 的引用集合内链接，避免把上一轮的 `[K9]` 指到本轮不存在的卡片。
 */
function AssistantBody({ turn }: { turn: Turn }) {
  const citations = turn.citations ?? [];
  const knownIds = citations.map((citation, index) => citation.id || `K${index + 1}`);
  const html = linkCitationMarks(renderMarkdown(turn.text), knownIds);
  const onClick = (event: MouseEvent<HTMLDivElement>) => {
    const target = event.target as HTMLElement | null;
    const anchor = target?.closest?.("a.citation-mark") as HTMLElement | null;
    if (!anchor) return;
    const id = anchor.dataset.citationId ?? "";
    if (!id) return;
    event.preventDefault();
    const card = document.getElementById(citationAnchorId(id));
    if (!card) return;
    card.scrollIntoView({ behavior: "smooth", block: "nearest" });
    card.classList.add("citation-flash");
    window.setTimeout(() => card.classList.remove("citation-flash"), 1200);
  };
  return (
    <div className="turn-body" onClick={onClick}>
      {/* html 来自 renderMarkdown（已转义原始 HTML）+ linkCitationMarks（只插入固定结构锚点）。 */}
      <div dangerouslySetInnerHTML={{ __html: html }} />
    </div>
  );
}

/**
 * 引用卡片：title / locator / source_uri / snippet / score / revision /
 * heading_path 全部来自服务端，缺失就整行不渲染（不用占位值冒充元数据）。
 *
 * `source_uri` 只在 http(s) 时渲染成链接：它来自上传文档元数据，属于用户可控
 * 内容，`javascript:` 之类协议作为 href 就是注入（见 `view.safeSourceUri`）。
 */
function CitationList({ citations }: { citations: CitationCard[] }) {
  return (
    <div className="citations">
      <span className="citations-head">引用 · {citations.length}</span>
      <ol>
        {citations.map((citation, index) => {
          const id = citation.id || `K${index + 1}`;
          const href = safeSourceUri(citation.sourceUri);
          return (
            <li key={`${citation.id || citation.documentId}-${index}`} id={citationAnchorId(id)}>
              <div className="citation-title">
                <span className="citation-id">{id}</span>
                <strong>{citation.title}</strong>
                {citation.locator && <span className="citation-locator">{citation.locator}</span>}
                <span className="citation-score">{citation.score.toFixed(3)}</span>
              </div>
              {citation.headingPath && (
                <p className="citation-heading">{citation.headingPath}</p>
              )}
              {citation.revision && (
                <p className="citation-revision">版本 {citation.revision}</p>
              )}
              {citation.snippet && <p className="citation-snippet">{citation.snippet}</p>}
              {citation.sourceUri &&
                (href ? (
                  <a
                    className="citation-source"
                    href={href}
                    target="_blank"
                    rel="noreferrer noopener"
                    title={citation.sourceUri}
                  >
                    {citation.sourceUri}
                  </a>
                ) : (
                  <span className="citation-source" title={citation.sourceUri}>
                    {citation.sourceUri}
                  </span>
                ))}
            </li>
          );
        })}
      </ol>
    </div>
  );
}
