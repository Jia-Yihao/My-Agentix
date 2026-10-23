"""MySQL 连接串闸门（G12 / §24.3 T1 ⑫）。

`MySQLIngestRequest.connection_url` 原来没有任何校验，直接进
`create_engine(request.connection_url)`。持有本服务 API key 的一方因此可以让
**服务端**去连任意地址：

- ``sqlite:////app/secrets.db``、``postgresql://`` 之类非 MySQL scheme 让导入服务
  变成了读任意本地文件 / 探测任意数据库的代理；
- ``mysql://attacker.example/`` 把服务端拉到攻击者控制的 MySQL 上，
  对方能读到握手包里的用户名与客户端信息，并按连接耗时做内网端口探测。

本模块做两层**词法层**判定（解析 URL，比 scheme / 主机 / query 选项），刻意**不做 DNS 解析**：

- 解析要出网、会把校验延迟绑到被校验对象身上（对方控制 TTL 就能拖住导入线程），
  且解析后连接时仍会重解析——TOCTOU 并没有被堵住，只是看起来堵住了；
- 真正拦住「服务端被当代理使」的是主机白名单 + 网络侧 egress 策略，
  词法层负责的是「不让 URL 结构本身成为注入点」。

第二层容易被漏掉：主机白名单只回答「连到谁」，不回答「连过去之后客户端愿意干什么」。
`?local_infile=1` 会让**对端服务器**在握手后要求本客户端上传本地文件（实测
SQLAlchemy 把它 coerce 后传进 pymysql 的 connect args，并置上 `CLIENT_LOCAL_FILES` 位），
于是连一台陌生 MySQL 从"端口探测"升级成"读走服务主机上的文件"。

默认关（``KB_MYSQL_URL_GUARD=false``）：flag-off 与加固前逐位等价。
"""

from dataclasses import dataclass
from urllib.parse import parse_qsl, urlsplit

#: 只放行 MySQL 及其驱动方言（``mysql+pymysql`` / ``mysql+asyncmy`` / ``mysql+aiomysql``）。
_ALLOWED_SCHEMES = frozenset({"mysql"})

#: 明确点名的危险 scheme——报错时给出具体名字比「scheme 不合法」有用得多。
_NOTABLE_SCHEMES = (
    "sqlite",
    "postgresql",
    "postgres",
    "oracle",
    "mssql",
    "clickhouse",
    "file",
    "http",
    "https",
)

#: URL query 里能改写**客户端行为**的选项。
#:
#: 主机白名单拦的是「连到谁」，拦不住「连过去之后客户端愿意干什么」。
#: ``local_infile`` 是其中确实可用的一条：SQLAlchemy 的 MySQL dialect 会把
#: query 里的它 coerce 后原样传给驱动（实测
#: ``mysql+pymysql://u:p@h/db?local_infile=1`` → connect args 含
#: ``{'local_infile': 1}``，``client_flag`` 也被置上 ``CLIENT.LOCAL_FILES``）。
#: 开了它，**服务端**可以在握手后要求客户端上传本地文件——于是"连一台陌生的
#: MySQL"就从端口探测升级成"读走服务主机上的文件"，与 §24.2 G12 同一个威胁面。
#: ``read_default_file`` / ``read_default_group`` 同理属于「由 URL 决定客户端
#: 去读哪个文件」，一并拒绝；``client_flag`` 用位判定，只拦带 LOCAL_FILES 位的。
_DANGEROUS_QUERY_OPTIONS = frozenset(
    {
        "local_infile",
        "allow_local_infile",  # mysql-connector-python 的同义写法
        "read_default_file",
        "read_default_group",
    }
)

#: ``CLIENT_LOCAL_FILES`` 能力位（pymysql / mysqlclient 定义一致）。
_LOCAL_FILES_BIT = 2

#: 显式关掉这些值时不算危险——"我自己钉住 local_infile=0" 正是要的效果。
_FALSY = frozenset({"0", "false", "no", "off"})


def _is_truthy_option(value: str) -> bool:
    """``?local_infile``（无值）也按危险处理：URL 里写出这个键就是意图开启。"""
    return value.strip().lower() not in _FALSY


class ConnectionUrlError(ValueError):
    """连接串被闸门拒绝。

    调用方是路由与服务层，不是 pydantic——所以这个类型**不会**走到 422 的
    `errors()[].input`（那里会连密码一起回显，见 §24.2 G12「修复形态」）。
    仍继承 `ValueError`：它是「输入不合格」的通用语义，也让 CLI 侧可以直接
    `except ValueError` 而不必知道闸门的存在。
    """


@dataclass(frozen=True, slots=True)
class AllowedHost:
    """白名单条目：``host`` 必填，``port`` 与 ``subdomains`` 可选。

    ``.corp.example.com`` / ``*.corp.example.com`` 写成条目时代表
    「该域及其子域」，且**不含 apex 本身**——apex 需要单独一条，
    避免"加了通配结果 apex 也放进来"这种静默扩大范围。
    """

    host: str
    port: int | None = None
    subdomains: bool = False

    @classmethod
    def parse(cls, raw: str) -> "AllowedHost":
        value = raw.strip()
        if value.startswith("*."):
            return cls(host=value[2:].strip().lower(), subdomains=True)
        if value.startswith("."):
            return cls(host=value[1:].strip().lower(), subdomains=True)
        host, _, port_text = value.partition(":")
        port = int(port_text) if port_text.isdigit() else None
        return cls(host=host.strip().lower(), port=port)

    def matches(self, host: str, port: int | None) -> bool:
        if self.subdomains:
            # 子域条目**不含 apex**：见 docstring，apex 需要单独一条。
            covered = host.endswith(f".{self.host}")
        else:
            covered = host == self.host
        if not covered:
            return False
        return self.port is None or self.port == port


def parse_allowed_hosts(raw: str) -> tuple[AllowedHost, ...]:
    """逗号/分号/空白分隔的条目列表；空串 ⇒ 空元组（表示"只校验 scheme"）。"""
    if not raw:
        return ()
    normalized = raw.replace(";", ",").replace("\n", ",")
    entries = tuple(AllowedHost.parse(part) for part in normalized.split(",") if part.strip())
    return entries


def enforce_connection_url(connection_url: str, config: object) -> str:
    """按 config 上的闸门开关校验；**关闭时直接原样返回**（逐位等价）。

    校验点选在服务层而不是 pydantic 层，是因为 pydantic 的 ValidationError 会把
    被拒输入放进 `errors()["input"]`，而 FastAPI 的 422 响应体带着这个字段——
    一条被拒的连接串连密码一起回显给调用方（并落进调用方/网关日志）。
    detail 由我们自己构造时，只说"为什么被拒"，不说"你传了什么"。
    """
    if not getattr(config, "mysql_url_guard", False):
        return connection_url
    allowed = parse_allowed_hosts(getattr(config, "mysql_allowed_hosts", "") or "")
    return check_connection_url(connection_url, allowed)


def describe_for_log(connection_url: str) -> str:
    """给日志用的安全形态：**密码永远不进日志**，只保留 scheme/用户/主机/库名。"""
    try:
        parts = urlsplit(connection_url)
    except ValueError:
        return "<unparseable url>"
    user = parts.username or ""
    host = parts.netloc.rpartition("@")[2] if parts.netloc else ""
    user_part = f"{user}@" if user else ""
    return f"{parts.scheme}://{user_part}{host}{parts.path}"


def check_query_options(query: str) -> None:
    """拦掉 URL query 里会改写**客户端行为**的选项（只回显选项名，不回显值）。"""
    if not query:
        return
    for raw_key, raw_value in parse_qsl(query, keep_blank_values=True, strict_parsing=False):
        key = raw_key.strip().lower()
        if key in _DANGEROUS_QUERY_OPTIONS and _is_truthy_option(raw_value):
            raise ConnectionUrlError(
                f"connection_url option {key!r} is not allowed by KB_MYSQL_URL_GUARD "
                "(it lets the MySQL server read files from this host)"
            )
        if key == "client_flag":
            try:
                bits = int(raw_value.strip(), 0)
            except ValueError:
                raise ConnectionUrlError(
                    "connection_url option 'client_flag' is not a numeric capability mask"
                ) from None
            if bits & _LOCAL_FILES_BIT:
                raise ConnectionUrlError(
                    "connection_url option 'client_flag' sets CLIENT_LOCAL_FILES, "
                    "which lets the MySQL server read files from this host"
                )


def check_connection_url(
    connection_url: str,
    allowed_hosts: tuple[AllowedHost, ...] = (),
    *,
    require_host: bool = True,
) -> str:
    """校验连接串，返回原值（便于链式使用）；不合格抛 `ConnectionUrlError`。

    ``allowed_hosts`` 为空时只校验 scheme 与「有主机名」——这是 guard-on 但
    尚未配置白名单的过渡态，日志与文档都要把它讲清楚，别让人以为白名单已经生效。
    """
    if not connection_url or not connection_url.strip():
        raise ConnectionUrlError("connection_url is empty")

    try:
        parts = urlsplit(connection_url.strip())
    except ValueError as exc:
        raise ConnectionUrlError(f"connection_url is not parseable: {exc}") from exc

    scheme = (parts.scheme or "").lower()
    base_scheme = scheme.split("+", 1)[0]
    if base_scheme not in _ALLOWED_SCHEMES:
        hint = f"（检测到 {base_scheme or '无'} 协议；本服务只允许 mysql[+driver]）" if base_scheme in _NOTABLE_SCHEMES else ""
        raise ConnectionUrlError(f"unsupported scheme {scheme or '<none>'}{hint}")

    # 选项闸与主机闸正交：即便连的是白名单内的库，也不该由请求体决定
    # 「客户端是否愿意被对方读文件」。
    check_query_options(parts.query)

    host: str | None
    port: int | None
    try:
        # urlsplit 本身对畸形 IPv6 抛 ValueError，`.hostname` / `.port` 也会
        # （非法端口、未闭合方括号）。这些都属"被校验的输入"，不是我们的故障，
        # 必须转成 ConnectionUrlError——否则内部异常类型会冒到调用方的错误分支里。
        host = (parts.hostname or "").lower().rstrip(".")
        port = parts.port
    except ValueError as exc:
        raise ConnectionUrlError(f"connection_url is not parseable: {exc}") from exc

    if not host:
        if require_host:
            raise ConnectionUrlError("connection_url must name a database host")
        return connection_url.strip()

    if allowed_hosts and not any(rule.matches(host, port) for rule in allowed_hosts):
        # 只回显主机，绝不回显整条 URL（里面可能有密码）。
        raise ConnectionUrlError(f"host {host!r} is not in KB_MYSQL_ALLOWED_HOSTS")
    return connection_url.strip()
