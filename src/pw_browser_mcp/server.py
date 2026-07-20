"""Playwright browser MCP server implementation."""

from __future__ import annotations

import asyncio
import base64
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Annotated, Literal

from mcp.server.fastmcp import Context, FastMCP, Image
from mcp.server.fastmcp.exceptions import ToolError
from playwright.async_api import (
    Browser,
    BrowserContext as PlaywrightBrowserContext,
    Dialog,
    Page,
    Playwright,
    async_playwright,
)

# ---------------------------------------------------------------------------
# Browser state
# ---------------------------------------------------------------------------


@dataclass
class _BrowserState:
    """Shared browser state injected via FastMCP lifespan context."""

    playwright: Playwright
    browser: Browser
    context: PlaywrightBrowserContext
    pages: list[Page] = field(default_factory=list)
    current_index: int = 0
    _console_messages: list[dict] = field(default_factory=list)
    _network_requests: list[dict] = field(default_factory=list)
    _pending_dialog: Dialog | None = None

    @property
    def page(self) -> Page:
        if not self.pages:
            raise ToolError("No browser pages are open. Navigate to a URL first.")
        return self.pages[self.current_index]

    async def new_page(self) -> Page:
        page = await self.context.new_page()
        self._attach_listeners(page)
        self.pages.append(page)
        self.current_index = len(self.pages) - 1
        return page

    def _attach_listeners(self, page: Page) -> None:
        page.on("console", lambda msg: self._console_messages.append({
            "type": msg.type,
            "text": msg.text,
            "url": page.url,
        }))
        page.on("request", lambda req: self._network_requests.append({
            "method": req.method,
            "url": req.url,
            "resource_type": req.resource_type,
        }))
        page.on("dialog", self._handle_dialog)

    def _handle_dialog(self, dialog: Dialog) -> None:
        self._pending_dialog = dialog


# ---------------------------------------------------------------------------
# Lifespan context manager
# ---------------------------------------------------------------------------


@asynccontextmanager
async def _lifespan(server: FastMCP) -> AsyncIterator[_BrowserState]:  # noqa: ARG001
    """Launch the browser when the server starts; shut it down on exit."""
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        context = await browser.new_context()
        state = _BrowserState(playwright=pw, browser=browser, context=context)
        # Open an initial blank page so tools work immediately.
        page = await context.new_page()
        state._attach_listeners(page)
        state.pages.append(page)
        try:
            yield state
        finally:
            await browser.close()


# ---------------------------------------------------------------------------
# Server instance
# ---------------------------------------------------------------------------

mcp = FastMCP(
    "pw-browser-mcp",
    instructions=(
        "A Playwright-powered browser automation server. "
        "Use browser_navigate to open pages, browser_snapshot to inspect the "
        "accessibility tree (use element refs for subsequent actions), and the "
        "other browser_* tools to interact with page elements."
    ),
    lifespan=_lifespan,
)


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------

def _state(ctx: Context) -> _BrowserState:
    return ctx.request_context.lifespan_context  # type: ignore[return-value]


# ---------------------------------------------------------------------------
# Navigation tools
# ---------------------------------------------------------------------------


@mcp.tool()
async def browser_navigate(
    url: str,
    ctx: Context,
) -> str:
    """Navigate the browser to a URL.

    Args:
        url: The URL to navigate to (e.g. "https://example.com").
    """
    state = _state(ctx)
    response = await state.page.goto(url, wait_until="domcontentloaded")
    status = response.status if response else "unknown"
    return f"Navigated to {url!r} — HTTP {status}"


@mcp.tool()
async def browser_navigate_back(ctx: Context) -> str:
    """Go back to the previous page in browser history."""
    state = _state(ctx)
    response = await state.page.go_back(wait_until="domcontentloaded")
    if response is None:
        return "Cannot go back — no previous page in history."
    return f"Navigated back to {state.page.url!r} — HTTP {response.status}"


# ---------------------------------------------------------------------------
# Snapshot / screenshot
# ---------------------------------------------------------------------------


@mcp.tool()
async def browser_snapshot(ctx: Context) -> str:
    """Capture an accessibility snapshot of the current page.

    Returns a YAML-like representation of the page's accessibility tree.
    Elements include ``[ref=eN]`` markers that can be used as the *selector*
    argument in other tools (e.g. ``browser_click``, ``browser_type``).
    """
    state = _state(ctx)
    snapshot = await state.page.aria_snapshot(mode="ai")
    return snapshot


@mcp.tool()
async def browser_take_screenshot(
    full_page: bool = False,
    ctx: Context = None,  # type: ignore[assignment]
) -> Image:
    """Take a screenshot of the current page.

    Args:
        full_page: When True, captures the full scrollable page instead of
                   just the visible viewport.
    """
    state = _state(ctx)
    data = await state.page.screenshot(full_page=full_page, type="png")
    return Image(data=data, format="png")


# ---------------------------------------------------------------------------
# Element interaction
# ---------------------------------------------------------------------------


@mcp.tool()
async def browser_click(
    selector: str,
    button: Literal["left", "right", "middle"] = "left",
    double_click: bool = False,
    modifiers: list[Literal["Alt", "Control", "ControlOrMeta", "Meta", "Shift"]] | None = None,
    ctx: Context = None,  # type: ignore[assignment]
) -> str:
    """Click on an element identified by a selector.

    The *selector* can be:
    * A CSS selector (e.g. ``"button.submit"``).
    * An XPath expression prefixed with ``//`` (e.g. ``"//button[@type='submit']"``).
    * A text selector prefixed with ``text=`` (e.g. ``"text=Sign in"``).
    * An ARIA ref from a previous ``browser_snapshot`` call (e.g. ``"[ref=e12]"``).

    Args:
        selector: Locator string identifying the target element.
        button: Mouse button to use — ``"left"`` (default), ``"right"``, or ``"middle"``.
        double_click: When True, performs a double-click instead of a single click.
        modifiers: Keyboard modifiers to hold while clicking.
    """
    state = _state(ctx)
    locator = state.page.locator(selector)
    if double_click:
        await locator.dblclick(button=button, modifiers=modifiers or [])
    else:
        await locator.click(button=button, modifiers=modifiers or [])
    return f"Clicked {selector!r}"


@mcp.tool()
async def browser_hover(
    selector: str,
    ctx: Context = None,  # type: ignore[assignment]
) -> str:
    """Hover the mouse over an element.

    Args:
        selector: Locator string identifying the target element.
    """
    state = _state(ctx)
    await state.page.locator(selector).hover()
    return f"Hovered over {selector!r}"


@mcp.tool()
async def browser_drag(
    start_selector: str,
    end_selector: str,
    ctx: Context = None,  # type: ignore[assignment]
) -> str:
    """Drag an element and drop it onto another element.

    Args:
        start_selector: Locator for the element to drag from.
        end_selector: Locator for the element to drop onto.
    """
    state = _state(ctx)
    source = state.page.locator(start_selector)
    target = state.page.locator(end_selector)
    await source.drag_to(target)
    return f"Dragged {start_selector!r} to {end_selector!r}"


@mcp.tool()
async def browser_type(
    selector: str,
    text: str,
    submit: bool = False,
    slowly: bool = False,
    ctx: Context = None,  # type: ignore[assignment]
) -> str:
    """Type text into a focusable element (e.g. an input or textarea).

    Args:
        selector: Locator string identifying the target element.
        text: The text to type.
        submit: When True, presses Enter after typing.
        slowly: When True, types character-by-character (triggers key handlers).
    """
    state = _state(ctx)
    locator = state.page.locator(selector)
    if slowly:
        await locator.press_sequentially(text)
    else:
        await locator.fill(text)
    if submit:
        await locator.press("Enter")
    return f"Typed into {selector!r}"


@mcp.tool()
async def browser_fill_form(
    fields: Annotated[
        list[dict],
        "List of field objects. Each object must have 'selector' (str), 'value' (str), "
        "and optionally 'type' (one of 'textbox', 'checkbox', 'radio', 'combobox').",
    ],
    ctx: Context = None,  # type: ignore[assignment]
) -> str:
    """Fill multiple form fields in one call.

    Each entry in *fields* requires:
    - ``selector``: element locator string.
    - ``value``: value to set.
    - ``type`` (optional): ``"textbox"`` (default), ``"checkbox"``, ``"radio"``,
      or ``"combobox"``.

    Args:
        fields: List of field descriptor objects.
    """
    state = _state(ctx)
    for item in fields:
        sel = item.get("selector") or item.get("ref", "")
        value = item.get("value", "")
        kind = item.get("type", "textbox")
        locator = state.page.locator(sel)

        if kind == "checkbox":
            checked = str(value).lower() in ("true", "1", "yes", "on")
            if checked:
                await locator.check()
            else:
                await locator.uncheck()
        elif kind == "radio":
            await locator.check()
        elif kind == "combobox":
            await locator.select_option(label=value)
        else:
            await locator.fill(value)

    filled = [f["selector"] for f in fields if "selector" in f]
    return f"Filled {len(fields)} field(s): {filled}"


@mcp.tool()
async def browser_select_option(
    selector: str,
    values: list[str],
    ctx: Context = None,  # type: ignore[assignment]
) -> str:
    """Select one or more options in a ``<select>`` dropdown.

    Args:
        selector: Locator string identifying the ``<select>`` element.
        values: List of option values or visible text labels to select.
    """
    state = _state(ctx)
    await state.page.locator(selector).select_option(values)
    return f"Selected {values!r} in {selector!r}"


@mcp.tool()
async def browser_press_key(
    key: str,
    ctx: Context = None,  # type: ignore[assignment]
) -> str:
    """Press a keyboard key on the currently focused element.

    Use standard key names (e.g. ``"Enter"``, ``"ArrowLeft"``, ``"Tab"``,
    ``"Escape"``, ``"F5"``) or a single character (``"a"``).

    Args:
        key: The key name to press.
    """
    state = _state(ctx)
    await state.page.keyboard.press(key)
    return f"Pressed key {key!r}"


@mcp.tool()
async def browser_file_upload(
    paths: list[str],
    ctx: Context = None,  # type: ignore[assignment]
) -> str:
    """Upload one or more files using the active file-chooser dialog.

    This tool must be called *after* performing an action that opens a
    file-chooser (e.g. clicking a file input element).

    Args:
        paths: Absolute file paths to upload.
    """
    state = _state(ctx)

    async def _set_input(chooser):  # noqa: ANN001
        await chooser.set_files(paths)

    state.page.once("filechooser", _set_input)
    return f"File chooser will upload: {paths}"


@mcp.tool()
async def browser_handle_dialog(
    accept: bool,
    prompt_text: str | None = None,
    ctx: Context = None,  # type: ignore[assignment]
) -> str:
    """Accept or dismiss the currently open browser dialog (alert / confirm / prompt).

    If no dialog is open this tool is a no-op.

    Args:
        accept: ``True`` to accept (click OK), ``False`` to dismiss (click Cancel).
        prompt_text: Text to enter when the dialog is a prompt. Ignored otherwise.
    """
    state = _state(ctx)
    dialog = state._pending_dialog
    if dialog is None:
        return "No dialog is currently open."
    state._pending_dialog = None
    if accept:
        await dialog.accept(prompt_text or "")
    else:
        await dialog.dismiss()
    action = "accepted" if accept else "dismissed"
    return f"Dialog {action}: {dialog.type!r} — {dialog.message!r}"


# ---------------------------------------------------------------------------
# Wait & evaluate
# ---------------------------------------------------------------------------


@mcp.tool()
async def browser_wait_for(
    text: str | None = None,
    text_gone: str | None = None,
    time: float | None = None,
    ctx: Context = None,  # type: ignore[assignment]
) -> str:
    """Wait for a condition before proceeding.

    Provide exactly one of the three arguments:

    Args:
        text: Wait until this text appears anywhere on the page.
        text_gone: Wait until this text is no longer visible on the page.
        time: Wait for this many seconds (float, e.g. ``1.5``).
    """
    provided = sum(v is not None for v in (text, text_gone, time))
    if provided != 1:
        raise ToolError("Provide exactly one of 'text', 'text_gone', or 'time'.")

    state = _state(ctx)

    if time is not None:
        await asyncio.sleep(time)
        return f"Waited {time}s"

    if text is not None:
        await state.page.wait_for_selector(f"text={text}", state="visible")
        return f"Text {text!r} is now visible"

    # text_gone
    await state.page.wait_for_selector(f"text={text_gone}", state="hidden")
    return f"Text {text_gone!r} is no longer visible"


@mcp.tool()
async def browser_evaluate(
    function: str,
    selector: str | None = None,
    ctx: Context = None,  # type: ignore[assignment]
) -> str:
    """Evaluate a JavaScript expression in the page context.

    Args:
        function: A JavaScript expression or arrow-function string to evaluate.
                  Examples: ``"document.title"`` or
                  ``"() => document.querySelectorAll('a').length"``.
        selector: When provided, the function is called with the matching DOM
                  element as its first argument (``(element) => element.id``).
    """
    state = _state(ctx)
    if selector:
        element = state.page.locator(selector)
        result = await element.evaluate(function)
    else:
        result = await state.page.evaluate(function)
    return json.dumps(result, ensure_ascii=False, default=str)


# ---------------------------------------------------------------------------
# Viewport
# ---------------------------------------------------------------------------


@mcp.tool()
async def browser_resize(
    width: int,
    height: int,
    ctx: Context = None,  # type: ignore[assignment]
) -> str:
    """Resize the browser viewport.

    Args:
        width: Viewport width in CSS pixels.
        height: Viewport height in CSS pixels.
    """
    state = _state(ctx)
    await state.page.set_viewport_size({"width": width, "height": height})
    return f"Viewport resized to {width}×{height}"


# ---------------------------------------------------------------------------
# Observability
# ---------------------------------------------------------------------------


@mcp.tool()
async def browser_console_messages(ctx: Context) -> str:
    """Return all browser console messages captured since the last page load.

    Messages are returned as a JSON array with ``type``, ``text``, and ``url``
    fields.
    """
    state = _state(ctx)
    return json.dumps(state._console_messages, ensure_ascii=False, indent=2)


@mcp.tool()
async def browser_network_requests(ctx: Context) -> str:
    """Return all network requests captured since the last page load.

    Requests are returned as a JSON array with ``method``, ``url``, and
    ``resource_type`` fields.
    """
    state = _state(ctx)
    return json.dumps(state._network_requests, ensure_ascii=False, indent=2)


# ---------------------------------------------------------------------------
# Tab management
# ---------------------------------------------------------------------------


@mcp.tool()
async def browser_tabs(
    action: Literal["list", "new", "close", "select"],
    index: int | None = None,
    url: str | None = None,
    ctx: Context = None,  # type: ignore[assignment]
) -> str:
    """Manage browser tabs.

    Args:
        action: One of:
                - ``"list"`` — list all open tabs with their index and URL.
                - ``"new"`` — open a new blank tab (or navigate it to *url*).
                - ``"close"`` — close the tab at *index* (default: current tab).
                - ``"select"`` — switch to the tab at *index*.
        index: Zero-based tab index (required for ``"close"`` and ``"select"``).
        url: URL to navigate the new tab to (only used with ``"new"``).
    """
    state = _state(ctx)

    if action == "list":
        lines = [
            f"[{i}]{' *' if i == state.current_index else '  '} {p.url}"
            for i, p in enumerate(state.pages)
        ]
        return "Open tabs:\n" + "\n".join(lines)

    if action == "new":
        page = await state.new_page()
        if url:
            await page.goto(url, wait_until="domcontentloaded")
        return f"Opened new tab [{state.current_index}]: {page.url}"

    if action == "close":
        target = state.current_index if index is None else index
        if not (0 <= target < len(state.pages)):
            raise ToolError(f"Tab index {target} is out of range (0–{len(state.pages) - 1}).")
        page = state.pages.pop(target)
        await page.close()
        if not state.pages:
            new_page = await state.context.new_page()
            state._attach_listeners(new_page)
            state.pages.append(new_page)
        state.current_index = min(target, len(state.pages) - 1)
        return f"Closed tab [{target}]. Active tab is now [{state.current_index}]."

    if action == "select":
        if index is None:
            raise ToolError("'select' requires an index.")
        if not (0 <= index < len(state.pages)):
            raise ToolError(f"Tab index {index} is out of range (0–{len(state.pages) - 1}).")
        state.current_index = index
        return f"Switched to tab [{index}]: {state.page.url}"

    raise ToolError(f"Unknown action {action!r}. Use 'list', 'new', 'close', or 'select'.")


# ---------------------------------------------------------------------------
# Browser lifecycle
# ---------------------------------------------------------------------------


@mcp.tool()
async def browser_close(ctx: Context) -> str:
    """Close the current browser tab.

    If it is the only tab, the browser context is reset to a fresh blank page.
    """
    state = _state(ctx)
    if len(state.pages) <= 1:
        await state.pages[0].close()
        new_page = await state.context.new_page()
        state._attach_listeners(new_page)
        state.pages = [new_page]
        state.current_index = 0
        return "Closed the page. A fresh blank page is now active."

    page = state.pages.pop(state.current_index)
    await page.close()
    state.current_index = min(state.current_index, len(state.pages) - 1)
    return f"Closed tab. Active tab is now [{state.current_index}]: {state.page.url}"


@mcp.tool()
async def browser_install(ctx: Context) -> str:
    """Install the Playwright browser binaries.

    This runs ``playwright install chromium`` and returns the output. Only
    needed when the browser has not been installed yet.
    """
    proc = await asyncio.create_subprocess_exec(
        "playwright", "install", "chromium",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    stdout, _ = await proc.communicate()
    output = stdout.decode(errors="replace").strip()
    if proc.returncode == 0:
        return f"Browser installed successfully.\n{output}"
    raise ToolError(f"Installation failed (exit {proc.returncode}):\n{output}")
