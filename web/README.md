# web

On-Call Agent 的运行面板：查看每次诊断的计划、工具调用、replan 决策与 token 成本，
管理知识库文档与索引作业，并进行基于知识库的问答。

## 环境要求

- Node.js **v22.17.0 或更高**（本项目测试使用 Node 内置的 TypeScript 类型剥离，
  需要 `--experimental-strip-types`；v22.18+ 起该能力默认开启，脚本参数仍兼容）。
- npm 11+。

## 开发

```bash
npm install
npm run dev      # http://localhost:5173，/api 代理到 :9900
```

后端先起：

```bash
.venv/Scripts/python.exe -m uvicorn app.main:app --port 9900
```

## 构建与检查

```bash
npm run build    # tsc -b && vite build，产物在 dist/，由 FastAPI 直接托管
npm run lint     # oxlint
npm test         # node --experimental-strip-types --test src/*.test.ts
```

打开 `http://127.0.0.1:9900`。

## 结构

| 文件 | 作用 |
|---|---|
| `src/App.tsx` | 三栏布局：运行列表 / 计划脊线 + 日志 + 报告 / 指标检查器；三个视图（诊断 / 问答 / 知识库） |
| `src/state.ts` | **事件到状态的唯一实现**：`applyEvent` / `replay` / `emptyLive` / `summarize`；实时流与回放共用 |
| `src/sse.ts` | SSE 传输层：`SseBuffer` 增量分帧、`applyBlock` 状态机、`diagnose()` 四态结局 |
| `src/chat.ts` | `/api/chat_stream` 消费：retrieval / content / done / citations / truncated |
| `src/view.ts` | 视图投影纯函数：候选目标、引用卡片、检索与索引状态文案 |
| `src/api.ts` | REST 调用（运行、评测、知识库管理） |
| `src/KnowledgePanel.tsx` | 知识库管理：文档、上传、索引作业、revisions 与 activate、删除、状态 |
| `src/ChatPanel.tsx` | 知识库问答：引用卡片与检索状态 |
| `src/markdown.ts` | 报告渲染，HTML 标签转义 |
| `src/styles.css` | 全部样式；IBM Plex Sans/Mono，无 UI 框架依赖 |

## 约定

- 事件到状态只有 `state.ts` 一份实现；`App.tsx` 只做 UI 适配，不重复解析事件。
- 后端 SSE 用 `type` 字段，trace 文件用 `event` 字段；`sse.ts` 的 `normalizeSseEvent`
  统一成一种形状，组件只处理一种结构。
- SSE 由 `sse-starlette` 发出，行结束符是 CRLF；分帧统一交给 `SseBuffer`，
  不要用 `split("\r\n")` 手工切块。
- 只有显式 `complete` 才算成功；EOF 没有终态一律按 `truncated` 处理，
  已收到的事件保留但不标记成功。
- `target_required` 的候选卡片点击后带 `target_choice` 重新发起一次诊断，
  这是用户显式动作，不做自动重试。
- 运行列表与评测结果独立加载，一个失败不阻断另一个；选中 run 写入 `?run=<id>`，
  刷新按 URL 恢复，恢复失败只提示、不新建诊断。
- 环境变量：`VITE_API_BASE`（跨域部署时用）、`VITE_API_KEY`（后端设了 `API_KEY` 时）。
