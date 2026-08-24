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

    def placeholder(self, note: str) -> dict:
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

    def _first_meta(self, log: Path) -> dict:
        """Pull cwd / gitBranch / timestamp from the first record that has them."""
        try:
            with log.open() as fh:
                for line in fh:
                    try:
                        o = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if o.get("cwd") or o.get("timestamp"):
                        return o
        except OSError:
            pass
        return {}

    def list_sessions(self) -> list[SessionInfo]:
        from ..core import count_images

        out: list[SessionInfo] = []
        if not self.available():
            return out
        for log in sorted(self.root.glob("*/*.jsonl")):
            meta = self._first_meta(log)
            out.append(
                SessionInfo(
                    id=log.stem,
                    tool=self.tool_name,
                    path=log,
                    size_bytes=log.stat().st_size,
                    image_count=count_images(log, self.is_image_block),
                    active=self.is_active(log),
                    updated=meta.get("timestamp"),
                    title=(meta.get("gitBranch") or None),
                    cwd=meta.get("cwd"),
                )
            )
        return out
