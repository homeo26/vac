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


def build_image_path_map(log_path: Path) -> dict:
    """Map toolUseId -> [source file paths] for images read from files
    (read/FileRead in Image mode). Lets us tell the model where to re-read a
    cleared image from, instead of losing it entirely."""
    m: dict[str, list] = {}

    def collect(o):
        if isinstance(o, dict):
            tid = o.get("toolUseId")
            if isinstance(tid, str):
                paths: list[str] = []

                def fp(y):
                    if isinstance(y, dict):
                        if y.get("mode") == "Image":
                            for key in ("image_paths", "paths"):
                                if isinstance(y.get(key), list):
                                    paths.extend(p for p in y[key] if isinstance(p, str))
                        for v in y.values():
                            fp(v)
                    elif isinstance(y, list):
                        for v in y:
                            fp(v)
                fp(o)
                if paths:
                    m[tid] = paths
            for v in o.values():
                collect(v)
        elif isinstance(o, list):
            for v in o:
                collect(v)

    try:
        with log_path.open() as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    collect(json.loads(line))
                except json.JSONDecodeError:
                    continue
    except OSError:
        pass
    return m


def _child_tid(parent_tid, key):
    """Track the enclosing toolUseId while recursing — the results map is keyed
    by toolUseId, so a key like 'tooluse_…' / 'toolu_…' scopes its subtree."""
    if isinstance(key, str) and (key.startswith("tooluse") or key.startswith("toolu_")):
        return key
    return parent_tid


def _img_note(default_note: str, id2paths: dict, tid) -> str:
    paths = id2paths.get(tid) if tid else None
    if paths:
        p = paths[0] if len(paths) == 1 else ", ".join(paths)
        # Passive on purpose: record the source for recovery, but explicitly
        # discourage auto re-reading (which would re-inflate the context).
        return f"[image removed by vac to save context (source: {p}); do not re-load unless the user explicitly asks]"
    return default_note


def _transform(obj, pred: Predicate, replace, state: dict, tid=None):
    """Replace image blocks with a placeholder unless they fall in the keep
    window (the last ``keep`` images encountered, by document order). The
    placeholder embeds the image's source file path when it can be resolved."""
    if isinstance(obj, dict):
        if pred(obj):
            state["seen"] += 1
            # keep the final `keep` images: index >= total - keep
            if state["seen"] > state["total"] - state["keep"]:
                return obj  # kept
            state["removed"] += 1
            return replace(obj, _img_note(state["note"], state.get("id2paths", {}), tid))
        this_tid = obj["toolUseId"] if isinstance(obj.get("toolUseId"), str) else tid
        return {k: _transform(v, pred, replace, state, _child_tid(this_tid, k))
                for k, v in obj.items()}
    if isinstance(obj, list):
        return [_transform(v, pred, replace, state, tid) for v in obj]
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
    state = {"seen": 0, "removed": 0, "total": total, "keep": max(0, keep), "note": note,
             "id2paths": build_image_path_map(log_path)}

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
    total_tokens: int
    target_tokens: int
    freed_tokens: int
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


_IMG_TOKENS = 1500  # rough per-image token cost the model sees (not the file bytes)


def entry_tokens(o) -> int:
    """Approximate model-visible tokens for one log entry (chars/4 of text +
    a flat cost per image). Handles BOTH Kiro shapes (kind:text/data, Text,
    toolUse) and Claude Code shapes (type:text/text, thinking, string
    message.content, type:image). This is what the context % counts — NOT
    file bytes."""
    chars = [0]
    imgs = [0]
    def c(x):
        if isinstance(x, dict):
            # Kiro shapes
            if x.get("kind") == "text" and isinstance(x.get("data"), str):
                chars[0] += len(x["data"])
            if x.get("kind") == "thinking" and isinstance(x.get("text"), str):
                chars[0] += len(x["text"])
            if set(x.keys()) == {"Text"} and isinstance(x["Text"], str):
                chars[0] += len(x["Text"])
            if x.get("kind") == "toolUse" and isinstance(x.get("input"), (dict, list)):
                chars[0] += len(json.dumps(x["input"]))
            # Claude Code shapes
            if x.get("type") == "text" and isinstance(x.get("text"), str):
                chars[0] += len(x["text"])
            if x.get("type") == "thinking" and isinstance(x.get("thinking"), str):
                chars[0] += len(x["thinking"])
            if isinstance(x.get("content"), str):  # Claude message.content as string
                chars[0] += len(x["content"])
            # images (both schemas)
            if x.get("kind") == "image" or x.get("type") == "image" \
                    or ("Image" in x and isinstance(x.get("Image"), dict)):
                imgs[0] += 1
            for v in x.values():
                c(v)
        elif isinstance(x, list):
            for v in x:
                c(v)
    c(o)
    return chars[0] // 4 + imgs[0] * _IMG_TOKENS


def _clean_entry(o, is_image, replace_image, mode, max_field_bytes, note, counters, id2paths=None):
    """Return a cleaned copy of one entry. Schema-aware for Kiro AND Claude:
      'outputs' — truncate long tool-output text + strip images, KEEP prompt/assistant text
      'hard'    — also collapse assistant/prompt text to stubs (guarantees the target)
    Image placeholders embed the source file path when resolvable (id2paths)."""
    id2paths = id2paths or {}
    if mode == "hard":
        def gut(x, tid=None):
            if isinstance(x, dict):
                if is_image(x):
                    counters["img"] += 1
                    return replace_image(x, _img_note(note, id2paths, tid))
                if x.get("kind") == "thinking":
                    return {"kind": "thinking", "text": "", "signature": x.get("signature", ""),
                            "redactedContent": x.get("redactedContent")}
                if x.get("type") == "thinking":
                    return {**x, "thinking": ""}
                new = {}
                this_tid = x["toolUseId"] if isinstance(x.get("toolUseId"), str) else tid
                for k, v in x.items():
                    # Kiro text node: {kind:text, data:str}
                    if k == "data" and x.get("kind") == "text" and isinstance(v, str) and len(v) > 40:
                        counters["trunc"] += 1; new[k] = "[pruned]"
                    # Claude text node: {type:text, text:str}
                    elif k == "text" and x.get("type") == "text" and isinstance(v, str) and len(v) > 40:
                        counters["trunc"] += 1; new[k] = "[pruned]"
                    # Claude message.content as a bare string
                    elif k == "content" and isinstance(v, str) and len(v) > 40:
                        counters["trunc"] += 1; new[k] = "[pruned]"
                    else:
                        new[k] = gut(v, _child_tid(this_tid, k))
                return new
            if isinstance(x, list):
                return [gut(v, tid) for v in x]
            return x
        # ToolResults entries: gut the whole payload (Kiro)
        if o.get("kind") == "ToolResults":
            counters["trunc"] += 1
            return {"kind": "ToolResults", "data": {"content": [
                {"kind": "text", "data": "[old turn pruned by vac to free context]"}]}}
        return gut(o)

    # mode == 'outputs' — truncate long tool-output text (both schemas), strip images
    def walk(x, in_tr, tid=None):
        if isinstance(x, dict):
            if is_image(x):
                counters["img"] += 1
                return replace_image(x, _img_note(note, id2paths, tid))
            here_tr = in_tr or x.get("kind") == "ToolResults" or x.get("type") == "tool_result"
            this_tid = x["toolUseId"] if isinstance(x.get("toolUseId"), str) else tid
            new = {}
            for k, v in x.items():
                if here_tr and k == "data" and x.get("kind") == "text" \
                        and isinstance(v, str) and len(v) > max_field_bytes:
                    nv, saved = _truncate(v, max_field_bytes)
                    if saved: counters["trunc"] += 1
                    new[k] = nv
                elif here_tr and k == "text" and x.get("type") == "text" \
                        and isinstance(v, str) and len(v) > max_field_bytes:
                    nv, saved = _truncate(v, max_field_bytes)
                    if saved: counters["trunc"] += 1
                    new[k] = nv
                elif here_tr and set(x.keys()) == {"Text"} and k == "Text" \
                        and isinstance(v, str) and len(v) > max_field_bytes:
                    nv, saved = _truncate(v, max_field_bytes)
                    if saved: counters["trunc"] += 1
                    new[k] = nv
                else:
                    new[k] = walk(v, here_tr, _child_tid(this_tid, k))
            return new
        if isinstance(x, list):
            return [walk(v, in_tr, tid) for v in x]
        return x
    return walk(o, o.get("kind") == "ToolResults")


def prune_oldest(
    log_path: Path,
    is_image: Predicate,
    replace_image,
    oldest_pct: float,
    mode: str = "outputs",
    max_field_bytes: int = 2000,
    dry_run: bool = True,
    backup: bool = True,
    note: str = "[image cleared by vac (old-region prune)]",
) -> PruneResult:
    """Free approximately ``oldest_pct`` percent of the session's CONTEXT TOKENS
    from the oldest side. Walks entries oldest→newest, cleaning each until the
    freed-token target is reached, then stops (recent turns untouched).

    mode 'outputs' (default): drop tool-result bodies / strip images / truncate
    old tool text — keeps prompts + assistant text (may free < target if the old
    region is text-heavy). mode 'hard': also collapse old assistant/prompt text,
    guaranteeing it reaches the target (loses old detail). Non-empty user prompts
    are always kept as anchors. JSON-validated before writing.
    """
    lines = log_path.read_text().splitlines()
    before = log_path.stat().st_size

    parsed = []  # (line_index, obj|None, tokens)
    for i, l in enumerate(lines):
        if not l.strip():
            parsed.append((i, None, 0))
            continue
        try:
            o = json.loads(l)
            parsed.append((i, o, entry_tokens(o)))
        except json.JSONDecodeError:
            parsed.append((i, None, 0))

    total_tokens = sum(t for _, _, t in parsed)
    target = int(total_tokens * max(0.0, min(100.0, oldest_pct)) / 100.0)

    counters = {"img": 0, "trunc": 0}
    freed = 0
    region = 0
    out_lines = list(lines)
    id2paths = build_image_path_map(log_path)

    for idx, (i, o, tok) in enumerate(parsed):
        if o is None:
            continue
        if freed >= target:
            break
        # Always keep a user prompt intact as an anchor (small, high value).
        if o.get("kind") == "Prompt":
            continue
        cleaned = _clean_entry(o, is_image, replace_image, mode, max_field_bytes, note, counters, id2paths)
        new_tok = entry_tokens(cleaned)
        if new_tok < tok:
            freed += (tok - new_tok)
            region += 1
            out_lines[i] = json.dumps(cleaned, ensure_ascii=False)

    new_text = "\n".join(out_lines) + "\n"
    after = len(new_text.encode("utf-8"))

    valid = True
    for l in out_lines:
        if l.strip():
            try:
                json.loads(l)
            except json.JSONDecodeError:
                valid = False
                break

    bkp = None
    if not dry_run and valid and (counters["img"] or counters["trunc"]):
        if backup:
            bkp = log_path.with_suffix(log_path.suffix + ".bak")
            shutil.copy2(log_path, bkp)
        log_path.write_text(new_text, encoding="utf-8")

    return PruneResult(len(parsed), region, counters["img"], counters["trunc"],
                       before, after, total_tokens, target, freed, bkp, dry_run, valid)


# --- Age filtering & archiving -----------------------------------------------

import re as _re
import tarfile as _tarfile
import shlex as _shlex
import subprocess as _subprocess
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


# --- AI session naming --------------------------------------------------------

def _readable_text(o) -> str:
    """Best-effort readable text from one entry (Kiro + Claude shapes)."""
    parts = []
    def grab(x):
        if isinstance(x, dict):
            if x.get("kind") == "text" and isinstance(x.get("data"), str):
                parts.append(x["data"])
            if x.get("type") == "text" and isinstance(x.get("text"), str):
                parts.append(x["text"])
            if isinstance(x.get("content"), str):
                parts.append(x["content"])
            for v in x.values():
                grab(v)
        elif isinstance(x, list):
            for v in x:
                grab(v)
    grab(o)
    return " ".join(t.strip() for t in parts if t and t.strip())


def build_digest(log_path: Path, max_chars: int = 3000) -> str:
    """Compact readable transcript from the start of a session, for titling."""
    out = []
    used = 0
    try:
        with log_path.open() as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    o = json.loads(line)
                except json.JSONDecodeError:
                    continue
                txt = _readable_text(o)
                if not txt:
                    continue
                txt = " ".join(txt.split())  # collapse whitespace
                out.append(txt[:600])
                used += len(txt[:600])
                if used >= max_chars:
                    break
    except OSError:
        return ""
    return "\n".join(out)[:max_chars]


TITLE_PROMPT = (
    "You are titling a coding-assistant chat session, like the auto-titles in the "
    "ChatGPT or Claude sidebar. Read the conversation below and output ONE concise, "
    "specific title: 3-7 words, Title Case, no surrounding quotes, no trailing "
    "punctuation, no preamble. Describe the concrete task/topic. Output ONLY the title.\n\n"
    "--- conversation ---\n{digest}\n--- end ---\nTitle:"
)


def _sanitize_title(raw: str) -> str:
    t = (raw or "").strip()
    # take the last non-empty line (skip any tool preamble on earlier lines)
    lines = [l.strip() for l in t.splitlines() if l.strip()]
    if lines:
        t = lines[-1]
    t = t.strip().strip('"').strip("'").strip()
    t = t.rstrip(".!,:;")
    return t[:70]


def generate_title(digest: str, llm_cmd: str = "claude -p", timeout: int = 120) -> str | None:
    """Generate a title by piping a prompt to an LLM CLI (stdin -> stdout).
    Returns None on failure. Default backend: `claude -p` (Claude Code headless)."""
    if not digest.strip():
        return None
    prompt = TITLE_PROMPT.format(digest=digest)
    try:
        proc = _subprocess.run(
            _shlex.split(llm_cmd),
            input=prompt, capture_output=True, text=True, timeout=timeout,
        )
    except (FileNotFoundError, _subprocess.TimeoutExpired, OSError):
        return None
    if proc.returncode != 0:
        return None
    title = _sanitize_title(proc.stdout)  # stdout only — ignore stderr info lines
    return title or None
