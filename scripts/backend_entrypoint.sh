#!/bin/bash
set -euo pipefail

INIT_KB_MILVUS="${INIT_KB_MILVUS:-true}"
AUTO_BOOTSTRAP_LIGHTRAG="${AUTO_BOOTSTRAP_LIGHTRAG:-true}"
LIGHTRAG_BOOTSTRAP_DIR="${LIGHTRAG_BOOTSTRAP_DIR:-/app/bootstrap/lightrag_template}"
LIGHTRAG_WORKING_DIR="${LIGHTRAG_WORKING_DIR:-./data/lightrag}"

resolve_path() {
  case "$1" in
    /*) printf '%s\n' "$1" ;;
    ""|.) printf '%s\n' "/app" ;;
    ./*) printf '%s\n' "/app/${1#./}" ;;
    *) printf '%s\n' "/app/$1" ;;
  esac
}

LIGHTRAG_WORKING_DIR_ABS=$(resolve_path "$LIGHTRAG_WORKING_DIR")
LIGHTRAG_BOOTSTRAP_DIR_ABS=$(resolve_path "$LIGHTRAG_BOOTSTRAP_DIR")

bootstrap_lightrag_data() {
  if [ "${AUTO_BOOTSTRAP_LIGHTRAG,,}" != "true" ]; then
    echo "[backend] LightRAG bootstrap disabled via AUTO_BOOTSTRAP_LIGHTRAG=${AUTO_BOOTSTRAP_LIGHTRAG}"
    return
  fi

  if [ ! -d "${LIGHTRAG_BOOTSTRAP_DIR_ABS}" ] || [ -z "$(ls -A "${LIGHTRAG_BOOTSTRAP_DIR_ABS}" 2>/dev/null)" ]; then
    echo "[backend] LightRAG bootstrap目录不存在或为空，跳过自动导入。"
    return
  fi

  mkdir -p "${LIGHTRAG_WORKING_DIR_ABS}"
  if [ -n "$(ls -A "${LIGHTRAG_WORKING_DIR_ABS}" 2>/dev/null)" ]; then
    echo "[backend] LightRAG 工作目录已存在内容，跳过自动导入。"
    return
  fi

  echo "[backend] LightRAG 工作目录为空，正在从模板复制初始数据..."
  cp -a "${LIGHTRAG_BOOTSTRAP_DIR_ABS}/." "${LIGHTRAG_WORKING_DIR_ABS}/"
  echo "[backend] LightRAG 初始数据复制完成。"
}

bootstrap_lightrag_data

# 按 DATABASE_URL 的 scheme 选择驱动等待数据库就绪。
# 2026-10-06 修复：本函数原先无条件用 pymysql，而 .env.example 的 DATABASE_URL 是
# postgresql+psycopg —— 结果容器用 MySQL 驱动去连 PostgreSQL 端口，180s 超时后
# raise SystemExit + set -e 让 backend 每次启动都退出，进入 restart 崩溃循环。
wait_for_database() {
  python - <<'PY'
import os
import time
from urllib.parse import urlparse

database_url = os.getenv("DATABASE_URL", "").strip()
if not database_url or database_url.startswith("sqlite"):
    print("[backend] DATABASE_URL indicates SQLite or is unset; skipping DB wait.")
    raise SystemExit(0)

parsed = urlparse(database_url)
scheme = (parsed.scheme or "").lower()

if scheme.startswith("mysql") or scheme.startswith("mariadb"):
    import pymysql

    host = parsed.hostname or os.getenv("MYSQL_HOST", "mysql")
    port = parsed.port or int(os.getenv("MYSQL_PORT", "3306"))
    user = parsed.username or os.getenv("MYSQL_USER") or "root"
    password = parsed.password or os.getenv("MYSQL_PASSWORD") or os.getenv("MYSQL_ROOT_PASSWORD") or ""
    db_name = (parsed.path or "").lstrip("/") or os.getenv("MYSQL_DATABASE") or None
    deadline = time.monotonic() + int(os.getenv("MYSQL_WAIT_SECONDS", "180"))
    print(f"[backend] Waiting for MySQL at {host}:{port} (db={db_name or '<default>'}) ...")
    while time.monotonic() < deadline:
        try:
            conn = pymysql.connect(
                host=host,
                port=port,
                user=user,
                password=password,
                database=db_name,
                connect_timeout=5,
                read_timeout=5,
                write_timeout=5,
            )
            with conn.cursor() as cur:
                cur.execute("SELECT 1")
            conn.close()
            print("[backend] MySQL is ready.")
            break
        except Exception:
            time.sleep(2)
    else:
        raise SystemExit(f"MySQL is not reachable at {host}:{port} within timeout")
elif scheme.startswith("postgres"):
    import psycopg

    host = parsed.hostname or os.getenv("POSTGRES_HOST", "postgres")
    port = parsed.port or int(os.getenv("POSTGRES_PORT", "5432"))
    deadline = time.monotonic() + int(os.getenv("POSTGRES_WAIT_SECONDS", "180"))
    print(f"[backend] Waiting for PostgreSQL at {host}:{port} ...")
    while time.monotonic() < deadline:
        try:
            with psycopg.connect(
                host=host,
                port=port,
                user=parsed.username or os.getenv("POSTGRES_USER", "postgres"),
                password=parsed.password or os.getenv("POSTGRES_PASSWORD", ""),
                dbname=(parsed.path or "/postgres").lstrip("/") or "postgres",
                connect_timeout=5,
            ) as conn:
                with conn.cursor() as cur:
                    cur.execute("SELECT 1")
            print("[backend] PostgreSQL is ready.")
            break
        except Exception:
            time.sleep(2)
    else:
        raise SystemExit(f"PostgreSQL is not reachable at {host}:{port} within timeout")
else:
    print(f"[backend] Unknown DATABASE_URL scheme {scheme!r}; skipping DB wait.")
PY
}

# KB 语料入库已由 kb_ingest 服务承接（见 docs/design/阶段五-品牌切换验证记录.md：旧脚本
# scripts/init_kb_milvus.py 已删除）。此处原先仍调用该脚本，导致每次启动都打印
# "ingestion failed" 且哨兵永不写入 —— 现改为直接提示走 kb_ingest。
run_milvus_ingest() {
  if [ "${INIT_KB_MILVUS,,}" = "false" ]; then
    echo "[backend] KB Milvus ingestion disabled via INIT_KB_MILVUS=false."
    return
  fi

  echo "[backend] KB 语料入库由 kb_ingest 服务承接（http://kb_ingest:8000），backend 不再执行。"
}

run_milvus_ingest

wait_for_database

exec "$@"
