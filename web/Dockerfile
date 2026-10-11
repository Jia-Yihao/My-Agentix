# web 前端容器（P2-3 compose web service）
# 构建上下文 = ./web（compose 中 build.context 指向本目录）——
# 根 .dockerignore 只对根上下文生效，本 Dockerfile 不受其排除规则影响。
# 阶段一：node:22-alpine 构建静态产物（npm ci 锁定依赖 → vue-tsc 类型检查 → vite build）；
# 阶段二：nginx:alpine 托管 dist（web/nginx.conf：SPA fallback + /api 反代 backend:8000 + SSE 配置）。

# node_modules 不进上下文：web/.dockerignore 已排除（见同目录）。

FROM node:22-alpine AS build
WORKDIR /app

# 构建期注入 API 基址（Vite 静态替换；变量名是 VITE_API_BASE_URL，
# 见 web/src/api/config.ts:38——清单笔误 VITE_API_BASE 已按实测修正）。
# 默认 /api = 同源反代路径（nginx 把 /api 转发 backend:8000），浏览器零跨域。
ARG VITE_API_BASE_URL=/api
ENV VITE_API_BASE_URL=$VITE_API_BASE_URL

COPY package.json package-lock.json* ./
RUN npm ci

COPY . .
RUN npm run build

FROM nginx:alpine
COPY nginx.conf /etc/nginx/conf.d/default.conf
COPY --from=build /app/dist /usr/share/nginx/html
EXPOSE 80
