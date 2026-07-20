#!/usr/bin/env python3
"""
Playwright MCP Server for Hermes Agent.

Provides controllable Playwright browser operations via MCP protocol.
Replicates BrowserManager's capabilities: anti-detection, UA caching,
persistent sessions, storage_state login, human-like simulation.

Usage (stdio mode — for Hermes MCP):
    python pw_mcp_server.py

Usage (HTTP/SSE mode — debug):
    python pw_mcp_server.py --transport sse --port 8931
"""

from __future__ import annotations

import asyncio
import json
import os
import random
import sys
import time
import logging
import tempfile
from pathlib import Path
from typing import Optional
from functools import partial
from dataclasses import dataclass, field

from mcp.server.fastmcp import FastMCP

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] pw-mcp: %(message)s",
    stream=sys.stderr,
)
log = logging.getLogger("pw-mcp")

# ---------------------------------------------------------------------------
# Anti-detection script (from BrowserManager)
# ---------------------------------------------------------------------------
ANTI_DETECTION_JS = """
// Playwright anti-detection
Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
// Override navigator properties for stealth
Object.defineProperty(navigator, 'plugins', {
    get: () => [1, 2, 3, 4, 5]
});
Object.defineProperty(navigator, 'languages', {
    get: () => ['zh-CN', 'zh', 'en']
});
// Override chrome.runtime for detection evasion
window.chrome = { runtime: {} };
// Remove webdriver trace
if (navigator.webdriver === false) {
    // already spoofed
} else {
    Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
}
"""

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
@dataclass
class PwConfig:
    """Playwright browser configuration."""
    chrome_exe: Optional[str] = None
    headless: bool = False
    data_dir: str = "./pw_mcp_data"
    window_width: int = 1366
    window_height: int = 1080
    locale: str = "zh-CN"
    timezone: str = "Asia/Shanghai"
    geolocation: Optional[dict] = None
    record_video: bool = False
    chrome_profile: Optional[str] = None       # "Default", "Profile 1", etc.
    chrome_user_data_root: str = ""            # auto-detect if empty


# Singleton browser state (process-local, single session)
class BrowserSession:
    """Manages a single Playwright browser instance with persistent state."""

    def __init__(self, config: PwConfig):
        self.config = config
        self._playwright = None
        self._browser = None
        self._context = None
        self._page = None
        self._ua = None
        self._data_root = Path(config.data_dir)
        self._data_root.mkdir(parents=True, exist_ok=True)
        self._state_path = self._data_root / "state.json"
        self._ua_path = self._data_root / "ua.json"

    # ---- UA management (mirrors BrowserManager) ----
    def _read_ua_cache(self) -> Optional[str]:
        try:
            if self._ua_path.exists():
                raw = self._ua_path.read_text(encoding="utf-8").strip()
                if raw:
                    obj = json.loads(raw)
                    ua = (obj.get("userAgent") or "").strip()
                    return ua or None
        except Exception:
            pass
        return None

    def _write_ua_cache(self, ua: str):
        try:
            payload = {
                "userAgent": ua,
                "updated_at": int(time.time()),
            }
            self._ua_path.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except Exception as e:
            log.warning(f"Failed writing UA cache: {e}")

    # ---- Chrome profile support ----
    @staticmethod
    def _default_chrome_user_data_root() -> str:
        """Detect Chrome User Data root directory for current platform."""
        import platform
        system = platform.system()
        if system == "Windows":
            base = Path(os.environ.get("LOCALAPPDATA", ""))
            if not base:
                base = Path.home() / "AppData" / "Local"
            candidate = base / "Google" / "Chrome" / "User Data"
            if candidate.exists():
                return str(candidate)
            # Chromium fallback
            candidate = base / "Chromium" / "User Data"
            if candidate.exists():
                return str(candidate)
        elif system == "Linux":
            candidate = Path.home() / ".config" / "google-chrome"
            if candidate.exists():
                return str(candidate)
            candidate = Path.home() / ".config" / "chromium"
            if candidate.exists():
                return str(candidate)
        elif system == "Darwin":
            candidate = Path.home() / "Library" / "Application Support" / "Google" / "Chrome"
            if candidate.exists():
                return str(candidate)
        return ""

    def _resolve_profile_path(self, profile_name: str) -> Optional[Path]:
        """Resolve a Chrome profile name to its full user_data_dir path."""
        if not profile_name:
            return None
        root = self.config.chrome_user_data_root or self._default_chrome_user_data_root()
        if not root:
            return None
        return Path(root) / profile_name

    def _detect_ua(self) -> str:
        """Detect UA from a temporary page."""
        from playwright.sync_api import sync_playwright
        pw = sync_playwright().start()
        try:
            browser = pw.chromium.launch(
                headless=True,
                executable_path=self.config.chrome_exe,
            )
            ctx = browser.new_context()
            page = ctx.new_page()
            ua = page.evaluate("() => navigator.userAgent")
            browser.close()
            pw.stop()
            return str(ua)
        except Exception as e:
            log.warning(f"UA detection failed: {e}")
            return ""

    def _get_ua(self) -> str:
        if self._ua:
            return self._ua
        ua = self._read_ua_cache()
        if ua:
            self._ua = ua
            return ua
        ua = self._detect_ua()
        if ua:
            self._write_ua_cache(ua)
        self._ua = ua
        return ua

    # ---- Browser lifecycle ----
    def _ensure_browser_running(self) -> bool:
        """Check if browser/page are still connected."""
        if self._browser and self._page:
            try:
                self._page.evaluate("1+1")
                return True
            except Exception:
                log.info("Browser disconnected, reconnecting...")
                self.close()
        elif self._context and self._page:
            # persistent context mode (_browser is None)
            try:
                self._page.evaluate("1+1")
                return True
            except Exception:
                log.info("Browser disconnected (persistent context), reconnecting...")
                self.close()
        return False

    def _launch_with_profile(self):
        """Launch using Chrome profile (persistent context)."""
        profile_path = self._resolve_profile_path(self.config.chrome_profile)
        if not profile_path:
            raise RuntimeError(
                f"Chrome profile '{self.config.chrome_profile}' not found. "
                f"Use pw_list_profiles() to see available profiles."
            )
        log.info(f"Launching with Chrome profile: {self.config.chrome_profile} -> {profile_path}")

        from playwright.sync_api import sync_playwright
        self._playwright = sync_playwright().start()

        launch_args = {
            "headless": self.config.headless,
            "args": [
                "--disable-blink-features=AutomationControlled",
                f"--window-size={self.config.window_width},{self.config.window_height}",
                "--disable-web-security",
                "--no-first-run",
                "--no-default-browser-check",
                "--disable-gpu",
                "--disable-dev-shm-usage",
            ],
        }
        if self.config.chrome_exe:
            launch_args["executable_path"] = self.config.chrome_exe

        # launch_persistent_context returns a BrowserContext directly
        self._context = self._playwright.chromium.launch_persistent_context(
            user_data_dir=str(profile_path),
            **launch_args,
            viewport={"width": self.config.window_width, "height": self.config.window_height},
            locale=self.config.locale,
            timezone_id=self.config.timezone,
            user_agent=self._get_ua() or None,
            permissions=["geolocation", "notifications"],
            color_scheme="light",
            extra_http_headers={
                "Accept-Language": "zh-CN,zh;q=0.9",
                "Upgrade-Insecure-Requests": "1",
            },
        )

        # Inject anti-detection
        self._context.add_init_script(ANTI_DETECTION_JS)

        self._page = self._context.new_page()
        # _browser stays None for persistent context mode

        # Human-like initial mouse movement
        try:
            self._page.mouse.move(
                random.randint(int(self.config.window_width * 0.4),
                                int(self.config.window_width * 0.6)),
                random.randint(int(self.config.window_height * 0.4),
                                int(self.config.window_height * 0.6)),
                steps=random.randint(8, 18),
            )
            self._page.wait_for_timeout(random.randint(300, 900))
        except Exception:
            pass

        log.info(f"Browser ready (profile: {self.config.chrome_profile})")

    def _launch_fresh(self):
        """Launch a fresh browser (no profile)."""
        from playwright.sync_api import sync_playwright

        self._playwright = sync_playwright().start()

        launch_args = {
            "headless": self.config.headless,
            "args": [
                "--disable-blink-features=AutomationControlled",
                f"--window-size={self.config.window_width},{self.config.window_height}",
                "--disable-web-security",
                "--disable-extensions",
                "--no-first-run",
                "--no-default-browser-check",
                "--disable-gpu",
                "--disable-dev-shm-usage",
            ],
        }
        if self.config.chrome_exe:
            launch_args["executable_path"] = self.config.chrome_exe

        log.info(f"Launching Chromium (headless={self.config.headless})")
        self._browser = self._playwright.chromium.launch(**launch_args)

        # UA from cache
        ua = self._get_ua()
        ua_arg = ua if ua else None

        # Load saved storage state if exists
        storage_state = None
        if self._state_path.exists() and self._state_path.stat().st_size > 0:
            try:
                storage_state = str(self._state_path)
            except Exception:
                pass

        self._context = self._browser.new_context(
            viewport={"width": self.config.window_width, "height": self.config.window_height},
            locale=self.config.locale,
            timezone_id=self.config.timezone,
            user_agent=ua_arg,
            storage_state=storage_state,
            permissions=["geolocation", "notifications"],
            color_scheme="light",
            extra_http_headers={
                "Accept-Language": "zh-CN,zh;q=0.9",
                "Upgrade-Insecure-Requests": "1",
            },
            record_video_dir="videos/" if self.config.record_video else None,
        )

        # Inject anti-detection
        self._context.add_init_script(ANTI_DETECTION_JS)

        self._page = self._context.new_page()

        # Human-like initial mouse movement
        try:
            self._page.mouse.move(
                random.randint(int(self.config.window_width * 0.4),
                                int(self.config.window_width * 0.6)),
                random.randint(int(self.config.window_height * 0.4),
                                int(self.config.window_height * 0.6)),
                steps=random.randint(8, 18),
            )
            self._page.wait_for_timeout(random.randint(300, 900))
        except Exception:
            pass

        log.info("Browser ready")

    def ensure_browser(self):
        """Lazy-init the browser if not already running."""
        if self._ensure_browser_running():
            return

        if self.config.chrome_profile:
            self._launch_with_profile()
        else:
            self._launch_fresh()

    def get_page(self):
        """Ensure browser is running and return the current page."""
        self.ensure_browser()
        return self._page

    def save_state(self):
        """Save cookies + localStorage for login persistence."""
        try:
            if self._context:
                self._context.storage_state(path=str(self._state_path))
                log.info(f"Storage state saved -> {self._state_path}")
        except Exception as e:
            log.warning(f"Failed saving storage state: {e}")

    def close(self):
        """Close all browser resources."""
        try:
            if self._page:
                self._page.close()
        except Exception:
            pass
        try:
            if self._context:
                self._context.close()
        except Exception:
            pass
        try:
            if self._browser:
                self._browser.close()
        except Exception:
            pass
        try:
            if self._playwright:
                self._playwright.stop()
        except Exception:
            pass
        self._page = None
        self._context = None
        self._browser = None
        self._playwright = None
        log.info("Browser closed")


# ---------------------------------------------------------------------------
# Global session
# ---------------------------------------------------------------------------
_config = PwConfig(
    chrome_exe=os.environ.get("AGENT_BROWSER_EXECUTABLE_PATH") or None,
    headless=os.environ.get("PW_HEADLESS", "0") == "1",
    data_dir=os.environ.get("PW_DATA_DIR", "./pw_mcp_data"),
    chrome_profile=os.environ.get("PW_CHROME_PROFILE") or None,
    chrome_user_data_root=os.environ.get("PW_CHROME_USER_DATA_ROOT", ""),
)
_session = BrowserSession(_config)

# ---------------------------------------------------------------------------
# MCP Server
# ---------------------------------------------------------------------------
mcp = FastMCP("playwright-browser", log_level="WARNING")


def _run_sync(fn, *args, **kwargs):
    """Run a synchronous function in a thread to avoid asyncio conflicts."""
    loop = asyncio.get_event_loop()
    return loop.run_in_executor(None, partial(fn, *args, **kwargs))


@mcp.tool()
async def pw_navigate(url: str, wait_until: str = "load") -> str:
    """Navigate the browser to a URL. Returns page title and URL."""
    def _sync():
        page = _session.get_page()
        page.goto(url, wait_until=wait_until, timeout=30000)
        return json.dumps({
            "success": True,
            "title": page.title(),
            "url": page.url,
        }, ensure_ascii=False)
    return await _run_sync(_sync)


@mcp.tool()
async def pw_click(selector: str, timeout: int = 10000) -> str:
    """Click an element identified by CSS selector. Returns success status."""
    def _sync():
        page = _session.get_page()
        try:
            element = page.wait_for_selector(selector, timeout=timeout)
            if not element:
                return json.dumps({"success": False, "error": f"Element not found: {selector}"})
            box = element.bounding_box()
            if box:
                tx = box["x"] + box["width"] * random.uniform(0.2, 0.8)
                ty = box["y"] + box["height"] * random.uniform(0.2, 0.8)
                page.mouse.move(tx, ty, steps=random.randint(5, 12))
                page.wait_for_timeout(random.randint(50, 200))
            element.click()
            return json.dumps({"success": True, "selector": selector})
        except Exception as e:
            return json.dumps({"success": False, "error": str(e)})
    return await _run_sync(_sync)


@mcp.tool()
async def pw_type(selector: str, text: str, timeout: int = 10000, clear_first: bool = True) -> str:
    """Type text into an input field. Optionally clear first."""
    def _sync():
        page = _session.get_page()
        try:
            element = page.wait_for_selector(selector, timeout=timeout)
            if not element:
                return json.dumps({"success": False, "error": f"Element not found: {selector}"})
            element.click()
            if clear_first:
                element.fill("")
                page.wait_for_timeout(100)
            page.keyboard.type(text, delay=random.randint(20, 80))
            return json.dumps({"success": True, "selector": selector, "chars": len(text)})
        except Exception as e:
            return json.dumps({"success": False, "error": str(e)})
    return await _run_sync(_sync)


@mcp.tool()
async def pw_screenshot(full_page: bool = False) -> str:
    """Take a screenshot of the current page. Returns the file path."""
    def _sync():
        page = _session.get_page()
        timestamp = int(time.time())
        shot_dir = Path(_config.data_dir) / "screenshots"
        shot_dir.mkdir(parents=True, exist_ok=True)
        path = str(shot_dir / f"screenshot_{timestamp}.png")
        page.screenshot(path=path, full_page=full_page)
        return json.dumps({"success": True, "path": path}, ensure_ascii=False)
    return await _run_sync(_sync)


@mcp.tool()
async def pw_get_html(selector: Optional[str] = None) -> str:
    """Get the HTML content of the page (or a specific element). Returns HTML string."""
    def _sync():
        page = _session.get_page()
        if selector:
            element = page.query_selector(selector)
            if not element:
                return json.dumps({"success": False, "error": f"Element not found: {selector}"})
            html = element.inner_html()
        else:
            html = page.content()
        return json.dumps({"success": True, "html": html[:50000]}, ensure_ascii=False)
    return await _run_sync(_sync)


@mcp.tool()
async def pw_get_text(selector: Optional[str] = None) -> str:
    """Get visible text content of the page (or a specific element)."""
    def _sync():
        page = _session.get_page()
        if selector:
            element = page.query_selector(selector)
            if not element:
                return json.dumps({"success": False, "error": f"Element not found: {selector}"})
            text = element.inner_text()
        else:
            text = page.inner_text("body")
        return json.dumps({"success": True, "text": text[:50000]}, ensure_ascii=False)
    return await _run_sync(_sync)


@mcp.tool()
async def pw_evaluate(js_code: str) -> str:
    """Execute JavaScript in the page context. Returns the result."""
    def _sync():
        page = _session.get_page()
        try:
            result = page.evaluate(js_code)
            return json.dumps({"success": True, "result": result}, ensure_ascii=False)
        except Exception as e:
            return json.dumps({"success": False, "error": str(e)})
    return await _run_sync(_sync)


@mcp.tool()
async def pw_wait(selector: str = "", timeout: int = 10000) -> str:
    """Wait for an element to appear, or wait for network idle if no selector.
    If selector is empty, waits for network idle."""
    def _sync():
        page = _session.get_page()
        try:
            if selector:
                page.wait_for_selector(selector, timeout=timeout)
            else:
                page.wait_for_load_state("networkidle", timeout=timeout)
            return json.dumps({"success": True})
        except Exception as e:
            return json.dumps({"success": False, "error": str(e)})
    return await _run_sync(_sync)


@mcp.tool()
async def pw_scroll(direction: str = "down", amount: int = 500) -> str:
    """Scroll the page. direction: 'down', 'up', 'bottom', 'top'."""
    def _sync():
        page = _session.get_page()
        try:
            if direction == "bottom":
                page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            elif direction == "top":
                page.evaluate("window.scrollTo(0, 0)")
            elif direction == "down":
                page.evaluate(f"window.scrollBy(0, {amount})")
            elif direction == "up":
                page.evaluate(f"window.scrollBy(0, -{amount})")
            page.wait_for_timeout(random.randint(200, 500))
            return json.dumps({"success": True})
        except Exception as e:
            return json.dumps({"success": False, "error": str(e)})
    return await _run_sync(_sync)


@mcp.tool()
async def pw_save_state() -> str:
    """Save browser session state (cookies + localStorage) for login persistence."""
    def _sync():
        _session.save_state()
        return json.dumps({"success": True, "path": str(_session._state_path)})
    return await _run_sync(_sync)


@mcp.tool()
async def pw_close() -> str:
    """Close the browser session. Use this when done to free resources."""
    def _sync():
        _session.close()
        return json.dumps({"success": True})
    return await _run_sync(_sync)


@mcp.tool()
async def pw_status() -> str:
    """Get browser status: running, URL, title, viewport size."""
    def _sync():
        try:
            if _session._page:
                page = _session._page
                url = page.url
                title = page.title()
                viewport = page.viewport_size
                return json.dumps({
                    "running": True,
                    "url": url,
                    "title": title,
                    "viewport": viewport,
                }, ensure_ascii=False)
            return json.dumps({"running": False})
        except Exception:
            return json.dumps({"running": False})
    return await _run_sync(_sync)


# ---------------------------------------------------------------------------
# Chrome Profile tools
# ---------------------------------------------------------------------------

@mcp.tool()
async def pw_list_profiles() -> str:
    """List all available Chrome profiles on this system.
    Returns profile names like 'Default', 'Profile 1', etc.
    Use pw_use_profile() to switch to a specific profile."""
    def _sync():
        root = _config.chrome_user_data_root or BrowserSession._default_chrome_user_data_root()
        if not root:
            return json.dumps({"success": False, "error": "Chrome User Data root not found. Set PW_CHROME_USER_DATA_ROOT env var."})
        root_path = Path(root)
        profiles = []
        # Always include Default if it exists
        default_dir = root_path / "Default"
        if default_dir.is_dir():
            profiles.append({"name": "Default", "path": str(default_dir)})
        # Scan for Profile N, Guest Profile, etc.
        for item in sorted(root_path.iterdir()):
            if item.name.startswith("Profile") and item.is_dir():
                profiles.append({"name": item.name, "path": str(item)})
        return json.dumps({
            "success": True,
            "user_data_root": str(root_path),
            "profiles": profiles,
            "current": _config.chrome_profile,
        }, ensure_ascii=False)
    return await _run_sync(_sync)


@mcp.tool()
async def pw_use_profile(profile_name: str) -> str:
    """Switch to a specific Chrome profile.
    Closes current browser and re-opens with the selected profile.
    Pass empty string ('') to go back to fresh (no profile) mode.
    Examples: 'Default', 'Profile 1', 'Profile 2'"""
    def _sync():
        old_profile = _config.chrome_profile
        if profile_name == "" or profile_name is None:
            _config.chrome_profile = None
        else:
            # Verify profile exists
            profile_path = _session._resolve_profile_path(profile_name)
            if not profile_path or not profile_path.is_dir():
                return json.dumps({
                    "success": False,
                    "error": f"Profile '{profile_name}' not found. Use pw_list_profiles() to see available profiles.",
                })
            _config.chrome_profile = profile_name

        # Close and reopen with new profile
        _session.close()
        try:
            _session.ensure_browser()
            return json.dumps({
                "success": True,
                "old_profile": old_profile,
                "new_profile": _config.chrome_profile,
                "message": f"Switched from '{old_profile or 'fresh'}' to '{_config.chrome_profile or 'fresh'}'",
            }, ensure_ascii=False)
        except Exception as e:
            return json.dumps({"success": False, "error": str(e)})
    return await _run_sync(_sync)


@mcp.tool()
async def pw_current_profile() -> str:
    """Show which Chrome profile is currently active.
    Returns null/None if using fresh (no profile) mode."""
    def _sync():
        return json.dumps({
            "success": True,
            "profile": _config.chrome_profile,
            "headless": _config.headless,
        }, ensure_ascii=False)
    return await _run_sync(_sync)


# ---------------------------------------------------------------------------
# Action Recorder — headed browser + user action recording
# ---------------------------------------------------------------------------
# Injected JS that records user interactions on the page
RECORDER_INJECT_JS = """
window.__pw_recorder_events = [];

// Record clicks
document.addEventListener('click', function(e) {
    var el = e.target;
    var tag = el.tagName.toLowerCase();
    var id = el.id ? '#' + el.id : '';
    var cls = '';
    if (el.className && typeof el.className === 'string') {
        cls = '.' + el.className.trim().split(/\\s+/).slice(0, 2).join('.');
    }
    var selector = tag + id + cls;
    // Try to find a more unique selector
    var path = [];
    var cur = el;
    while (cur && cur !== document.body && cur !== document) {
        var s = cur.tagName.toLowerCase();
        if (cur.id) { s = '#' + cur.id; path.unshift(s); break; }
        if (cur.className && typeof cur.className === 'string' && cur.className.trim()) {
            s += '.' + cur.className.trim().split(/\\s+/).slice(0, 1).join('.');
        }
        path.unshift(s);
        cur = cur.parentElement;
    }
    var fullSelector = path.join(' > ');
    window.__pw_recorder_events.push({
        type: 'click',
        selector: fullSelector,
        tag: tag,
        text: (el.textContent || '').trim().slice(0, 100),
        url: window.location.href,
        ts: Date.now()
    });
}, true);

// Record input changes (debounced)
var __pw_input_timers = {};
document.addEventListener('input', function(e) {
    var el = e.target;
    if (el.tagName.toLowerCase() !== 'input' && el.tagName.toLowerCase() !== 'textarea') return;
    var id = el.id ? '#' + el.id : '';
    var name = el.name ? '[name="' + el.name + '"]' : '';
    var sel = el.tagName.toLowerCase() + id + name;
    var val = el.value || '';
    clearTimeout(__pw_input_timers[sel]);
    __pw_input_timers[sel] = setTimeout(function() {
        window.__pw_recorder_events.push({
            type: 'type',
            selector: sel,
            value: val,
            tag: el.tagName.toLowerCase(),
            url: window.location.href,
            ts: Date.now()
        });
    }, 300);
}, true);

// Record navigation (SPA and regular)
window.addEventListener('popstate', function() {
    window.__pw_recorder_events.push({
        type: 'navigate',
        url: window.location.href,
        title: document.title,
        ts: Date.now()
    });
});

// Record scroll (throttled)
var __pw_scroll_timer = null;
window.addEventListener('scroll', function() {
    if (__pw_scroll_timer) return;
    __pw_scroll_timer = setTimeout(function() {
        __pw_scroll_timer = null;
        window.__pw_recorder_events.push({
            type: 'scroll',
            x: window.scrollX,
            y: window.scrollY,
            url: window.location.href,
            ts: Date.now()
        });
    }, 500);
}, true);

// Record select changes
document.addEventListener('change', function(e) {
    var el = e.target;
    if (el.tagName.toLowerCase() !== 'select') return;
    var id = el.id ? '#' + el.id : '';
    var sel = 'select' + id;
    window.__pw_recorder_events.push({
        type: 'select',
        selector: sel,
        value: el.value,
        text: el.options[el.selectedIndex] ? el.options[el.selectedIndex].text : '',
        url: window.location.href,
        ts: Date.now()
    });
}, true);

console.log('[PW Recorder] Recording started');
"""


class Recorder:
    """Manages a headed browser session that records user actions."""

    def __init__(self):
        self._playwright = None
        self._browser = None
        self._context = None
        self._page = None
        self._recording = False

    def start(self, url: str = "", chrome_exe: Optional[str] = None,
              chrome_profile: Optional[str] = None, user_data_root: str = "") -> dict:
        """Open a visible browser and start recording user actions.
        If chrome_profile is set, uses that Chrome profile (persistent context)."""
        if self._recording:
            return {"success": False, "error": "Already recording. Stop first with pw_record_stop()"}

        from playwright.sync_api import sync_playwright

        self._playwright = sync_playwright().start()

        # Determine profile mode
        profile_name = chrome_profile or _config.chrome_profile
        profile_path = None
        if profile_name:
            root = user_data_root or _config.chrome_user_data_root or BrowserSession._default_chrome_user_data_root()
            if root:
                candidate = Path(root) / profile_name
                if candidate.is_dir():
                    profile_path = candidate

        launch_args = {
            "headless": False,  # MUST be visible for user
            "args": [
                "--disable-blink-features=AutomationControlled",
                "--window-size=1280,900",
                "--disable-web-security",
                "--no-first-run",
                "--no-default-browser-check",
            ],
        }
        if chrome_exe or _config.chrome_exe:
            launch_args["executable_path"] = chrome_exe or _config.chrome_exe

        if profile_path:
            log.info(f"Opening recorder browser with profile: {profile_name} -> {profile_path}")
            self._context = self._playwright.chromium.launch_persistent_context(
                user_data_dir=str(profile_path),
                **launch_args,
                viewport={"width": 1280, "height": 900},
                locale=_config.locale,
                timezone_id=_config.timezone,
                permissions=["geolocation", "notifications"],
                color_scheme="light",
                extra_http_headers={
                    "Accept-Language": "zh-CN,zh;q=0.9",
                },
            )
            self._page = self._context.new_page()
        else:
            log.info("Opening recorder browser (headed, visible)...")
            self._browser = self._playwright.chromium.launch(**launch_args)
            self._context = self._browser.new_context(
                viewport={"width": 1280, "height": 900},
                locale=_config.locale,
                timezone_id=_config.timezone,
                permissions=["geolocation", "notifications"],
                color_scheme="light",
                extra_http_headers={
                    "Accept-Language": "zh-CN,zh;q=0.9",
                },
            )
            self._page = self._context.new_page()

        # Inject anti-detection
        self._context.add_init_script(ANTI_DETECTION_JS)

        # Inject recorder JS after page load
        self._page.add_init_script(RECORDER_INJECT_JS)

        if url:
            self._page.goto(url, wait_until="domcontentloaded", timeout=30000)

        self._recording = True
        result = {
            "success": True,
            "message": f"Recording started. Interact with the browser window. URL: {url or '(blank)'}",
            "url": self._page.url,
            "chrome_profile": profile_name,
        }
        log.info(f"Recorder started. URL={url or '(blank)'}, profile={profile_name or 'fresh'}")
        return result

    def stop(self) -> dict:
        """Stop recording and return the captured actions."""
        if not self._recording or not self._page:
            return {"success": False, "error": "Not recording"}

        # Extract recorded events
        try:
            events = self._page.evaluate("() => window.__pw_recorder_events || []")
        except Exception as e:
            events = []
            log.warning(f"Could not extract events: {e}")

        # Get final URL
        try:
            final_url = self._page.url
            final_title = self._page.title()
        except Exception:
            final_url = ""
            final_title = ""

        # Close the recording browser
        try:
            self._page.close()
        except Exception:
            pass
        try:
            self._context.close()
        except Exception:
            pass
        try:
            if self._browser:
                self._browser.close()
        except Exception:
            pass
        try:
            self._playwright.stop()
        except Exception:
            pass

        self._playwright = None
        self._browser = None
        self._context = None
        self._page = None
        self._recording = False

        # Simplify and deduplicate events
        simplified = _simplify_recording(events)

        # Generate replay script
        replay_script = _generate_replay_script(simplified, final_url)

        result = {
            "success": True,
            "total_events": len(events),
            "simplified_actions": len(simplified),
            "final_url": final_url,
            "final_title": final_title,
            "actions": simplified,
            "playwright_code": replay_script,
        }
        log.info(f"Recording stopped. {len(events)} raw events → {len(simplified)} actions")
        return result

    @property
    def is_recording(self) -> bool:
        return self._recording


def _simplify_recording(events: list) -> list:
    """Compress raw event stream into meaningful actions."""
    if not events:
        return []

    actions = []
    last_url = ""
    last_typed = {}  # selector -> last value

    for ev in events:
        t = ev.get("type", "")

        if t == "navigate":
            url = ev.get("url", "")
            if url and url != last_url:
                actions.append({"action": "navigate", "url": url, "title": ev.get("title", "")})
                last_url = url

        elif t == "click":
            sel = ev.get("selector", "")
            text = ev.get("text", "")
            action = {"action": "click", "selector": sel}
            if text:
                action["text_hint"] = text
            actions.append(action)

        elif t == "type":
            sel = ev.get("selector", "")
            val = ev.get("value", "")
            prev = last_typed.get(sel, "")
            # Only record if value actually changed
            if val != prev:
                actions.append({"action": "type", "selector": sel, "value": val})
                last_typed[sel] = val

        elif t == "select":
            sel = ev.get("selector", "")
            val = ev.get("value", "")
            text = ev.get("text", "")
            actions.append({"action": "select", "selector": sel, "value": val, "option_text": text})

        elif t == "scroll":
            # Deduplicate nearby scrolls
            y = ev.get("y", 0)
            if not actions or actions[-1].get("action") != "scroll" or abs(actions[-1].get("y", 0) - y) > 200:
                actions.append({"action": "scroll", "x": ev.get("x", 0), "y": y})

    return actions


def _generate_replay_script(actions: list, start_url: str) -> str:
    """Generate a Playwright Python script from recorded actions."""
    lines = [
        '"""Auto-recorded Playwright script."""',
        "from playwright.sync_api import sync_playwright",
        "",
        "",
        "def run():",
        '    with sync_playwright() as pw:',
        '        browser = pw.chromium.launch(headless=False)',
        '        ctx = browser.new_context(viewport={"width": 1280, "height": 900})',
        "        page = ctx.new_page()",
    ]

    if start_url:
        lines.append(f'        page.goto("{start_url}", wait_until="domcontentloaded")')

    for action in actions:
        a = action.get("action", "")
        if a == "navigate":
            lines.append(f'        page.goto("{action["url"]}")')
        elif a == "click":
            sel = action.get("selector", "")
            hint = action.get("text_hint", "")
            comment = f"  # {hint}" if hint else ""
            lines.append(f'        page.click("{sel}"){comment}')
            lines.append(f'        page.wait_for_timeout(500)')
        elif a == "type":
            val = action.get("value", "")
            sel = action.get("selector", "")
            lines.append(f'        page.fill("{sel}", "")')
            lines.append(f'        page.keyboard.type("{val}", delay=50)')
        elif a == "select":
            sel = action.get("selector", "")
            val = action.get("value", "")
            lines.append(f'        page.select_option("{sel}", "{val}")')
        elif a == "scroll":
            y = action.get("y", 0)
            lines.append(f'        page.evaluate("window.scrollTo(0, {y})")')

    lines.extend([
        "        page.wait_for_timeout(2000)",
        '        page.screenshot(path="recording_result.png")',
        "        browser.close()",
        "",
        "",
        'if __name__ == "__main__":',
        "    run()",
    ])

    return "\n".join(lines)


# Global recorder instance
_recorder = Recorder()


@mcp.tool()
async def pw_record_start(url: str = "") -> str:
    """Open a VISIBLE browser window and start recording your manual operations.
    You interact with it, and all clicks/typing/navigation are captured.
    Pass a URL to open initially, or leave blank for a blank page.
    Uses the currently active chrome_profile if set."""
    def _sync():
        result = _recorder.start(url=url, chrome_exe=_config.chrome_exe,
                                  chrome_profile=_config.chrome_profile)
        return json.dumps(result, ensure_ascii=False)
    return await _run_sync(_sync)


@mcp.tool()
async def pw_record_stop() -> str:
    """Stop recording and return the captured actions as a playable script.
    Call this after you finish interacting with the recording browser."""
    def _sync():
        result = _recorder.stop()
        return json.dumps(result, ensure_ascii=False, indent=2)
    return await _run_sync(_sync)


# ---------------------------------------------------------------------------
# Entry
# ---------------------------------------------------------------------------
def main():
    import argparse
    parser = argparse.ArgumentParser(description="Playwright MCP Server")
    parser.add_argument("--transport", choices=["stdio", "sse"], default="stdio")
    parser.add_argument("--port", type=int, default=8931)
    parser.add_argument("--headless", action="store_true")
    parser.add_argument("--chrome-exe", help="Path to Chrome/Chromium executable")
    parser.add_argument("--chrome-profile", help="Chrome profile name: Default, Profile 1, etc.")
    args = parser.parse_args()

    if args.headless:
        _config.headless = True
    if args.chrome_exe:
        _config.chrome_exe = args.chrome_exe
    if args.chrome_profile:
        _config.chrome_profile = args.chrome_profile

    log.info(f"Starting Playwright MCP Server (transport={args.transport})")
    log.info(f"  Chrome: {_config.chrome_exe or 'default'}")
    log.info(f"  Headless: {_config.headless}")
    log.info(f"  Data dir: {_config.data_dir}")
    log.info(f"  Chrome profile: {_config.chrome_profile or 'none'}")

    if args.transport == "sse":
        mcp.run(transport="sse", port=args.port)
    else:
        mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
