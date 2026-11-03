import sys, os
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
import embed
print("HF_ENDPOINT =", os.environ.get("HF_ENDPOINT"))
print("cache dir   =", embed.CACHE_DIR)
v = embed.embed(["中文向量测试", "RAG 记忆库"])
if v is None:
    print("EMBED FAILED:", embed.error())
    sys.exit(1)
print("dim =", len(v[0]))
print("head =", [round(x, 4) for x in v[0][:5]])
import numpy as np
a, b = np.array(v[0]), np.array(v[1])
print("cos =", float(a @ b / (np.linalg.norm(a)*np.linalg.norm(b))))
