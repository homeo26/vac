"""Adapter interface and shared session model.

Each supported tool (Kiro CLI, Claude Code, ...) provides a SessionStore that
knows how to discover its sessions on disk, extract lightweight metadata, and
recognize an "image" content block in that tool's log schema. All mutation
logic (counting, GC, backup) lives in vac.core and is schema-agnostic — it only
needs the ``is_image_block`` predicate each adapter supplies.
"""
from __future__ import annotations

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
        """True if ``obj`` is an image content block in this tool's schema."""
        raise NotImplementedError

    def is_active(self, log_path: Path) -> bool:
        """Best-effort: is the session currently open (unsafe to edit)?"""
        return False

    # Replacement written in place of a neutralized image block. Adapters may
    # override to match their schema's text-block shape.
    def placeholder(self, note: str) -> dict:
        return {"kind": "text", "data": note}
