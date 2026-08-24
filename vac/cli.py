"""vac CLI — vacuum up cruft from AI coding-agent sessions.

Commands:
  vac list                 inventory sessions across installed tools
  vac analyze <id|path>    what's consuming space in one session
  vac clean <id|path>      GC images (keep last N); dry-run by default
  vac doctor               find sessions likely to be wedged (many/oversized images, stale lock)
"""
from __future__ import annotations

import json as _json
from pathlib import Path
from typing import Optional

import typer
from rich.console import Console
from rich.table import Table

from .adapters import ALL_STORES, SessionStore
from .core import clean_images, count_images

app = typer.Typer(add_completion=False, help="Vacuum up cruft from AI coding-agent sessions.")
console = Console()

# Anthropic's stricter per-image dimension cap kicks in for "many-image"
# requests (>~20 images). Sessions above this are at risk of the 2000px error.
MANY_IMAGE_RISK = 20


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
    sort: str = typer.Option("size", help="Sort by: size | images | updated"),
    as_json: bool = typer.Option(False, "--json", help="Machine-readable output"),
):
    """Inventory sessions across installed tools."""
    rows = []
    for store in active_stores():
        if tool and store.tool_name != tool:
            continue
        rows.extend(store.list_sessions())

    key = {"size": lambda s: -s.size_bytes,
           "images": lambda s: -s.image_count,
           "updated": lambda s: s.updated or ""}.get(sort, lambda s: -s.size_bytes)
    rows.sort(key=key)

    if as_json:
        console.print_json(_json.dumps([r.__dict__ | {"path": str(r.path)} for r in rows], default=str))
        return

    if not rows:
        console.print("[yellow]No sessions found (no supported tools installed?).[/yellow]")
        return

    t = Table(title="Sessions")
    t.add_column("tool"); t.add_column("id", overflow="fold"); t.add_column("size", justify="right")
    t.add_column("imgs", justify="right"); t.add_column("updated"); t.add_column("title/cwd", overflow="fold")
    for r in rows:
        flag = " [red]●live[/red]" if r.active else ""
        risk = " [yellow]⚠[/yellow]" if r.image_count > MANY_IMAGE_RISK else ""
        t.add_row(r.tool, r.id[:12] + flag, _human(r.size_bytes),
                  f"{r.image_count}{risk}", (r.updated or "")[:10],
                  (r.title or r.cwd or ""))
    console.print(t)


@app.command()
def analyze(id_or_path: str = typer.Argument(..., help="Session id or path to .jsonl")):
    """Show what's consuming space in one session."""
    store, path = _find(id_or_path)
    size = path.stat().st_size
    imgs = count_images(path, store.is_image_block)
    console.print(f"[bold]{store.tool_name}[/bold] session [cyan]{path.stem}[/cyan]")
    console.print(f"  path:   {path}")
    console.print(f"  size:   {_human(size)}")
    console.print(f"  images: {imgs}" + (f"  [yellow]⚠ >{MANY_IMAGE_RISK}: many-image risk[/yellow]"
                                         if imgs > MANY_IMAGE_RISK else ""))
    console.print(f"  active: {'yes (locked)' if store.is_active(path) else 'no'}")


@app.command()
def clean(
    id_or_path: str = typer.Argument(..., help="Session id or path to .jsonl"),
    keep: int = typer.Option(0, help="Keep the last N images (0 = strip all)"),
    apply: bool = typer.Option(False, "--apply", help="Actually write (default is dry-run)"),
    force: bool = typer.Option(False, "--force", help="Edit even if the session looks active"),
    no_backup: bool = typer.Option(False, "--no-backup", help="Do not create a .bak"),
):
    """GC image blocks from a session (dry-run unless --apply)."""
    store, path = _find(id_or_path)
    if store.is_active(path) and apply and not force:
        console.print("[red]Session looks active (locked/recently written). "
                      "Close it, or pass --force.[/red]")
        raise typer.Exit(2)

    res = clean_images(
        path, store.is_image_block, store.placeholder,
        keep=keep, dry_run=not apply, backup=not no_backup,
    )
    verb = "would remove" if res.dry_run else "removed"
    console.print(f"images: {res.total_images} total → {verb} {res.removed}, kept {res.kept}")
    console.print(f"size:   {_human(res.before_bytes)} → {_human(res.after_bytes)}")
    if res.backup:
        console.print(f"backup: {res.backup}")
    if res.dry_run and res.removed:
        console.print("[dim]dry-run — rerun with --apply to write changes[/dim]")


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
        console.print(f"    fix: [cyan]vac clean {s.id} --keep 3 --apply[/cyan]")


if __name__ == "__main__":
    app()
