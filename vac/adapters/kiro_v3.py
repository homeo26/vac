"""Kiro session store — v3 / KAS (Kiro Agent Server) format.

Layout (verified, kiro-cli 2.20 / KAS 0.66):
  ~/.kiro/sessions/<workspace-hash>/<session-id>/messages.jsonl   event log
  ~/.kiro/sessions/<workspace-hash>/<session-id>/session.json     metadata
  ~/.kiro/sessions/<workspace-hash>/<session-id>/{snapshots,tool-outputs,...}/

Each line is {"id", "timestamp", "payload": {"type": ..., ...}}.

Images are NOT embedded as base64. A read of an image file produces a
tool_result whose content is a text stub:

  {"type": "tool_result", "toolCallId": ..., "content":
   "Image: /tmp/x.jpg\\nFormat: jpeg\\nSize: 1234 bytes\\nMediaType: image/jpeg"}

KAS re-reads that file from disk every time it rebuilds the request, so a
stale/oversized file (e.g. >2000px in a many-image request) wedges the session
even though messages.jsonl is small. Clearing = replacing the stub text, which
stops KAS from re-attaching the file.
"""
from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Optional

from .base import SessionInfo, SessionStore, first_record

_IMG_STUB = re.compile(r"^Image: (?P<path>.+?)\nFormat: .*?\nMediaType: image/", re.S)


class KiroV3Store(SessionStore):
    tool_name = "kiro"

    def __init__(self, root: Optional[Path] = None):
        self.root = root or (Path.home() / ".kiro" / "sessions")

    def available(self) -> bool:
        return self.root.is_dir()

    def _logs(self):
        # <hash>/<session-id>/messages.jsonl ; skip the legacy "cli" dir
        for log in self.root.glob("*/*/messages.jsonl"):
            if log.parent.parent.name != "cli":
                yield log

    @staticmethod
    def _stub_paths(content: str) -> list[str]:
        return re.findall(r"^Image: (.+)$", content, re.M)

    def is_image_block(self, obj: dict) -> bool:
        if not isinstance(obj, dict):
            return False
        t = obj.get("type")
        # Inline base64 images: tool_result.images[] (screenshots, image reads)
        # and user.images[] (pasted into the prompt). These are sent to the
        # model verbatim on every turn, independent of the text stub.
        if t in ("tool_result", "user") and obj.get("images"):
            return True
        if t != "tool_result":
            return False
        c = obj.get("content")
        return isinstance(c, str) and bool(_IMG_STUB.match(c))

    def replace_image(self, obj: dict, note: str) -> dict:
        out = dict(obj)
        n_inline = len(out.pop("images", None) or [])
        if obj.get("type") == "user":
            # Keep the user's words; just record that pasted images were removed.
            if n_inline:
                out["content"] = (out.get("content") or "") + \
                    f"\n[{n_inline} pasted image(s) removed by vac to save context]"
            return out
        c = obj.get("content", "")
        paths = self._stub_paths(c) if isinstance(c, str) else []
        if paths and "source:" not in note:
            p = paths[0] if len(paths) == 1 else ", ".join(paths)
            note = (f"[image removed by vac to save context (source: {p}); "
                    "do not re-load unless the user explicitly asks]")
        if isinstance(c, str) and c.startswith("[image removed by vac"):
            return out  # text already cleared on an earlier run; just drop images
        if isinstance(c, str) and not _IMG_STUB.match(c) and c:
            return out  # real tool text (e.g. screenshot tool's caption): keep it
        out["content"] = note
        return out

    def is_active(self, log_path: Path) -> bool:
        # No lock file in this format; a live session appends continuously.
        try:
            return (time.time() - log_path.stat().st_mtime) < 60
        except OSError:
            return False

    def _meta(self, log_path: Path) -> dict:
        mp = log_path.parent / "session.json"
        try:
            return json.loads(mp.read_text())
        except (OSError, json.JSONDecodeError):
            return {}

    def set_title(self, log_path: Path, title: str) -> bool:
        mp = log_path.parent / "session.json"
        d = self._meta(log_path)
        if not d:
            return False
        d["title"] = title
        try:
            mp.write_text(json.dumps(d, indent=2))
            return True
        except OSError:
            return False

    def looks_like(self, record: dict) -> bool:
        # {"id": ..., "timestamp": ..., "payload": {"type": ...}}
        return isinstance(record.get("payload"), dict)

    def owns_path(self, path: Path) -> bool:
        # The v3 root also contains the v1 store (sessions/cli), so the folder
        # alone isn't proof: require the v3 record shape.
        rec = first_record(path)
        return rec is not None and self.looks_like(rec)

    def resolve(self, id_or_path: str) -> Optional[Path]:
        p = Path(id_or_path).expanduser()
        if p.is_dir() and (p / "messages.jsonl").exists():
            p = p / "messages.jsonl"
        if p.suffix == ".jsonl" and p.exists():
            return p if self.owns_path(p) else None
        matches = [m for m in self.root.glob(f"*/{id_or_path}/messages.jsonl")
                   if m.parent.parent.name != "cli"]
        return matches[0] if matches else None

    def list_sessions(self) -> list[SessionInfo]:
        from ..core import count_images

        out: list[SessionInfo] = []
        if not self.available():
            return out
        for log in sorted(self._logs()):
            meta = self._meta(log)
            ws = meta.get("workspacePaths") or []
            out.append(
                SessionInfo(
                    id=log.parent.name,
                    tool=self.tool_name,
                    path=log,
                    size_bytes=log.stat().st_size,
                    image_count=count_images(log, self.is_image_block),
                    active=self.is_active(log),
                    updated=meta.get("lastModifiedAt"),
                    title=(meta.get("title") or "")[:80] or None,
                    cwd=ws[0] if ws else None,
                )
            )
        return out
