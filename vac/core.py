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


def max_line_bytes(log_path: Path) -> int:
    """Largest single JSONL entry in bytes. A huge single entry (one runaway
    tool output or embedded image) is the signature of a context-bomb session."""
    biggest = 0
    try:
        with log_path.open("rb") as fh:
            for line in fh:
                if len(line) > biggest:
                    biggest = len(line)
    except OSError:
        return 0
    return biggest


@dataclass
class CleanResult:
    total_images: int
    removed: int
    kept: int
    before_bytes: int
    after_bytes: int
    backup: Path | None
    dry_run: bool


def _transform(obj, pred: Predicate, replace, state: dict):
    """Replace image blocks with a placeholder unless they fall in the keep
    window (the last ``keep`` images encountered, by document order)."""
    if isinstance(obj, dict):
        if pred(obj):
            state["seen"] += 1
            # keep the final `keep` images: index >= total - keep
            if state["seen"] > state["total"] - state["keep"]:
                return obj  # kept
            state["removed"] += 1
            return replace(obj, state["note"])
        return {k: _transform(v, pred, replace, state) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_transform(v, pred, replace, state) for v in obj]
    return obj


def clean_images(
    log_path: Path,
    pred: Predicate,
    replace,
    keep: int = 0,
    dry_run: bool = True,
    backup: bool = True,
    note: str = "[image cleared by vac to reduce session size]",
) -> CleanResult:
    """GC image blocks in a session log, retaining the last ``keep`` images.

    ``replace(obj, note)`` returns the schema-correct text placeholder for a
    matched image block. keep=0 strips all images. When ``dry_run`` no file is
    written (after_bytes is computed from the transformed content in memory).
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
            out_lines.append(json.dumps(_transform(obj, pred, replace, state),
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


# --- Region-based pruning (the "compact only the oldest N%" operation) --------

@dataclass
class PruneResult:
    total_entries: int
    region_entries: int
    images_removed: int
    outputs_truncated: int
    before_bytes: int
    after_bytes: int
    backup: Path | None
    dry_run: bool
    valid: bool


def _truncate(s: str, max_bytes: int) -> tuple[str, int]:
    """Truncate to head + tail with a marker; returns (new, chars_saved)."""
    if len(s) <= max_bytes:
        return s, 0
    head = (max_bytes * 2) // 3
    tail = max_bytes - head
    saved = len(s) - head - tail
    return (s[:head] + f"\n…[vac pruned {saved} chars of old tool output]…\n" + s[-tail:]), saved


def prune_oldest(
    log_path: Path,
    is_image: Predicate,
    replace_image,
    oldest_pct: float,
    max_field_bytes: int = 2000,
    dry_run: bool = True,
    backup: bool = True,
    note: str = "[image cleared by vac (old-region prune)]",
) -> PruneResult:
    """Clean the OLDEST ``oldest_pct`` percent of a session's entries, keeping
    the recent remainder verbatim. In the old region:
      - image blocks (any form) are neutralized;
      - inside ToolResults entries, large text payloads are truncated.
    User prompts and assistant text are preserved (only bulky tool output/images
    are shed), so the conversation narrative survives. Deterministic, non-lossy
    for the dialogue. Output is JSON-validated before writing.
    """
    lines = log_path.read_text().splitlines()
    entry_idxs = [i for i, l in enumerate(lines) if l.strip()]
    total = len(entry_idxs)
    cutoff = int(total * max(0.0, min(100.0, oldest_pct)) / 100.0)
    region = set(entry_idxs[:cutoff])
    before = log_path.stat().st_size

    imgs = 0
    trunc = 0

    def walk(x, in_tr: bool):
        nonlocal imgs, trunc
        if isinstance(x, dict):
            if is_image(x):
                imgs += 1
                return replace_image(x, note)
            new = {}
            for k, v in x.items():
                # Truncate bulky tool-output text, only within ToolResults.
                if in_tr and k == "data" and isinstance(v, str) and x.get("kind") == "text" \
                        and len(v) > max_field_bytes:
                    nv, saved = _truncate(v, max_field_bytes)
                    if saved:
                        trunc += 1
                    new[k] = nv
                elif in_tr and k == "Text" and isinstance(v, str) and len(v) > max_field_bytes \
                        and set(x.keys()) == {"Text"}:
                    nv, saved = _truncate(v, max_field_bytes)
                    if saved:
                        trunc += 1
                    new[k] = nv
                else:
                    new[k] = walk(v, in_tr)
            return new
        if isinstance(x, list):
            return [walk(v, in_tr) for v in x]
        return x

    out_lines = []
    for i, raw in enumerate(lines):
        if i not in region or not raw.strip():
            out_lines.append(raw)
            continue
        try:
            obj = json.loads(raw)
        except json.JSONDecodeError:
            out_lines.append(raw)
            continue
        out_lines.append(json.dumps(walk(obj, obj.get("kind") == "ToolResults"),
                                    ensure_ascii=False))

    new_text = "\n".join(out_lines) + "\n"
    after = len(new_text.encode("utf-8"))

    # Schema/validity guard: every non-empty output line must parse as JSON.
    valid = True
    for l in out_lines:
        if l.strip():
            try:
                json.loads(l)
            except json.JSONDecodeError:
                valid = False
                break

    bkp = None
    if not dry_run and valid and (imgs or trunc):
        if backup:
            bkp = log_path.with_suffix(log_path.suffix + ".bak")
            shutil.copy2(log_path, bkp)
        log_path.write_text(new_text, encoding="utf-8")

    return PruneResult(total, cutoff, imgs, trunc, before, after, bkp, dry_run, valid)


# --- Age filtering & archiving -----------------------------------------------

import re as _re
import tarfile as _tarfile
from datetime import datetime, timezone, timedelta


def parse_duration(s: str) -> timedelta:
    """Parse a duration like '60d', '2w', '12h', '30m'. Bare number = days."""
    m = _re.fullmatch(r"\s*(\d+)\s*([dhwm]?)\s*", str(s).lower())
    if not m:
        raise ValueError(f"bad duration {s!r} — use e.g. 60d, 2w, 12h, 30m")
    n = int(m.group(1))
    unit = m.group(2) or "d"
    return {"d": timedelta(days=n), "w": timedelta(weeks=n),
            "h": timedelta(hours=n), "m": timedelta(minutes=n)}[unit]


def _parse_ts(ts: str | None):
    if not ts:
        return None
    try:
        return datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except ValueError:
        return None


def age_days(updated_at: str | None, path: Path) -> float:
    """Age in days from the session's real last-used time (updated_at), falling
    back to the file mtime only when no metadata timestamp is available.

    Using updated_at avoids misclassifying sessions that vac itself rewrote
    (which bumps mtime) as 'recent'.
    """
    dt = _parse_ts(updated_at)
    if dt is None:
        try:
            dt = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
        except OSError:
            return 0.0
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - dt).total_seconds() / 86400.0


def session_file_group(log_path: Path) -> list[Path]:
    """All on-disk files belonging to a session (log + metadata + history +
    lock + any .bak), matched by the id stem in the same directory."""
    sid = log_path.name[:-len(".jsonl")] if log_path.name.endswith(".jsonl") else log_path.stem
    return sorted(p for p in log_path.parent.glob(sid + "*") if p.is_file())


def archive_files(groups: list[list[Path]], out_path: Path, remove: bool) -> tuple[int, int]:
    """tar.gz the given file groups; optionally delete originals afterward.
    Returns (files_archived, total_bytes). Originals are only removed after the
    archive is written successfully."""
    n = 0
    total = 0
    with _tarfile.open(out_path, "w:gz") as tar:
        for grp in groups:
            for f in grp:
                if f.exists():
                    tar.add(f, arcname=f.name)
                    n += 1
                    total += f.stat().st_size
    if remove:
        for grp in groups:
            for f in grp:
                try:
                    f.unlink()
                except OSError:
                    pass
    return n, total
