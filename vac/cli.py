"""vac CLI — vacuum up cruft from AI coding-agent sessions.

Commands:
  vac list                 inventory sessions across installed tools
  vac analyze <id|path>    what's consuming space in one session
  vac clean <id|path>      GC images (keep last N); dry-run by default
  vac doctor               find sessions likely to be wedged (many/oversized images, stale lock)
"""
from __future__ import annotations

import json as _json
import shutil
from pathlib import Path
from typing import Optional

import typer
from rich.console import Console
from rich.table import Table

from .adapters import ALL_STORES, SessionStore
from .core import (clean_images, count_images, max_line_bytes, prune_oldest,
                   parse_duration, age_days, session_file_group, archive_files,
                   build_digest, generate_title)

app = typer.Typer(add_completion=False, help="Vacuum up cruft from AI coding-agent sessions.")
console = Console()

# Anthropic's stricter per-image dimension cap kicks in for "many-image"
# requests (>~20 images). Sessions above this are at risk of the 2000px error.
MANY_IMAGE_RISK = 20
# A single entry this large is almost always a runaway tool output or embedded
# image, and re-sending it every turn can exceed the model's context window.
BIG_ENTRY_BYTES = 1_000_000
# A whole session this large very likely exceeds the context window.
BIG_SESSION_BYTES = 10_000_000


def active_stores() -> list[SessionStore]:
    stores = [cls() for cls in ALL_STORES]
    return [s for s in stores if s.available()]


def _human(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f}{unit}" if unit == "B" else f"{n/1:.0f}{unit}" if False else f"{n:.1f}{unit}"
        n /= 1024
    return f"{n:.1f}GB"


def _find(id_or_path: str) -> tuple[SessionStore, Path]:
    # direct path?
    p = Path(id_or_path)
    for store in active_stores():
        resolved = store.resolve(id_or_path)
        if resolved:
            return store, resolved
    console.print(f"[red]No session found for '{id_or_path}' in any installed tool.[/red]")
    raise typer.Exit(1)


@app.command("list")
def list_cmd(
    tool: Optional[str] = typer.Option(None, help="Filter by tool: kiro | claude"),
    sort: str = typer.Option("size", help="Sort by: size | images | updated | age"),
    older_than: Optional[str] = typer.Option(None, "--older-than",
        help="Only sessions last used more than this ago (e.g. 60d, 2w, 12h)"),
    as_json: bool = typer.Option(False, "--json", help="Machine-readable output"),
):
    """Inventory sessions across installed tools."""
    cutoff = parse_duration(older_than).total_seconds() / 86400.0 if older_than else None
    rows = []
    for store in active_stores():
        if tool and store.tool_name != tool:
            continue
        for s in store.list_sessions():
            if cutoff is not None and age_days(s.updated, s.path) < cutoff:
                continue
            rows.append(s)

    key = {"size": lambda s: -s.size_bytes,
           "images": lambda s: -s.image_count,
           "updated": lambda s: s.updated or "",
           "age": lambda s: -age_days(s.updated, s.path)}.get(sort, lambda s: -s.size_bytes)
    rows.sort(key=key)

    if as_json:
        console.print_json(_json.dumps(
            [r.__dict__ | {"path": str(r.path), "age_days": round(age_days(r.updated, r.path), 1)}
             for r in rows], default=str))
        return

    if not rows:
        console.print("[yellow]No matching sessions.[/yellow]")
        return

    t = Table(title="Sessions" + (f" (older than {older_than})" if older_than else ""))
    t.add_column("tool"); t.add_column("id", overflow="fold"); t.add_column("size", justify="right")
    t.add_column("imgs", justify="right"); t.add_column("age", justify="right")
    t.add_column("title/cwd", overflow="fold")
    for r in rows:
        flag = " [red]●live[/red]" if r.active else ""
        risk = " [yellow]⚠[/yellow]" if r.image_count > MANY_IMAGE_RISK else ""
        t.add_row(r.tool, r.id[:12] + flag, _human(r.size_bytes),
                  f"{r.image_count}{risk}", f"{age_days(r.updated, r.path):.0f}d",
                  (r.title or r.cwd or ""))
    console.print(t)


@app.command()
def analyze(id_or_path: str = typer.Argument(..., help="Session id or path to .jsonl")):
    """Show what's consuming space in one session."""
    store, path = _find(id_or_path)
    size = path.stat().st_size
    imgs = count_images(path, store.is_image_block)
    biggest = max_line_bytes(path)
    console.print(f"[bold]{store.tool_name}[/bold] session [cyan]{path.stem}[/cyan]")
    console.print(f"  path:   {path}")
    console.print(f"  size:   {_human(size)}" + (f"  [yellow]⚠ large session[/yellow]"
                                                 if size > BIG_SESSION_BYTES else ""))
    console.print(f"  images: {imgs}" + (f"  [yellow]⚠ >{MANY_IMAGE_RISK}: many-image risk[/yellow]"
                                         if imgs > MANY_IMAGE_RISK else ""))
    console.print(f"  largest entry: {_human(biggest)}" + (f"  [yellow]⚠ context-bomb entry[/yellow]"
                                                           if biggest > BIG_ENTRY_BYTES else ""))
    console.print(f"  active: {'yes (locked)' if store.is_active(path) else 'no'}")


@app.command()
def clean(
    id_or_path: str = typer.Argument(..., help="Session id or path to .jsonl"),
    keep: int = typer.Option(3, help="Keep the newest N images (default 3; 0 = strip all)"),
    dry_run: bool = typer.Option(False, "--dry-run", "-n", help="Preview without writing"),
    force: bool = typer.Option(False, "--force", help="Edit even if the session looks active"),
    no_backup: bool = typer.Option(False, "--no-backup", help="Do not create a .bak"),
):
    """GC image blocks from a session. Applies by default; use --dry-run to preview."""
    store, path = _find(id_or_path)
    active = store.is_active(path)
    if active and not dry_run and not force:
        console.print("[red]Session looks active (locked/recently written). "
                      "Close it, or pass --force.[/red]")
        raise typer.Exit(2)
    if active and not dry_run and force:
        console.print("[yellow]⚠ this session is ACTIVE — on-disk edits do NOT change the running "
                      "session and its next save may overwrite them. Close and re-resume it "
                      "for the change to take effect.[/yellow]")

    res = clean_images(
        path, store.is_image_block, store.replace_image,
        keep=keep, dry_run=dry_run, backup=not no_backup,
    )
    verb = "would remove" if res.dry_run else "removed"
    console.print(f"images: {res.total_images} total → {verb} {res.removed}, kept {res.kept}")
    console.print(f"size:   {_human(res.before_bytes)} → {_human(res.after_bytes)}")
    if res.backup:
        console.print(f"backup: {res.backup}")
    if res.dry_run and res.removed:
        console.print("[dim]dry-run — omit --dry-run to write changes[/dim]")
    if not res.dry_run and active:
        console.print("[yellow]Restart the session (close & re-resume) to see this take effect.[/yellow]")


@app.command()
def doctor(
    as_json: bool = typer.Option(False, "--json", help="Machine-readable output"),
):
    """Find sessions likely to be wedged (many images, or a stale lock)."""
    problems = []
    for store in active_stores():
        for s in store.list_sessions():
            reasons = []
            if s.image_count > MANY_IMAGE_RISK:
                reasons.append(f"{s.image_count} images (>{MANY_IMAGE_RISK}: 2000px many-image risk)")
            biggest = max_line_bytes(s.path)
            if biggest > BIG_ENTRY_BYTES:
                reasons.append(f"{_human(biggest)} single entry (context-bomb: runaway output/image)")
            if s.size_bytes > BIG_SESSION_BYTES:
                reasons.append(f"{_human(s.size_bytes)} session (may exceed context window)")
            if reasons:
                problems.append((s, reasons))

    if as_json:
        console.print_json(_json.dumps(
            [{"tool": s.tool, "id": s.id, "path": str(s.path), "reasons": r} for s, r in problems]))
        return

    if not problems:
        console.print("[green]No at-risk sessions found.[/green]")
        return
    for s, reasons in problems:
        console.print(f"[yellow]⚠[/yellow] [bold]{s.tool}[/bold] {s.id[:12]} — " + "; ".join(reasons))
        console.print(f"    fix: [cyan]vac clean {s.id} --keep 3[/cyan]")


@app.command()
def prune(
    id_or_path: str = typer.Argument(..., help="Session id or path to .jsonl"),
    oldest: float = typer.Option(30.0, help="Free ~this %% of CONTEXT TOKENS from the oldest side"),
    mode: str = typer.Option("outputs", help="'outputs' (drop old tool outputs, keep text) | 'hard' (also drop old text)"),
    max_field: int = typer.Option(2000, help="Truncate tool outputs longer than this many chars (in the old region)"),
    dry_run: bool = typer.Option(False, "--dry-run", "-n", help="Preview without writing"),
    force: bool = typer.Option(False, "--force", help="Edit even if the session looks active"),
    no_backup: bool = typer.Option(False, "--no-backup", help="Do not create a .bak"),
):
    """Free a percentage of the CONTEXT TOKENS from the oldest side — the
    token-reducing 'compact the first N%' that Kiro's /compact and /rewind
    can't do. Reports tokens freed (what actually moves the context %), not
    just file bytes. Works for both Kiro and Claude Code sessions."""
    store, path = _find(id_or_path)
    if mode not in ("outputs", "hard"):
        console.print("[red]--mode must be 'outputs' or 'hard'[/red]")
        raise typer.Exit(2)
    active = store.is_active(path)
    if active and not dry_run and not force:
        console.print("[red]Session looks active (locked/recently written). "
                      "Close it, or pass --force.[/red]")
        raise typer.Exit(2)
    if active and not dry_run and force:
        console.print("[yellow]⚠ this session is ACTIVE — on-disk edits do NOT change the running "
                      "session and its next save may overwrite them. Close and re-resume it "
                      "for the context to actually drop.[/yellow]")

    res = prune_oldest(
        path, store.is_image_block, store.replace_image,
        oldest_pct=oldest, mode=mode, max_field_bytes=max_field,
        dry_run=dry_run, backup=not no_backup,
    )
    if not res.valid:
        console.print("[red]Aborted: pruning would produce invalid JSON — nothing written.[/red]")
        raise typer.Exit(3)

    pct_total = (res.freed_tokens / res.total_tokens * 100) if res.total_tokens else 0
    verb = "would free" if res.dry_run else "freed"
    console.print(f"context: ~{res.total_tokens:,} tokens total; target {oldest:g}% = ~{res.target_tokens:,}")
    console.print(f"{verb}: ~{res.freed_tokens:,} tokens ({pct_total:.1f}% of context) "
                  f"via {res.region_entries} entries, {res.images_removed} images, {res.outputs_truncated} outputs")
    console.print(f"file:   {_human(res.before_bytes)} -> {_human(res.after_bytes)}  (mode={mode})")
    if not res.dry_run and res.backup:
        console.print(f"backup: {res.backup}")
    if res.freed_tokens < res.target_tokens and mode == "outputs":
        console.print("[yellow]note:[/yellow] old region is text-heavy; dropping tool output alone "
                      "did not reach the target. Use [cyan]--mode hard[/cyan] to also drop old text.")
    if res.dry_run and res.freed_tokens:
        console.print("[dim]dry-run - omit --dry-run to write changes[/dim]")
    if not res.dry_run and active:
        console.print("[yellow]Restart the session (close & re-resume) — the running session won't "
                      "reflect this until reload.[/yellow]")


@app.command()
def archive(
    older_than: str = typer.Option(..., "--older-than",
        help="Archive sessions last used more than this ago (e.g. 60d, 2w)"),
    tool: Optional[str] = typer.Option(None, help="Filter by tool: kiro | claude"),
    out: Optional[str] = typer.Option(None, help="Archive file path (default ~/.kiro/sessions/vac-archive-<ts>.tar.gz)"),
    dry_run: bool = typer.Option(False, "--dry-run", "-n", help="Preview without archiving/removing"),
    include_active: bool = typer.Option(False, "--include-active",
        help="Also archive locked/active sessions (not recommended)"),
):
    """Archive (tar.gz) and remove sessions older than a threshold, by last-used
    time. Reversible: extract the tarball to restore any session. Dry-run by
    default; skips active/locked sessions unless --include-active."""
    cutoff = parse_duration(older_than).total_seconds() / 86400.0
    selected = []
    skipped_active = 0
    for store in active_stores():
        if tool and store.tool_name != tool:
            continue
        for s in store.list_sessions():
            if age_days(s.updated, s.path) < cutoff:
                continue
            if s.active and not include_active:
                skipped_active += 1
                continue
            selected.append(s)

    if not selected:
        console.print(f"[yellow]No sessions older than {older_than}"
                      + (f" ({skipped_active} skipped as active)" if skipped_active else "") + ".[/yellow]")
        return

    groups = [session_file_group(s.path) for s in selected]
    total_bytes = sum(f.stat().st_size for g in groups for f in g if f.exists())

    console.print(f"[bold]{len(selected)}[/bold] sessions older than {older_than}"
                  f"  ({_human(total_bytes)} across {sum(len(g) for g in groups)} files)"
                  + (f"  [dim]· {skipped_active} active skipped[/dim]" if skipped_active else ""))
    for s in selected[:20]:
        console.print(f"  {s.tool} {s.id[:12]}  {age_days(s.updated, s.path):.0f}d  "
                      f"{_human(s.size_bytes)}  {(s.title or s.cwd or '')[:50]}")
    if len(selected) > 20:
        console.print(f"  … and {len(selected) - 20} more")

    if dry_run:
        console.print("[dim]dry-run — omit --dry-run to archive + remove[/dim]")
        return

    from datetime import datetime
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    out_path = Path(out) if out else (Path.home() / ".kiro" / "sessions" / f"vac-archive-{ts}.tar.gz")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    n, arch_bytes = archive_files(groups, out_path, remove=True)
    console.print(f"[green]archived[/green] {n} files ({_human(arch_bytes)}) → {out_path}")
    console.print(f"restore with: [cyan]tar -xzf {out_path} -C <dir>[/cyan]")


def _needs_title(info, include_generic: bool) -> bool:
    t = (info.title or "").strip()
    if not t:
        return True
    if include_generic:
        low = t.lower()
        return len(t) < 8 or low.startswith(("read ", "can you", "//", "##")) or "http" in low
    return False


@app.command()
def name(
    id_or_path: Optional[str] = typer.Argument(None, help="Name one session; omit to scan all untitled"),
    include_generic: bool = typer.Option(False, "--include-generic", help="Also re-title short/generic names"),
    all_: bool = typer.Option(False, "--all", help="Re-title every matching session, even ones with a good title"),
    newer_than: Optional[str] = typer.Option(None, "--newer-than", help="Only sessions used within this window (e.g. 30d, 2w)"),
    llm_cmd: str = typer.Option("claude -p", "--llm-cmd", help="Titler command: reads a digest on stdin, prints a title"),
    tool: Optional[str] = typer.Option(None, help="Filter by tool: kiro | claude"),
    limit: int = typer.Option(25, help="Max sessions to name in one run"),
    dry_run: bool = typer.Option(False, "--dry-run", "-n", help="Preview titles without writing"),
    no_backup: bool = typer.Option(False, "--no-backup", help="Don't back up metadata before writing"),
):
    """AI-name sessions from their content (like the ChatGPT/Claude sidebar).
    Uses a local LLM CLI (`claude -p` by default) — no API key needed. Titles
    persist only where the tool has a writable title store (Kiro)."""
    cutoff = parse_duration(newer_than).total_seconds() / 86400.0 if newer_than else None
    # Build (store, SessionInfo) targets.
    targets = []
    if id_or_path:
        store, path = _find(id_or_path)
        info = next((s for s in store.list_sessions() if s.path == path), None)
        if info:
            targets.append((store, info))
    else:
        for store in active_stores():
            if tool and store.tool_name != tool:
                continue
            for s in store.list_sessions():
                if s.active:
                    continue
                if cutoff is not None and age_days(s.updated, s.path) > cutoff:
                    continue
                if all_ or _needs_title(s, include_generic):
                    targets.append((store, s))

    if not targets:
        console.print("[yellow]No sessions need naming.[/yellow]")
        return

    named = skipped = 0
    for store, s in targets[:limit]:
        digest = build_digest(s.path)
        title = generate_title(digest, llm_cmd) if digest else None
        cur = (s.title or "").strip() or "(untitled)"
        if not title:
            console.print(f"[dim]{s.tool} {s.id[:12]}[/dim]  — [red]could not generate[/red] (empty digest or LLM error)")
            skipped += 1
            continue
        console.print(f"{s.tool} {s.id[:12]}  [dim]{cur[:32]}[/dim] → [bold]{title}[/bold]")
        if not dry_run:
            mp = s.path.with_suffix(".json")
            if not no_backup and mp.exists():
                shutil.copy2(mp, mp.with_suffix(".json.bak"))
            if store.set_title(s.path, title):
                named += 1
            else:
                console.print(f"    [yellow]cannot persist title for {s.tool} (no writable title store)[/yellow]")
                skipped += 1

    if not dry_run:
        console.print(f"\n[green]named {named}[/green], skipped {skipped}")
    else:
        console.print(f"\n[dim]dry-run — {len(targets[:limit])} proposed; omit --dry-run to write[/dim]")


if __name__ == "__main__":
    app()
