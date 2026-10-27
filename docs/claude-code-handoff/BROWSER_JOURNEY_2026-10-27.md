# 当前构建浏览器旅程（2026-10-27）

对象：`web/dist`（`index-C814GCVU.js`，由当前源码 `npm run build` 产出，Vite dev server
`--port 5311` 提供同源代理以便前端带 `X-API-Key`）。后端为**容器内**应用
（`oncall-agent`，镜像 `sha256:7788efe30239...`）+ 真实 Milvus + 真实 MiniLM。

浏览器：Playwright Chromium（Claude Code 内置 MCP），视口按任务要求的五档逐一设置。

## 1. 五档视口实测

| 视口 | `.inspector` | `.inspector-toggle` | `.inspector-inline` | 横向溢出 | 备注 |
|---|---|---|---|---|---|
| 1440×900 | `flex`（显示） | `none` | `none` | 无 | 右侧栏常驻，无替代入口 |
| 1100×900 | `none` | `flex` | `none`→点开后 `block` | 无 | 替代入口按断点出现 |
| 1024×900 | `none` | `flex` | `block` | 无 | 展开态在缩小后保持 |
| 768×900 | `none` | `flex` | `block` | 无 | `grid-template-columns: 232px 536px` |
| 390×844 | `none` | `flex` | `block` | 无 | 单列 `375px` |
| 375×812 | `none` | `flex` | `block` | 无 | `docScrollWidth=360`，无元素越界 |

点击替代入口后（1100 宽，350ms 后读取）：`aria-expanded="true"`、`hidden` 移除、
`display: block`，内容为「本次运行」8 字段 + 「评测结果」+「这套系统在做什么」。

## 2. 真实数据面板（1440）

知识库面板连的是**容器内真实 Milvus**：

```
就绪  collection biz  片段数 955  文档 active 1 · pending 0
嵌入 local/sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2 · 384 维
Milvus 已连接 · 0 ms
标题             状态    chunks  active revision           更新时间
acceptance upload active   1     rev-e103d6275f0ef5fd       2026/10/27 21:18:47
最近作业  2026/10/27 21:18:46  ready  job-7a556378cca4 · doc-f6f6d9e55cac · 1/1 片段
```

截图：`screenshots/kb-real-milvus-1440-2026-10-27.png`

即：**HTTP 202 → 轮询 → ready → 列表出现 active revision** 在真实浏览器里可见，
不是只看 API 返回。

## 3. 中文 IME 组合态

在真实 textarea 上派发浏览器真实事件序列
（`compositionstart → input(isComposing) → compositionupdate → compositionend → input`）：

| 检查 | 结果 |
|---|---|
| 事件顺序被完整接收 | 是（`compositionstart, input, compositionupdate, compositionend, input`） |
| 组合结束后值正确 | `订单服务变慢` |
| React 受控值同步 | 是 |
| 组合期间误触发提交 | 否（无 `complete`/`error` 事件、无导航） |

**边界**：这是**合成事件**，不是真实输入法进程。真实 IME（微软拼音/搜狗）的候选窗
行为、以及读屏器朗读，本轮仍未验证。

## 4. aria-live

`[role="status"][aria-live="polite"]` 存在，文本随状态变化（观测到 `待命。`）。
**边界**：只确认了 DOM 与文案，未用读屏器确认实际朗读。

## 5. 模式配置与刷新入口

| 检查 | 结果 |
|---|---|
| 在线多智能体模式 | `已配置`，模型 `DeepSeek-V4.1-Flash-1017` |
| 本地端侧模式 | `未配置`，模型 `qwen-ops` |
| 「重新读取配置」按钮 | **出现 1 个**（未配置即可刷新，不需要先制造错误） |
| 错误提示 | 无 |

这一条同时验证了 A12 的“未配置也要有刷新入口”。

## 6. 未完成 / 未验证（必须保留）

1. **同名实例 target_candidate_id 的浏览器点击**：需要真实 MCP 目录返回同名双实例；
   本轮容器内 MCP 是本地 stub，返回的服务清单不含同名实例。该路径目前只有
   `view.test.ts` + `test_aiops_target_api.py`（含真实 `_service_kwargs` 到
   `aiops_service` 的 HTTP 端到端）覆盖，**没有**本轮的浏览器点击证据。
2. **作业失败/重试的真实浏览器操作**：容器里没有失败作业（唯一一次失败在本地 store），
   面板只观察到 `ready` 作业；`hasRetry=false`。`retry` 的 UI 路径本轮未在浏览器里点过。
3. **断流续读**：需要人为制造 SSE 中断；本轮未做（API 侧分页已有 21 项测试）。
4. **tab 切换/关闭后重开的历史一致性**：未验证。
5. **引用卡片**：本轮没有跑知识问答（`/api/chat` 需要在线模型 + 真实问答链路，
   且会消耗 token），引用卡片的浏览器渲染未观察。
