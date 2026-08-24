"""Schema-agnostic session surgery: count images, GC them (keep last N), with
backup + dry-run. Adapters supply the ``is_image_block`` predicate and the
``placeholder`` factory; everything here works on parsed JSONL lines.
"""
from __future__ import annotations

import json
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

Predicate = Callable[[dict], bool]


def _walk_count(obj, pred: Predicate) -> int:
    n = 0
    if isinstance(obj, dict):
        if pred(obj):
            return 1  # count the image block as a unit; don't descend into it
        for v in obj.values():
            n += _walk_count(v, pred)
    elif isinstance(obj, list):
        for v in obj:
            n += _walk_count(v, pred)
    return n


def count_images(log_path: Path, pred: Predicate) -> int:
    total = 0
    try:
        with log_path.open() as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    total += _walk_count(json.loads(line), pred)
                except json.JSONDecodeError:
                    continue
    except OSError:
        return 0
    return total


@dataclass
class CleanResult:
    total_images: int
    removed: int
    kept: int
    before_bytes: int
    after_bytes: int
    backup: Path | None
    dry_run: bool


def _transform(obj, pred: Predicate, placeholder, state: dict):
    """Replace image blocks with a placeholder unless they fall in the keep
    window (the last ``keep`` images encountered, by document order)."""
    if isinstance(obj, dict):
        if pred(obj):
            state["seen"] += 1
            # keep the final `keep` images: index >= total - keep
            if state["seen"] > state["total"] - state["keep"]:
                return obj  # kept
            state["removed"] += 1
            return placeholder(state["note"])
        return {k: _transform(v, pred, placeholder, state) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_transform(v, pred, placeholder, state) for v in obj]
    return obj


def clean_images(
    log_path: Path,
    pred: Predicate,
    placeholder,
    keep: int = 0,
    dry_run: bool = True,
    backup: bool = True,
    note: str = "[image cleared by vac to reduce session size]",
) -> CleanResult:
    """GC image blocks in a session log, retaining the last ``keep`` images.

    keep=0 strips all images. Returns a CleanResult; when ``dry_run`` no file is
    written (after_bytes is estimated from the transformed content in memory).
    """
    before = log_path.stat().st_size
    total = count_images(log_path, pred)
    state = {"seen": 0, "removed": 0, "total": total, "keep": max(0, keep), "note": note}

    out_lines: list[str] = []
    with log_path.open() as fh:
        for line in fh:
            raw = line.rstrip("\n")
            if not raw:
                out_lines.append("")
                continue
            try:
                obj = json.loads(raw)
            except json.JSONDecodeError:
                out_lines.append(raw)  # leave non-JSON lines untouched
                continue
            out_lines.append(json.dumps(_transform(obj, pred, placeholder, state),
                                        ensure_ascii=False))

    new_text = "\n".join(out_lines) + "\n"
    after = len(new_text.encode("utf-8"))

    bkp = None
    if not dry_run and state["removed"] > 0:
        if backup:
            bkp = log_path.with_suffix(log_path.suffix + ".bak")
            shutil.copy2(log_path, bkp)
        log_path.write_text(new_text, encoding="utf-8")

    return CleanResult(
        total_images=total,
        removed=state["removed"],
        kept=total - state["removed"],
        before_bytes=before,
        after_bytes=after,
        backup=bkp,
        dry_run=dry_run,
    )
