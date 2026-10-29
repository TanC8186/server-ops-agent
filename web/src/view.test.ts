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
  boundTarget,
  candidateKey,
  chatOutcomeLabel,
  citationAnchorId,
  citationCards,
  evalManifestSummary,
  jobStatusLabel,
  knowledgeStatusLabel,
  linkCitationMarks,
  localFailureNote,
  removedCitationsText,
  retrievalLabel,
  safeSourceUri,
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
      revision: "rev-abcdef0123456789",
      heading_path: ["磁盘", "容量排查"],
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
    revision: "rev-abcdef0123456789",
    headingPath: "磁盘 > 容量排查",
  });
  // 缺失标题时回退到 document_id，不编造来源；版本/章节缺失保持空串。
  assert.equal(cards[1].title, "doc-2");
  assert.equal(cards[1].sourceUri, "");
  assert.equal(cards[1].score, 0);
  assert.equal(cards[1].revision, "");
  assert.equal(cards[1].headingPath, "");
  assert.deepEqual(citationCards("nope"), []);
});

test("G5 引用来源：只有绝对 http(s) 才能成为链接", () => {
  assert.equal(safeSourceUri("https://example.invalid/a#b"), "https://example.invalid/a#b");
  assert.equal(safeSourceUri("  http://example.invalid/x  "), "http://example.invalid/x");

  // 协议注入：javascript:/data:/file: 一律不渲染成链接。
  for (const bad of [
    "javascript:alert(1)",
    "JaVaScRiPt:alert(1)",
    "data:text/html,<script>alert(1)</script>",
    "file:///C:/secret.txt",
    "vbscript:msgbox",
    // 控制字符拆开协议名后，浏览器仍会按脚本解释。
    "\tjava\nscript:alert(1)",
    "java\u0000script:alert(1)",
    // 反斜杠变体与相对地址
    "https:\\\\example.invalid\\a",
    "/manual/disk.md",
    "mailto:ops@example.invalid",
    "",
  ]) {
    assert.equal(safeSourceUri(bad), null, `不应成为链接: ${bad}`);
  }
  assert.equal(safeSourceUri(undefined), null);
});

test("G5 正文 [K#] 只在同一 turn 的引用集合内变成锚点", () => {
  const html = '<p>先看 inode [K1]，再看容量 [K2]，最后 [K9]。</p>';
  const linked = linkCitationMarks(html, ["K1"]);
  assert.match(linked, /href="#citation-K1"/);
  assert.match(linked, /data-citation-id="K1"/);
  // 本轮不存在的编号（可能是上一轮的引用）保持纯文本，不造死链。
  assert.match(linked, /\[K2\]/);
  assert.match(linked, /\[K9\]/);
  assert.equal((linked.match(/class="citation-mark"/g) ?? []).length, 1);

  // 标签属性里出现的 [K1] 绝不能被替换（会破坏 HTML 结构）。
  const withAttr = '<a title="[K1]" href="https://example.invalid">x</a>';
  assert.equal(linkCitationMarks(withAttr, ["K1"]), withAttr);

  // 没有引用时不改动正文。
  assert.equal(linkCitationMarks(html, []), html);
  assert.equal(citationAnchorId("K1"), "citation-K1");
  // 非法字符不会进入 id（避免属性注入/重复 id）。
  assert.equal(citationAnchorId('K1" onclick="x'), "citation-K1__onclick__x");
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

test("A12：未配置模式也要有“重新读取配置”入口，不只是错误时", async () => {
  const { shouldOfferModeReload } = await import("./view.ts");

  // 读取成功但有模式未配置 -> 需要入口（旧实现遗漏的场景）
  assert.equal(
    shouldOfferModeReload({ modesState: "loaded", hasError: false, configured: [true, false] }),
    true,
  );
  // 错误时 -> 需要入口
  assert.equal(
    shouldOfferModeReload({ modesState: "error", hasError: true, configured: [true, true] }),
    true,
  );
  // 都配置好了 -> 不需要
  assert.equal(
    shouldOfferModeReload({ modesState: "loaded", hasError: false, configured: [true, true] }),
    false,
  );
  // 仍在加载 -> 不显示
  assert.equal(
    shouldOfferModeReload({ modesState: "loading", hasError: false, configured: [false] }),
    false,
  );
});

test("A12：<=1180px 使用内联 inspector 替代入口（与 CSS 断点一致）", async () => {
  const { usesInlineInspector, NARROW_INSPECTOR_BREAKPOINT_PX } = await import("./view.ts");

  assert.equal(NARROW_INSPECTOR_BREAKPOINT_PX, 1180);
  assert.equal(usesInlineInspector(1180), true);
  assert.equal(usesInlineInspector(1100), true);
  assert.equal(usesInlineInspector(1181), false);
  assert.equal(usesInlineInspector(1440), false);
});

test("G5 RunInspector 的已绑定目标：有服务名才算绑定，实例/地域不编造", () => {
  assert.deepEqual(
    boundTarget({ service: "orders", instance_id: "i-a", region: "r-a", topics: ["t"] }),
    { service: "orders", instanceId: "i-a", region: "r-a", topics: ["t"] },
  );
  // 旧服务端字段名
  assert.deepEqual(
    boundTarget({ service_name: "orders", region_code: "ap-guangzhou" }),
    { service: "orders", instanceId: "", region: "ap-guangzhou", topics: [] },
  );
  // 未绑定：null（界面显示“未绑定”），而不是一个空壳对象。
  assert.equal(boundTarget(null), null);
  assert.equal(boundTarget({ instance_id: "i-a" }), null);
  assert.equal(boundTarget({ service: "  " }), null);
});

test("G8 本地失败说明：只有 local 模式才说“已停止、没切云端”", () => {
  // local：必须同时给出“已停止”和“没调用在线模型”两个事实。
  const local = localFailureNote("local", "本地模型不可用或计划校验失败");
  assert.match(local, /本地模式已停止/);
  assert.match(local, /没有调用在线模型/);
  assert.match(local, /原因：本地模型不可用或计划校验失败/);

  // 后端没给细节时，仍然要给出“已停止”这句，不能因为 message 空就什么都不显示。
  assert.match(localFailureNote("local", ""), /本地模式已停止/);
  assert.doesNotMatch(localFailureNote("local", ""), /原因/);

  // cloud_multi 失败不能说“本地模式已停止”——那是另一条链路。
  assert.equal(localFailureNote("cloud_multi", "上游 502"), "");

  // 历史运行没有模式记录时同样不猜。
  assert.equal(localFailureNote(null, "boom"), "");
  assert.equal(localFailureNote(undefined, "boom"), "");
});
