import { useCallback, useEffect, useRef, useState } from "react";
import { api, type ChatHistoryMessage } from "./api";
import { chatStream, type ChatDone } from "./chat";
import { renderMarkdown } from "./markdown";
import {
  chatOutcomeLabel,
  citationCards,
  removedCitationsText,
  retrievalLabel,
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
      try {
        const info = await api.chatSession(sessionId);
        if (cancelled || version !== requestVersion.current) return;
        const restored = historyToTurns(info.history ?? []);
        if (restored.length) {
          setTurns(restored);
          nextId.current = restored.length + 1;
          setUsingServerHistory(true);
        }
      } catch {
        // 历史读取失败不影响继续提问；不要把它显示成问答错误。
        if (!cancelled && version === requestVersion.current) setUsingServerHistory(false);
      }
    })();
    return () => { cancelled = true; };
  }, [sessionId]);

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
        onDone: (done: ChatDone) => {
          const cards = citationCards(done.citations);
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
          patch((turn) => ({ ...turn, streaming: false, truncated: true }));
          setError(message);
          setBusy(false);
        },
        onError: (message) => {
          patch((turn) => ({ ...turn, streaming: false }));
          setError(message);
          setBusy(false);
        },
      },
      controller.signal,
    );
    abortRef.current = null;
  }, [draft, busy, sessionId]);

  const clearSession = useCallback(async () => {
    abortRef.current?.abort();
    requestVersion.current += 1;
    setTurns([]);
    setError("");
    try {
      await api.clearChatSession(sessionId);
    } catch (clearError) {
      setError(`清空会话失败：${(clearError as Error).message}`);
    }
  }, [sessionId]);

  return (
    <div className="chat">
      <div className="chat-log">
        {turns.length === 0 && (
          <p className="note">
            基于运维知识库的问答。上传的 runbook 会被切分、向量化，回答时检索 top-k 片段；
            回答附带的引用都来自本轮检索，未授权的引用会被服务端移除。
          </p>
        )}
        {usingServerHistory && turns.length > 0 && (
          <p className="note">已恢复服务端保存的会话历史（刷新后仍可查看引用与检索状态）。</p>
        )}
        {turns.map((turn) => (
          <article key={turn.id} className={`turn ${turn.role}`}>
            <span className="turn-role">{turn.role === "user" ? "你" : "Agent"}</span>
            {turn.role === "assistant" ? (
              <div
                className="turn-body"
                dangerouslySetInnerHTML={{ __html: renderMarkdown(turn.text) }}
              />
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

/** 引用卡片：title / locator / source_uri / snippet / score 全部来自服务端。 */
function CitationList({ citations }: { citations: CitationCard[] }) {
  return (
    <div className="citations">
      <span className="citations-head">引用 · {citations.length}</span>
      <ol>
        {citations.map((citation, index) => (
          <li key={`${citation.id || citation.documentId}-${index}`}>
            <div className="citation-title">
              <span className="citation-id">{citation.id || `K${index + 1}`}</span>
              <strong>{citation.title}</strong>
              {citation.locator && <span className="citation-locator">{citation.locator}</span>}
              <span className="citation-score">{citation.score.toFixed(3)}</span>
            </div>
            {citation.snippet && <p className="citation-snippet">{citation.snippet}</p>}
            {citation.sourceUri && (
              <span className="citation-source" title={citation.sourceUri}>
                {citation.sourceUri}
              </span>
            )}
          </li>
        ))}
      </ol>
    </div>
  );
}
