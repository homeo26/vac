"""Kiro CLI session store.

Layout (verified):
  ~/.kiro/sessions/cli/{id}.jsonl   append-only conversation log
  ~/.kiro/sessions/cli/{id}.json    metadata (title, cwd, updated_at)
  ~/.kiro/sessions/cli/{id}.lock    present only while the session is active

Image content block:
  {"kind": "image", "data": {"format": "png", "source": {"kind": ..., "data": <base64>}}}
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

from .base import SessionInfo, SessionStore


class KiroStore(SessionStore):
    tool_name = "kiro"

    def __init__(self, root: Optional[Path] = None):
        self.root = root or (Path.home() / ".kiro" / "sessions" / "cli")

    def available(self) -> bool:
        return self.root.is_dir()

    def is_image_block(self, obj: dict) -> bool:
        if not isinstance(obj, dict):
            return False
        # Form 1 — inline content block: {"kind": "image", "data": {...}}
        if obj.get("kind") == "image":
            return True
        # Form 2 — tool-result image item: {"Image": {"format", "source": {...}}}
        # where source.data is a base64 string OR a raw-bytes integer array.
        img = obj.get("Image")
        if isinstance(img, dict):
            src = img.get("source")
            if isinstance(src, dict) and src.get("data") is not None:
                return True
        return False

    def replace_image(self, obj: dict, note: str) -> dict:
        # Tool-result form -> text item (matches the items[] {"Text": {...}} shape).
        if "Image" in obj and obj.get("kind") != "image":
            return {"Text": {"text": note}}
        # Inline content-block form -> text content block.
        return {"kind": "text", "data": note}

    def is_active(self, log_path: Path) -> bool:
        return log_path.with_suffix(".lock").exists()

    def resolve(self, id_or_path: str) -> Optional[Path]:
        p = Path(id_or_path)
        if p.suffix == ".jsonl" and p.exists():
            return p
        cand = self.root / f"{id_or_path}.jsonl"
        return cand if cand.exists() else None

    def _meta(self, session_id: str) -> dict:
        mp = self.root / f"{session_id}.json"
        if mp.exists():
            try:
                return json.loads(mp.read_text())
            except (json.JSONDecodeError, OSError):
                pass
        return {}

    def list_sessions(self) -> list[SessionInfo]:
        from ..core import count_images  # local import to avoid cycle

        out: list[SessionInfo] = []
        if not self.available():
            return out
        for log in sorted(self.root.glob("*.jsonl")):
            sid = log.stem
            meta = self._meta(sid)
            out.append(
                SessionInfo(
                    id=sid,
                    tool=self.tool_name,
                    path=log,
                    size_bytes=log.stat().st_size,
                    image_count=count_images(log, self.is_image_block),
                    active=self.is_active(log),
                    updated=meta.get("updated_at"),
                    title=(meta.get("title") or "")[:80] or None,
                    cwd=meta.get("cwd"),
                )
            )
        return out
