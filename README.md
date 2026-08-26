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

| Command | Options | What it does |
|---|---|---|
| `vac list` | `--older-than <dur>` · `--tool kiro\|claude` · `--sort size\|images\|updated\|age` · `--json` | Inventory sessions: size, images, age, live?, at-risk? |
| `vac analyze <id>` | — | Break down one session: size, image count, largest entry, active? |
| `vac clean <id>` | `--keep N` · `--apply` · `--force` · `--no-backup` | Strip embedded images (fixes the 2000px / payload-size crashes). `--keep N` retains the newest N |
| `vac prune <id>` | `--oldest N` · `--mode outputs\|hard` · `--max-field <n>` · `--apply` · `--force` · `--no-backup` | Free ~N% of context **tokens** from the oldest turns (`hard` guarantees the target; `outputs` keeps text) |
| `vac doctor` | `--json` | Flag sessions likely wedged: many images, context-bomb entry, or oversized session |
| `vac name [<id>]` | `--include-generic` · `--llm-cmd "claude -p"` · `--tool` · `--limit N` · `--apply` | AI-name untitled sessions from their content (ChatGPT/Claude-style). Uses a local LLM CLI — no API key. Kiro only (writable title store) |
| `vac archive` | `--older-than <dur>` · `--tool` · `--include-active` · `--apply` | Archive (tar.gz) + remove sessions older than a threshold — reversible |

`<id>` = a session id or a path to a `.jsonl`. `<dur>` = `60d` `2w` `12h` `30m`. Commands that write (`clean`, `prune`, `archive`) are **dry-run by default** — add `--apply`; they create a `.bak`, refuse to edit active/locked sessions (`--force` overrides), and JSON-validate before writing. Works on both **Kiro CLI** and **Claude Code**.

```bash
vac doctor                                  # triage everything
vac clean <id> --keep 1 --apply             # drop old images
vac prune <id> --oldest 20 --mode hard --apply   # free ~20% of context tokens
vac archive --older-than 60d --apply        # reclaim disk (reversible)
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
