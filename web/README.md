# web/ —— 前端模块与统一工作台说明（企业级评估 §8.3）

> 技术栈：Vue 3 + TypeScript + Vite。`npm run dev`（5173）/ `npm run build`。

## 模块边界

`src/modules/` 双模块：

| 模块 | 定位 | API 前缀 |
|---|---|---|
| `chat/` | Agent 对话助手（编排能力） | `/api/v1/*`（编排域） |
| `knowledge/` | 知识库工作台（kb/文档/索引管理） | `/api/v1/retrieval/*`（检索域） |

登录后的 `/` 默认进入统一聊天工作台。输入框处的知识库开关决定消息走检索域还是 Agent 编排域：开启时用户选择知识库并调用检索 SSE；关闭时调用 Agent SSE。上传入口随开关切换语义：知识库开启时仅允许 PDF 并持久写入当前知识库；关闭时通过 `/api/v1/upload/file` 上传助手本轮附件。两条链路暂时仍使用各自的服务端会话与历史存储，前端统一呈现不代表历史已合并；助手回答保存为知识库文档尚未实现。

模块间**禁止**直接 import 对方内部组件/store；工作台集成组件放 `src/components/`，共享 API 配置放 `src/api/`，各域 API 与业务状态仍留在所属模块。

## 认证流约定

1. 登录门在路由层：未认证访问 → 统一跳登录页（跨模块共享登录态，单点会话）；
2. JWT 经 **HttpOnly cookie** 携带（`AUTH_COOKIE_SESSION_FALLBACK` 支撑会话态回退）；
3. 401 响应统一拦截 → 清登录态 → 跳登录（不允许模块内各自处理）。

## API 前缀纪律

- 一切请求走 `src/api/` 封装，禁止组件内裸 fetch/axios；
- base path 来自环境变量（`VITE_API_BASE_URL`），不允许模块内硬编码；
- 两个域的前缀差异（见上表）只在 `src/api/` 内体现。

## 环境变量

| 变量 | 作用 | 默认 |
|---|---|---|
| `VITE_API_BASE_URL` | 后端地址（dev 时 Vite proxy 以它为 target；不填走同源） | `http://localhost:8000`（proxy） |
| `VITE_API_KEY` | 共享 API key——后端 `REQUIRE_API_KEY=true` 时必填，前端以 `x-api-key` 头携带所有请求。**注意**：`VITE_*` 会打进前端产物，只作门禁钥匙；机密仍走 JWT | 空 |
| `VITE_PORT` | dev server 端口 | `5173` |

## Token 流转（流式问答）

- 深度模式（knowledge 域，`/api/v1/retrieval/chat/stream`）走 SSE，事件：`meta` / `progress` / `thinking_chunk` / `tool_running` / `answer_chunk`（`finished=true` 为终帧，带 `citations`/`answer_id`/`terminate_reason`）/ `error`；
- 编排域（chat 模块，`/api/v1/chat/stream`）SSE 事件：`metadata` / `message` / `tool_start` / `tool_end` / `done` / `error`；
- 渲染端按事件类型增量拼接；`error` 事件携带 `code`（用户报障时必须收集 `trace_id` / `request_id`）。

## 已知边界

- SSE 断线重连当前为"整体重发"语义（Last-Event-ID 续播为 P2 规划项，见评估报告 §2.3）；
- 前端不做权限裁剪的最终裁决（RBAC 以后端为准，前端仅隐藏入口）。
