---
description: HCOM reference and setup status
allowed-tools: Bash
disable-model-invocation: true
---

# HCOM Status

## Installation Check
!`command -v hcom >/dev/null 2>&1 && hcom -v || echo "NOT INSTALLED - restart Claude Code to auto-install"`

## Hooks Check
!`grep -q '"HCOM"' ~/.claude/settings.json 2>/dev/null && echo "HOOKS: installed ✓" || echo "HOOKS: not installed - restart Claude Code"`

## Session Check
!`echo "HCOM_SESSION_ID=$HCOM_SESSION_ID"`

## Current Instances
Run `hcom list` to see active instances (requires active session)

---

# Setup Guide

Based on checks above:

**NOT_INSTALLED** → Restart Claude Code. Plugin will auto-install hcom.

**HOOKS: not installed** → Restart Claude Code. After install, hcom sets up hooks.

**SESSION: not active** → Run `hcom start` to enable in current session. If that fails, restart Claude Code.

**All green** → You're ready. Use `hcom` for TUI or `hcom send "msg"` to message.

## Quick Setup Flow
1. Install plugin → restart Claude Code
2. (hcom installs automatically)
3. Restart Claude Code again (hooks now active)
4. Run `hcom start` in your session


If anything goes wrong or theres any issues, fresh start:
```bash
/plugin uninstall hcom@hcom
hcom reset all && pip uninstall hcom
pip install hcom && hcom -v
# Then restart claude code and it should be working
```


---

# Reference

## README
!`curl -fsSL https://raw.githubusercontent.com/aannoo/claude-hook-comms/main/README.md 2>/dev/null || echo "Failed to fetch README from GitHub"`

---

# Troubleshooting

**"hcom: command not found"**
- Restart Claude Code to trigger auto-install
- Or manual: `pip install hcom`

**Hooks not working (no messages received)**
- Check: `grep hcom ~/.claude/settings.json`
- If missing, restart Claude Code after hcom install

**hcom start fails**
- Restart Claude Code (hooks need to be active first)

**Messages not delivered**
- Recipient must have run `hcom start`
- Check `hcom list` for instance status

**Logs**
- `~/.hcom/.tmp/logs/hooks.log`

---

# Uninstall

To completely remove hcom:

```bash
/plugin uninstall hcom@hcom
hcom reset all
pip uninstall hcom
```
