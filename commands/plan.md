---
description: Produce an implementation contract before broad work
args: "task description"
---

For the given task, produce an implementation contract with:
- Product concept and audience
- Architecture and phases
- Planned files
- Acceptance tests
- Drift guardrails and stop conditions

Save with: `python3 "${CLAUDE_PLUGIN_ROOT}/scripts/max_token_saver.py" save-contract --title "TITLE"`
Pass JSON on stdin. Stop after the contract unless user approves implementation.
