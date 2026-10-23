"""免鉴权 / 免限流路径的唯一清单。

`APIKeyMiddleware` 与 `GlobalRateLimitMiddleware` 原先各写一份豁免集合，注释写着
"同口径"——但同口径靠注释不是机制：加一个键只改一处，两套闸门就会在同一个 URL 上
给出相反答案。这份模块把清单变成单点，两边都 import 它。

`/api` 不在这里：它是根信息端点（返回应用名/版本号），不是探针。探针是 `/health`
（compose healthcheck 与 Vite proxy 都指向它）。`REQUIRE_API_KEY=true` 时读版本号
没有理由匿名放行；把它从清单里删掉属于 G7 的卫生项，不是漏洞修复——匹配一直是
精确成员判断，从未构成前缀绕过。

前缀豁免单独列出：`/docs`、`/redoc` 下面的子页与静态资源都该一起放行。
"""

from __future__ import annotations

PUBLIC_PATHS: frozenset[str] = frozenset(
    {
        "/docs",
        "/redoc",
        "/health",
        "/liveness",
        "/readiness",
        "/favicon.ico",
        "/openapi.json",
        "/metrics",
    }
)

PUBLIC_PREFIXES: tuple[str, ...] = ("/docs", "/redoc")
