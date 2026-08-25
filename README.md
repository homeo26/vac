# vac

🧹 Vacuum up cruft from your AI coding-agent sessions. A small, standalone Python CLI that cleans, analyzes, and repairs local session logs for **Kiro CLI** and **Claude Code**.

Long screenshot-heavy sessions accumulate embedded base64 images. Because these agents replay the full conversation every turn, one oversized image (>2000px) can wedge the whole session with:

```
image dimensions exceed max allowed size for many-image requests: 2000 pixels
```

`vac` fixes and prevents that.

## Install

```bash
pipx install vac-cli      # installs the `vac` command
```

## Usage

```bash
vac list                    # inventory sessions: size, image count, live?, at-risk?
vac analyze <id|path>       # what's consuming space in one session
vac clean <id> --keep 3     # GC images, keep the last 3 (dry-run by default)
vac clean <id> --keep 3 --apply   # actually write (creates a .bak)
vac doctor                  # find sessions likely wedged (many/oversized images)
vac list --json             # machine-readable everywhere
```

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

## License

MIT
