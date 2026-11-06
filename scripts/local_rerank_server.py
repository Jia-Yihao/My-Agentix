"""本地 bge-reranker-v2-m3 精排服务（py312 + CUDA，零外部依赖调用）。

协议对齐 ``agentix/retrieval/infra/rag/rerank.py`` 的「通用自建服务」分支::

    POST /v1/rerank {"query": str, "documents": [str], "model": str, "top_n": int}
    -> {"results": [{"index": int, "relevance_score": float}, ...]}

分数为 ``sigmoid(logit) ∈ [0, 1]``，与 dashscope qwen3-rerank 的
relevance_score 同量纲；sigmoid 单调，排序与原始 logit 完全一致，
因此 ``RETRIEVAL_RERANK_MIN_SCORE`` 在本地/云端端点之间可直接互换。

启动（用带 CUDA torch 的解释器，不是项目 .venv）::

    D:/conda_envs/py312/python.exe scripts/local_rerank_server.py

所有可调项走环境变量（登记在 .env.example），不写死在代码里。
"""

from __future__ import annotations

import os
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import torch
from fastapi import FastAPI
from pydantic import BaseModel
from transformers import AutoModelForSequenceClassification, AutoTokenizer


def _load_repo_env() -> None:
    """服务跑在独立解释器里，直接复用仓库根 .env；进程环境变量优先。"""
    path = Path(__file__).resolve().parents[1] / ".env"
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if key.startswith("export "):
            key = key[7:].strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        if key and key not in os.environ:
            os.environ[key] = value


_load_repo_env()

MODEL_PATH = os.getenv(
    "LOCAL_RERANK_MODEL_PATH", r"E:\My-Agentix\reranker-cache\model"
)
HOST = os.getenv("LOCAL_RERANK_HOST", "127.0.0.1")
PORT = int(os.getenv("LOCAL_RERANK_PORT", "8765"))
BATCH_SIZE = max(1, int(os.getenv("LOCAL_RERANK_BATCH_SIZE", "16")))
MAX_LENGTH = max(32, int(os.getenv("LOCAL_RERANK_MAX_LENGTH", "512")))
MAX_DOC_CHARS = max(64, int(os.getenv("LOCAL_RERANK_MAX_DOC_CHARS", "2000")))
DEVICE = os.getenv("LOCAL_RERANK_DEVICE", "auto")

_state: dict[str, Any] = {}


def _resolve_device() -> str:
    if DEVICE != "auto":
        return DEVICE
    return "cuda" if torch.cuda.is_available() else "cpu"


def load_model() -> dict[str, Any]:
    started = time.perf_counter()
    device = _resolve_device()
    dtype = torch.float16 if device.startswith("cuda") else torch.float32
    tokenizer = AutoTokenizer.from_pretrained(MODEL_PATH)
    model = AutoModelForSequenceClassification.from_pretrained(
        MODEL_PATH, dtype=dtype
    ).to(device).eval()
    info = {
        "model_path": MODEL_PATH,
        "device": device,
        "dtype": str(dtype).replace("torch.", ""),
        "load_seconds": round(time.perf_counter() - started, 2),
        "cuda_name": torch.cuda.get_device_name(0) if device.startswith("cuda") else "",
    }
    _state.update(tokenizer=tokenizer, model=model, device=device, info=info)
    return info


@asynccontextmanager
async def lifespan(app: FastAPI):
    print(f"[local-rerank] loading {MODEL_PATH}", flush=True)
    print(f"[local-rerank] ready {load_model()}", flush=True)
    yield
    _state.clear()


app = FastAPI(title="local-rerank", lifespan=lifespan)


class RerankRequest(BaseModel):
    query: str
    documents: list[str]
    model: str = ""
    top_n: int = 10


class RerankResult(BaseModel):
    index: int
    relevance_score: float


@app.get("/health")
def health() -> dict[str, Any]:
    return {"ok": bool(_state), **_state.get("info", {})}


@app.post("/v1/rerank")
def rerank(req: RerankRequest) -> dict[str, Any]:
    if not _state:
        load_model()
    if not req.query or not req.documents:
        return {"results": []}

    tokenizer = _state["tokenizer"]
    model = _state["model"]
    device = _state["device"]
    scores: list[float] = []
    docs = [(doc or "")[:MAX_DOC_CHARS] for doc in req.documents]

    with torch.inference_mode():
        for start in range(0, len(docs), BATCH_SIZE):
            pairs = [(req.query, doc) for doc in docs[start:start + BATCH_SIZE]]
            encoded = tokenizer(
                pairs,
                padding=True,
                truncation=True,
                max_length=MAX_LENGTH,
                return_tensors="pt",
            ).to(device)
            logits = model(**encoded).logits.view(-1)
            scores.extend(torch.sigmoid(logits.float()).cpu().tolist())

    top_n = max(1, min(int(req.top_n), len(scores)))
    order = sorted(range(len(scores)), key=lambda i: (-scores[i], i))[:top_n]
    return {
        "results": [
            RerankResult(index=i, relevance_score=round(scores[i], 6))
            for i in order
        ]
    }


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host=HOST, port=PORT, log_level="warning")
