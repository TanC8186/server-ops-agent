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
  jobStatusLabel,
  knowledgeStatusLabel,
  removedCitationsText,
  retrievalLabel,
  targetCandidates,
  targetRequestBody,
} from "./view.ts";

test("target_required 候选规范化并保留 service/instance/region", () => {
  const candidates = targetCandidates([
    { service: "svc-a", instance_id: "ins-1", region: "ap-guangzhou" },
    { service_name: "svc-b", region_code: "ap-shanghai" },
  ]);
  assert.deepEqual(candidates, [
    { service: "svc-a", instanceId: "ins-1", region: "ap-guangzhou" },
    { service: "svc-b", instanceId: "", region: "ap-shanghai" },
  ]);
  assert.equal(candidateKey(candidates[0]), "svc-a");
});

test("候选缺少服务名或重复时被丢弃，不允许默认取第一项", () => {
  const candidates = targetCandidates([
    { instance_id: "ins-0" },
    { service: "" },
    { service: "svc-a" },
    { service: "svc-a", region: "other" },
    "svc-b",
    null,
  ]);
  assert.deepEqual(candidates.map((item) => item.service), ["svc-a"]);
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
