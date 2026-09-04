"""Contract tests for dsh's cwd-confined exploration and image bridge."""

from __future__ import annotations

import asyncio
import base64
from pathlib import Path

import pytest
import yaml

from ecarsi import _harness_deepseek as H
from ecarsi.harness import ToolSpec


PNG_1X1 = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
)


def _run(tool: ToolSpec, **kwargs):
    return asyncio.run(tool.handler(kwargs))


def test_readonly_tools_are_exact_and_cwd_confined(tmp_path: Path):
    outside = tmp_path.parent / f"{tmp_path.name}-outside.txt"
    outside.write_text("secret\n")
    (tmp_path / "report.txt").write_text("alpha\nbeta\n")
    (tmp_path / "figure.png").write_bytes(PNG_1X1)

    tools = {tool.name: tool for tool in H._readonly_tools(
        str(tmp_path), ("read", "glob", "grep"),
    )}
    assert set(tools) == {"Read", "Glob", "Grep"}

    text = _run(tools["Read"], file_path="report.txt")
    assert "alpha\nbeta" in text["content"][0]["text"]

    image = _run(tools["Read"], file_path="figure.png")
    assert [block["type"] for block in image["content"]] == ["text", "image"]
    assert image["content"][1]["mimeType"] == "image/png"

    denied = _run(tools["Read"], file_path=str(outside))
    assert denied["is_error"] is True
    assert "outside the working directory" in denied["content"][0]["text"]

    globbed = _run(tools["Glob"], pattern="**/*.txt")
    assert globbed["content"][0]["text"] == "report.txt"
    assert _run(tools["Glob"], pattern="../*")["is_error"] is True

    grepped = _run(tools["Grep"], pattern="beta", path=".")
    assert "report.txt:2:beta" in grepped["content"][0]["text"]
    assert _run(tools["Grep"], pattern="secret", path=str(outside))["is_error"] is True


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


def test_patch_enables_images_and_disables_all_sdk_coding_tools():
    patch = yaml.safe_load(H._render_patch(
        "http://127.0.0.1:1234/mcp", ("read", "glob", "grep"), "doubao", "vision-model",
        "file:///tmp/raw-attachment.mjs",
    ))
    inserted = {row["id"]: row for row in patch[0]["insert"]}
    assert inserted["ecarsi-attachments"]["name"] == "file:///tmp/raw-attachment.mjs"
    model = inserted["ecarsi-llm-provider"]["config"]["providers"]["doubao"]["models"][0]
    assert model == {"id": "vision-model", "input": ["text", "image"]}

    rows = {row["id"]: row for row in patch[1:]}
    assert all(rows[tool_id]["disabled"] is True for tool_id in H._BUILTIN_DISABLE_IDS)


def test_unknown_capability_fails_closed(tmp_path: Path):
    with pytest.raises(ValueError, match="unsupported allowed_builtin"):
        H._readonly_tools(str(tmp_path), ("read", "write"))
    with pytest.raises(ValueError, match="unsupported allowed_builtin"):
        H._render_patch("http://127.0.0.1:1/mcp", ("write",), "doubao", "m", "file:///tmp/raw.mjs")
