"""Ordered output.csv writer with the exact required columns."""

from __future__ import annotations

import csv
from pathlib import Path
from typing import Iterable

from ..data.models import OUTPUT_COLUMNS
from ..policy.consistency import OutputRow


def write_output_csv(rows: Iterable[OutputRow], path: Path) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    n = 0
    with path.open("w", encoding="utf-8", newline="") as fh:
        w = csv.writer(fh, lineterminator="\n")
        w.writerow(OUTPUT_COLUMNS)
        for r in rows:
            w.writerow(r.as_list())
            n += 1
    return n
