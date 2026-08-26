"""Tests for vac core + adapters (Kiro and Claude Code shapes)."""
import json
from datetime import datetime, timezone, timedelta

import pytest

from vac.adapters import KiroStore, ClaudeCodeStore
from vac.core import (
    count_images, clean_images, prune_oldest, entry_tokens,
    parse_duration, age_days, session_file_group, archive_files,
)


def write_jsonl(path, objs):
    path.write_text("\n".join(json.dumps(o) for o in objs) + "\n")
    return path


def valid_jsonl(path) -> bool:
    for l in path.read_text().splitlines():
        if l.strip():
            json.loads(l)  # raises if invalid
    return True


# ---------------- Kiro image handling ----------------

def test_kiro_detects_both_image_forms():
    s = KiroStore()
    assert s.is_image_block({"kind": "image", "data": {}})
    assert s.is_image_block({"Image": {"source": {"kind": "bytes", "data": [1, 2, 3]}}})
    assert not s.is_image_block({"kind": "text", "data": "hi"})


def test_kiro_clean_removes_both_forms(tmp_path):
    s = KiroStore()
    f = write_jsonl(tmp_path / "k.jsonl", [
        {"kind": "Prompt", "data": {"content": [{"kind": "text", "data": "hi"}]}},
        {"kind": "AssistantMessage", "data": {"content": [
            {"kind": "text", "data": "see"},
            {"kind": "image", "data": {"format": "png", "source": {"kind": "bytes", "data": [1] * 50}}}]}},
        {"kind": "ToolResults", "data": {"results": {"t1": {"result": {"Success": {"items": [
            {"Image": {"format": "png", "source": {"kind": "bytes", "data": [2] * 50}}}]}}}}}},
    ])
    assert count_images(f, s.is_image_block) == 2
    res = clean_images(f, s.is_image_block, s.replace_image, keep=0, dry_run=False, backup=False)
    assert res.removed == 2
    assert count_images(f, s.is_image_block) == 0
    assert valid_jsonl(f)
    # tool-result Image must become a plain-string {"Text": "..."} (not nested)
    txt = f.read_text()
    assert '"Text":' in txt
    for l in f.read_text().splitlines():
        o = json.loads(l)
        def check(x):
            if isinstance(x, dict):
                if set(x.keys()) == {"Text"}:
                    assert isinstance(x["Text"], str)
                for v in x.values():
                    check(v)
            elif isinstance(x, list):
                for v in x:
                    check(v)
        check(o)


def test_kiro_clean_keep_n(tmp_path):
    s = KiroStore()
    imgs = [{"kind": "image", "data": {"source": {"kind": "bytes", "data": [i]}}} for i in range(4)]
    f = write_jsonl(tmp_path / "k.jsonl",
                    [{"kind": "AssistantMessage", "data": {"content": [im]}} for im in imgs])
    res = clean_images(f, s.is_image_block, s.replace_image, keep=1, dry_run=False, backup=False)
    assert res.removed == 3
    assert count_images(f, s.is_image_block) == 1


# ---------------- Claude image handling ----------------

def test_claude_detects_and_replaces_image():
    s = ClaudeCodeStore()
    assert s.is_image_block({"type": "image", "source": {"type": "base64", "data": "AAAA"}})
    assert not s.is_image_block({"type": "text", "text": "hi"})
    assert s.replace_image({"type": "image"}, "x") == {"type": "text", "text": "x"}


def test_claude_clean(tmp_path):
    s = ClaudeCodeStore()
    f = write_jsonl(tmp_path / "c.jsonl", [
        {"type": "assistant", "message": {"role": "assistant", "content": [
            {"type": "text", "text": "hi"},
            {"type": "image", "source": {"type": "base64", "data": "B" * 100}}]}},
    ])
    assert count_images(f, s.is_image_block) == 1
    clean_images(f, s.is_image_block, s.replace_image, keep=0, dry_run=False, backup=False)
    assert count_images(f, s.is_image_block) == 0
    assert valid_jsonl(f)


# ---------------- token accounting + prune ----------------

def test_entry_tokens_counts_both_schemas():
    kiro = {"kind": "text", "data": "x" * 400}
    claude = {"type": "text", "text": "y" * 400}
    claude_str = {"message": {"content": "z" * 400}}
    assert entry_tokens(kiro) == 100
    assert entry_tokens(claude) == 100
    assert entry_tokens(claude_str) == 100


def test_prune_hard_frees_target_tokens(tmp_path):
    s = KiroStore()
    # 10 old assistant text entries + a recent one
    objs = [{"kind": "AssistantMessage", "data": {"content": [{"kind": "text", "data": "w" * 4000}]}}
            for _ in range(10)]
    objs.append({"kind": "AssistantMessage", "data": {"content": [{"kind": "text", "data": "recent"}]}})
    f = write_jsonl(tmp_path / "k.jsonl", objs)
    before = sum(entry_tokens(json.loads(l)) for l in f.read_text().splitlines() if l.strip())
    res = prune_oldest(f, s.is_image_block, s.replace_image, oldest_pct=50, mode="hard",
                       dry_run=False, backup=False)
    after = sum(entry_tokens(json.loads(l)) for l in f.read_text().splitlines() if l.strip())
    assert res.freed_tokens >= res.target_tokens          # hard mode hits the target
    assert after < before
    assert res.valid and valid_jsonl(f)


def test_prune_outputs_truncates_toolresult_text(tmp_path):
    s = KiroStore()
    objs = [
        {"kind": "Prompt", "data": {"content": [{"kind": "text", "data": "q"}]}},
        {"kind": "ToolResults", "data": {"content": [{"kind": "text", "data": "T" * 20000}]}},
        {"kind": "AssistantMessage", "data": {"content": [{"kind": "text", "data": "recent"}]}},
    ]
    f = write_jsonl(tmp_path / "k.jsonl", objs)
    res = prune_oldest(f, s.is_image_block, s.replace_image, oldest_pct=60, mode="outputs",
                       max_field_bytes=500, dry_run=False, backup=False)
    assert res.outputs_truncated >= 1
    assert valid_jsonl(f)


def test_prune_dry_run_writes_nothing(tmp_path):
    s = KiroStore()
    f = write_jsonl(tmp_path / "k.jsonl",
                    [{"kind": "AssistantMessage", "data": {"content": [{"kind": "text", "data": "w" * 8000}]}}])
    orig = f.read_text()
    prune_oldest(f, s.is_image_block, s.replace_image, oldest_pct=50, mode="hard", dry_run=True)
    assert f.read_text() == orig


# ---------------- helpers ----------------

@pytest.mark.parametrize("s,days", [("60d", 60), ("2w", 14), ("12h", 0.5), ("30m", 30 / 1440), ("45", 45)])
def test_parse_duration(s, days):
    assert abs(parse_duration(s).total_seconds() / 86400 - days) < 1e-6


def test_parse_duration_bad():
    with pytest.raises(ValueError):
        parse_duration("nope")


def test_age_days_uses_updated_at(tmp_path):
    f = tmp_path / "x.jsonl"; f.write_text("{}\n")
    old = (datetime.now(timezone.utc) - timedelta(days=100)).isoformat().replace("+00:00", "Z")
    assert age_days(old, f) == pytest.approx(100, abs=1)


def test_archive_roundtrip(tmp_path):
    sid = "sess-1"
    for ext in (".jsonl", ".json", ".history"):
        (tmp_path / f"{sid}{ext}").write_text("data")
    grp = session_file_group(tmp_path / f"{sid}.jsonl")
    assert len(grp) == 3
    out = tmp_path / "arch.tar.gz"
    n, total = archive_files([grp], out, remove=True)
    assert n == 3 and out.exists()
    assert not (tmp_path / f"{sid}.jsonl").exists()
    import tarfile
    assert set(tarfile.open(out).getnames()) == {f"{sid}.jsonl", f"{sid}.json", f"{sid}.history"}


# ---------------- AI naming helpers ----------------

def test_build_digest_extracts_text(tmp_path):
    from vac.core import build_digest
    f = write_jsonl(tmp_path / "k.jsonl", [
        {"kind": "Prompt", "data": {"content": [{"kind": "text", "data": "build a carpool app"}]}},
        {"kind": "AssistantMessage", "data": {"content": [{"kind": "text", "data": "sure, here is a plan"}]}},
    ])
    d = build_digest(f)
    assert "carpool app" in d and "here is a plan" in d


def test_build_digest_claude_shape(tmp_path):
    from vac.core import build_digest
    f = write_jsonl(tmp_path / "c.jsonl", [
        {"type": "user", "message": {"role": "user", "content": "explain krypto backfill"}},
    ])
    assert "krypto backfill" in build_digest(f)


def test_sanitize_title():
    from vac.core import _sanitize_title
    assert _sanitize_title('  "My Great Title."  ') == "My Great Title"
    assert _sanitize_title("info line\nActual Title") == "Actual Title"
    assert len(_sanitize_title("x" * 200)) <= 70


def test_generate_title_via_stub_cmd(tmp_path):
    from vac.core import generate_title
    # a stub "LLM" that ignores stdin and prints a fixed title
    assert generate_title("some digest", llm_cmd="printf 'Carpool Organizer App'") == "Carpool Organizer App"


def test_generate_title_empty_digest():
    from vac.core import generate_title
    assert generate_title("", llm_cmd="printf X") is None


def test_kiro_set_title(tmp_path):
    from vac.adapters import KiroStore
    (tmp_path / "s.jsonl").write_text("{}\n")
    (tmp_path / "s.json").write_text(json.dumps({"title": "", "cwd": "/x"}))
    s = KiroStore()
    assert s.set_title(tmp_path / "s.jsonl", "New AI Title") is True
    assert json.loads((tmp_path / "s.json").read_text())["title"] == "New AI Title"
