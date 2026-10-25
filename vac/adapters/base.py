"""Adapter interface and shared session model.

Each supported tool (Kiro CLI, Claude Code, ...) provides a SessionStore that
knows how to discover its sessions on disk, extract lightweight metadata, and
recognize an "image" content block in that tool's log schema. All mutation
logic (counting, GC, backup) lives in vac.core and is schema-agnostic — it only
needs the ``is_image_block`` predicate each adapter supplies.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Optional


@dataclass
class SessionInfo:
    id: str
    tool: str
    path: Path
    size_bytes: int
    image_count: int
    active: bool
    updated: Optional[str] = None
    title: Optional[str] = None
    cwd: Optional[str] = None


class SessionStore:
    """Base class for a tool's on-disk session store."""

    tool_name: str = "base"

    def available(self) -> bool:
        """True if this tool's session directory exists on the machine."""
        raise NotImplementedError

    def list_sessions(self) -> list[SessionInfo]:
        raise NotImplementedError

    def resolve(self, id_or_path: str) -> Optional[Path]:
        """Resolve a session id (or a direct path) to its .jsonl log file."""
        raise NotImplementedError

    def is_image_block(self, obj: dict) -> bool:
        """True if ``obj`` is an image block in ANY of this tool's schemas.

        A single tool may serialize images more than one way (e.g. an inline
        content block AND a raw-bytes payload inside a tool-result map); this
        must recognize all of them.
        """
        raise NotImplementedError

    def is_active(self, log_path: Path) -> bool:
        """Best-effort: is the session currently open (unsafe to edit)?"""
        return False

    def replace_image(self, obj: dict, note: str) -> dict:
        """Return the replacement written in place of a matched image block.

        The shape must match the specific form of ``obj`` so the surrounding
        structure stays valid (a content block becomes a text content block; a
        tool-result image item becomes a text item). Adapters override.
        """
        return {"kind": "text", "data": note}

    def set_title(self, log_path: Path, title: str) -> bool:
        """Persist a display title for the session. Returns True on success.
        Only tools with a writable title store (e.g. Kiro's .json metadata)
        support this; others return False."""
        return False

    # --- path ownership -----------------------------------------------------
    # All three stores use .jsonl, so "any existing .jsonl" is not enough to
    # claim a path: the first store checked would claim every file. A store
    # owns a path when it sits under the store's root, or (for copies kept
    # elsewhere) when its first record has this store's shape.

    def looks_like(self, record: dict) -> bool:
        """True if ``record`` (a log's first JSON line) is in this store's format."""
        return False

    def owns_path(self, path: Path) -> bool:
        try:
            path.resolve().relative_to(self.root.resolve())  # type: ignore[attr-defined]
            return True
        except (ValueError, AttributeError, OSError):
            pass
        rec = first_record(path)
        return rec is not None and self.looks_like(rec)


def first_record(path: Path) -> Optional[dict]:
    """First non-blank JSON object in a .jsonl file, or None."""
    try:
        with path.open() as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    return None
                return obj if isinstance(obj, dict) else None
    except OSError:
        return None
    return None
