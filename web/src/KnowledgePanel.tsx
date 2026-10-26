import { useCallback, useEffect, useRef, useState } from "react";
import {
  api,
  type KnowledgeDocument,
  type KnowledgeJob,
  type KnowledgeRevision,
  type KnowledgeStatus,
} from "./api";
import { jobStatusLabel, knowledgeStatusLabel } from "./view";

const JOB_POLL_MS = 1500;

function fmtDate(value: string | null | undefined) {
  if (!value) return "—";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return date.toLocaleString("zh-CN", { hour12: false });
}

function progressText(job: KnowledgeJob) {
  const total = job.progress?.chunks_total;
  const done = job.progress?.chunks_done;
  if (total === undefined && done === undefined) return "";
  return `${done ?? 0}/${total ?? "?"} 片段`;
}

/**
 * 知识库管理面板。
 *
 * 上传接口返回 202 + `job_id` 时，只表示“已排队”，新版本在作业 ready 之前
 * **不可检索**。因此面板会显示“已排队，索引中”并轮询作业状态，不把 202 当成功。
 */
export default function KnowledgePanel() {
  const [status, setStatus] = useState<KnowledgeStatus | null>(null);
  const [statusError, setStatusError] = useState("");
  const [documents, setDocuments] = useState<KnowledgeDocument[]>([]);
  const [docsError, setDocsError] = useState("");
  const [loadingDocs, setLoadingDocs] = useState(true);
  const [jobs, setJobs] = useState<KnowledgeJob[]>([]);
  const [jobsError, setJobsError] = useState("");
  const [notice, setNotice] = useState("");
  const [error, setError] = useState("");
  const [uploading, setUploading] = useState(false);
  const [pendingJob, setPendingJob] = useState<KnowledgeJob | null>(null);
  const [openDoc, setOpenDoc] = useState<string | null>(null);
  const [revisions, setRevisions] = useState<KnowledgeRevision[]>([]);
  const [revisionsError, setRevisionsError] = useState("");
  const fileInput = useRef<HTMLInputElement | null>(null);

  const loadStatus = useCallback(async () => {
    try {
      setStatus(await api.knowledgeStatus());
      setStatusError("");
    } catch (err) {
      setStatus(null);
      setStatusError(`无法读取知识库状态：${(err as Error).message}`);
    }
  }, []);

  const loadDocuments = useCallback(async () => {
    setLoadingDocs(true);
    try {
      const data = await api.listKnowledgeDocuments();
      setDocuments(data.documents ?? []);
      setDocsError("");
    } catch (err) {
      setDocsError(`无法读取文档列表：${(err as Error).message}`);
    } finally {
      setLoadingDocs(false);
    }
  }, []);

  const loadJobs = useCallback(async () => {
    try {
      const data = await api.listKnowledgeJobs();
      setJobs(data.jobs ?? []);
      setJobsError("");
    } catch (err) {
      setJobsError(`无法读取索引作业：${(err as Error).message}`);
    }
  }, []);

  useEffect(() => {
    // eslint-disable-next-line react/set-state-in-effect -- 首次读取知识库数据，状态在请求返回后更新。
    void loadStatus();
    void loadDocuments();
    void loadJobs();
  }, [loadStatus, loadDocuments, loadJobs]);

  // 轮询尚未结束的作业；ready 后刷新列表与状态。
  useEffect(() => {
    const active = pendingJob && ["queued", "running"].includes(pendingJob.status);
    if (!active) return;
    const timer = window.setTimeout(async () => {
      try {
        const job = await api.getKnowledgeJob(pendingJob.job_id);
        setPendingJob(job);
        if (!["queued", "running"].includes(job.status)) {
          void loadDocuments();
          void loadStatus();
          void loadJobs();
          if (job.status === "ready") setNotice(`索引完成：${job.document_id ?? job.job_id}`);
          if (job.status === "failed") {
            // 终态一到就清掉“已排队，索引中”，否则两条矛盾提示会同时留在页面。
            setNotice("");
            setError(`索引失败：${job.error?.message ?? job.error?.code ?? "未知原因"}`);
          }
        }
      } catch (err) {
        setError(`查询作业状态失败：${(err as Error).message}`);
        setPendingJob(null);
      }
    }, JOB_POLL_MS);
    return () => window.clearTimeout(timer);
  }, [pendingJob, loadDocuments, loadStatus, loadJobs]);

  const upload = async (file: File | undefined) => {
    if (!file) return;
    setUploading(true);
    setError("");
    setNotice("");
    try {
      const accepted = await api.uploadKnowledgeDocument(file);
      const job: KnowledgeJob = {
        job_id: accepted.job_id,
        document_id: accepted.document_id,
        status: accepted.status || "queued",
      };
      setPendingJob(job);
      setNotice("已排队，索引中。完成前新版本不可检索。");
      void loadJobs();
    } catch (err) {
      setError(`上传失败：${(err as Error).message}`);
    } finally {
      setUploading(false);
      if (fileInput.current) fileInput.current.value = "";
    }
  };

  const retryJob = async (jobId: string) => {
    setError("");
    try {
      const accepted = await api.retryKnowledgeJob(jobId);
      setPendingJob({
        job_id: accepted.job_id,
        document_id: accepted.document_id,
        status: accepted.status || "queued",
      });
      setNotice("已重新排队，索引中。旧作业保留为历史。");
      void loadJobs();
    } catch (err) {
      setError(`重试失败：${(err as Error).message}`);
    }
  };

  const openRevisions = async (documentId: string) => {
    if (openDoc === documentId) {
      setOpenDoc(null);
      return;
    }
    setOpenDoc(documentId);
    setRevisions([]);
    setRevisionsError("");
    try {
      const data = await api.listKnowledgeRevisions(documentId);
      setRevisions(data.revisions ?? []);
    } catch (err) {
      setRevisionsError(`无法读取版本：${(err as Error).message}`);
    }
  };

  /** 刷新当前展开文档的版本列表（手风琴保持展开）。 */
  const refreshRevisions = async (documentId: string) => {
    try {
      const data = await api.listKnowledgeRevisions(documentId);
      setRevisions(data.revisions ?? []);
      setRevisionsError("");
    } catch (err) {
      setRevisionsError(`无法读取版本：${(err as Error).message}`);
    }
  };

  const refreshAll = () => {
    void loadStatus();
    void loadDocuments();
    void loadJobs();
    // 已展开的版本列表也要重拉：否则作业结束后版本状态仍是旧的 staged。
    if (openDoc) void refreshRevisions(openDoc);
  };

  const activate = async (documentId: string, revision: string) => {
    setError("");
    try {
      await api.activateKnowledgeRevision(documentId, revision);
      setNotice(`已切换到版本 ${revision}`);
      // 直接刷新当前展开的版本列表；不能再调 openRevisions（那会把手风琴收起）。
      setOpenDoc(documentId);
      try {
        const data = await api.listKnowledgeRevisions(documentId);
        setRevisions(data.revisions ?? []);
        setRevisionsError("");
      } catch (err) {
        setRevisionsError(`无法读取版本：${(err as Error).message}`);
      }
      void loadDocuments();
      void loadStatus();
    } catch (err) {
      setError(`切换版本失败：${(err as Error).message}`);
    }
  };

  const remove = async (documentId: string) => {
    setError("");
    try {
      const result = await api.deleteKnowledgeDocument(documentId);
      setNotice(`已删除文档，移除 ${result.deleted_chunks} 个片段`);
      if (openDoc === documentId) setOpenDoc(null);
      void loadDocuments();
      void loadStatus();
    } catch (err) {
      setError(`删除失败：${(err as Error).message}`);
    }
  };

  const statusLabel = knowledgeStatusLabel(status?.status);

  return (
    <div className="stream knowledge">
      {error && <p className="note error">{error}</p>}
      {notice && <p className="note">{notice}</p>}

      <section className="knowledge-section" aria-labelledby="kb-status-title">
        <div className="section-intro">
          <span className="eyebrow">检索依赖</span>
          <h3 id="kb-status-title">知识库状态</h3>
        </div>
        {statusError ? (
          <p className="note error">{statusError}</p>
        ) : status ? (
          <>
            <p className={`status-line ${statusLabel.tone}`}>
              <strong>{statusLabel.text}</strong>
              {statusLabel.detail && <span> · {statusLabel.detail}</span>}
              {status.reason && <span> · {status.reason}</span>}
            </p>
            {status.status === "invalid_index" && (
              <p className="note error">
                索引与当前嵌入配置不一致（维度或描述符），问答会返回 invalid_index；需要重建索引后才能检索。
              </p>
            )}
            <dl className="kv">
              <dt>collection</dt><dd>{status.collection ?? "—"}</dd>
              <dt>片段数</dt><dd>{status.chunks ?? "—"}</dd>
              <dt>文档</dt>
              <dd>{status.documents ? `active ${status.documents.active ?? 0} · pending ${status.documents.pending ?? 0}` : "—"}</dd>
              <dt>嵌入</dt>
              <dd>{status.embedding ? `${status.embedding.provider ?? "?"}/${status.embedding.model ?? "?"} · ${status.embedding.dimension ?? "?"} 维` : "—"}</dd>
              <dt>Milvus</dt>
              <dd>{status.milvus ? `${status.milvus.connected ? "已连接" : "未连接"}${status.milvus.latency_ms != null ? ` · ${status.milvus.latency_ms} ms` : ""}` : "—"}</dd>
            </dl>
            <button className="btn ghost" onClick={refreshAll}>刷新状态</button>
          </>
        ) : (
          <p className="note">正在读取知识库状态…</p>
        )}
      </section>

      <section className="knowledge-section" aria-labelledby="kb-upload-title">
        <div className="section-intro">
          <span className="eyebrow">上传</span>
          <h3 id="kb-upload-title">新增或更新文档</h3>
          <p>支持 txt / md。上传成功后进入索引队列，完成前新版本不可检索。</p>
        </div>
        <div className="upload-row">
          <input
            ref={fileInput}
            type="file"
            accept=".txt,.md"
            aria-label="选择知识库文档"
            onChange={(event) => void upload(event.target.files?.[0])}
            disabled={uploading}
          />
          {uploading && <span className="note">上传中…</span>}
        </div>
        {pendingJob && (
          <div className="job-card">
            <div className="job-line">
              <span className="job-id">{pendingJob.job_id}</span>
              <span className={`badge job-${jobStatusLabel(pendingJob.status).tone}`}>
                {jobStatusLabel(pendingJob.status).text}
              </span>
              {progressText(pendingJob) && <span className="job-progress">{progressText(pendingJob)}</span>}
            </div>
            <p className="note">
              {pendingJob.status === "queued" || pendingJob.status === "running"
                ? "已排队，索引中。"
                : jobStatusLabel(pendingJob.status).detail}
              {pendingJob.error?.message ? ` · ${pendingJob.error.message}` : ""}
            </p>
          </div>
        )}
      </section>

      <section className="knowledge-section" aria-labelledby="kb-docs-title">
        <div className="section-intro">
          <span className="eyebrow">文档</span>
          <h3 id="kb-docs-title">知识库文档</h3>
        </div>
        {docsError && <p className="note error">{docsError}</p>}
        {!docsError && loadingDocs && <p className="note">加载中…</p>}
        {!docsError && !loadingDocs && documents.length === 0 && (
          <p className="empty">还没有文档。上传 runbook 后会出现在这里。</p>
        )}
        {documents.length > 0 && (
          <table className="kb-table">
            <thead>
              <tr>
                <th>标题</th>
                <th>状态</th>
                <th>chunks</th>
                <th>active revision</th>
                <th>更新时间</th>
                <th>操作</th>
              </tr>
            </thead>
            <tbody>
              {documents.map((doc) => (
                <DocRow
                  key={doc.document_id}
                  doc={doc}
                  open={openDoc === doc.document_id}
                  revisions={revisions}
                  revisionsError={revisionsError}
                  onToggle={() => void openRevisions(doc.document_id)}
                  onActivate={(revision) => void activate(doc.document_id, revision)}
                  onDelete={() => void remove(doc.document_id)}
                />
              ))}
            </tbody>
          </table>
        )}
      </section>

      <section className="knowledge-section" aria-labelledby="kb-jobs-title">
        <div className="section-intro">
          <span className="eyebrow">索引作业</span>
          <h3 id="kb-jobs-title">最近作业</h3>
        </div>
        {jobsError && <p className="note error">{jobsError}</p>}
        {!jobsError && jobs.length === 0 && <p className="empty">还没有索引作业。</p>}
        {jobs.length > 0 && (
          <div className="log">
            {jobs.map((job) => {
              const label = jobStatusLabel(job.status);
              return (
                <div key={job.job_id} className={`log-row${label.tone === "error" ? " alert" : ""}`}>
                  <span className="t">{fmtDate(job.started_at)}</span>
                  <span className="k">{job.status}</span>
                  <div className="v">
                    <span className="job-id">{job.job_id}</span>
                    {job.document_id ? ` · ${job.document_id}` : ""}
                    {progressText(job) ? ` · ${progressText(job)}` : ""}
                    {job.error?.message ? ` · ${job.error.message}` : ""}
                    {job.status === "failed" && (
                      <button className="btn ghost kb-inline" onClick={() => void retryJob(job.job_id)}>重试</button>
                    )}
                  </div>
                </div>
              );
            })}
          </div>
        )}
      </section>
    </div>
  );
}

function DocRow({
  doc,
  open,
  revisions,
  revisionsError,
  onToggle,
  onActivate,
  onDelete,
}: {
  doc: KnowledgeDocument;
  open: boolean;
  revisions: KnowledgeRevision[];
  revisionsError: string;
  onToggle: () => void;
  onActivate: (revision: string) => void;
  onDelete: () => void;
}) {
  const status = knowledgeStatusLabel(doc.status === "active" ? "ready" : "degraded");
  return (
    <>
      <tr>
        <td>
          <strong>{doc.title || doc.document_id}</strong>
          <span className="doc-id">{doc.document_id}</span>
        </td>
        <td><span className={`badge ${status.tone === "ok" ? "ok" : status.tone === "warn" ? "p2" : "p1"}`}>{doc.status || status.text}</span></td>
        <td className="num">{doc.chunks ?? 0}</td>
        <td className="mono">{doc.active_revision || "—"}{doc.pending_revision ? ` → ${doc.pending_revision}` : ""}</td>
        <td className="mono">{fmtDate(doc.updated_at)}</td>
        <td className="kb-actions">
          <button className="btn ghost kb-inline" onClick={onToggle}>{open ? "收起版本" : "版本"}</button>
          <button className="btn ghost kb-inline danger" onClick={onDelete}>删除</button>
        </td>
      </tr>
      {open && (
        <tr>
          <td colSpan={6}>
            <div className="revision-panel">
              {revisionsError && <p className="note error">{revisionsError}</p>}
              {!revisionsError && revisions.length === 0 && <p className="note">没有版本记录。</p>}
              <ul className="revision-list">
                {revisions.map((revision) => (
                  <li key={revision.revision}>
                    <span className="mono">{revision.revision}</span>
                    <span className={`badge ${revision.status === "active" ? "ok" : revision.status === "staged" ? "p2" : ""}`}>{revision.status}</span>
                    <span className="mono">{revision.chunks} 片段</span>
                    <span className="mono">{fmtDate(revision.created_at)}</span>
                    {revision.status !== "active" && (
                      <button className="btn ghost kb-inline" onClick={() => onActivate(revision.revision)}>切换到此版本</button>
                    )}
                  </li>
                ))}
              </ul>
            </div>
          </td>
        </tr>
      )}
    </>
  );
}
