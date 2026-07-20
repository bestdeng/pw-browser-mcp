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

    async def _detect_ua(self) -> str:
        """Detect UA from a temporary page."""
        from playwright.async_api import async_playwright
        pw = await async_playwright().start()
        try:
            browser = await pw.chromium.launch(
                headless=True,
                executable_path=self.config.chrome_exe,
            )
            ctx = await browser.new_context()
            page = await ctx.new_page()
            ua = await page.evaluate("() => navigator.userAgent")
            await browser.close()
            await pw.stop()
            return str(ua)
        except Exception as e:
            log.warning(f"UA detection failed: {e}")
            return ""

    async def _get_ua(self) -> str:
        if self._ua:
            return self._ua
        ua = self._read_ua_cache()
        if ua:
            self._ua = ua
            return ua
        ua = await self._detect_ua()
        if ua:
            self._write_ua_cache(ua)
        self._ua = ua
        return ua

    # ---- Browser lifecycle ----
    async def _ensure_browser_running(self) -> bool:
        """Check if browser/page are still connected."""
        if self._browser and self._page:
            try:
                await self._page.evaluate("1+1")
                return True
            except Exception:
                log.info("Browser disconnected, reconnecting...")
                await self.close()
        elif self._context and self._page:
            # persistent context mode (_browser is None)
            try:
                await self._page.evaluate("1+1")
                return True
            except Exception:
                log.info("Browser disconnected (persistent context), reconnecting...")
                await self.close()
        return False

    async def _launch_with_profile(self):
        """Launch using Chrome profile (persistent context)."""
        profile_path = self._resolve_profile_path(self.config.chrome_profile)
        if not profile_path:
            raise RuntimeError(
                f"Chrome profile '{self.config.chrome_profile}' not found. "
                f"Use pw_list_profiles() to see available profiles."
            )
        log.info(f"Launching with Chrome profile: {self.config.chrome_profile} -> {profile_path}")

        from playwright.async_api import async_playwright
        self._playwright = await async_playwright().start()

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

        ua = await self._get_ua()

        # launch_persistent_context returns a BrowserContext directly
        self._context = await self._playwright.chromium.launch_persistent_context(
            user_data_dir=str(profile_path),
            **launch_args,
            viewport={"width": self.config.window_width, "height": self.config.window_height},
            locale=self.config.locale,
            timezone_id=self.config.timezone,
            user_agent=ua or None,
            permissions=["geolocation", "notifications"],
            color_scheme="light",
            extra_http_headers={
                "Accept-Language": "zh-CN,zh;q=0.9",
                "Upgrade-Insecure-Requests": "1",
            },
        )

        # Inject anti-detection
        await self._context.add_init_script(ANTI_DETECTION_JS)

        self._page = await self._context.new_page()
        # _browser stays None for persistent context mode

        # Human-like initial mouse movement
        try:
            await self._page.mouse.move(
                random.randint(int(self.config.window_width * 0.4),
                                int(self.config.window_width * 0.6)),
                random.randint(int(self.config.window_height * 0.4),
                                int(self.config.window_height * 0.6)),
                steps=random.randint(8, 18),
            )
            await self._page.wait_for_timeout(random.randint(300, 900))
        except Exception:
            pass

        log.info(f"Browser ready (profile: {self.config.chrome_profile})")

    async def _launch_fresh(self):
        """Launch a fresh browser (no profile)."""
        from playwright.async_api import async_playwright

        self._playwright = await async_playwright().start()

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
        self._browser = await self._playwright.chromium.launch(**launch_args)

        # UA from cache
        ua = await self._get_ua()
        ua_arg = ua if ua else None

        # Load saved storage state if exists
        storage_state = None
        if self._state_path.exists() and self._state_path.stat().st_size > 0:
            try:
                storage_state = str(self._state_path)
            except Exception:
                pass

        self._context = await self._browser.new_context(
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
        await self._context.add_init_script(ANTI_DETECTION_JS)

        self._page = await self._context.new_page()

        # Human-like initial mouse movement
        try:
            await self._page.mouse.move(
                random.randint(int(self.config.window_width * 0.4),
                                int(self.config.window_width * 0.6)),
                random.randint(int(self.config.window_height * 0.4),
                                int(self.config.window_height * 0.6)),
                steps=random.randint(8, 18),
            )
            await self._page.wait_for_timeout(random.randint(300, 900))
        except Exception:
            pass

        log.info("Browser ready")

    async def ensure_browser(self):
        """Lazy-init the browser if not already running."""
        if await self._ensure_browser_running():
            return

        if self.config.chrome_profile:
            await self._launch_with_profile()
        else:
            await self._launch_fresh()

    async def get_page(self):
        """Ensure browser is running and return the current page."""
        await self.ensure_browser()
        return self._page

    async def save_state(self):
        """Save cookies + localStorage for login persistence."""
        try:
            if self._context:
                await self._context.storage_state(path=str(self._state_path))
                log.info(f"Storage state saved -> {self._state_path}")
        except Exception as e:
            log.warning(f"Failed saving storage state: {e}")

    async def close(self):
        """Close all browser resources."""
        try:
            if self._page:
                await self._page.close()
        except Exception:
            pass
        try:
            if self._context:
                await self._context.close()
        except Exception:
            pass
        try:
            if self._browser:
                await self._browser.close()
        except Exception:
            pass
        try:
            if self._playwright:
                await self._playwright.stop()
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


# Helper: run an async callable from an MCP tool that returns JSON string
async def _call(fn):
    """Wrap an async callable, handle exceptions to JSON."""
    try:
        result = await fn()
        if isinstance(result, str):
            return result
        return json.dumps(result, ensure_ascii=False)
    except Exception as e:
        return json.dumps({"success": False, "error": str(e)}, ensure_ascii=False)


@mcp.tool()
async def pw_navigate(url: str, wait_until: str = "load") -> str:
    """Navigate the browser to a URL. Returns page title and URL."""
    async def _do():
        page = await _session.get_page()
        await page.goto(url, wait_until=wait_until, timeout=30000)
        return {
            "success": True,
            "title": await page.title(),
            "url": page.url,
        }
    return await _call(_do)


@mcp.tool()
async def pw_click(selector: str, timeout: int = 10000) -> str:
    """Click an element identified by CSS selector. Returns success status."""
    async def _do():
        page = await _session.get_page()
        element = await page.wait_for_selector(selector, timeout=timeout)
        if not element:
            return {"success": False, "error": f"Element not found: {selector}"}
        box = await element.bounding_box()
        if box:
            tx = box["x"] + box["width"] * random.uniform(0.2, 0.8)
            ty = box["y"] + box["height"] * random.uniform(0.2, 0.8)
            await page.mouse.move(tx, ty, steps=random.randint(5, 12))
            await page.wait_for_timeout(random.randint(50, 200))
        await element.click()
        return {"success": True, "selector": selector}
    return await _call(_do)


@mcp.tool()
async def pw_type(selector: str, text: str, timeout: int = 10000, clear_first: bool = True) -> str:
    """Type text into an input field. Optionally clear first."""
    async def _do():
        page = await _session.get_page()
        element = await page.wait_for_selector(selector, timeout=timeout)
        if not element:
            return {"success": False, "error": f"Element not found: {selector}"}
        await element.click()
        if clear_first:
            await element.fill("")
            await page.wait_for_timeout(100)
        await page.keyboard.type(text, delay=random.randint(20, 80))
        return {"success": True, "selector": selector, "chars": len(text)}
    return await _call(_do)


@mcp.tool()
async def pw_screenshot(full_page: bool = False) -> str:
    """Take a screenshot of the current page. Returns the file path."""
    async def _do():
        page = await _session.get_page()
        timestamp = int(time.time())
        shot_dir = Path(_config.data_dir) / "screenshots"
        shot_dir.mkdir(parents=True, exist_ok=True)
        path = str(shot_dir / f"screenshot_{timestamp}.png")
        await page.screenshot(path=path, full_page=full_page)
        return {"success": True, "path": path}
    return await _call(_do)


@mcp.tool()
async def pw_get_html(selector: Optional[str] = None) -> str:
    """Get the HTML content of the page (or a specific element). Returns HTML string."""
    async def _do():
        page = await _session.get_page()
        if selector:
            element = await page.query_selector(selector)
            if not element:
                return {"success": False, "error": f"Element not found: {selector}"}
            html = await element.inner_html()
        else:
            html = await page.content()
        return {"success": True, "html": html[:50000]}
    return await _call(_do)


@mcp.tool()
async def pw_get_text(selector: Optional[str] = None) -> str:
    """Get visible text content of the page (or a specific element)."""
    async def _do():
        page = await _session.get_page()
        if selector:
            element = await page.query_selector(selector)
            if not element:
                return {"success": False, "error": f"Element not found: {selector}"}
            text = await element.inner_text()
        else:
            text = await page.inner_text("body")
        return {"success": True, "text": text[:50000]}
    return await _call(_do)


@mcp.tool()
async def pw_evaluate(js_code: str) -> str:
    """Execute JavaScript in the page context. Returns the result."""
    async def _do():
        page = await _session.get_page()
        result = await page.evaluate(js_code)
        return {"success": True, "result": result}
    return await _call(_do)


@mcp.tool()
async def pw_wait(selector: str = "", timeout: int = 10000) -> str:
    """Wait for an element to appear, or wait for network idle if no selector.
    If selector is empty, waits for network idle."""
    async def _do():
        page = await _session.get_page()
        if selector:
            await page.wait_for_selector(selector, timeout=timeout)
        else:
            await page.wait_for_load_state("networkidle", timeout=timeout)
        return {"success": True}
    return await _call(_do)


@mcp.tool()
async def pw_scroll(direction: str = "down", amount: int = 500) -> str:
    """Scroll the page. direction: 'down', 'up', 'bottom', 'top'."""
    async def _do():
        page = await _session.get_page()
        if direction == "bottom":
            await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        elif direction == "top":
            await page.evaluate("window.scrollTo(0, 0)")
        elif direction == "down":
            await page.evaluate(f"window.scrollBy(0, {amount})")
        elif direction == "up":
            await page.evaluate(f"window.scrollBy(0, -{amount})")
        await page.wait_for_timeout(random.randint(200, 500))
        return {"success": True}
    return await _call(_do)


@mcp.tool()
async def pw_save_state() -> str:
    """Save browser session state (cookies + localStorage) for login persistence."""
    async def _do():
        await _session.save_state()
        return {"success": True, "path": str(_session._state_path)}
    return await _call(_do)


@mcp.tool()
async def pw_close() -> str:
    """Close the browser session. Use this when done to free resources."""
    async def _do():
        await _session.close()
        return {"success": True}
    return await _call(_do)


@mcp.tool()
async def pw_status() -> str:
    """Get browser status: running, URL, title, viewport size."""
    async def _do():
        try:
            if _session._page:
                page = _session._page
                url = page.url
                title = await page.title()
                viewport = page.viewport_size
                return {
                    "running": True,
                    "url": url,
                    "title": title,
                    "viewport": viewport,
                }
            return {"running": False}
        except Exception:
            return {"running": False}
    return await _call(_do)


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
            return {"success": False, "error": "Chrome User Data root not found. Set PW_CHROME_USER_DATA_ROOT env var."}
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
        return {
            "success": True,
            "user_data_root": str(root_path),
            "profiles": profiles,
            "current": _config.chrome_profile,
        }
    # This is pure filesystem I/O, no Playwright — safe to run sync in executor
    loop = asyncio.get_event_loop()
    return json.dumps(await loop.run_in_executor(None, _sync), ensure_ascii=False)


@mcp.tool()
async def pw_use_profile(profile_name: str) -> str:
    """Switch to a specific Chrome profile.
    Closes current browser and re-opens with the selected profile.
    Pass empty string ('') to go back to fresh (no profile) mode.
    Examples: 'Default', 'Profile 1', 'Profile 2'"""
    async def _do():
        old_profile = _config.chrome_profile
        if profile_name == "" or profile_name is None:
            _config.chrome_profile = None
        else:
            # Verify profile exists
            profile_path = _session._resolve_profile_path(profile_name)
            if not profile_path or not profile_path.is_dir():
                return {
                    "success": False,
                    "error": f"Profile '{profile_name}' not found. Use pw_list_profiles() to see available profiles.",
                }
            _config.chrome_profile = profile_name

        # Close and reopen with new profile
        await _session.close()
        await _session.ensure_browser()
        return {
            "success": True,
            "old_profile": old_profile,
            "new_profile": _config.chrome_profile,
            "message": f"Switched from '{old_profile or 'fresh'}' to '{_config.chrome_profile or 'fresh'}'",
        }
    return await _call(_do)


@mcp.tool()
async def pw_current_profile() -> str:
    """Show which Chrome profile is currently active.
    Returns null/None if using fresh (no profile) mode."""
    return json.dumps({
        "success": True,
        "profile": _config.chrome_profile,
        "headless": _config.headless,
    }, ensure_ascii=False)


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

    async def start(self, url: str = "", chrome_exe: Optional[str] = None,
                    chrome_profile: Optional[str] = None, user_data_root: str = "") -> dict:
        """Open a visible browser and start recording user actions.
        If chrome_profile is set, uses that Chrome profile (persistent context)."""
        if self._recording:
            return {"success": False, "error": "Already recording. Stop first with pw_record_stop()"}

        from playwright.async_api import async_playwright

        self._playwright = await async_playwright().start()

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
            self._context = await self._playwright.chromium.launch_persistent_context(
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
            self._page = await self._context.new_page()
        else:
            log.info("Opening recorder browser (headed, visible)...")
            self._browser = await self._playwright.chromium.launch(**launch_args)
            self._context = await self._browser.new_context(
                viewport={"width": 1280, "height": 900},
                locale=_config.locale,
                timezone_id=_config.timezone,
                permissions=["geolocation", "notifications"],
                color_scheme="light",
                extra_http_headers={
                    "Accept-Language": "zh-CN,zh;q=0.9",
                },
            )
            self._page = await self._context.new_page()

        # Inject anti-detection
        await self._context.add_init_script(ANTI_DETECTION_JS)

        # Inject recorder JS after page load
        await self._page.add_init_script(RECORDER_INJECT_JS)

        if url:
            await self._page.goto(url, wait_until="domcontentloaded", timeout=30000)

        self._recording = True
        result = {
            "success": True,
            "message": f"Recording started. Interact with the browser window. URL: {url or '(blank)'}",
            "url": self._page.url,
            "chrome_profile": profile_name,
        }
        log.info(f"Recorder started. URL={url or '(blank)'}, profile={profile_name or 'fresh'}")
        return result

    async def stop(self) -> dict:
        """Stop recording and return the captured actions."""
        if not self._recording or not self._page:
            return {"success": False, "error": "Not recording"}

        # Extract recorded events
        try:
            events = await self._page.evaluate("() => window.__pw_recorder_events || []")
        except Exception as e:
            events = []
            log.warning(f"Could not extract events: {e}")

        # Get final URL
        try:
            final_url = self._page.url
            final_title = await self._page.title()
        except Exception:
            final_url = ""
            final_title = ""

        # Close the recording browser
        try:
            await self._page.close()
        except Exception:
            pass
        try:
            await self._context.close()
        except Exception:
            pass
        try:
            if self._browser:
                await self._browser.close()
        except Exception:
            pass
        try:
            await self._playwright.stop()
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
    async def _do():
        result = await _recorder.start(url=url, chrome_exe=_config.chrome_exe,
                                       chrome_profile=_config.chrome_profile)
        return result
    return await _call(_do)


@mcp.tool()
async def pw_record_stop() -> str:
    """Stop recording and return the captured actions as a playable script.
    Call this after you finish interacting with the recording browser."""
    async def _do():
        result = await _recorder.stop()
        return result
    return await _call(_do)


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
