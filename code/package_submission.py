"""Build <repo>/code.zip for submission.

Includes: code/ (without caches, run logs, bytecode), requirements.txt, .env.example, docs/,
VALIDATION.md, and the root README.md. Excludes .env, dataset/, output.csv, log.txt.

    python code/package_submission.py
"""

from __future__ import annotations

import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "code.zip"
EXCLUDE_DIRS = {".cache", ".runs", "__pycache__", ".pytest_cache"}
EXCLUDE_FILES = {"sample_output.csv"}


def _include(p: Path) -> bool:
    if any(part in EXCLUDE_DIRS for part in p.parts):
        return False
    if p.name in EXCLUDE_FILES or p.suffix in (".pyc", ".pyo"):
        return False
    return True


def main() -> int:
    if OUT.exists():
        OUT.unlink()
    n = 0
    with zipfile.ZipFile(OUT, "w", zipfile.ZIP_DEFLATED) as z:
        for base in (ROOT / "code", ROOT / "docs"):
            for p in sorted(base.rglob("*")):
                if p.is_file() and _include(p):
                    z.write(p, p.relative_to(ROOT).as_posix())
                    n += 1
        for f in ("requirements.txt", ".env.example", "README.md", "VALIDATION.md", "problem_statement.md"):
            p = ROOT / f
            if p.exists():
                z.write(p, f)
                n += 1
    print(f"wrote {OUT} with {n} files ({OUT.stat().st_size / 1024:.0f} KB)")
    with zipfile.ZipFile(OUT) as z:
        names = z.namelist()
    for must in ("code/main.py", "code/evaluation/usage_report.md", "code/README.md", "requirements.txt"):
        if must not in names:
            print(f"MISSING: {must}", file=sys.stderr)
            return 1
    if any(n_.endswith("/.env") or n_ == ".env" for n_ in names):
        print("ERROR: .env must not be packaged", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
