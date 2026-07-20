# pw-browser-mcp

Playwright MCP server for Hermes Agent — 17 browser tools with Chrome profile selection, anti-detection, and action recording.

## Tools (17)

**Navigation & Interaction (9):** `pw_navigate`, `pw_click`, `pw_type`, `pw_screenshot`, `pw_get_html`, `pw_get_text`, `pw_evaluate`, `pw_wait`, `pw_scroll`

**Session (3):** `pw_save_state`, `pw_close`, `pw_status`

**Chrome Profile (3):** `pw_list_profiles`, `pw_use_profile`, `pw_current_profile`

**Recording (2):** `pw_record_start`, `pw_record_stop`

## Quick Start

```bash
# 1. Install deps into Hermes venv
# Windows:
HERMES_PY="C:\Users\xxx\AppData\Local\hermes\hermes-agent\venv\Scripts\python.exe"
# Linux:
# HERMES_PY="/home/xxx/.local/share/hermes/hermes-agent/venv/bin/python3"

uv pip install --python "$HERMES_PY" "mcp[cli]" playwright

# 2. Register with Hermes (auto-discovers 17 tools)
echo Y | hermes mcp add pw-browser \
  --command "$HERMES_PY" \
  --args "/abs/path/to/pw_mcp_server.py"

# 3. Start a session and use
# /new
# pw_list_profiles()
# pw_use_profile(profile_name="Profile 1")
# pw_navigate(url="https://example.com")
```

## Environment Variables

| Variable | Purpose | Default |
|---|---|---|
| `AGENT_BROWSER_EXECUTABLE_PATH` | Path to system Chrome | auto-detect |
| `PW_HEADLESS=1` | Headless mode | 0 (headed) |
| `PW_DATA_DIR` | Data dir for state/ua/screenshots | `./pw_mcp_data` |
| `PW_CHROME_PROFILE` | Default Chrome profile name | none |
| `PW_CHROME_USER_DATA_ROOT` | Chrome User Data root override | auto-detect |
