# pw-browser-mcp

A [Model Context Protocol (MCP)](https://modelcontextprotocol.io/) server that exposes **Playwright** browser automation as tools for AI assistants and agents.

## Features

- **21 browser tools** covering navigation, element interaction, form filling, screenshots, tab management, and more
- Accessibility-tree snapshots with **element refs** (via `page.aria_snapshot(mode="ai")`) that can be directly passed back to interaction tools
- Network request and console-message capture
- Built with the [Python MCP SDK](https://github.com/modelcontextprotocol/python-sdk) (`FastMCP`) and [Playwright](https://playwright.dev/python/)

## Installation

```bash
pip install pw-browser-mcp
playwright install chromium
```

## Usage

### Run as a stdio MCP server (default)

```bash
pw-browser-mcp
# or
python -m pw_browser_mcp
```

### Run over HTTP (Streamable HTTP transport)

```bash
pw-browser-mcp --transport streamable-http --host 127.0.0.1 --port 8000
```

### Claude Desktop configuration

Add to `~/Library/Application Support/Claude/claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "pw-browser-mcp": {
      "command": "pw-browser-mcp"
    }
  }
}
```

## Available Tools

| Tool | Description |
|---|---|
| `browser_navigate` | Navigate to a URL |
| `browser_navigate_back` | Go back in browser history |
| `browser_snapshot` | Capture an accessibility snapshot (ARIA tree with element refs) |
| `browser_take_screenshot` | Take a PNG screenshot of the viewport or full page |
| `browser_click` | Click an element |
| `browser_hover` | Hover over an element |
| `browser_drag` | Drag one element onto another |
| `browser_type` | Type text into an element |
| `browser_fill_form` | Fill multiple form fields in one call |
| `browser_select_option` | Select a dropdown option |
| `browser_press_key` | Press a keyboard key |
| `browser_file_upload` | Upload files via a file-chooser dialog |
| `browser_handle_dialog` | Accept or dismiss an alert/confirm/prompt dialog |
| `browser_wait_for` | Wait for text to appear/disappear, or pause for N seconds |
| `browser_evaluate` | Execute JavaScript on the page or on a specific element |
| `browser_resize` | Resize the browser viewport |
| `browser_console_messages` | Retrieve captured browser console messages |
| `browser_network_requests` | Retrieve captured network requests |
| `browser_tabs` | List, open, close, or switch between tabs |
| `browser_close` | Close the current browser tab |
| `browser_install` | Install Playwright browser binaries |

### Element selectors

Tools that act on elements accept a `selector` argument.  Use any of:

- **CSS selector** — `"button.submit"`, `"#search-input"`
- **XPath** — `"//button[@type='submit']"`
- **Text selector** — `"text=Sign in"`
- **ARIA ref** — `"[ref=e12]"` (from a prior `browser_snapshot` call)

## Development

```bash
pip install -e ".[dev]"
playwright install chromium
pytest
```

## License

MIT
