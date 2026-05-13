---
description: Compress memory files with protected-span validation
args: "[level] [file]"
---

Compress a memory/instructions file to reduce recurring context usage.

Default target: `CLAUDE.md`; default level: `medium`.

Run: `python3 "${CLAUDE_PLUGIN_ROOT}/scripts/max_token_saver.py" compress "${TARGET}" --level "${LEVEL}" --auto`

Parse the JSON payload. Rewrite `marked_content` using `prompt`, preserve every `<protect>...</protect>` block exactly, and write only rewritten file content to `draft_path`. Then run `finalize_command_json` and inspect the result.

If `status=quality_guard_failed` and `next_level` is present, rerun `retry_auto_command` once.

Report: original/new token estimate, saved %, backup location.
