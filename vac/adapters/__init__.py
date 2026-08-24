from .base import SessionInfo, SessionStore
from .kiro import KiroStore
from .claude_code import ClaudeCodeStore

# Registry of all known adapters. `vac` auto-selects the ones whose store dir
# exists (see cli.active_stores).
ALL_STORES = [KiroStore, ClaudeCodeStore]

__all__ = ["SessionInfo", "SessionStore", "KiroStore", "ClaudeCodeStore", "ALL_STORES"]
