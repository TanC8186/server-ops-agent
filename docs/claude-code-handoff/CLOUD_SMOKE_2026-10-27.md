# 真实云模型冒烟（2026-10-27）

对象：当前工作区；模型 = `DeepSeek-V4.1-Flash-1017`（Anthropic 兼容网关，
`provider=anthropic`）。只做有限调用，**不连接任何生产监控/告警写入**。
工具层用进程内 fake（返回真实 MCP 的 payload 形态），**LLM 是真实的**。

## 1. 基础连通性

| 项 | 值 |
|---|---|
| 首次调用延迟 | 1.69 s |
| tokens | in 39 / out 41 |
| 返回 | `{"ok":true}` |

## 2. JSON 契约（SupervisorPlan schema，3 次）

| 次数 | 延迟 | JSON 合法 | in/out tokens |
|---|---|---|---|
| 1 | 3.77 s | 是 | 307 / 271 |
| 2 | 1.70 s | 是 | 307 / 138 |
| 3 | 2.89 s | 是 | 307 / 247 |
| 合计 | — | 3/3 | 921 / 656 |

## 3. 非 JSON / 超时 / 降级

| 场景 | 结果 |
|---|---|
| 强制自然语言输出 | `AnthropicInvalidRequestError 400: sensitive_words_detected`（**网关侧的提示词过滤**，不是本项目缺陷；同一 prompt 在别处正常） |
| `asyncio.wait_for(timeout=0.02)` | `TimeoutError`，0.02 s 返回，**无重试、无额外费用** |
| `max_tokens=60` 的 Reviewer prompt | `finish_reason=max_tokens`，输出 60 tokens、内容为空串（截断） |

## 4. 端到端 cloud_multi（真实 LLM + 真形态 fake 工具）

### 4.1 修复前：报告退化为“没有观测”

```
status=degraded, quality=sufficient, has_observation=false
gaps:
 - Reviewer 失败，报告仅展示已收集证据: JSONDecodeError: Unterminated string
   starting at: line 1 column 1830 (char 1829)
 - logs 专科失败: JSONDecodeError: Expecting value: line 1 column 1 (char 0)
```

诊断：模型返回的是**正常输出**（带前置说明 / 被 `max_tokens` 截断），旧实现
直接 `json.loads(reply)`，于是整轮退化为无观测。这条与 `test_json_extraction.py`
和 `_ask` 的重试一起修好了。

### 4.2 修复后：完整报告，且数值来自证据

```
seconds=34.5  status=completed  quality=sufficient  gaps=12  report_chars=1999

## 当前观测
- [证据观察] CPU 指标统计：均值93.5%，最大97%，最小88%，P95 96%，计数3。
  （服务端核算: E2 cpu_usage_percent=93.5%；证据: E2）
- [证据观察] CPU 最低仍为88%，采样窗口内持续高位。
  （服务端核算: E2 cpu_usage_percent=88% target=checkout-service；证据: E2）
- [证据观察] 日志查询 total=1，仅一条错误样本，样本量很小。
  （服务端核算: E3 metric=1 target=checkout-service；证据: E3）
```

关键点：

1. **12 条证据缺口不是失败**，是模型主动列出的缺失数据（接口延迟/QPS/容器配额/
   主机层指标…）——这正是报告结构要求的“证据缺口”一节应有的内容。
2. 观测行的数值（93.5 / 88 / 1）都能回到证据；`服务端核算` 是服务端按证据渲染的，
   不是模型措辞。
3. `quality=sufficient` 与 `status=completed` 同时成立：本轮证据足够支撑“CPU 持续
   高位”这个观测，但不支撑任何根因。

### 4.3 本轮顺带修掉的两个真实缺陷

| 缺陷 | 现象 | 修复 |
|---|---|---|
| JSON 解析过脆 | 围栏/前置说明/截断一律失败，Reviewer 与 logs 专科各挂一次 | `_extract_json()`（围栏、前后缀、嵌套花括号、字符串内花括号）+ 一次按失败原因区分的重试（截断→要求精简闭合；其他→要求只输出 JSON）；网络错误不重试 |
| 条数事实被误判 | “样本数=3”“日志仅 1 条”被判成“数值无法在证据中找到” | `statistics.count/total` 与顶层 `total` 作为 **count 单位**的事实收录：可引用，但 duration/percent 声明仍匹配不上（A06 不被重新打开） |

## 5. 费用与失败分母

| 项 | 值 |
|---|---|
| 本轮可见 token 总量（不含 3 次 schema 调用） | 约 1.6k in / 1.0k out |
| 失败分母 | 4 次调用失败（1 次网关内容过滤、1 次计划内超时、2 次 JSON 退化，均已在上文归类） |
| 费用 | **未记录**：网关未提供单价，不做估算 |
| 远端取消边界 | **未验证**：`asyncio.wait_for` 只证明本地等待被放弃；远端是否继续生成/计费未知 |

## 6. SSH 只读冒烟

**未执行**。`.env` 里 `SSH_HOST` 未指向任何我已获授权的 staging 主机；按任务边界
“默认不连接生产、不执行修改命令”，本轮不伪造 SSH 证据。代码侧已有：
`SSH_TOOLS` 目标范围标注、`configured_ssh_endpoint` 不进观测、
命令超时与 `deadline` 下传的单元测试（`test_ssh_safety.py`、`test_runtime_target_binding.py`）。

## 7. 可写进简历 / 不可写

**可以写**：
- 真实 LLM（非 mock）跑通 Supervisor→metrics/logs→Reviewer 全链路，产出结构化报告，
  观测数值由服务端按证据渲染。
- 对模型非 JSON 输出的健壮性：围栏/前缀/截断均可恢复，截断会触发“精简重试”。

**不能写**：
- “诊断准确率 XX%”——本轮只有一个合成故障场景，没有标签集。
- “取消能终止远端生成”——未验证。
- “SSH 已联调”——未执行。
