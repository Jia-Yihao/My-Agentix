# web/ —— 前端模块与统一工作台

> 技术栈：Vue 3 + TypeScript + Vite。开发用 `npm run dev`（5173），构建用 `npm run build`。

## 模块边界

`src/modules/` 双模块：

| 模块 | 定位 | API 前缀 |
|---|---|---|
| `chat/` | Agent 对话助手（编排能力） | `/api/v1/*`（编排域） |
| `knowledge/` | 知识库工作台（kb/文档/索引管理） | `/api/v1/retrieval/*`（检索域） |

登录后的 `/` 默认进入统一聊天工作台。输入框处的知识库开关决定消息走检索域还是 Agent 编排域：开启时用户选择知识库并调用检索 SSE；关闭时调用 Agent SSE。上传入口随开关切换语义：知识库开启时仅允许 PDF 并持久写入当前知识库；关闭时通过 `/api/v1/upload/file` 上传助手本轮附件。两条链路暂时仍使用各自的服务端会话与历史存储，前端统一呈现不代表历史已合并；助手回答保存为知识库文档尚未实现。

模块之间不允许直接 import 对方的内部组件与 store。工作台集成组件放 `src/components/`，共享 API 配置放 `src/api/`，各域 API 与业务状态留在所属模块。

## 认证流约定

1. 登录门在路由层：未认证访问统一跳登录页，跨模块共享登录态（单点会话）。
2. JWT 经 HttpOnly cookie 携带，`AUTH_COOKIE_SESSION_FALLBACK` 支撑会话态回退。
3. 401 统一拦截，清登录态后跳登录，模块内不各自处理。

## API 前缀纪律

- 所有请求走 `src/api/` 封装，组件内不直接写 fetch 或 axios。
- base path 来自 `VITE_API_BASE_URL`，不在模块内硬编码。
- 两个域的前缀差异（见上表）只在 `src/api/` 内体现。

## 环境变量

| 变量 | 作用 | 默认 |
|---|---|---|
| `VITE_API_BASE_URL` | 后端地址，开发时作为 Vite proxy 的 target；不填走同源 | `http://localhost:8000`（proxy） |
| `VITE_API_KEY` | 共享 API key。后端 `REQUIRE_API_KEY=true` 时必填，前端以 `x-api-key` 头携带。`VITE_*` 会打进前端产物，只能当门禁钥匙用，机密仍走 JWT | 空 |
| `VITE_PORT` | 开发服务器端口 | `5173` |

## Token 流转（流式问答）

- 深度模式（knowledge 域，`/api/v1/retrieval/chat/stream`）走 SSE，事件有 `meta` / `progress` / `thinking_chunk` / `tool_running` / `answer_chunk` / `error`。`answer_chunk` 的 `finished=true` 为终帧，带 `citations` / `answer_id` / `terminate_reason`。
- 编排域（chat 模块，`/api/v1/chat/stream`）的 SSE 事件：`metadata` / `message` / `tool_start` / `tool_end` / `done` / `error`。
- 渲染端按事件类型增量拼接。`error` 事件携带 `code`，用户报障时需一并收集 `trace_id` 与 `request_id`。

## 已知边界

- SSE 断线重连是整体重发语义，Last-Event-ID 续播尚未实现。
- 前端不做权限裁剪的最终裁决。RBAC 以后端为准，前端只隐藏入口。
