"""对外调用 kb_ingest（知识入库服务）时构造鉴权请求头。

**为什么这个函数在 ``domain/policies`` 而不是别处**

它是"出站调用的鉴权策略"，本身不碰任何 IO，也不需要第三方库——纯函数、
只用 stdlib。放在这里是为了**不破坏分层**：

* ``application`` 层（``file_handler`` / ``general_handler`` / ``task_handlers``）
  是真正的调用方，但规则 2 禁止它直接引用 ``infrastructure`` 符号；
* ``domain`` 层规则 3 要求零第三方依赖、不得引用 ``infrastructure`` / ``interfaces``。

所以"共享的、纯的、无依赖的"这个位置只剩 ``domain/policies``——
与 ``upload_paths.py`` 同一处理方式（那里也是因为共享逻辑无处可放才下沉）。

**为什么不干脆在每个调用点写死 ``{"X-API-Key": ...}``**

本仓此前正是这么散的：``kb_ingest`` 侧 5 个端点全无鉴权，而 agentix 侧
4 个调用点各写各的请求头（都没有带 Key）。两边一旦不同步就会出现
"服务端要求 Key、客户端不带" → 全部 401 的静默故障。
把"头名 + 是否携带"收敛成单一真源，是让两边**不可能**漂移的前提。
"""
from __future__ import annotations

from urllib.parse import urlsplit

#: 与 ``kb_ingest.api.deps`` 和 ``agentix.infrastructure.core.security`` 同名同义。
#: 三处必须一致；本常量是客户端侧的单一真源。
API_KEY_HEADER = "X-API-Key"


def _origin(url: str | None) -> tuple[str, str] | None:
    """取出 ``(scheme, netloc)`` 作为"源"。解析失败返回 ``None``。"""
    if not url:
        return None
    try:
        parts = urlsplit(url)
    except ValueError:
        return None
    if not parts.scheme or not parts.netloc:
        return None
    return (parts.scheme.lower(), parts.netloc.lower())


def ingest_auth_headers(api_key: str | None) -> dict[str, str]:
    """把共享密钥转成请求头。

    返回空 dict 表示"不携带鉴权头"，对应服务端 ``KB_REQUIRE_API_KEY=false``
    的既有部署（不改变行为）。

    两个刻意的决定，都有具体理由：

    1. **空串 / ``None`` / 全空白 ⇒ 不带头**，而不是带一个空的
       ``X-API-Key:``。后者会被服务端的 ``not x_api_key`` 判为失败，
       属于"制造一个必然失败的请求"。
    2. **不改动密钥本身的值**（只在判空时用 ``strip()``）。
       ⚠️ 首版写的是 ``key = (api_key or "").strip()`` 再发送——那是个 bug：
       服务端拿到的是 ``os.getenv("KB_API_KEY")`` 的**原值**（pydantic 默认
       不 strip，kb_ingest 侧 ``os.getenv`` 更不 strip），若 ``.env`` 里写的是
       ``KB_API_KEY=abc ``（尾随空格这种最常见的复制粘贴残留），客户端发出
       ``"abc"``、服务端比对 ``"abc "`` ⇒ **401**，而两边"看起来配置一致"。
       这正是把值收敛成单一真源要避免的那类静默不一致，所以这里不擅自规整。
    """
    key = api_key or ""
    if not key.strip():
        return {}
    return {API_KEY_HEADER: key}


def ingest_auth_headers_for(
    target_url: str | None,
    *,
    api_key: str | None,
    ingest_service_url: str | None,
) -> dict[str, str]:
    """仅在 ``target_url`` 指向**本仓自己的 kb_ingest** 时附带共享密钥。

    为什么需要这道判断：外部检索链的目标可以是**任意第三方服务**
    （``KB_EXTERNAL_SEARCH_URL`` 是运维可配的自由 URL）。若不分青红皂白
    就把 ``KB_API_KEY`` 附到每一个出站请求上，等于**把本仓的共享密钥送给外部服务**——
    用一个安全问题去换另一个安全问题。

    比较用 **origin（scheme + netloc）相等**，而不是 ``str.startswith``：
    字符串前缀会放过 ``http://kb_ingest:8000.evil.example`` 这类
    "前缀相同但主机不同"的地址——与 ``upload_paths`` 里修掉的是同一类错误。
    路径部分不参与比较（``/api/search`` 与 ``/api/ingest/excel`` 同源）。

    解析不出 origin（相对 URL、空值、非法 URL）时**一律不附带**：宁可让
    需要鉴权的调用拿到 401（可见、可排查），也不要静默把密钥发到未知目标。
    """
    if _origin(target_url) is None:
        return {}
    if _origin(target_url) != _origin(ingest_service_url):
        return {}
    return ingest_auth_headers(api_key)
