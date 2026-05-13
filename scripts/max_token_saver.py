#!/usr/bin/env python3
"""Max-Token-Saver: aggressive token savings for Claude Code.

- Compact response enforcement
- Adaptive tool-output filtering (threshold drops as context fills)
- Statusline deduplication
- Memory/context file compression with protected-span validation
- Prompt-risk detection and soft guardrails
- Implementation-contract drift checks
"""

from __future__ import annotations

import argparse
import dataclasses
import hashlib
import json
import os
import re
import shlex
import shutil
import sys
import time
from pathlib import Path
from typing import Any


PLUGIN_NAME = "max-token-saver"
MAX_CAPTURE_CHARS = 80_000
TOOL_FILTER_THRESHOLD = 3_000
TOOL_FILTER_THRESHOLD_HIGH_CONTEXT = 1_500
SUMMARY_HEAD_LINES = 6
SUMMARY_TAIL_LINES = 12
CONTEXT_TARGET_LINES = 150
MAX_STRUCTURED_LINES = 20
MAX_STRUCTURED_ITEMS = 3
MAX_STRUCTURED_STRING = 100
MAX_ERROR_LINES_SHOWN = 5
COMPRESSION_LEVELS = {"light", "medium", "aggressive"}
MTS_MODES = {"compact", "normal", "off"}
STATUSLINE_DEDUP_SECONDS = 30
REPEAT_COMMAND_THRESHOLD = 2

DEFAULT_MEMORY_FILES = (
    "CLAUDE.md",
    "AGENTS.md",
    "GEMINI.md",
    ".claude/CLAUDE.md",
    ".claude/rules",
    ".cursor/rules",
    ".windsurf/rules",
)

NOISY_COMMAND_RE = re.compile(
    r"\b(test|pytest|vitest|jest|mocha|rspec|cargo\s+test|go\s+test|npm\s+test|pnpm\s+test|yarn\s+test|"
    r"build|tsc|eslint|ruff|mypy|grep|rg|find|ls|cat|tail|docker|kubectl|journalctl)\b",
    re.IGNORECASE,
)

HIGH_RISK_PROMPT_RE = re.compile(
    r"\b("
    r"fix (it|errors?|bugs?|everything)|"
    r"make (it|this) better|"
    r"review (everything|this repo|all)|"
    r"build (me )?(an? )?(?:[\w-]+\s+){0,6}(app|game|website|site)|"
    r"implement (everything|the whole thing)|"
    r"refactor (everything|the repo|all)"
    r")\b",
    re.IGNORECASE,
)

FENCED_CODE_RE = re.compile(r"```.*?```", re.DOTALL)
INLINE_CODE_RE = re.compile(r"`[^`\n]+`")
URL_RE = re.compile(r"https?://[^\s)>\"]+")
PATH_RE = re.compile(r"(?:\./|\../|/|[A-Za-z]:\\)[\w./\\~:@%+=,-]+")
ENV_RE = re.compile(r"\b[A-Z][A-Z0-9_]{2,}\b")
VERSION_RE = re.compile(r"\b(?:v?\d+\.\d+(?:\.\d+)?|\d{4}-\d{2}-\d{2}|#[0-9]+)\b")
HEADING_RE = re.compile(r"^(#{1,6}\s+.+)$", re.MULTILINE)
COMMAND_RE = re.compile(
    r"`([^`\n]*(?:npm|pnpm|yarn|python3?|node|git|cargo|go|uv|pip|docker|kubectl|make|npx|bun|claude)[^`\n]*)`"
)
FRONTMATTER_RE = re.compile(r"\A---\n(.*?)\n---", re.DOTALL)
FRONTMATTER_KEY_RE = re.compile(r"^([A-Za-z0-9_-]+):", re.MULTILINE)
WARNING_RE = re.compile(r"^.*\b(?:warning|danger|critical|destructive|irreversible|do not)\b.*$", re.I | re.M)
SIGNAL_KEY_RE = re.compile(
    r"(error|warning|fail|exception|trace|stack|message|reason|status|code|"
    r"path|file|line|column|test|summary|detail|selected|request|finding|"
    r"endpoint|url|severity|title|evidence|auth|authorization|location)",
    re.IGNORECASE,
)
BULK_KEY_RE = re.compile(r"(stdout|stderr|output|content|body|text|items|results|entries|logs?)", re.IGNORECASE)
NEVER_REWRITE_TOOL_RE = re.compile(
    r"^(Read|Write|Edit|MultiEdit|NotebookEdit|TodoWrite|TaskOutput|AskUserQuestion|ExitPlanMode)$"
)
TOOL_FILTER_ALLOW_RE = re.compile(r"^(Bash|Grep|Glob|LS|Task|WebFetch|WebSearch|mcp__.*)$")


@dataclasses.dataclass(frozen=True)
class ProtectedSpan:
    kind: str
    value: str


@dataclasses.dataclass(frozen=True)
class PositionedSpan:
    kind: str
    value: str
    start: int
    end: int


@dataclasses.dataclass(frozen=True)
class ToolSnapshot:
    tool_name: str
    tool_label: str
    command: str
    exit_code: Any
    stdout: str
    stderr: str
    raw_text: str
    raw_chars: int
    raw_tokens_estimate: int
    payload_kind: str
    confidence: str
    structured_lines: tuple[str, ...]
    duration_ms: int | None


@dataclasses.dataclass(frozen=True)
class CompressionResult:
    rc: int
    payload: dict[str, Any]


def data_dir() -> Path:
    raw = os.environ.get("CLAUDE_PLUGIN_DATA")
    preferred = Path(raw) if raw else Path.home() / ".claude" / "plugins" / PLUGIN_NAME
    fallback = Path(os.environ.get("TMPDIR", "/tmp")) / f"{PLUGIN_NAME}-plugin-data"
    for path in (preferred, fallback):
        try:
            path.mkdir(parents=True, exist_ok=True)
            return path
        except OSError:
            continue
    return Path.cwd()


def ledger_path() -> Path:
    return data_dir() / "ledger.jsonl"


def contracts_dir() -> Path:
    path = data_dir() / "contracts"
    path.mkdir(parents=True, exist_ok=True)
    return path


def overrides_path() -> Path:
    return data_dir() / "overrides.json"


def estimate_tokens(text: str) -> int:
    if not text:
        return 0
    words = re.findall(r"\S+", text)
    return max(1, round(max(len(words) * 1.25, len(text) / 4)))


def token_estimate_label(tokens: int) -> str:
    return f"~{tokens}tok"


def slugify(text: str, default: str = "task", max_length: int = 48) -> str:
    slug = re.sub(r"[^a-zA-Z0-9]+", "-", text.strip().lower()).strip("-")
    slug = re.sub(r"-+", "-", slug)
    if not slug:
        slug = default
    return slug[:max_length].strip("-") or default


def timestamp() -> str:
    return time.strftime("%Y%m%d-%H%M%S")


def stable_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()[:16]


def read_stdin_json() -> dict[str, Any]:
    raw = sys.stdin.read()
    if not raw.strip():
        return {}
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        if len(raw) > MAX_CAPTURE_CHARS:
            raw = raw[:MAX_CAPTURE_CHARS]
        return {"raw_stdin": raw}


def write_json(obj: dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(obj, ensure_ascii=False))


_last_statusline_ts: float = 0.0
_last_statusline_hash: str = ""


def append_ledger(event: str, payload: dict[str, Any]) -> None:
    global _last_statusline_ts, _last_statusline_hash
    if event == "statusline_snapshot":
        now = time.time()
        payload_hash = stable_hash(json.dumps(payload, sort_keys=True))
        if payload_hash == _last_statusline_hash and (now - _last_statusline_ts) < STATUSLINE_DEDUP_SECONDS:
            return
        _last_statusline_ts = now
        _last_statusline_hash = payload_hash
    record = {
        "ts": round(time.time(), 3),
        "event": event,
        "session_id": payload.get("session_id") or payload.get("sessionId"),
        "cwd": payload.get("cwd") or os.getcwd(),
        "payload": payload,
    }
    with ledger_path().open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False) + "\n")


def load_ledger(limit: int | None = None) -> list[dict[str, Any]]:
    path = ledger_path()
    if not path.exists():
        return []
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    if limit:
        lines = lines[-limit:]
    records: list[dict[str, Any]] = []
    for line in lines:
        try:
            records.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return records


def load_overrides() -> dict[str, Any]:
    path = overrides_path()
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}


def save_overrides(overrides: dict[str, Any]) -> None:
    overrides_path().write_text(json.dumps(overrides, indent=2) + "\n", encoding="utf-8")


def default_mts_mode() -> str:
    raw = os.environ.get("MTS_DEFAULT_MODE", "compact").strip().lower()
    return raw if raw in MTS_MODES else "compact"


def get_mts_mode() -> str:
    overrides = load_overrides()
    mode = str(overrides.get("mode") or default_mts_mode()).strip().lower()
    return mode if mode in MTS_MODES else "compact"


def set_mts_mode(mode: str, quiet: bool = False) -> int:
    mode = mode.strip().lower()
    if mode not in MTS_MODES:
        if not quiet:
            print(f"Invalid mode: {mode}. Use compact, normal, or off.")
        return 2
    overrides = load_overrides()
    overrides["mode"] = mode
    overrides["mode_updated_at"] = timestamp()
    save_overrides(overrides)
    if not quiet:
        print(f"Max-Token-Saver mode: {mode}")
    return 0


def caveman_active() -> bool:
    if os.environ.get("MTS_IGNORE_CAVEMAN") == "1":
        return False
    marker = Path.home() / ".claude" / ".caveman-active"
    try:
        if marker.exists():
            raw = marker.read_text(encoding="utf-8", errors="replace").strip().lower()
            if raw and raw not in {"0", "false", "off", "disabled"}:
                return True
    except OSError:
        pass
    settings_path = Path.home() / ".claude" / "settings.json"
    try:
        data = json.loads(settings_path.read_text(encoding="utf-8"))
        enabled = data.get("enabledPlugins") or {}
        if any("caveman" in str(name).lower() and bool(value) for name, value in enabled.items()):
            return True
    except (OSError, json.JSONDecodeError):
        pass
    return False


def mts_response_context(mode: str | None = None) -> str:
    mode = mode or get_mts_mode()
    if mode == "off":
        return ""
    quality_floor = "Quality floor: correctness first; never skip verification or claim untested passes."
    if mode == "normal":
        return "MAX-TOKEN-SAVER normal. Avoid filler. " + quality_floor
    if caveman_active():
        return ""
    return (
        "MAX-TOKEN-SAVER COMPACT. Answer-first. No pleasantries/restating/narration/padding. "
        "Shortest complete response. No unrequested caveats/examples/rationale. "
        "Bullets only when shorter. bug->fix->verify. " + quality_floor
    )


def session_start_context() -> str:
    mode = get_mts_mode()
    if mode == "off":
        return "Max-Token-Saver off; hooks track telemetry+filtering."
    if mode == "compact" and caveman_active():
        return (
            "Max-Token-Saver compact response reinforcement paused because Caveman appears active. "
            "Max-Token-Saver still runs telemetry, memory compression, tool-output filtering, prompt guidance, and drift guardrails."
        )
    return (
        f"MAX-TOKEN-SAVER {mode}. "
        + mts_response_context(mode)
        + " Soft-suggest on vague prompts."
    )


def set_full_output(count: int = 1) -> int:
    count = max(1, count)
    overrides = load_overrides()
    overrides["full_output_remaining"] = count
    save_overrides(overrides)
    print(f"Max-Token-Saver full-output override enabled for next {count} tool call(s).")
    return 0


def consume_full_output_override() -> bool:
    overrides = load_overrides()
    remaining = int(overrides.get("full_output_remaining") or 0)
    if remaining <= 0:
        return False
    if remaining == 1:
        overrides.pop("full_output_remaining", None)
    else:
        overrides["full_output_remaining"] = remaining - 1
    save_overrides(overrides)
    return True


def positioned_spans(text: str) -> list[PositionedSpan]:
    spans: list[PositionedSpan] = []
    for regex, kind in (
        (FENCED_CODE_RE, "fenced code block"),
        (INLINE_CODE_RE, "inline code"),
        (URL_RE, "URL"),
        (PATH_RE, "path"),
        (ENV_RE, "env/API token"),
        (VERSION_RE, "number/date/version"),
        (HEADING_RE, "heading"),
        (WARNING_RE, "warning constraint"),
    ):
        spans.extend(PositionedSpan(kind, match.group(0), match.start(), match.end()) for match in regex.finditer(text))

    for match in COMMAND_RE.finditer(text):
        spans.append(PositionedSpan("command", match.group(1), match.start(1), match.end(1)))

    frontmatter = FRONTMATTER_RE.search(text)
    if frontmatter:
        for match in FRONTMATTER_KEY_RE.finditer(frontmatter.group(1)):
            start = frontmatter.start(1) + match.start(1)
            end = frontmatter.start(1) + match.end(1)
            spans.append(PositionedSpan("frontmatter key", match.group(1), start, end))

    spans.sort(key=lambda span: (-(span.end - span.start), span.start))
    selected: list[PositionedSpan] = []
    occupied: list[tuple[int, int]] = []
    for span in spans:
        if any(not (span.end <= start or span.start >= end) for start, end in occupied):
            continue
        selected.append(span)
        occupied.append((span.start, span.end))
    selected.sort(key=lambda span: span.start)
    return selected


def protected_spans(text: str) -> list[ProtectedSpan]:
    spans = [ProtectedSpan(span.kind, span.value) for span in positioned_spans(text)]
    seen: set[tuple[str, str]] = set()
    unique: list[ProtectedSpan] = []
    for span in spans:
        key = (span.kind, span.value)
        if key not in seen:
            seen.add(key)
            unique.append(span)
    return unique


def mark_protected(source: Path, dest: Path | None = None, quiet: bool = False) -> int:
    text = source.read_text(encoding="utf-8", errors="replace")
    spans = positioned_spans(text)
    if dest is None:
        dest = source.with_suffix(source.suffix + ".protected")

    parts: list[str] = []
    cursor = 0
    for index, span in enumerate(spans, start=1):
        parts.append(text[cursor:span.start])
        digest = stable_hash(span.value)
        kind = span.kind.replace('"', "")
        parts.append(f'<protect id="{index}" kind="{kind}" sha="{digest}">')
        parts.append(span.value)
        parts.append("</protect>")
        cursor = span.end
    parts.append(text[cursor:])
    marked = "".join(parts)
    dest.write_text(marked, encoding="utf-8")

    before = estimate_tokens(text)
    after = estimate_tokens(marked)
    if not quiet:
        print(f"Marked protected spans: {len(spans)}")
        print(f"Source: {token_estimate_label(before)}")
        print(f"Marked: {token_estimate_label(after)}")
        print(f"Wrote: {dest}")
    return 0


def strip_protect(source: Path, dest: Path | None = None, quiet: bool = False) -> int:
    text = source.read_text(encoding="utf-8", errors="replace")
    stripped = re.sub(r'<protect\b[^>]*>(.*?)</protect>', lambda match: match.group(1), text, flags=re.DOTALL)
    if dest is None:
        dest = source.with_suffix(source.suffix + ".stripped")
    dest.write_text(stripped, encoding="utf-8")
    if not quiet:
        print(f"Removed protect markers: {text.count('<protect ')}")
        print(f"Wrote: {dest}")
    return 0


def recover_protected(original: Path, candidate: Path, output: Path | None = None, quiet: bool = False) -> int:
    original_text = original.read_text(encoding="utf-8", errors="replace")
    candidate_text = candidate.read_text(encoding="utf-8", errors="replace")
    original_spans = protected_spans(original_text)
    candidate_spans = positioned_spans(candidate_text)

    if output is None:
        output = candidate
    if not original_spans:
        if output != candidate:
            shutil.copyfile(candidate, output)
        if not quiet:
            print("No protected spans found in original.")
        return 0
    if len(candidate_spans) < len(original_spans):
        if not quiet:
            print(
                f"Recovery refused: compressed has fewer span positions "
                f"({len(candidate_spans)}) than original ({len(original_spans)})."
            )
        return 2

    parts: list[str] = []
    cursor = 0
    replacements = 0
    for original_span, candidate_span in zip(original_spans, candidate_spans):
        parts.append(candidate_text[cursor:candidate_span.start])
        if candidate_span.value != original_span.value:
            replacements += 1
        parts.append(original_span.value)
        cursor = candidate_span.end
    parts.append(candidate_text[cursor:])
    recovered = "".join(parts)
    output.write_text(recovered, encoding="utf-8")

    if not quiet:
        print(f"Recovered protected spans: {replacements}")
        print(f"Wrote: {output}")
    return validate_file(original, output, quiet=quiet)


def compression_prompt(level: str) -> str:
    return f"""You are compressing a Claude Code memory/instructions file to reduce recurring context usage.

**Level:** {level}

**Goal:** Make the rewritten file materially shorter while preserving meaning.

**Style:** Professional, dense; prefer compact bullets and semicolons over paragraphs. Remove filler, repetition, hedging.

**Strict rules:**
- Do NOT edit/delete/summarize anything inside `<protect>...</protect>` blocks
- Do NOT remove warnings or "do not" rules
- Do NOT invent new rules
- Keep protected blocks in order

**Level guidelines:**
- light: Remove filler; target 15-30% reduction outside protected blocks
- medium: Collapse to decision bullets; target 35-55% reduction
- aggressive: Keep only rules, facts, commands, risks, decisions; target 50-70% reduction

Output **only** the full rewritten file content. No explanations.

Now rewrite:
"""


def compression_workspace(target: Path) -> Path:
    return data_dir() / "compression" / slugify(str(target.resolve()), "target")


def compression_min_savings(level: str, original_tokens: int) -> float:
    if original_tokens < 500:
        return 0.0
    if level == "light":
        return 10.0
    if level == "aggressive":
        return 35.0
    return 25.0


def stronger_compression_level(level: str) -> str | None:
    if level == "light":
        return "medium"
    if level == "medium":
        return "aggressive"
    return None


def compress_auto_command(target: Path, level: str) -> str:
    return " ".join(
        shlex.quote(part)
        for part in (
            "python3",
            str(Path(__file__).resolve()),
            "compress",
            str(target),
            "--level",
            level,
            "--auto",
        )
    )


def create_compression_manifest(target: Path, level: str = "medium", quiet: bool = False) -> tuple[int, dict[str, Any] | None]:
    if level not in COMPRESSION_LEVELS:
        print(f"Invalid compression level: {level}. Use: {', '.join(sorted(COMPRESSION_LEVELS))}")
        return 2, None
    if not target.exists() or not target.is_file():
        print(f"Target not found: {target}")
        return 1, None

    workspace = compression_workspace(target)
    workspace.mkdir(parents=True, exist_ok=True)
    stamp = timestamp()
    suffix = target.suffix or ".txt"
    backup = workspace / f"{target.stem}.mts-backup.{stamp}{suffix}"
    marked = workspace / f"{target.stem}.mts-marked.{stamp}{suffix}"
    draft = workspace / f"{target.stem}.mts-draft.{stamp}{suffix}"
    prompt_path = workspace / f"{target.stem}.compress-prompt.{stamp}.txt"

    shutil.copyfile(target, backup)
    mark_protected(backup, marked, quiet=quiet)
    shutil.copyfile(marked, draft)
    prompt_path.write_text(compression_prompt(level), encoding="utf-8")

    before_tokens = estimate_tokens(target.read_text(encoding="utf-8", errors="replace"))
    manifest = {
        "target": str(target),
        "level": level,
        "backup": str(backup),
        "marked": str(marked),
        "draft": str(draft),
        "prompt": str(prompt_path),
        "created_at": stamp,
        "original_tokens_estimate": before_tokens,
    }
    (workspace / "latest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    return 0, manifest


def compress_prepare(target: Path, level: str = "medium") -> int:
    rc, manifest = create_compression_manifest(target, level)
    if rc != 0 or manifest is None:
        return rc
    print("Max-Token-Saver compression prepared.")
    print(f"- Target: {manifest['target']}")
    print(f"- Level: {manifest['level']}")
    print(f"- Backup: {manifest['backup']}")
    print(f"- Draft: {manifest['draft']}")
    print(f"- Prompt: {manifest['prompt']}")
    print(f"- Original: {token_estimate_label(int(manifest['original_tokens_estimate']))}")
    print(f"\nFinalize: {finalize_command(Path(manifest['target']), Path(manifest['draft']))}")
    return 0


def finalize_command(target: Path, draft: Path) -> str:
    return " ".join(
        shlex.quote(part)
        for part in (
            "python3",
            str(Path(__file__).resolve()),
            "compress",
            str(target),
            "--finalize",
            "--draft",
            str(draft),
        )
    )


def compress_auto(target: Path, level: str = "medium") -> int:
    rc, manifest = create_compression_manifest(target, level, quiet=True)
    if rc != 0 or manifest is None:
        return rc

    prompt = Path(manifest["prompt"]).read_text(encoding="utf-8", errors="replace")
    marked_content = Path(manifest["draft"]).read_text(encoding="utf-8", errors="replace")
    next_level = stronger_compression_level(level)
    payload = {
        "mode": "mts_compress_auto",
        "instruction": "Rewrite marked_content using prompt. Save only rewritten content to draft_path, then run finalize_command_json. If status=quality_guard_failed and next_level present, rerun retry_auto_command once.",
        "target": manifest["target"],
        "level": manifest["level"],
        "backup_path": manifest["backup"],
        "draft_path": manifest["draft"],
        "prompt": prompt,
        "marked_content": marked_content,
        "finalize_command": finalize_command(Path(manifest["target"]), Path(manifest["draft"])),
        "finalize_command_json": finalize_command(Path(manifest["target"]), Path(manifest["draft"])) + " --json",
        "next_level": next_level,
        "retry_auto_command": compress_auto_command(Path(manifest["target"]), next_level) if next_level else None,
        "quality_guard_min_savings_percent": compression_min_savings(level, int(manifest["original_tokens_estimate"])),
        "original_tokens_estimate": manifest["original_tokens_estimate"],
    }
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


def compress_finalize(target: Path, draft: Path | None = None, json_output: bool = False) -> int:
    workspace = compression_workspace(target)
    manifest_path = workspace / "latest.json"
    if not manifest_path.exists():
        print(f"No manifest found for {target}. Run compress --prepare first.")
        return 1
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    backup = Path(manifest["backup"])
    if draft is None:
        draft = Path(manifest["draft"])
    if not draft.exists():
        print(f"Draft not found: {draft}")
        return 1

    strip_protect(draft, target, quiet=json_output)
    rc = validate_file(backup, target, quiet=json_output)
    recovered = False
    restored = False
    quality_guard_failed = False
    if rc != 0:
        rc = recover_protected(backup, target, target, quiet=json_output)
        recovered = rc == 0
    if rc != 0:
        shutil.copyfile(backup, target)
        restored = True

    after_tokens = estimate_tokens(target.read_text(encoding="utf-8", errors="replace"))
    before_tokens = int(manifest.get("original_tokens_estimate") or 0)
    saved = 100 * (before_tokens - after_tokens) / before_tokens if before_tokens else 0
    level = str(manifest.get("level") or "medium")
    min_savings = compression_min_savings(level, before_tokens)
    next_level = stronger_compression_level(level)
    if rc == 0 and min_savings and saved < min_savings and os.environ.get("MTS_ALLOW_LOW_SAVINGS") != "1":
        quality_guard_failed = True
        shutil.copyfile(backup, target)
        restored = True
        rc = 3

    status = "success" if rc == 0 else ("quality_guard_failed" if quality_guard_failed else "failed")
    retry_command = compress_auto_command(Path(manifest["target"]), next_level) if quality_guard_failed and next_level else None
    payload = {
        "status": status,
        "target": str(target),
        "level": level,
        "original_tokens_estimate": before_tokens,
        "compressed_tokens_estimate": after_tokens,
        "memory_saved_percent": round(saved, 1),
        "recovery_used": recovered,
        "backup_restored": restored,
        "quality_guard_failed": quality_guard_failed,
        "min_savings_percent": min_savings,
        "backup_path": str(backup),
        "next_level": next_level,
        "recommended_retry_command": retry_command,
    }

    append_ledger("memory_compression_finalized", {
        "target": str(target), "level": level,
        "original_tokens_estimate": before_tokens,
        "compressed_tokens_estimate": after_tokens,
        "memory_saved_percent": round(saved, 1),
        "success": rc == 0,
    })

    if json_output:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print(f"{'Compressed' if rc == 0 else 'Failed'}: {target}")
        print(f"  {token_estimate_label(before_tokens)} -> {token_estimate_label(after_tokens)} ({saved:.1f}% saved)")
        if restored:
            print("  Backup restored.")
        if retry_command:
            print(f"  Retry: {retry_command}")
    return rc


def compress_command(target: Path, level: str, prepare: bool, finalize: bool, auto: bool, draft: Path | None, json_output: bool) -> int:
    if auto:
        return compress_auto(target, level)
    if finalize:
        return compress_finalize(target, draft, json_output=json_output)
    return compress_prepare(target, level)


def validate_file(original: Path, compressed: Path, quiet: bool = False) -> int:
    before = original.read_text(encoding="utf-8", errors="replace")
    after = compressed.read_text(encoding="utf-8", errors="replace")
    missing = [span for span in protected_spans(before) if span.value not in after]
    before_tokens = estimate_tokens(before)
    after_tokens = estimate_tokens(after)
    saved = 100 * (before_tokens - after_tokens) / before_tokens if before_tokens else 0

    if not quiet:
        print(f"Original: {token_estimate_label(before_tokens)} | Compressed: {token_estimate_label(after_tokens)} | Saved: {saved:.1f}%")
    if missing:
        if not quiet:
            print(f"FAILED: {len(missing)} protected spans missing")
            for span in missing[:10]:
                value = span.value.replace("\n", "\\n")[:120]
                print(f"  - {span.kind}: {value}")
        return 2
    if not quiet:
        print("Validation passed.")
    return 0


def discover_memory_paths(paths: list[Path]) -> list[Path]:
    candidates: list[Path] = []
    raw_paths = paths or [Path(p) for p in DEFAULT_MEMORY_FILES]
    for path in raw_paths:
        if path.is_dir():
            candidates.extend(p for p in path.rglob("*") if p.is_file() and p.suffix.lower() in {".md", ".txt", ".mdx"})
        elif path.is_file():
            candidates.append(path)
    return sorted(set(candidates))


def duplicate_line_count(text: str) -> int:
    normalized = []
    for line in text.splitlines():
        item = re.sub(r"\s+", " ", line.strip()).lower()
        if len(item) >= 24:
            normalized.append(item)
    seen: set[str] = set()
    return sum(1 for item in normalized if item in seen or not seen.add(item))


def audit(paths: list[Path]) -> int:
    files = discover_memory_paths(paths)
    if not files:
        print("No memory/context files found.")
        return 1

    rows = []
    total_tokens = 0
    for file in files:
        text = file.read_text(encoding="utf-8", errors="replace")
        tokens = estimate_tokens(text)
        total_tokens += tokens
        lines = text.count("\n") + 1 if text else 0
        protected = len(protected_spans(text))
        duplicate_lines = duplicate_line_count(text)
        severity = "high" if lines > CONTEXT_TARGET_LINES or tokens > 3000 else ("medium" if duplicate_lines > 8 or tokens > 1500 else "ok")
        rows.append((tokens, file, lines, duplicate_lines, protected, severity))

    rows.sort(reverse=True)
    print(f"Max-Token-Saver audit | Total: {token_estimate_label(total_tokens)}")
    for tokens, file, lines, dupes, protected, severity in rows:
        print(f"  {file}: {token_estimate_label(tokens)} {lines}L {dupes}dup [{severity}]")
    print("\nRecommendations: compress always-loaded files; move runbooks to on-demand; use /mts:plan for broad tasks.")
    return 0


def status() -> int:
    lifetime_records = load_ledger()
    records = lifetime_records[-500:]
    if not records:
        print("Max-Token-Saver: no events yet. Run a session then retry.")
        return 0

    tool_blocked = 0
    prompt_suggestions = 0
    failures = 0
    compactions = 0
    by_command: dict[str, int] = {}

    for record in records:
        event = record.get("event")
        payload = record.get("payload", {})
        if event == "tool_output_filtered":
            blocked = int(payload.get("tokens_blocked_estimate") or 0)
            tool_blocked += blocked
            source = str(payload.get("command") or payload.get("tool_name") or "?").split()[0]
            by_command[source] = by_command.get(source, 0) + blocked
        elif event == "prompt_risk_suggested":
            prompt_suggestions += 1
        elif event == "tool_failure":
            failures += 1
        elif event == "pre_compact":
            compactions += 1

    lifetime_blocked = sum(
        int((r.get("payload") or {}).get("tokens_blocked_estimate") or 0)
        for r in lifetime_records if r.get("event") in {"tool_output_filtered", "tool_failure_summary"}
    )

    print(f"Max-Token-Saver | mode: {get_mts_mode()} | caveman: {'yes' if caveman_active() else 'no'}")
    print(f"  Session: blocked ~{tool_blocked}tok | suggestions: {prompt_suggestions} | failures: {failures} | compactions: {compactions}")
    print(f"  Lifetime blocked: ~{lifetime_blocked}tok")
    if by_command:
        print("  Waste heat map:")
        for cmd, tok in sorted(by_command.items(), key=lambda x: x[1], reverse=True)[:5]:
            print(f"    {cmd}: ~{tok}tok")
    return 0


def nested_get(data: dict[str, Any], *paths: str) -> Any:
    for path in paths:
        current: Any = data
        found = True
        for part in path.split("."):
            if isinstance(current, dict) and part in current:
                current = current[part]
            else:
                found = False
                break
        if found:
            return current
    return None


def compact_statusline_fields(data: dict[str, Any]) -> dict[str, Any]:
    return {
        "model": nested_get(data, "model.display_name", "model.name", "model"),
        "context": nested_get(data, "context_window.current_usage", "context.current_usage", "context_percentage"),
        "context_tokens": nested_get(data, "context_window.current_tokens", "context.current_tokens", "tokens.context"),
        "session_tokens": nested_get(data, "session.total_tokens", "usage.total_tokens", "tokens.total"),
        "cache_read": nested_get(data, "tokens.cache_read", "cache_read_tokens", "usage.cache_read_tokens"),
        "cache_write": nested_get(data, "tokens.cache_write", "cache_write_tokens", "usage.cache_write_tokens"),
        "five_hour": nested_get(data, "rate_limits.five_hour.percent_used", "rate_limits.five_hour", "five_hour_usage"),
        "seven_day": nested_get(data, "rate_limits.seven_day.percent_used", "rate_limits.seven_day", "seven_day_usage"),
    }


def statusline() -> int:
    data = read_stdin_json()
    fields = compact_statusline_fields(data)
    append_ledger("statusline_snapshot", fields)

    context_pct = None
    if fields.get("context") is not None:
        try:
            context_pct = int(str(fields["context"]).rstrip("%"))
        except (ValueError, TypeError):
            pass
    if context_pct is not None:
        overrides = load_overrides()
        overrides["last_context_pct"] = context_pct
        save_overrides(overrides)

    records = load_ledger(limit=200)
    blocked = sum(
        int((r.get("payload") or {}).get("tokens_blocked_estimate") or 0)
        for r in records if r.get("event") == "tool_output_filtered"
    )

    pieces = ["MTS"]
    mode = get_mts_mode()
    if mode != "off":
        pieces.append(mode)
    if fields.get("context") is not None:
        pieces.append(f"ctx {fields['context']}")
    if fields.get("five_hour") is not None:
        pieces.append(f"5h {fields['five_hour']}")
    if blocked:
        pieces.append(f"blocked ~{blocked}t")
    print(" | ".join(str(p) for p in pieces))
    return 0


# --- Tool output filtering ---

def looks_like_json_text(text: str) -> bool:
    stripped = text.strip()
    return stripped.startswith("{") or stripped.startswith("[")


def safe_json_loads(text: str) -> Any | None:
    if not text or not looks_like_json_text(text):
        return None
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return None


def clip_inline(value: Any, max_chars: int = MAX_STRUCTURED_STRING) -> str:
    text = re.sub(r"\s+", " ", str(value)).strip()
    return text if len(text) <= max_chars else text[:max_chars - 3] + "..."


def signal_key(key: str) -> bool:
    return bool(SIGNAL_KEY_RE.search(key))


def bulk_key(key: str) -> bool:
    return bool(BULK_KEY_RE.search(key))


def container_preview(value: Any) -> str:
    if isinstance(value, dict):
        return f"obj({len(value)})"
    if isinstance(value, list):
        return f"list({len(value)})"
    return type(value).__name__


def collect_structured_lines(value: Any, path: str = "", lines: list[str] | None = None, budget: int = MAX_STRUCTURED_LINES) -> list[str]:
    if lines is None:
        lines = []
    if len(lines) >= budget:
        return lines

    if isinstance(value, dict):
        keys = list(value.keys())
        ordered = sorted(keys, key=lambda k: (not signal_key(str(k)), str(k).lower()))
        for key in ordered[:MAX_STRUCTURED_ITEMS * 2]:
            child = value[key]
            child_path = f"{path}.{key}" if path else str(key)
            if isinstance(child, (str, int, float, bool)) or child is None:
                if bulk_key(str(key)) and isinstance(child, str) and len(child) > MAX_STRUCTURED_STRING:
                    lines.append(f"{child_path}: {len(child)}ch")
                else:
                    lines.append(f"{child_path}: {clip_inline(child)}")
            elif isinstance(child, (dict, list)):
                lines.append(f"{child_path}: {container_preview(child)}")
                if signal_key(str(key)) or len(lines) < MAX_STRUCTURED_ITEMS:
                    collect_structured_lines(child, child_path, lines, budget)
            else:
                lines.append(f"{child_path}: {clip_inline(child)}")
            if len(lines) >= budget:
                break
        return lines

    if isinstance(value, list):
        lines.append(f"{path or 'items'}: list({len(value)})")
        for i, child in enumerate(value[:MAX_STRUCTURED_ITEMS]):
            child_path = f"{path}[{i}]" if path else f"items[{i}]"
            if isinstance(child, (dict, list)):
                lines.append(f"{child_path}: {container_preview(child)}")
                collect_structured_lines(child, child_path, lines, budget)
            else:
                lines.append(f"{child_path}: {clip_inline(child)}")
            if len(lines) >= budget:
                break
        return lines

    lines.append(f"{path or 'value'}: {clip_inline(value)}")
    return lines


def stringify_response(response: Any) -> str:
    if response is None:
        return ""
    if isinstance(response, str):
        return response
    if isinstance(response, (dict, list)):
        try:
            return json.dumps(response, ensure_ascii=False, indent=2)
        except TypeError:
            return str(response)
    return str(response)


def tool_name_from_event(data: dict[str, Any], tool_input: dict[str, Any]) -> str:
    return str(
        data.get("tool_name") or data.get("tool") or tool_input.get("tool")
        or tool_input.get("name") or ("Bash" if tool_input.get("command") or tool_input.get("cmd") else "Tool")
    )


def build_tool_snapshot(data: dict[str, Any]) -> ToolSnapshot:
    tool_input = data.get("tool_input") or {}
    response = data.get("tool_response") or {}
    tool_name = tool_name_from_event(data, tool_input)
    command = str(tool_input.get("command") or tool_input.get("cmd") or "")
    stdout = response.get("stdout")
    stderr = response.get("stderr")
    output = response.get("output")
    exit_code = response.get("exit_code", response.get("status", "unknown"))

    structured_value = None
    confidence = "low"
    payload_kind = "text"

    if isinstance(output, (dict, list)):
        structured_value = output
        payload_kind = "structured"
        confidence = "high"
    elif isinstance(output, str):
        parsed = safe_json_loads(output)
        if isinstance(parsed, (dict, list)):
            structured_value = parsed
            payload_kind = "json-text"
            confidence = "medium"
    elif isinstance(response, dict) and any(isinstance(response.get(k), (dict, list)) for k in ("result", "data", "payload")):
        for k in ("result", "data", "payload"):
            candidate = response.get(k)
            if isinstance(candidate, (dict, list)):
                structured_value = candidate
                payload_kind = f"response-{k}"
                confidence = "high"
                break
    elif isinstance(response, dict):
        response_view = {k: v for k, v in response.items() if k not in {"stdout", "stderr", "output", "interrupted", "isImage"}}
        if response_view and any(isinstance(v, (dict, list)) for v in response_view.values()):
            structured_value = response_view
            payload_kind = "response-object"
            confidence = "medium"

    raw_parts = []
    if isinstance(stdout, str) and stdout:
        raw_parts.append(stdout)
    if isinstance(stderr, str) and stderr:
        raw_parts.append(stderr)
    if output and not isinstance(output, str):
        raw_parts.append(stringify_response(output))
    elif isinstance(output, str) and output and output not in raw_parts:
        raw_parts.append(output)
    raw_text = "\n".join(p for p in raw_parts if p)

    structured_lines: tuple[str, ...] = ()
    if structured_value is not None:
        structured_lines = tuple(collect_structured_lines(structured_value))

    tool_label = f"{tool_name}: {command}" if command else tool_name
    return ToolSnapshot(
        tool_name=tool_name, tool_label=tool_label, command=command,
        exit_code=exit_code, stdout=stdout if isinstance(stdout, str) else "",
        stderr=stderr if isinstance(stderr, str) else "", raw_text=raw_text,
        raw_chars=len(raw_text), raw_tokens_estimate=estimate_tokens(raw_text),
        payload_kind=payload_kind, confidence=confidence,
        structured_lines=structured_lines, duration_ms=data.get("duration_ms"),
    )


def exit_is_failure(exit_code: Any) -> bool:
    return str(exit_code).lower() not in {"0", "success", "true", "none"}


def get_effective_threshold() -> int:
    overrides = load_overrides()
    context_pct = overrides.get("last_context_pct")
    if context_pct and int(context_pct) > 50:
        return TOOL_FILTER_THRESHOLD_HIGH_CONTEXT
    return TOOL_FILTER_THRESHOLD


def should_filter_snapshot(snapshot: ToolSnapshot, failed: bool = False) -> tuple[bool, str]:
    if NEVER_REWRITE_TOOL_RE.match(snapshot.tool_name):
        return False, "tool-blocklist"

    threshold = get_effective_threshold()
    structured_heavy = bool(snapshot.structured_lines) and (
        snapshot.confidence in {"high", "medium"} and len(snapshot.structured_lines) >= 4
    )
    raw_heavy = snapshot.raw_chars >= threshold
    very_heavy = snapshot.raw_chars >= threshold * 3
    noisy_command = bool(snapshot.command and NOISY_COMMAND_RE.search(snapshot.command))
    tool_failure = exit_is_failure(snapshot.exit_code)
    medium_heavy = snapshot.raw_chars >= threshold // 2

    if failed and (raw_heavy or structured_heavy or medium_heavy):
        return True, "failure-summary"
    if very_heavy:
        return True, "very-large"
    if snapshot.tool_name.startswith("mcp__") and (medium_heavy or structured_heavy):
        return True, "mcp-structured"
    if structured_heavy:
        return True, "structured-heavy"
    if raw_heavy:
        return True, "any-large"
    if noisy_command and medium_heavy:
        return True, "noisy-command"
    if tool_failure and medium_heavy:
        return True, "failed-medium"
    return False, "below-threshold"


def updated_tool_output(snapshot: ToolSnapshot, response: dict[str, Any], summary: str) -> dict[str, Any]:
    if snapshot.stdout or snapshot.stderr or snapshot.command:
        return {
            "stdout": summary,
            "stderr": "",
            "interrupted": bool(response.get("interrupted", False)),
            "isImage": bool(response.get("isImage", False)),
        }
    return {"output": summary}


def summarize_output(snapshot: ToolSnapshot) -> str:
    combined = []
    if snapshot.stdout:
        combined.append(("stdout", snapshot.stdout))
    if snapshot.stderr:
        combined.append(("stderr", snapshot.stderr))

    error_lines = []
    prioritized_error_lines: list[tuple[int, str]] = []
    failure_markers = re.compile(
        r"(error|failed|failure|exception|traceback|panic|assert|expected|received|"
        r"\bFAIL\b|\bFAILED\b|[\w./-]+:\d+(?::\d+)?)",
        re.IGNORECASE,
    )

    def error_priority(line: str) -> int:
        lowered = line.lower()
        score = 0
        if "error ts" in lowered or ": error " in lowered:
            score += 20
        if any(t in lowered for t in ("not assignable", "type", "cannot", "missing", "undefined", "traceback", "runtimeerror", "assertion", "panic")):
            score += 35
        if any(t in lowered for t in ("failed", "failure", "exception", "blocked")):
            score += 20
        if any(t in lowered for t in ("unused", "never read")):
            score -= 25
        if "warning" in lowered or "deprecated" in lowered:
            score -= 20
        return score

    total_failure_lines = 0
    for label, text in combined:
        for line in text.splitlines():
            if failure_markers.search(line):
                total_failure_lines += 1
                if len(error_lines) >= 20:
                    continue
                tagged_line = f"{label}: {line}"
                error_lines.append(tagged_line)
                prioritized_error_lines.append((error_priority(tagged_line), tagged_line))

    prioritized_error_lines.sort(key=lambda item: item[0], reverse=True)
    ordered_error_lines = [line for _, line in prioritized_error_lines]
    prioritization_note = None
    lowered_lines = [line.lower() for line in ordered_error_lines]
    if any("never read" in l or "unused" in l for l in lowered_lines) and any(
        t in l for l in lowered_lines for t in ("not assignable", "type", "cannot", "missing", "undefined")
    ):
        prioritization_note = "Fix type/semantic errors before cleanup-only issues."

    def clipped_lines(text: str, head: int, tail: int, prefer_tail: bool = False) -> list[str]:
        lines = text.splitlines()
        if len(lines) <= head + tail:
            return lines
        if prefer_tail:
            tail_window = min(tail, 8)
            return [f"...{len(lines) - tail_window} lines clipped..."] + lines[-tail_window:]
        return lines[:head] + [f"...{len(lines) - head - tail} lines clipped..."] + lines[-tail:]

    summary = [
        f"[MTS filtered] `{snapshot.tool_label}` | exit:{snapshot.exit_code} | ~{snapshot.raw_tokens_estimate}tok | errors:{total_failure_lines}",
        "Use `/mts:full` then rerun if clue missing.",
        "",
    ]
    if snapshot.structured_lines:
        summary.append("Fields:")
        summary.extend(f"- {line}" for line in snapshot.structured_lines[:MAX_STRUCTURED_LINES])
        summary.append("")
    if ordered_error_lines:
        summary.append("Errors (priority):")
        summary.extend(f"- {line}" for line in ordered_error_lines[:MAX_ERROR_LINES_SHOWN])
        if prioritization_note:
            summary.append(f"  ({prioritization_note})")
        summary.append("")

    if snapshot.stdout and not ordered_error_lines:
        summary.append("Tail:")
        summary.extend(clipped_lines(snapshot.stdout, SUMMARY_HEAD_LINES, SUMMARY_TAIL_LINES, prefer_tail=True))
        summary.append("")
    elif snapshot.stdout and len(snapshot.stdout.splitlines()) > SUMMARY_TAIL_LINES:
        summary.append("Tail:")
        tail_lines = snapshot.stdout.splitlines()[-SUMMARY_TAIL_LINES:]
        summary.extend(tail_lines)
        summary.append("")
    if snapshot.stderr and not ordered_error_lines:
        summary.append("Stderr tail:")
        summary.extend(clipped_lines(snapshot.stderr, 3, 8, prefer_tail=True))

    return "\n".join(summary).strip()


# --- Hooks ---

def hook_prompt(data: dict[str, Any]) -> int:
    prompt = str(data.get("prompt") or data.get("user_prompt") or data.get("message") or "")
    prompt_lower = prompt.strip().lower()

    if re.search(r"\b(stop|disable|deactivate|turn off)\b.*\b(mts|max.token.saver)\b|\bnormal mode\b", prompt_lower):
        set_mts_mode("off", quiet=True)
    elif re.search(r"\b(activate|enable|turn on|start)\b.*\b(mts|max.token.saver)\b", prompt_lower):
        set_mts_mode("compact", quiet=True)
    elif prompt_lower.startswith("/mts:off"):
        set_mts_mode("off", quiet=True)
    elif prompt_lower.startswith("/mts:on"):
        set_mts_mode("compact", quiet=True)

    contexts = []
    response_context = mts_response_context()
    if response_context:
        contexts.append(response_context)

    if prompt and HIGH_RISK_PROMPT_RE.search(prompt):
        append_ledger("prompt_risk_suggested", {"prompt_hash": stable_hash(prompt), "cwd": data.get("cwd"), "session_id": data.get("session_id")})
        contexts.append("Broad prompt detected. Offer: (a) plan first, (b) narrow scope, (c) proceed. Skip if scope is clear.")

    if not contexts:
        return 0

    write_json({
        "hookSpecificOutput": {
            "hookEventName": "UserPromptSubmit",
            "additionalContext": "\n\n".join(contexts),
        }
    })
    return 0


def hook_session_start(data: dict[str, Any]) -> int:
    append_ledger("session_start", {"session_id": data.get("session_id"), "cwd": data.get("cwd"), "mode": get_mts_mode()})
    sys.stdout.write(session_start_context())
    return 0


def hook_post_tool(data: dict[str, Any], failed: bool = False) -> int:
    snapshot = build_tool_snapshot(data)
    response = data.get("tool_response") or {}
    if consume_full_output_override():
        append_ledger("full_output_override_used", {
            "session_id": data.get("session_id"), "tool_name": snapshot.tool_name,
            "raw_chars": snapshot.raw_chars, "raw_tokens_estimate": snapshot.raw_tokens_estimate,
        })
        return 0

    event_payload = {
        "session_id": data.get("session_id"), "cwd": data.get("cwd"),
        "tool_name": snapshot.tool_name, "tool_label": snapshot.tool_label,
        "command": snapshot.command, "exit_code": snapshot.exit_code,
        "raw_chars": snapshot.raw_chars, "raw_tokens_estimate": snapshot.raw_tokens_estimate,
        "payload_kind": snapshot.payload_kind, "confidence": snapshot.confidence,
        "structured_lines": len(snapshot.structured_lines),
    }
    if failed:
        append_ledger("tool_failure", event_payload)

    should_filter, reason = should_filter_snapshot(snapshot, failed=failed)
    if failed:
        if snapshot.raw_chars or snapshot.structured_lines:
            summary = summarize_output(snapshot)
            summary_tokens = estimate_tokens(summary)
            blocked = max(0, snapshot.raw_tokens_estimate - summary_tokens)
            if blocked <= 0 and snapshot.raw_tokens_estimate < 200:
                return 0
            append_ledger("tool_failure_summary", {**event_payload, "filter_reason": reason, "tokens_blocked_estimate": blocked})
            write_json({
                "hookSpecificOutput": {
                    "hookEventName": "PostToolUseFailure",
                    "updatedToolOutput": updated_tool_output(snapshot, response, summary),
                    "additionalContext": f"Filtered failed `{snapshot.tool_name}` -~{blocked}tok",
                }
            })
        return 0

    if not should_filter:
        return 0

    summary = summarize_output(snapshot)
    summary_tokens = estimate_tokens(summary)
    blocked = max(0, snapshot.raw_tokens_estimate - summary_tokens)
    if blocked <= 0:
        return 0
    append_ledger("tool_output_filtered", {**event_payload, "filter_reason": reason, "tokens_blocked_estimate": blocked})
    write_json({
        "hookSpecificOutput": {
            "hookEventName": "PostToolUse",
            "updatedToolOutput": updated_tool_output(snapshot, response, summary),
            "additionalContext": f"Filtered `{snapshot.tool_name}` -~{blocked}tok ({reason})",
        }
    })
    return 0


def hook_compact(data: dict[str, Any]) -> int:
    append_ledger("pre_compact", {"session_id": data.get("session_id"), "cwd": data.get("cwd")})
    return 0


# --- Drift guard ---

def git_changed_files() -> tuple[list[str], str | None]:
    import subprocess
    try:
        result = subprocess.run(["git", "status", "--short", "--untracked-files=all"], check=False, capture_output=True, text=True, timeout=8)
    except Exception as exc:
        return [], str(exc)
    if result.returncode != 0:
        return [], (result.stderr or result.stdout or "git status failed").strip()[:240]
    files = []
    for line in result.stdout.splitlines():
        if len(line) < 4:
            continue
        path = line[3:].strip()
        if " -> " in path:
            path = path.split(" -> ", 1)[1]
        files.append(path)
    return files, None


def guard(contract_path: Path | None = None) -> int:
    if contract_path is None:
        candidates = sorted(contracts_dir().glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
        if not candidates:
            print("No contract found. Run /mts:plan first.")
            return 1
        contract_path = candidates[0]

    try:
        contract = json.loads(contract_path.read_text(encoding="utf-8"))
    except Exception as exc:
        print(f"Could not read contract: {exc}")
        return 2

    changed, git_error = git_changed_files()
    planned = set(contract.get("planned_files") or [])
    tests = contract.get("acceptance_tests") or []
    unexpected = [f for f in changed if planned and f not in planned]

    print(f"Max-Token-Saver drift guard: {contract_path.name}")
    if git_error:
        print(f"  Git unavailable: {git_error}")
    else:
        print(f"  Changed: {len(changed)} | Planned: {len(planned)} | Unplanned: {len(unexpected)}")
    if unexpected:
        for f in unexpected[:10]:
            print(f"    drift: {f}")
    if tests:
        print("  Tests to run:")
        for t in tests[:10]:
            print(f"    - {t}")
    return 0


def save_contract(path: Path, payload: str) -> int:
    try:
        contract = json.loads(payload)
    except json.JSONDecodeError as exc:
        print(f"Contract must be JSON: {exc}")
        return 2
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(contract, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"Saved: {path}")
    return 0


def contract_path_from_title(title: str) -> Path:
    return contracts_dir() / f"{slugify(title)}.json"


# --- CLI ---

def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="Max-Token-Saver")
    sub = parser.add_subparsers(dest="command", required=True)

    audit_p = sub.add_parser("audit")
    audit_p.add_argument("paths", nargs="*", type=Path)

    validate_p = sub.add_parser("validate")
    validate_p.add_argument("original", type=Path)
    validate_p.add_argument("compressed", type=Path)

    compress_p = sub.add_parser("compress")
    compress_p.add_argument("target", type=Path, nargs="?", default=Path("CLAUDE.md"))
    compress_p.add_argument("--level", choices=sorted(COMPRESSION_LEVELS), default="medium")
    compress_mode = compress_p.add_mutually_exclusive_group()
    compress_mode.add_argument("--auto", action="store_true")
    compress_mode.add_argument("--prepare", action="store_true")
    compress_mode.add_argument("--finalize", action="store_true")
    compress_mode.add_argument("--info", action="store_true")
    compress_p.add_argument("--draft", type=Path)
    compress_p.add_argument("--json", action="store_true")

    mark_p = sub.add_parser("mark-protected")
    mark_p.add_argument("source", type=Path)
    mark_p.add_argument("dest", nargs="?", type=Path)

    strip_p = sub.add_parser("strip-protect")
    strip_p.add_argument("source", type=Path)
    strip_p.add_argument("dest", nargs="?", type=Path)

    recover_p = sub.add_parser("recover-protected")
    recover_p.add_argument("original", type=Path)
    recover_p.add_argument("candidate", type=Path)
    recover_p.add_argument("output", nargs="?", type=Path)

    sub.add_parser("status")
    sub.add_parser("statusline")

    mode_p = sub.add_parser("mode")
    mode_p.add_argument("mode", choices=sorted(MTS_MODES | {"status"}))

    full_p = sub.add_parser("full")
    full_p.add_argument("--count", type=int, default=1)

    hook_p = sub.add_parser("hook")
    hook_p.add_argument("event", choices=["session-start", "prompt", "post-tool-use", "post-tool-use-failure", "compact"])

    guard_p = sub.add_parser("guard")
    guard_p.add_argument("contract", nargs="?", type=Path)

    save_p = sub.add_parser("save-contract")
    save_p.add_argument("path", nargs="?", type=Path)
    save_p.add_argument("--title")

    args = parser.parse_args(argv)

    if args.command == "audit":
        return audit(args.paths)
    if args.command == "validate":
        return validate_file(args.original, args.compressed)
    if args.command == "compress":
        if args.info:
            from pathlib import Path as P
            ws = compression_workspace(args.target)
            mp = ws / "latest.json"
            if mp.exists():
                print(mp.read_text())
            else:
                print("No manifest.")
            return 0
        return compress_command(args.target, args.level, prepare=args.prepare or not args.finalize, finalize=args.finalize, auto=args.auto, draft=args.draft, json_output=args.json)
    if args.command == "mark-protected":
        return mark_protected(args.source, args.dest)
    if args.command == "strip-protect":
        return strip_protect(args.source, args.dest)
    if args.command == "recover-protected":
        return recover_protected(args.original, args.candidate, args.output)
    if args.command == "status":
        return status()
    if args.command == "statusline":
        return statusline()
    if args.command == "mode":
        if args.mode == "status":
            print(f"Mode: {get_mts_mode()}")
            return 0
        return set_mts_mode(args.mode)
    if args.command == "full":
        return set_full_output(args.count)
    if args.command == "hook":
        data = read_stdin_json()
        if args.event == "session-start":
            return hook_session_start(data)
        if args.event == "prompt":
            return hook_prompt(data)
        if args.event == "post-tool-use":
            return hook_post_tool(data, failed=False)
        if args.event == "post-tool-use-failure":
            return hook_post_tool(data, failed=True)
        if args.event == "compact":
            return hook_compact(data)
    if args.command == "guard":
        return guard(args.contract)
    if args.command == "save-contract":
        if args.path:
            path = args.path
        elif args.title:
            path = contract_path_from_title(args.title)
        else:
            print("Requires path or --title.")
            return 2
        return save_contract(path, sys.stdin.read())
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
