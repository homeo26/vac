"""Claude Code session store.

Layout (observed):
  ~/.claude/projects/<path-slug>/{uuid}.jsonl   one transcript per session
  Each line is a record; conversational lines carry `message` = {role, content}
  where content is a str or a list of blocks. Per-line `cwd`, `sessionId`,
  `gitBranch`, `timestamp` are present.

Image content block (standard Anthropic shape — NOT yet verified against a
real Claude Code transcript containing an image; the walker is defensive):
  {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": <base64>}}

Claude Code has no sidecar .lock file, so active-session detection is
best-effort (mtime heuristic) and `vac` requires --force to edit here.
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Optional

from .base import SessionInfo, SessionStore


class ClaudeCodeStore(SessionStore):
    tool_name = "claude"

    def __init__(self, root: Optional[Path] = None):
        self.root = root or (Path.home() / ".claude" / "projects")

    def available(self) -> bool:
        return self.root.is_dir()

    def is_image_block(self, obj: dict) -> bool:
        return isinstance(obj, dict) and obj.get("type") == "image"

    def replace_image(self, obj: dict, note: str) -> dict:
        # Match Claude Code's block schema (type/text) rather than Kiro's.
        return {"type": "text", "text": note}

    def is_active(self, log_path: Path) -> bool:
        # Heuristic: modified within the last 60s => likely a live session.
        try:
            return (time.time() - log_path.stat().st_mtime) < 60
        except OSError:
            return False

    def resolve(self, id_or_path: str) -> Optional[Path]:
        p = Path(id_or_path)
        if p.suffix == ".jsonl" and p.exists():
            return p
        # id is a uuid; search all project slugs for {uuid}.jsonl
        matches = list(self.root.glob(f"*/{id_or_path}.jsonl"))
        return matches[0] if matches else None

    def _meta(self, log: Path) -> dict:
        """Single pass: cwd (first seen), updated (LAST record's timestamp =
        real last-used), and a title from the first user message."""
        cwd = None
        last_ts = None
        title = None
        try:
            with log.open() as fh:
                for line in fh:
                    try:
                        o = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if cwd is None and o.get("cwd"):
                        cwd = o["cwd"]
                    if o.get("timestamp"):
                        last_ts = o["timestamp"]  # keep overwriting -> last wins
                    if title is None:
                        m = o.get("message")
                        if isinstance(m, dict) and m.get("role") == "user":
                            c = m.get("content")
                            if isinstance(c, str) and c.strip():
                                title = c.strip()[:80]
                            elif isinstance(c, list):
                                for b in c:
                                    if isinstance(b, dict) and b.get("type") == "text" and b.get("text"):
                                        title = b["text"].strip()[:80]
                                        break
        except OSError:
            pass
        return {"cwd": cwd, "updated": last_ts, "title": title}

    def list_sessions(self) -> list[SessionInfo]:
        from ..core import count_images

        out: list[SessionInfo] = []
        if not self.available():
            return out
        for log in sorted(self.root.glob("*/*.jsonl")):
            meta = self._meta(log)
            out.append(
                SessionInfo(
                    id=log.stem,
                    tool=self.tool_name,
                    path=log,
                    size_bytes=log.stat().st_size,
                    image_count=count_images(log, self.is_image_block),
                    active=self.is_active(log),
                    updated=meta.get("updated"),
                    title=meta.get("title"),
                    cwd=meta.get("cwd"),
                )
            )
        return out
