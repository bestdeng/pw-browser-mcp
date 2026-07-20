"""Tests for pw-browser-mcp server tools."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# Helpers / fixtures
# ---------------------------------------------------------------------------


def _make_state(**overrides):
    """Return a minimal fake _BrowserState."""
    from pw_browser_mcp.server import _BrowserState

    # Build a mock _BrowserState without actually launching a browser.
    state = MagicMock(spec=_BrowserState)
    state._console_messages = []
    state._network_requests = []
    state._pending_dialog = None
    state.current_index = 0

    # Default mock page
    mock_page = MagicMock()
    mock_page.url = "about:blank"
    mock_page.goto = AsyncMock(return_value=MagicMock(status=200))
    mock_page.go_back = AsyncMock(return_value=MagicMock(status=200))
    mock_page.aria_snapshot = AsyncMock(return_value="- generic [ref=e1]: hello")
    mock_page.screenshot = AsyncMock(return_value=b"\x89PNG\r\n")
    mock_page.locator = MagicMock(return_value=MagicMock(
        click=AsyncMock(),
        dblclick=AsyncMock(),
        hover=AsyncMock(),
        fill=AsyncMock(),
        press=AsyncMock(),
        check=AsyncMock(),
        uncheck=AsyncMock(),
        select_option=AsyncMock(),
        evaluate=AsyncMock(return_value="elem_result"),
        press_sequentially=AsyncMock(),
        drag_to=AsyncMock(),
    ))
    mock_page.keyboard = MagicMock(press=AsyncMock())
    mock_page.evaluate = AsyncMock(return_value=42)
    mock_page.wait_for_selector = AsyncMock()
    mock_page.set_viewport_size = AsyncMock()
    mock_page.once = MagicMock()
    mock_page.close = AsyncMock()

    state.pages = [mock_page]
    state.page = mock_page  # type: ignore[assignment]

    # new_page helper
    state.new_page = AsyncMock(return_value=mock_page)
    state._attach_listeners = MagicMock()

    for k, v in overrides.items():
        setattr(state, k, v)
    return state


def _make_ctx(state=None):
    """Return a fake Context whose lifespan_context is *state*."""
    if state is None:
        state = _make_state()
    ctx = MagicMock()
    ctx.request_context.lifespan_context = state
    return ctx


# ---------------------------------------------------------------------------
# Import smoke-test
# ---------------------------------------------------------------------------


def test_server_imports():
    """The server module must be importable and expose the FastMCP instance."""
    from pw_browser_mcp.server import mcp
    assert mcp.name == "pw-browser-mcp"


@pytest.mark.asyncio
async def test_tools_registered():
    """All expected browser tools must be registered."""
    from pw_browser_mcp.server import mcp

    tools = await mcp.list_tools()
    names = {t.name for t in tools}
    expected = {
        "browser_navigate",
        "browser_navigate_back",
        "browser_snapshot",
        "browser_take_screenshot",
        "browser_click",
        "browser_hover",
        "browser_drag",
        "browser_type",
        "browser_fill_form",
        "browser_select_option",
        "browser_press_key",
        "browser_file_upload",
        "browser_handle_dialog",
        "browser_wait_for",
        "browser_evaluate",
        "browser_resize",
        "browser_console_messages",
        "browser_network_requests",
        "browser_tabs",
        "browser_close",
        "browser_install",
    }
    assert expected.issubset(names), f"Missing tools: {expected - names}"


# ---------------------------------------------------------------------------
# Navigation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_browser_navigate():
    from pw_browser_mcp.server import browser_navigate

    ctx = _make_ctx()
    result = await browser_navigate("https://example.com", ctx)
    assert result.startswith("Navigated to")
    assert "200" in result
    ctx.request_context.lifespan_context.page.goto.assert_awaited_once()


@pytest.mark.asyncio
async def test_browser_navigate_back():
    from pw_browser_mcp.server import browser_navigate_back

    state = _make_state()
    state.page.url = "https://prev.example.com"
    state.page.go_back = AsyncMock(return_value=MagicMock(status=200))
    ctx = _make_ctx(state)
    result = await browser_navigate_back(ctx)
    assert "back" in result.lower() or "prev" in result.lower() or "200" in result


@pytest.mark.asyncio
async def test_browser_navigate_back_no_history():
    from pw_browser_mcp.server import browser_navigate_back

    state = _make_state()
    state.page.go_back = AsyncMock(return_value=None)
    ctx = _make_ctx(state)
    result = await browser_navigate_back(ctx)
    assert "no previous" in result.lower() or "cannot" in result.lower()


# ---------------------------------------------------------------------------
# Snapshot / screenshot
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_browser_snapshot():
    from pw_browser_mcp.server import browser_snapshot

    ctx = _make_ctx()
    result = await browser_snapshot(ctx)
    assert "ref=e1" in result


@pytest.mark.asyncio
async def test_browser_take_screenshot():
    from pw_browser_mcp.server import browser_take_screenshot
    from mcp.server.fastmcp import Image

    ctx = _make_ctx()
    result = await browser_take_screenshot(False, ctx)
    assert isinstance(result, Image)


# ---------------------------------------------------------------------------
# Click / hover / drag
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_browser_click():
    from pw_browser_mcp.server import browser_click

    ctx = _make_ctx()
    result = await browser_click("button.submit", ctx=ctx)
    assert "button.submit" in result
    ctx.request_context.lifespan_context.page.locator.assert_called_with("button.submit")


@pytest.mark.asyncio
async def test_browser_click_double():
    from pw_browser_mcp.server import browser_click

    ctx = _make_ctx()
    result = await browser_click("#btn", double_click=True, ctx=ctx)
    assert "#btn" in result
    locator = ctx.request_context.lifespan_context.page.locator.return_value
    locator.dblclick.assert_awaited_once()


@pytest.mark.asyncio
async def test_browser_hover():
    from pw_browser_mcp.server import browser_hover

    ctx = _make_ctx()
    result = await browser_hover("#menu", ctx=ctx)
    assert "#menu" in result


@pytest.mark.asyncio
async def test_browser_drag():
    from pw_browser_mcp.server import browser_drag

    ctx = _make_ctx()
    result = await browser_drag("#item", "#target", ctx=ctx)
    assert "#item" in result
    assert "#target" in result


# ---------------------------------------------------------------------------
# Typing / form filling
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_browser_type():
    from pw_browser_mcp.server import browser_type

    ctx = _make_ctx()
    result = await browser_type("input[name=q]", "hello", ctx=ctx)
    assert "input[name=q]" in result
    locator = ctx.request_context.lifespan_context.page.locator.return_value
    locator.fill.assert_awaited_with("hello")


@pytest.mark.asyncio
async def test_browser_type_submit():
    from pw_browser_mcp.server import browser_type

    ctx = _make_ctx()
    await browser_type("#search", "query", submit=True, ctx=ctx)
    locator = ctx.request_context.lifespan_context.page.locator.return_value
    locator.press.assert_awaited_with("Enter")


@pytest.mark.asyncio
async def test_browser_fill_form_textbox():
    from pw_browser_mcp.server import browser_fill_form

    ctx = _make_ctx()
    result = await browser_fill_form(
        [{"selector": "#name", "value": "Alice", "type": "textbox"}],
        ctx=ctx,
    )
    assert "1 field" in result
    locator = ctx.request_context.lifespan_context.page.locator.return_value
    locator.fill.assert_awaited_with("Alice")


@pytest.mark.asyncio
async def test_browser_fill_form_checkbox():
    from pw_browser_mcp.server import browser_fill_form

    ctx = _make_ctx()
    await browser_fill_form(
        [{"selector": "#agree", "value": "true", "type": "checkbox"}],
        ctx=ctx,
    )
    locator = ctx.request_context.lifespan_context.page.locator.return_value
    locator.check.assert_awaited_once()


@pytest.mark.asyncio
async def test_browser_select_option():
    from pw_browser_mcp.server import browser_select_option

    ctx = _make_ctx()
    result = await browser_select_option("select#country", ["US"], ctx=ctx)
    assert "US" in result
    locator = ctx.request_context.lifespan_context.page.locator.return_value
    locator.select_option.assert_awaited_with(["US"])


@pytest.mark.asyncio
async def test_browser_press_key():
    from pw_browser_mcp.server import browser_press_key

    ctx = _make_ctx()
    result = await browser_press_key("Enter", ctx=ctx)
    assert "Enter" in result
    ctx.request_context.lifespan_context.page.keyboard.press.assert_awaited_with("Enter")


# ---------------------------------------------------------------------------
# Dialogs
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_browser_handle_dialog_accept():
    from pw_browser_mcp.server import browser_handle_dialog

    state = _make_state()
    mock_dialog = MagicMock()
    mock_dialog.type = "confirm"
    mock_dialog.message = "Are you sure?"
    mock_dialog.accept = AsyncMock()
    mock_dialog.dismiss = AsyncMock()
    state._pending_dialog = mock_dialog
    ctx = _make_ctx(state)

    result = await browser_handle_dialog(True, ctx=ctx)
    assert "accepted" in result
    mock_dialog.accept.assert_awaited_once()


@pytest.mark.asyncio
async def test_browser_handle_dialog_dismiss():
    from pw_browser_mcp.server import browser_handle_dialog

    state = _make_state()
    mock_dialog = MagicMock()
    mock_dialog.type = "alert"
    mock_dialog.message = "Hello"
    mock_dialog.accept = AsyncMock()
    mock_dialog.dismiss = AsyncMock()
    state._pending_dialog = mock_dialog
    ctx = _make_ctx(state)

    result = await browser_handle_dialog(False, ctx=ctx)
    assert "dismissed" in result
    mock_dialog.dismiss.assert_awaited_once()


@pytest.mark.asyncio
async def test_browser_handle_dialog_none():
    from pw_browser_mcp.server import browser_handle_dialog

    ctx = _make_ctx()
    result = await browser_handle_dialog(True, ctx=ctx)
    assert "no dialog" in result.lower()


# ---------------------------------------------------------------------------
# Wait / evaluate
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_browser_wait_for_time():
    from pw_browser_mcp.server import browser_wait_for

    ctx = _make_ctx()
    with patch("pw_browser_mcp.server.asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
        result = await browser_wait_for(time=0.1, ctx=ctx)
        mock_sleep.assert_awaited_with(0.1)
    assert "0.1" in result


@pytest.mark.asyncio
async def test_browser_wait_for_text():
    from pw_browser_mcp.server import browser_wait_for

    ctx = _make_ctx()
    result = await browser_wait_for(text="Submit", ctx=ctx)
    ctx.request_context.lifespan_context.page.wait_for_selector.assert_awaited_once_with(
        "text=Submit", state="visible"
    )
    assert "Submit" in result


@pytest.mark.asyncio
async def test_browser_wait_for_text_gone():
    from pw_browser_mcp.server import browser_wait_for

    ctx = _make_ctx()
    result = await browser_wait_for(text_gone="Loading", ctx=ctx)
    ctx.request_context.lifespan_context.page.wait_for_selector.assert_awaited_once_with(
        "text=Loading", state="hidden"
    )
    assert "Loading" in result


@pytest.mark.asyncio
async def test_browser_wait_for_too_many_args():
    from pw_browser_mcp.server import browser_wait_for
    from mcp.server.fastmcp.exceptions import ToolError

    ctx = _make_ctx()
    with pytest.raises(ToolError, match="exactly one"):
        await browser_wait_for(text="foo", time=1.0, ctx=ctx)


@pytest.mark.asyncio
async def test_browser_evaluate_page():
    from pw_browser_mcp.server import browser_evaluate

    ctx = _make_ctx()
    result = await browser_evaluate("document.title", ctx=ctx)
    ctx.request_context.lifespan_context.page.evaluate.assert_awaited_with("document.title")
    assert result == "42"


@pytest.mark.asyncio
async def test_browser_evaluate_element():
    from pw_browser_mcp.server import browser_evaluate

    ctx = _make_ctx()
    result = await browser_evaluate("(el) => el.id", selector="#foo", ctx=ctx)
    locator = ctx.request_context.lifespan_context.page.locator.return_value
    locator.evaluate.assert_awaited_with("(el) => el.id")
    assert result == '"elem_result"'


# ---------------------------------------------------------------------------
# Viewport
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_browser_resize():
    from pw_browser_mcp.server import browser_resize

    ctx = _make_ctx()
    result = await browser_resize(1280, 720, ctx=ctx)
    ctx.request_context.lifespan_context.page.set_viewport_size.assert_awaited_with(
        {"width": 1280, "height": 720}
    )
    assert "1280" in result and "720" in result


# ---------------------------------------------------------------------------
# Observability
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_browser_console_messages_empty():
    from pw_browser_mcp.server import browser_console_messages

    ctx = _make_ctx()
    result = await browser_console_messages(ctx)
    assert json.loads(result) == []


@pytest.mark.asyncio
async def test_browser_console_messages_populated():
    from pw_browser_mcp.server import browser_console_messages

    state = _make_state()
    state._console_messages = [{"type": "log", "text": "hello", "url": "http://x.com"}]
    ctx = _make_ctx(state)
    data = json.loads(await browser_console_messages(ctx))
    assert len(data) == 1
    assert data[0]["text"] == "hello"


@pytest.mark.asyncio
async def test_browser_network_requests():
    from pw_browser_mcp.server import browser_network_requests

    state = _make_state()
    state._network_requests = [{"method": "GET", "url": "http://x.com", "resource_type": "document"}]
    ctx = _make_ctx(state)
    data = json.loads(await browser_network_requests(ctx))
    assert data[0]["method"] == "GET"


# ---------------------------------------------------------------------------
# Tab management
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_browser_tabs_list():
    from pw_browser_mcp.server import browser_tabs

    ctx = _make_ctx()
    result = await browser_tabs("list", ctx=ctx)
    assert "[0]" in result


@pytest.mark.asyncio
async def test_browser_tabs_select_out_of_range():
    from pw_browser_mcp.server import browser_tabs
    from mcp.server.fastmcp.exceptions import ToolError

    ctx = _make_ctx()
    with pytest.raises(ToolError):
        await browser_tabs("select", index=99, ctx=ctx)


@pytest.mark.asyncio
async def test_browser_tabs_select_requires_index():
    from pw_browser_mcp.server import browser_tabs
    from mcp.server.fastmcp.exceptions import ToolError

    ctx = _make_ctx()
    with pytest.raises(ToolError, match="index"):
        await browser_tabs("select", ctx=ctx)


# ---------------------------------------------------------------------------
# Browser close
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_browser_close_only_tab():
    from pw_browser_mcp.server import browser_close

    state = _make_state()
    original_page = state.pages[0]  # keep reference before close replaces it
    new_page_mock = MagicMock()
    new_page_mock.url = "about:blank"
    state.context = MagicMock()
    state.context.new_page = AsyncMock(return_value=new_page_mock)
    ctx = _make_ctx(state)

    result = await browser_close(ctx)
    assert "fresh" in result.lower() or "blank" in result.lower() or "closed" in result.lower()
    original_page.close.assert_awaited_once()
