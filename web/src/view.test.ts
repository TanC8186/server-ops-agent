/**
 * 视图投影纯函数测试（node:test + TS type stripping）。
 *
 * 覆盖：
 * - `target_required` 候选规范化、去重与请求体（选择是显式动作）；
 * - 引用卡片字段来源（不编造元数据）；
 * - 检索状态含 invalid_index 的独立文案；
 * - 知识库 collection / 作业状态的 202 轮询文案。
 */

import assert from "node:assert/strict";
import { test } from "node:test";

import {
  candidateKey,
  chatOutcomeLabel,
  citationCards,
  evalManifestSummary,
  jobStatusLabel,
  knowledgeStatusLabel,
  removedCitationsText,
  retrievalLabel,
  targetCandidates,
  targetRequestBody,
} from "./view.ts";

test("target_required 候选规范化并保留 service/instance/region", () => {
  const candidates = targetCandidates([
    { target_id: "tgt-aaaaaaaaaaaa1111", service: "svc-a", instance_id: "ins-1", region: "ap-guangzhou" },
    { service_name: "svc-b", region_code: "ap-shanghai" },
  ]);
  assert.deepEqual(candidates, [
    {
      targetId: "tgt-aaaaaaaaaaaa1111",
      service: "svc-a",
      instanceId: "ins-1",
      region: "ap-guangzhou",
      topics: [],
      sshHost: "",
    },
    {
      targetId: "",
      service: "svc-b",
      instanceId: "",
      region: "ap-shanghai",
      topics: [],
      sshHost: "",
    },
  ]);
  // 身份优先用服务端候选 ID；没有 ID 时退化为 service+instance+region。
  assert.equal(candidateKey(candidates[0]), "tgt-aaaaaaaaaaaa1111");
  assert.equal(candidateKey(candidates[1]), "svc-b\u001f\u001fap-shanghai");
  assert.notEqual(candidateKey(candidates[0]), candidateKey(candidates[1]));
});

test("A04：同名不同实例是两张卡，各自有唯一 targetId 与 key", () => {
  const candidates = targetCandidates([
    { target_id: "tgt-00000000000000aa", service: "orders", instance_id: "i-a", region: "r-a" },
    { target_id: "tgt-00000000000000bb", service: "orders", instance_id: "i-b", region: "r-b" },
  ]);
  assert.equal(candidates.length, 2);
  assert.deepEqual(candidates.map((item) => item.service), ["orders", "orders"]);
  assert.deepEqual(candidates.map((item) => item.instanceId), ["i-a", "i-b"]);
  assert.deepEqual(candidates.map((item) => item.region), ["r-a", "r-b"]);
  assert.deepEqual(
    candidates.map((item) => item.targetId),
    ["tgt-00000000000000aa", "tgt-00000000000000bb"],
  );
  assert.notEqual(candidateKey(candidates[0]), candidateKey(candidates[1]));
  // 服务端顺序保持
  assert.equal(candidates[0].instanceId, "i-a");
});

test("A04：按完整身份去重——完全相同才合并，实例/地域不同则保留", () => {
  const candidates = targetCandidates([
    { target_id: "tgt-1111111111111111", service: "orders", instance_id: "i-a", region: "r-a" },
    { target_id: "tgt-1111111111111111", service: "orders", instance_id: "i-a", region: "r-a" },
    { target_id: "tgt-2222222222222222", service: "orders", instance_id: "i-b", region: "r-a" },
    { target_id: "tgt-3333333333333333", service: "orders", instance_id: "i-a", region: "r-b" },
  ]);
  assert.equal(candidates.length, 3);
  assert.deepEqual(
    candidates.map((item) => item.targetId),
    ["tgt-1111111111111111", "tgt-2222222222222222", "tgt-3333333333333333"],
  );

  // 无 target_id 的旧候选：身份退化为 service+instance+region
  const legacy = targetCandidates([
    { service: "orders", instance_id: "i-a", region: "r-a" },
    { service: "orders", instance_id: "i-a", region: "r-a" },
    { service: "orders", instance_id: "i-b", region: "r-b" },
  ]);
  assert.equal(legacy.length, 2);
  assert.deepEqual(legacy.map((item) => item.instanceId), ["i-a", "i-b"]);
  assert.equal(legacy[0].targetId, "");
  assert.notEqual(candidateKey(legacy[0]), candidateKey(legacy[1]));
});

test("候选缺少服务名或不合法时被丢弃，不允许默认取第一项（同名不同实例保留）", () => {
  const candidates = targetCandidates([
    { instance_id: "ins-0" },
    { service: "" },
    { service: "svc-a" },
    { service: "svc-a", region: "other" },
    "svc-b",
    null,
  ]);
  assert.deepEqual(candidates.map((item) => item.service), ["svc-a", "svc-a"]);
  assert.deepEqual(candidates.map((item) => item.region), ["", "other"]);
  assert.deepEqual(targetCandidates(undefined), []);
});

test("显式选择才带 target_choice，空选择不改变原请求", () => {
  const base = { session_id: "s", execution_mode: "local", symptom: "慢" };
  assert.deepEqual(targetRequestBody(base, " svc-b "), { ...base, target_choice: "svc-b" });
  assert.deepEqual(targetRequestBody(base, ""), base);
  assert.deepEqual(targetRequestBody(base, "   "), base);
  assert.deepEqual(targetRequestBody(base), base);
  assert.equal("target_choice" in targetRequestBody(base, ""), false);
});

test("A04：有候选 ID 时带 target_candidate_id，缺失/空白时不带", () => {
  const base = { session_id: "s", execution_mode: "local", symptom: "慢" };
  const withCandidate = targetRequestBody(base, "orders", "tgt-abcdef0123456789");
  assert.equal(withCandidate.target_candidate_id, "tgt-abcdef0123456789");
  assert.equal(withCandidate.target_choice, "orders");
  assert.deepEqual(base, { session_id: "s", execution_mode: "local", symptom: "慢" });

  // 只给候选 ID 也可以
  assert.deepEqual(targetRequestBody(base, null, " tgt-abcdef0123456789 "), {
    ...base,
    target_candidate_id: "tgt-abcdef0123456789",
  });

  for (const blank of ["", "   ", null, undefined]) {
    const body = targetRequestBody(base, "orders", blank);
    assert.equal("target_candidate_id" in body, false);
    assert.equal(body.target_choice, "orders");
  }
  // 两个都没给：原请求原样返回，不制造一次“绑定空目标”
  assert.deepEqual(targetRequestBody(base), base);
  assert.equal("target_candidate_id" in targetRequestBody(base), false);
});

test("A10：manifest 摘要只读取真实字段，旧格式明确说明", () => {
  const legacy = evalManifestSummary(null);
  assert.equal(legacy.present, false);
  assert.match(legacy.text, /旧格式（无 manifest）/);

  const summary = evalManifestSummary({
    execution_mode: "local",
    llm: { provider: "local", model: "qwen3-4b", local_llm_model: "Qwen3-4B-Instruct" },
    embedding: { provider: "local", model: "bge-small-zh" },
    scenarios: ["s1", "s2", "s3"],
    git: { commit: "abc1234", dirty_worktree: true },
  });
  assert.equal(summary.present, true);
  assert.match(summary.text, /qwen3-4b/);
  assert.match(summary.text, /Qwen3-4B-Instruct/);
  assert.match(summary.text, /bge-small-zh/);
  assert.match(summary.text, /3 个/);
  assert.match(summary.text, /abc1234/);
  assert.match(summary.text, /未提交/);
  assert.deepEqual(summary.missing, []);

  // 字段缺失时只报告缺失，不编造值
  const partial = evalManifestSummary({ execution_mode: "cloud_multi" });
  assert.equal(partial.present, true);
  assert.doesNotMatch(partial.text, /qwen|bge|abc1234/);
  assert.ok(partial.missing.includes("llm.model"));
  assert.ok(partial.missing.includes("git.commit"));
  assert.ok(partial.missing.includes("embedding.model"));
});


test("引用卡片只读取服务端字段", () => {
  const cards = citationCards([
    {
      id: "K1",
      title: "磁盘容量排查",
      locator: "L10-L20",
      source_uri: "https://example.invalid/manual#disk",
      snippet: "先看 inode",
      score: 0.8123,
      document_id: "doc-1",
    },
    { id: "K2", document_id: "doc-2" },
  ]);
  assert.equal(cards.length, 2);
  assert.deepEqual(cards[0], {
    id: "K1",
    title: "磁盘容量排查",
    locator: "L10-L20",
    sourceUri: "https://example.invalid/manual#disk",
    snippet: "先看 inode",
    score: 0.8123,
    documentId: "doc-1",
  });
  // 缺失标题时回退到 document_id，不编造来源
  assert.equal(cards[1].title, "doc-2");
  assert.equal(cards[1].sourceUri, "");
  assert.equal(cards[1].score, 0);
  assert.deepEqual(citationCards("nope"), []);
});

test("检索状态五种取值互不混淆，invalid_index 不等于 unavailable", () => {
  const statuses = ["hit", "miss", "unavailable", "timeout", "invalid_index"];
  const texts = statuses.map((status) => retrievalLabel(status).text);
  assert.equal(new Set(texts).size, 5);
  assert.equal(retrievalLabel("invalid_index").tone, "error");
  assert.match(retrievalLabel("invalid_index").detail, /重建索引/);
  assert.equal(retrievalLabel("timeout").tone, "error");
  assert.equal(retrievalLabel("miss").tone, "warn");
  assert.equal(retrievalLabel(undefined).tone, "muted");
});

test("未授权引用被移除时给出明确提示", () => {
  assert.equal(removedCitationsText(["K9", "K10"]), "已移除未授权引用：K9、K10");
  assert.equal(removedCitationsText([]), "");
  assert.equal(removedCitationsText(undefined), "");
});

test("done outcome 文案：answered 不额外报错", () => {
  assert.equal(chatOutcomeLabel("answered"), "");
  assert.equal(chatOutcomeLabel("insufficient_evidence"), "知识库证据不足");
  assert.equal(chatOutcomeLabel("failed"), "回答生成失败");
});

test("知识库状态与作业状态：invalid_index 与 unavailable 分开展示", () => {
  assert.equal(knowledgeStatusLabel("ready").tone, "ok");
  assert.equal(knowledgeStatusLabel("degraded").tone, "warn");
  assert.equal(knowledgeStatusLabel("unavailable").text, "不可用");
  assert.match(knowledgeStatusLabel("invalid_index").text, /重建/);
  assert.notEqual(knowledgeStatusLabel("invalid_index").text, knowledgeStatusLabel("unavailable").text);

  assert.equal(jobStatusLabel("queued").text, "已排队");
  assert.equal(jobStatusLabel("running").text, "索引中");
  assert.equal(jobStatusLabel("ready").tone, "ok");
  assert.equal(jobStatusLabel("failed").tone, "error");
});

test("A09：分页游标优先用位置游标，其次 seq，读完返回 null", async () => {
  const { nextRunCursor, isStuckPage } = await import("./view.ts");

  // 未截断 -> 结束
  assert.equal(nextRunCursor({ truncated: false }, 1), null);
  // 位置游标优先（无 seq 边界页）
  assert.deepEqual(nextRunCursor({ truncated: true, next_after_event: 2000 }, 1), {
    after_event: 2000,
  });
  // 其次 seq 游标
  assert.deepEqual(nextRunCursor({ truncated: true, next_after_seq: 42 }, 1), {
    after_seq: 42,
  });
  // 截断却无任何游标 -> 不能继续，且必须能被识别为“卡住”
  const stuck = { truncated: true };
  assert.equal(nextRunCursor(stuck, 1), null);
  assert.equal(isStuckPage(stuck, 1), true);
  assert.equal(isStuckPage({ truncated: false }, 1), false);
});

test("A09：页数上限保护，避免服务端异常时无限翻页", async () => {
  const { MAX_RUN_PAGES, nextRunCursor } = await import("./view.ts");
  assert.equal(nextRunCursor({ truncated: true, next_after_event: 10 }, MAX_RUN_PAGES), null);
});

test("A09：多页事件按服务端顺序追加，不重排", async () => {
  const { appendRunEvents } = await import("./view.ts");
  const first = appendRunEvents([], { events: [{ event: "a" }, { event: "b" }] });
  const second = appendRunEvents(first, { events: [{ event: "c" }] });
  assert.deepEqual(
    second.map((item) => (item as { event: string }).event),
    ["a", "b", "c"],
  );
  assert.deepEqual(appendRunEvents(first, {}), first);
});
