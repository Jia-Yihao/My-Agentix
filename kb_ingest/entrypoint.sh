#!/bin/bash
set -euo pipefail

# Optional toggle: INIT_KB_EXCEL=false to skip Excel ingestion
INIT_KB_EXCEL="${INIT_KB_EXCEL:-true}"
KB_EXCEL_PATH="${KB_EXCEL_PATH:-/app/data/kb/legal_corpus.txt}"
KB_EXCEL_SENTINEL="${KB_EXCEL_SENTINEL:-/app/data/.kb_excel_ingested}"

wait_for_milvus() {
  python - <<'PY'
import os
import socket
import time

host = os.getenv("MILVUS_HOST", "milvus")
port = int(os.getenv("MILVUS_PORT", "19530"))

deadline = time.monotonic() + 120
while time.monotonic() < deadline:
    try:
        with socket.create_connection((host, port), timeout=3):
            break
    except OSError:
        time.sleep(2)
else:
    raise SystemExit("Milvus is not reachable for kb_ingest initialisation")
PY
}

milvus_has_data() {
  python - <<'PY'
import os

try:
    from pymilvus import Collection, connections

    host = os.getenv("MILVUS_HOST", "milvus")
    port = os.getenv("MILVUS_PORT", "19530")
    collection_name = os.getenv("MILVUS_COLLECTION", "legal")

    connections.connect(alias="default", host=host, port=port)
    collection = Collection(collection_name)
    if collection.num_entities > 0:
        raise SystemExit(0)
except Exception:
    pass
raise SystemExit(1)
PY
}

if [ "${INIT_KB_EXCEL}" != "false" ]; then
  if [ -f "${KB_EXCEL_PATH}" ]; then
    need_import=true
    if [ -f "${KB_EXCEL_SENTINEL}" ]; then
      if milvus_has_data; then
        need_import=false
        echo "[kb_ingest] Excel dataset already ingested (collection contains data), skipping."
      else
        echo "[kb_ingest] Sentinel exists but Milvus collection is empty, re-importing data."
        rm -f "${KB_EXCEL_SENTINEL}"
      fi
    fi

    if [ "${need_import}" = true ]; then
      # Do not block API startup on a potentially long-running ingestion job.
      # Run in background so `/health` and `/openapi.json` are available immediately.
      (
        echo "[kb_ingest] Waiting for Milvus to become available..."
        wait_for_milvus
        echo "[kb_ingest] Importing Excel dataset: ${KB_EXCEL_PATH}"
        if python -m kb_service.cli process-excel "${KB_EXCEL_PATH}"; then
          touch "${KB_EXCEL_SENTINEL}"
          echo "[kb_ingest] Excel ingestion completed."
        else
          echo "[kb_ingest] Excel ingestion failed; continuing without preloaded data." >&2
        fi
      ) &
    fi
  else
    echo "[kb_ingest] Excel dataset not found at ${KB_EXCEL_PATH}, skipping ingestion."
  fi
else
  echo "[kb_ingest] Excel ingestion disabled via INIT_KB_EXCEL=false."
fi

exec "$@"
