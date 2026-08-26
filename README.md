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

At a glance:

| Command | Purpose |
|---|---|
| [`vac list`](#vac-list) | Inventory sessions (size, images, age, live?, at-risk?) |
| [`vac analyze`](#vac-analyze-idpath) | Break down what's consuming one session |
| [`vac clean`](#vac-clean-idpath) | Strip embedded images (fixes 2000px / payload crashes) |
| [`vac prune`](#vac-prune-idpath) | Free a % of context **tokens** from the oldest turns |
| [`vac doctor`](#vac-doctor) | Find sessions likely to be wedged |
| [`vac archive`](#vac-archive) | Archive + remove old sessions (reversible) |

**Global conventions:** `<id>` is a session id or a path to a `.jsonl`. Commands that write (`clean`, `prune`, `archive`) are **dry-run by default** — add `--apply` to write. They create a `.bak`, refuse to edit an active/locked session (override with `--force`), and JSON-validate before writing. All commands support both **Kiro CLI** and **Claude Code** sessions.

---

### `vac list`
Inventory sessions across installed tools.

| Option | Description |
|---|---|
| `--older-than <dur>` | Only sessions last used ≥ this ago (`60d`, `2w`, `12h`, `30m`) |
| `--tool kiro\|claude` | Restrict to one tool |
| `--sort size\|images\|updated\|age` | Sort order (default `size`) |
| `--json` | Machine-readable output |

```bash
vac list                        # everything, biggest first
vac list --older-than 60d --sort age
```

### `vac analyze <id|path>`
Show what's consuming one session: total size, image count, largest single entry, and whether it's active. No options.

```bash
vac analyze 22a4b1a5-b8f1-44e0-98ae-502b95f030b5
```

### `vac clean <id|path>`
Garbage-collect embedded image blocks (both Kiro forms + Claude). Fixes the 2000px many-image error and payload-size crashes. Shrinks **file bytes** (images are cheap in tokens — use `prune` for context relief).

| Option | Description |
|---|---|
| `--keep N` | Keep the newest N images, remove the rest (default `0` = remove all) |
| `--apply` | Write changes (default: dry-run) |
| `--force` | Edit even if the session looks active |
| `--no-backup` | Don't write a `.bak` |

```bash
vac clean <id> --keep 1            # preview: drop all but the newest image
vac clean <id> --keep 1 --apply    # write it
```

### `vac prune <id|path>`
Free ~N% of the session's **context tokens** from the oldest side — the "compact the first N%" that Kiro's `/compact` and `/rewind` can't do. Walks oldest→newest, cleaning until the token target is hit.

| Option | Description |
|---|---|
| `--oldest N` | Target: free ~N% of context tokens from the oldest side (default `30`) |
| `--mode outputs` | (default) Drop old tool-output bodies / strip images, **keep** prompts + assistant text (non-lossy for dialogue; may free less) |
| `--mode hard` | Also collapse old assistant/prompt text — **guarantees** the target, loses old detail |
| `--max-field <chars>` | `outputs` mode: truncate tool outputs longer than this |
| `--apply` / `--force` / `--no-backup` | As above |

```bash
vac prune <id> --oldest 20                 # preview token savings (safe)
vac prune <id> --oldest 20 --mode hard --apply
```

### `vac doctor`
Scan all sessions and flag the ones likely to be wedged — too many images (2000px risk), an oversized single entry (context bomb), or an oversized session. Prints the exact fix command per finding.

| Option | Description |
|---|---|
| `--json` | Machine-readable output |

```bash
vac doctor
```

### `vac archive`
Archive (tar.gz) and remove sessions older than a threshold, by real last-used time. Reversible — extract the tarball to restore.

| Option | Description |
|---|---|
| `--older-than <dur>` | **Required.** Archive sessions last used ≥ this ago (`60d`, `2w`) |
| `--tool kiro\|claude` | Restrict to one tool |
| `--include-active` | Also archive locked/active sessions (not recommended) |
| `--apply` | Write (default: dry-run preview) |

```bash
vac archive --older-than 60d                # preview what would be archived
vac archive --older-than 60d --apply        # tar.gz -> ~/.kiro/sessions/vac-archive-<ts>.tar.gz
```

## Key concepts

**Tokens vs. bytes — which command to use.** Kiro's context **%** counts *tokens*,
not file size. An embedded image is megabytes on disk but only ~1–2K tokens.

- Fixing a *file-size* problem (2000px image error, "connection closed", disk)?
  → **`vac clean`** (removes image bytes).
- Lowering the *context %* / delaying auto-compaction? → **`vac prune`** (removes
  text tokens). `--mode hard` guarantees the target; `--mode outputs` is non-lossy
  for the dialogue but frees less.

**Age is real last-used time.** `--older-than` and the age column use each session's
`updated_at` metadata, not the file mtime — so sessions that `vac` itself rewrote
aren't misflagged as recently used. Durations: `60d`, `2w`, `12h`, `30m`.

**Archiving is reversible.** `vac archive` tar.gz's each session (log + metadata +
history) to `~/.kiro/sessions/vac-archive-<timestamp>.tar.gz`; restore with
`tar -xzf <archive> -C <dir>`.

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
