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

import hashlib
import json
import os
import time
import urllib.request
from collections import OrderedDict
from pathlib import Path

import platform_info

ROOT = Path(__file__).resolve().parent.parent
CACHE_DIR = ROOT / "data" / "models"

BACKEND = (os.environ.get("DSH_RAG_EMBED") or "zhipu").strip().lower()

ZHIPU_URL = "https://open.bigmodel.cn/api/paas/v4/embeddings"
ZHIPU_MODEL = os.environ.get("DSH_RAG_ZHIPU_MODEL") or "embedding-3"
ZHIPU_DIM = int(os.environ.get("DSH_RAG_ZHIPU_DIM") or 1024)
ZHIPU_BATCH = 32          # 官方上限 64，留余量
# 工具调用是**同步**的：一次 30s 超时 × 3 次会把整个 MCP 卡住近 90 秒，
# 比「失败后降级成关键词」难受得多。10s × 2 次足够覆盖正常的网络抖动。
ZHIPU_TIMEOUT = 10
ZHIPU_RETRIES = 1
# 输入窗口：智谱 embedding-3 约 3072 token，超长要切。缓存键也按同一个窗口算。
MAX_CHARS = 4000
CACHE_MAX = 512

LOCAL_MODEL = "BAAI/bge-small-zh-v1.5"
LOCAL_DIM = 512

if BACKEND == "local":
    DIM = LOCAL_DIM
    MODEL_NAME = f"fastembed/{LOCAL_MODEL}"
else:
    DIM = ZHIPU_DIM
    MODEL_NAME = f"zhipu/{ZHIPU_MODEL}@{ZHIPU_DIM}"

_error: str | None = None
# 进程内缓存：只存最近用到的若干条（LRU），长跑的服务不会无限涨内存
_cache: OrderedDict[str, list[float]] = OrderedDict()


def _key(text: str) -> str:
    """缓存键：按「实际送进模型的那一段」算，截断后相同就是同一条。"""
    return hashlib.sha1(text[:MAX_CHARS].encode("utf-8")).hexdigest()


def error() -> str | None:
    return _error


# ------------------------------------------------------------------ key 解析
_KEY: str | None = None
_KEY_NAMES = ("ZHIPUAI_API_KEY", "ZHIPU_API_KEY", "BIGMODEL_API_KEY")


def get_key() -> str | None:
    """密钥来源收在 platform_info 里：环境变量 → 平台存储 → data/.zhipu_key。

    以前这里自己碰 winreg，等于把 Windows 假设写进了向量层；换到 Linux 时
    那段代码不会报错，只会永远返回 None。
    """
    global _KEY, _error
    if _KEY:
        return _KEY
    _KEY = platform_info.get_secret(*_KEY_NAMES, fallback_file=ROOT / "data" / ".zhipu_key")
    if _KEY:
        return _KEY
    _error = (
        "找不到智谱 API key（找过："
        + "、".join(platform_info.describe_sources(*_KEY_NAMES))
        + "、data/.zhipu_key）"
    )
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
    if BACKEND == "off":
        # 自测 / 离线：完全不发网络请求，检索退回纯关键词。
        # 自测脚本设 DSH_RAG_EMBED=off，跑起来不花钱也不被网络拖慢。
        return None

    todo: list[tuple[str, str]] = []
    for t in texts:
        k = _key(t)
        if k not in _cache:
            todo.append((k, t))

    if todo:
        try:
            payload = [t[:MAX_CHARS] for _, t in todo]
            if BACKEND == "local":
                vecs = _local_embed(payload)
            else:
                vecs = []
                for i in range(0, len(payload), ZHIPU_BATCH):
                    vecs.extend(_zhipu_embed(payload[i : i + ZHIPU_BATCH]))
            if len(vecs) != len(payload):
                raise RuntimeError("向量条数不匹配")
            for (k, _), v in zip(todo, vecs):
                _cache[k] = v
                _cache.move_to_end(k)
            while len(_cache) > CACHE_MAX:
                _cache.popitem(last=False)
            _error = None
        except Exception as exc:  # noqa: BLE001
            _error = f"{type(exc).__name__}: {exc}"
            return None

    out: list[list[float]] = []
    for t in texts:
        k = _key(t)
        v = _cache.get(k)
        if v is None:
            return None
        _cache.move_to_end(k)
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
