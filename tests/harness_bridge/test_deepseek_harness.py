"""Contract tests for the dsh adapter's MCP bridge, patch and attachment plugin."""

from __future__ import annotations

import asyncio
import base64
import subprocess
from pathlib import Path
from urllib.parse import urlparse

import pytest
import yaml

from harness_bridge import _harness_deepseek as H
from harness_bridge import ToolSpec

PNG_1X1 = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
)


def test_mcp_bridge_preserves_image_content():
    async def image(_args):
        return {"content": [
            {"type": "text", "text": "figure"},
            {"type": "image", "data": base64.b64encode(PNG_1X1).decode("ascii"),
             "mimeType": "image/png"},
        ]}

    fn = H._tool_fn(ToolSpec("image", "image", {}, image), {}, False, "test")
    result = asyncio.run(fn())
    assert [block.type for block in result.content] == ["text", "image"]
    assert result.content[1].mimeType == "image/png"


def test_mcp_bridge_captures_submit_and_marks_errors():
    async def submit(args):
        if args["answer"] == "bad":
            return {"content": [{"type": "text", "text": "rejected"}], "is_error": True}
        return {"content": [{"type": "text", "text": "ok"}], "_submitted": {"answer": args["answer"]}}

    holder: dict = {}
    fn = H._tool_fn(ToolSpec("submit", "submit", {"answer": str}, submit), holder, True, "test")
    rejected = asyncio.run(fn(answer="bad"))
    assert rejected.isError is True and holder == {}
    accepted = asyncio.run(fn(answer="good"))
    assert accepted.isError is False
    assert accepted.content[0].text == "ok"  # the private _submitted key never crosses the wire
    assert holder == {"value": {"answer": "good"}}


def test_mcp_bridge_lets_handler_exceptions_reach_fastmcp():
    async def broken(_args):
        raise KeyError("cluster")

    fn = H._tool_fn(ToolSpec("broken", "broken", {}, broken), {}, False, "test")
    with pytest.raises(KeyError):  # FastMCP converts this into an isError result for the model
        asyncio.run(fn())


def test_patch_enables_images_and_disables_all_sdk_coding_tools():
    patch = yaml.safe_load(H._render_patch(
        "http://127.0.0.1:1234/mcp", ("read", "glob", "grep"), "doubao", "vision-model",
        "file:///tmp/raw-attachment.mjs",
    ))
    inserted = {row["id"]: row for row in patch[0]["insert"]}
    assert inserted["harness-bridge-tools"]["config"]["failOnStartupError"] is True
    assert inserted["harness-bridge-attachments"]["name"] == "file:///tmp/raw-attachment.mjs"
    model = inserted["harness-bridge-llm-provider"]["config"]["providers"]["doubao"]["models"][0]
    assert model == {"id": "vision-model", "input": ["text", "image"]}

    rows = {row["id"]: row for row in patch[1:]}
    assert rows["sandbox-policy"]["config"] == {"mode": "read-only"}
    assert all(rows[tool_id]["disabled"] is True for tool_id in H._BUILTIN_DISABLE_IDS)


def test_patch_for_deepseek_official_declares_no_custom_route():
    patch = yaml.safe_load(H._render_patch(
        "http://127.0.0.1:1/mcp", (), "deepseek-official", None, "file:///tmp/raw.mjs",
    ))
    assert [row["id"] for row in patch[0]["insert"]] == ["harness-bridge-tools", "harness-bridge-attachments"]


def test_patch_fails_closed_on_unknown_capability_or_missing_model():
    with pytest.raises(ValueError, match="unsupported allowed_builtin"):
        H._render_patch("http://127.0.0.1:1/mcp", ("write",), "doubao", "m", "file:///tmp/raw.mjs")
    with pytest.raises(ValueError, match="needs a model id"):
        H._render_patch("http://127.0.0.1:1/mcp", (), "doubao", None, "file:///tmp/raw.mjs")


def test_raw_attachment_plugin_is_valid_javascript(tmp_path: Path):
    dsh_root = tmp_path / "dsh"
    dsh_bin = dsh_root / "apps" / "cli" / "lib" / "bin.js"
    attachment_api = dsh_root / "packages" / "attachment" / "attachment" / "lib" / "index.js"
    dsh_bin.parent.mkdir(parents=True)
    attachment_api.parent.mkdir(parents=True)
    dsh_bin.write_text("// fake dsh entrypoint\n")
    attachment_api.write_text("// fake attachment API\n")

    plugin_url, api_url = H._write_raw_attachment_plugin(str(tmp_path), str(dsh_bin))
    plugin = Path(urlparse(plugin_url).path)
    assert api_url == attachment_api.as_uri()
    subprocess.run(["node", "--check", str(plugin)], check=True, capture_output=True, text=True)
