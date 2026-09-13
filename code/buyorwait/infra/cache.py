"""Content-addressed JSON cache for model outputs (CLAUDE.md §23).

Key = sha256(prompt_version | schema_version | model | canonical input | image sha256).
Caching never changes correctness: the cached value is the validated model output that would
have been produced by the same inputs. Hits are counted so the usage report can state real calls.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Optional


class JsonCache:
    def __init__(self, root: Path, enabled: bool = True):
        self.root = Path(root)
        self.enabled = enabled
        self.hits = 0
        self.misses = 0
        if enabled:
            self.root.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def key(*parts: Any) -> str:
        h = hashlib.sha256()
        for p in parts:
            if isinstance(p, bytes):
                h.update(hashlib.sha256(p).hexdigest().encode())
            else:
                h.update(json.dumps(p, sort_keys=True, default=str).encode("utf-8"))
            h.update(b"\x1f")
        return h.hexdigest()

    def _path(self, key: str) -> Path:
        return self.root / key[:2] / f"{key}.json"

    def get(self, key: str) -> Optional[dict]:
        if not self.enabled:
            return None
        p = self._path(key)
        if p.is_file():
            try:
                self.hits += 1
                return json.loads(p.read_text(encoding="utf-8"))
            except Exception:
                return None
        self.misses += 1
        return None

    def put(self, key: str, value: dict) -> None:
        if not self.enabled:
            return
        p = self._path(key)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(value, default=str), encoding="utf-8")

    def clear(self) -> int:
        n = 0
        if self.root.exists():
            for f in self.root.rglob("*.json"):
                f.unlink()
                n += 1
        return n
