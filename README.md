# vac

[![PyPI](https://img.shields.io/pypi/v/vac-cli.svg)](https://pypi.org/project/vac-cli/)
[![CI](https://github.com/homeo26/vac/actions/workflows/ci.yml/badge.svg)](https://github.com/homeo26/vac/actions/workflows/ci.yml)
[![Python](https://img.shields.io/pypi/pyversions/vac-cli.svg)](https://pypi.org/project/vac-cli/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

🧹 Vacuum up cruft from your AI coding-agent sessions. A small, standalone Python CLI that **cleans, prunes, analyzes, archives, and repairs** local session logs for **Kiro CLI** and **Claude Code**.

Long agent sessions rot over time — embedded screenshots, giant tool outputs, and ever-growing history. Because these agents replay the whole conversation every turn, that bloat causes real failures:

- **Oversized images** (>2000px) wedge a session: `image dimensions exceed max allowed size for many-image requests: 2000 pixels`
- **Huge embedded payloads** crash the load: `Agent connection closed` / `the selected model cannot continue this conversation`
- **Runaway context** triggers premature, quality-killing auto-compaction
- **Hundreds of stale sessions** quietly eat disk

`vac` fixes wedged sessions and keeps them lean:

- **`clean`** — strip embedded images (both Kiro forms + Claude), fixing the 2000px / payload-size crashes
- **`prune`** — free a chosen **% of context tokens** from the oldest turns (the "compact the first N%" Kiro's `/compact` and `/rewind` can't do)
- **`analyze` / `doctor`** — see what's eating a session and triage the ones about to break
- **`list` / `archive`** — inventory by age and archive old sessions (reversible)

Everything is **dry-run by default, backs up before writing, refuses to touch live sessions, and JSON-validates the result** — so it never corrupts a session.

## Install

Pick whichever you have — the installed command is always `vac`.

**pip (universal — works anywhere Python + pip exist):**
```bash
pip install vac-cli
```

**pipx / uv (isolated global tool):**
```bash
pipx install vac-cli
uv tool install vac-cli
```

**Homebrew (macOS/Linux):**
```bash
brew tap homeo26/vac
brew install vac          # short form after tapping
# or one-shot without tapping first:
brew install homeo26/vac/vac
```

**From source, no pipx/uv needed (one-liner):**
```bash
curl -fsSL https://raw.githubusercontent.com/homeo26/vac/main/install.sh | bash
```
This creates an isolated venv under `~/.vac` and links `vac` into `~/.local/bin`
(uses `uv` automatically if present). Make sure `~/.local/bin` is on your `PATH`.

**From a git checkout (for development):**
```bash
git clone https://github.com/homeo26/vac.git && cd vac
pip install -e ".[test]"        # or:  uv tool install .
```

> Requires Python 3.9+.

## Commands

| Command | What it does |
|---|---|
| `vac list` | Inventory sessions (size, image count, age, live?, at-risk?). Flags: `--older-than 60d`, `--tool kiro\|claude`, `--sort size\|images\|updated\|age`, `--json` |
| `vac analyze <id>` | Show what's consuming one session: size, image count, largest entry, active state |
| `vac clean <id>` | GC image blocks (fixes file-size/payload crashes & the 2000px many-image error). `--keep N` retains the newest N images. Dry-run unless `--apply` |
| `vac prune <id>` | Free ~N% of **context tokens** from the oldest side (the "compact the first N%" Kiro can't do). `--oldest N`, `--mode outputs\|hard`, `--max-field <chars>`. Dry-run unless `--apply` |
| `vac doctor` | Flag sessions likely wedged: many images, oversized single entry (context bomb), or oversized session |
| `vac archive` | tar.gz + remove sessions older than a threshold (by real last-used time). `--older-than 60d`, `--tool`, `--include-active`. Reversible; dry-run unless `--apply` |

Global safety: `clean`/`prune`/`archive` are **dry-run by default**, write a `.bak`, refuse to edit active/locked sessions (use `--force`), and JSON-validate before writing. Works across **Kiro CLI** and **Claude Code**.

## Usage examples

```bash
vac list                    # inventory sessions: size, image count, live?, at-risk?
vac analyze <id|path>       # what's consuming space in one session
vac clean <id> --keep 3     # GC images, keep the last 3 (dry-run by default)
vac clean <id> --keep 3 --apply   # actually write (creates a .bak)
vac prune <id> --oldest 30  # clean ONLY the oldest 30% of the session (dry-run)
vac prune <id> --oldest 30 --apply
vac doctor                  # find sessions likely wedged (many/oversized images)
vac list --older-than 60d   # sessions not used in 60+ days (by real last-used time)
vac archive --older-than 60d        # preview archiving old sessions (dry-run)
vac archive --older-than 60d --apply  # tar.gz + remove them (reversible)
vac list --json             # machine-readable everywhere
```

### Age filtering & archiving

`--older-than` accepts `60d`, `2w`, `12h`, `30m`. Age is computed from each
session's real **last-used time** (`updated_at` in metadata), not the file
mtime — so sessions that `vac` itself rewrote are not misflagged as recent.

`vac archive --older-than N` tar.gz's every matching session (log + metadata +
history) into `~/.kiro/sessions/vac-archive-<timestamp>.tar.gz` and removes the
originals. It's **reversible** — `tar -xzf <archive> -C <dir>` restores any
session. Dry-run by default; active/locked sessions are skipped unless
`--include-active`.

### `vac prune` — free a percentage of the context (by tokens)

Neither Kiro's `/compact` nor `/rewind` can free a chosen slice of context from
the *start*. `vac prune --oldest N` does: it frees ~N% of the **context tokens**
(what the context % actually counts — not file bytes) from the oldest side,
walking oldest→newest and cleaning entries until the token target is met.

Modes:
- `--mode outputs` (default) — drop old tool-output bodies / strip images / truncate
  old tool text, **keeping** prompts and assistant text. Non-lossy for the dialogue,
  but frees less if the old region is mostly text (it tells you and suggests `hard`).
- `--mode hard` — also collapse old assistant/prompt text to stubs, guaranteeing it
  reaches the target. Loses old detail, but genuinely frees the requested %.

It reports tokens freed and % of context; user prompts are kept as anchors; the
result is JSON-validated before writing. Dry-run + `.bak` by default.

Note: `vac clean` (image GC) shrinks *file bytes* — great for payload-size crashes
and disk — but images are cheap in tokens, so use `prune` (especially `--mode hard`)
to actually lower the context % and delay auto-compaction.

## Safety

- **Dry-run by default** — `clean` never writes unless you pass `--apply`.
- **Automatic backup** — writes a `.bak` beside the log (disable with `--no-backup`).
- **Won't touch live sessions** — refuses to edit a locked/recently-active session unless `--force`.
- **Keeps source paths** — cleared images become a text placeholder, and file-based images can simply be re-read.
- **Local only** — never makes network calls.

## Supported tools

| Tool | Store | Image forms handled |
|---|---|---|
| Kiro CLI | `~/.kiro/sessions/cli/*.jsonl` | inline `{"kind":"image",...}` **and** tool-result `{"Image":{"source":{...}}}` (base64 or raw-bytes array) |
| Claude Code | `~/.claude/projects/**/*.jsonl` | `{"type":"image","source":{"type":"base64",...}}` |

Adapters are pluggable — more tools can be added.

## `doctor` checks

`vac doctor` flags sessions likely to be wedged:
- **Many-image risk** — more than ~20 images (Anthropic's stricter 2000px cap).
- **Context-bomb entry** — any single log entry over ~1 MB (a runaway tool output or embedded image re-sent every turn).
- **Oversized session** — total size likely to exceed the model's context window.

Each finding prints the exact `vac clean … --apply` command to fix it.

## Development

```bash
git clone https://github.com/homeo26/vac.git && cd vac
pip install -e ".[test]"
pytest -q                 # run the test suite
```

CI runs the tests on every push/PR (Python 3.9 and 3.12). Releases publish to
PyPI automatically via OIDC Trusted Publishing when a `v*` tag is pushed — no
tokens stored (see `.github/workflows/publish.yml`).

## License

MIT
