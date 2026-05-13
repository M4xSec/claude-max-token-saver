# Max-Token-Saver (Caveman Slayer)

Aggressive token savings plugin for Claude Code. Saves tokens through 5 mechanisms:

1. **Compact response enforcement** — instructs the model to be answer-first, no filler
2. **Adaptive tool-output filtering** — compacts noisy outputs above 3k chars (drops to 1.5k when context >50%)
3. **Memory/context file compression** — shrinks always-loaded CLAUDE.md with protected-span safety
4. **Prompt-risk detection** — catches vague broad prompts before they trigger retry loops
5. **Drift guardrails** — implementation contracts + scope-drift checking

## Install

```bash
bash install.sh --force
```

Restart Claude Code after installation.

## Commands

| Command | Purpose |
|---|---|
| `/mts:on` | Enable compact response mode |
| `/mts:off` | Disable response compression |
| `/mts:status` | Show usage dashboard and waste heat map |
| `/mts:full` | Get full unfiltered output for next tool call |
| `/mts:compress [level] [file]` | Compress memory files (light/medium/aggressive) |
| `/mts:audit [paths]` | Find bloated context files |
| `/mts:plan "task"` | Create implementation contract |
| `/mts:guard` | Check for scope drift |

## Key Numbers

| Parameter | Value |
|---|---|
| Filter threshold (normal) | 3,000 chars |
| Filter threshold (high context) | 1,500 chars |
| Summary head/tail lines | 6/12 |
| Max error lines shown | 5 |
| Max structured lines | 20 |
| Adaptive trigger | context >50% |

## How It Works

### Tool Output Filtering
- Fires on every PostToolUse and PostToolUseFailure hook
- Never filters: Read, Write, Edit (code must stay intact)
- Filters everything else above threshold (Bash, Grep, MCP, Agent, etc.)
- Extracts error/failure lines by priority, keeps structured field summaries
- Replaces the full output in conversation with a compact summary

### Response Compression
- SessionStart + UserPromptSubmit hooks inject compact-mode instructions
- If Caveman is active, defers response brevity to Caveman (avoids double-compression)
- Still runs all other mechanisms regardless

### Adaptive Threshold
- Tracks context usage from statusline data
- When context >50%, threshold drops from 3000 to 1500 chars
- More aggressive filtering as you approach the compaction wall

### Memory Compression
- Protected spans: code blocks, URLs, paths, env vars, versions, commands, warnings
- Quality guard rejects low-savings results and restores backup
- Levels: light (15-30%), medium (35-55%), aggressive (50-70%)

## Environment Variables

| Variable | Purpose |
|---|---|
| `MTS_DEFAULT_MODE` | Default mode: compact/normal/off |
| `MTS_IGNORE_CAVEMAN` | Set to `1` to always run response compression |
| `MTS_ALLOW_LOW_SAVINGS` | Set to `1` to keep low-savings compression results |

## Uninstall

```bash
rm -rf ~/.claude/plugins/marketplaces/max-token-saver
rm -rf ~/.claude/plugins/max-token-saver
```

## License

MIT
