"""Download the ONNX-quantized MiniLM embedder into backend/memory/models/.

Idempotent — skips files that already exist. Run during build/packaging
(scripts/bundle_python.py calls this) and optionally by hand. The app is
safe without it: backend/memory/embedding.py falls back to transformers
fp32, then to hash embeddings.
"""

from __future__ import annotations

import sys
import urllib.request
from pathlib import Path

REPO = "https://huggingface.co/Xenova/all-MiniLM-L6-v2/resolve/main"
FILES = [
    ("onnx/model_quantized.onnx", "model.onnx"),
    ("tokenizer.json", "tokenizer.json"),
    ("tokenizer_config.json", "tokenizer_config.json"),
]


def main() -> int:
    root = Path(__file__).resolve().parent.parent
    out = root / "backend" / "memory" / "models" / "minilm-l6-v2"
    out.mkdir(parents=True, exist_ok=True)

    ok = True
    for remote, local in FILES:
        dest = out / local
        if dest.exists() and dest.stat().st_size > 1000:
            print(f"skip {local} (exists)")
            continue
        url = f"{REPO}/{remote}"
        print(f"fetch {url}")
        try:
            req = urllib.request.Request(url, headers={
                "User-Agent": "Addled/1.0 (local embedder fetcher)"})
            with urllib.request.urlopen(req, timeout=180) as resp:
                data = resp.read()
            dest.write_bytes(data)
            print(f"  -> {dest} ({len(data) // 1024} KB)")
        except Exception as e:
            print(f"FAILED {local}: {e}")
            ok = False

    print("EMBEDDER FETCH OK" if ok else
          "EMBEDDER FETCH INCOMPLETE (hash/transformers fallback will be used)")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
