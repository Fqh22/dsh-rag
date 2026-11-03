"""向量化后端：默认走智谱 embedding-3，可切回本地 fastembed。

为什么换成智谱：
- 本地 bge-small-zh（512 维）要 91MB 模型 + ONNX 冷启动，检索质量也一般；
- 智谱 embedding-3 支持自定义维度（这里选 1024），中文质量明显更好，按量计费极便宜；
- key 已经在用（本机用户级环境变量 + 云端 AstrBot 的 zhipu provider）。

代价：需要联网，且文本会出本机。想完全离线时把 DSH_RAG_EMBED 设成 local。

⚠️ 换后端 = 换维度 = 必须重建所有向量，否则新旧向量混算会出错。
   重建用 scripts/reembed.py。
"""
from __future__ import annotations

import json
import os
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CACHE_DIR = ROOT / "data" / "models"

BACKEND = (os.environ.get("DSH_RAG_EMBED") or "zhipu").strip().lower()

ZHIPU_URL = "https://open.bigmodel.cn/api/paas/v4/embeddings"
ZHIPU_MODEL = os.environ.get("DSH_RAG_ZHIPU_MODEL") or "embedding-3"
ZHIPU_DIM = int(os.environ.get("DSH_RAG_ZHIPU_DIM") or 1024)
ZHIPU_BATCH = 32          # 官方上限 64，留余量
ZHIPU_TIMEOUT = 30
ZHIPU_RETRIES = 2

LOCAL_MODEL = "BAAI/bge-small-zh-v1.5"
LOCAL_DIM = 512

if BACKEND == "local":
    DIM = LOCAL_DIM
    MODEL_NAME = f"fastembed/{LOCAL_MODEL}"
else:
    DIM = ZHIPU_DIM
    MODEL_NAME = f"zhipu/{ZHIPU_MODEL}@{ZHIPU_DIM}"

_error: str | None = None
_cache: dict[str, list[float]] = {}


def error() -> str | None:
    return _error


# ------------------------------------------------------------------ key 解析
def _read_registry() -> str | None:
    """DSH 桌面端是登录时启动的，未必继承后来设置的用户环境变量，所以回退到注册表。"""
    try:
        import winreg
    except ImportError:
        return None
    names = ("ZHIPUAI_API_KEY", "ZHIPU_API_KEY", "BIGMODEL_API_KEY")
    for root in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
        try:
            key = winreg.OpenKey(root, r"Environment")
        except OSError:
            continue
        for name in names:
            try:
                val, _ = winreg.QueryValueEx(key, name)
                if val and str(val).strip():
                    return str(val).strip()
            except OSError:
                continue
    return None


_KEY: str | None = None


def get_key() -> str | None:
    global _KEY, _error
    if _KEY:
        return _KEY
    for name in ("ZHIPUAI_API_KEY", "ZHIPU_API_KEY", "BIGMODEL_API_KEY"):
        v = os.environ.get(name)
        if v and v.strip():
            _KEY = v.strip()
            return _KEY
    v = _read_registry()
    if v:
        _KEY = v
        return _KEY
    f = ROOT / "data" / ".zhipu_key"
    if f.is_file():
        try:
            _KEY = f.read_text(encoding="utf-8").strip()
            return _KEY
        except OSError:
            pass
    _error = "找不到智谱 API key（环境变量 / 注册表 / data/.zhipu_key 都没有）"
    return None


# ------------------------------------------------------------------ 智谱后端
def _zhipu_embed(batch: list[str]) -> list[list[float]]:
    key = get_key()
    if not key:
        raise RuntimeError(_error or "缺少 key")

    body = json.dumps(
        {"model": ZHIPU_MODEL, "input": batch, "dimensions": ZHIPU_DIM},
        ensure_ascii=False,
    ).encode("utf-8")
    req = urllib.request.Request(
        ZHIPU_URL,
        data=body,
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
        method="POST",
    )

    last_exc: Exception | None = None
    for attempt in range(ZHIPU_RETRIES + 1):
        try:
            with urllib.request.urlopen(req, timeout=ZHIPU_TIMEOUT) as resp:
                obj = json.loads(resp.read().decode("utf-8"))
            items = obj.get("data") or []
            if len(items) != len(batch):
                raise RuntimeError(f"返回条数不匹配：{len(items)} != {len(batch)}")
            items.sort(key=lambda x: x.get("index", 0))
            return [list(map(float, it["embedding"])) for it in items]
        except Exception as exc:  # noqa: BLE001
            last_exc = exc
            if attempt < ZHIPU_RETRIES:
                time.sleep(0.6 * (attempt + 1))
    raise RuntimeError(f"智谱 embedding 失败: {type(last_exc).__name__}: {last_exc}")


def _local_embed(texts: list[str]) -> list[list[float]]:
    from fastembed import TextEmbedding

    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    os.environ.setdefault("HF_ENDPOINT", "https://hf-mirror.com")
    model = TextEmbedding(model_name=LOCAL_MODEL, cache_dir=str(CACHE_DIR), threads=4)
    return [v.tolist() for v in model.embed(texts)]


# ------------------------------------------------------------------ 对外接口
def embed(texts: list[str]) -> list[list[float]] | None:
    """批量向量化。失败返回 None，调用方降级为纯关键词检索。"""
    global _error
    if not texts:
        return []

    todo: list[str] = []
    keymap: list[str] = []
    for t in texts:
        k = t[:4000]
        if k in _cache:
            continue
        todo.append(t)
        keymap.append(k)

    if todo:
        try:
            if BACKEND == "local":
                vecs = _local_embed(todo)
            else:
                vecs = []
                for i in range(0, len(todo), ZHIPU_BATCH):
                    vecs.extend(_zhipu_embed(todo[i : i + ZHIPU_BATCH]))
            if len(vecs) != len(todo):
                raise RuntimeError("向量条数不匹配")
            for k, v in zip(keymap, vecs):
                _cache[k] = v
            _error = None
        except Exception as exc:  # noqa: BLE001
            _error = f"{type(exc).__name__}: {exc}"
            return None

    out: list[list[float]] = []
    for t in texts:
        v = _cache.get(t[:4000])
        if v is None:
            return None
        out.append(v)
    return out


def probe() -> dict:
    """自检：返回后端配置与一次真实调用结果。"""
    info = {
        "backend": BACKEND,
        "model": MODEL_NAME,
        "dim": DIM,
        "hasKey": bool(get_key()),
        "sampleDim": None,
        "error": None,
    }
    vecs = embed(["连通性自检"])
    if vecs:
        info["sampleDim"] = len(vecs[0])
    else:
        info["error"] = _error
    return info
