"""Contract tests for the cwd-confined host tools shared by the OpenAI and dsh adapters."""

from __future__ import annotations

import asyncio
import base64
import os
from pathlib import Path

import pytest

from harness_bridge import ToolSpec
from harness_bridge import _harness_host_tools as T

PNG_1X1 = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
)


def _run(tool: ToolSpec, **kwargs):
    return asyncio.run(tool.handler(kwargs))


def _text(result):
    return result["content"][0]["text"]


@pytest.fixture
def workdir(tmp_path: Path) -> Path:
    (tmp_path / "report.txt").write_text("alpha\nbeta\n")
    (tmp_path / "figure.png").write_bytes(PNG_1X1)
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "notes.md").write_text("beta again\n")
    (tmp_path / "blob.bin").write_bytes(b"beta\x00binary")
    (tmp_path / ".hidden").write_text("beta hidden\n")
    outside = tmp_path.parent / f"{tmp_path.name}-outside.txt"
    outside.write_text("secret beta\n")
    os.symlink(outside, tmp_path / "escape.txt")
    return tmp_path


def test_readonly_tools_are_exact_and_cwd_confined(workdir: Path):
    tools = {tool.name: tool for tool in T.readonly_tools(str(workdir), ("read", "glob", "grep"))}
    assert set(tools) == {"Read", "Glob", "Grep"}
    assert [tool.name for tool in T.readonly_tools(str(workdir), ("read",))] == ["Read"]

    text = _run(tools["Read"], file_path="report.txt")
    assert "alpha\nbeta" in _text(text)

    image = _run(tools["Read"], file_path="figure.png")
    assert [block["type"] for block in image["content"]] == ["text", "image"]
    assert image["content"][1]["mimeType"] == "image/png"

    assert _run(tools["Read"], file_path="blob.bin")["is_error"] is True
    outside = workdir.parent / f"{workdir.name}-outside.txt"
    denied = _run(tools["Read"], file_path=str(outside))
    assert denied["is_error"] is True and "outside the working directory" in _text(denied)
    via_symlink = _run(tools["Read"], file_path="escape.txt")
    assert via_symlink["is_error"] is True and "outside the working directory" in _text(via_symlink)
    assert _run(tools["Read"], file_path="missing.txt")["is_error"] is True

    assert _text(_run(tools["Glob"], pattern="**/*.txt")) == "report.txt"  # the escaping symlink is dropped
    assert _text(_run(tools["Glob"], pattern="sub/*")) == "sub/notes.md"
    assert _run(tools["Glob"], pattern="../*")["is_error"] is True
    assert _run(tools["Glob"], pattern="/etc/*")["is_error"] is True


def test_grep_is_recursive_confined_and_skips_binary_and_hidden(workdir: Path):
    grep = T.readonly_tools(str(workdir), ("grep",))[0]

    body = _text(_run(grep, pattern="beta", path="."))
    assert body.splitlines() == ["report.txt:2:beta", "sub/notes.md:1:beta again"]
    assert _text(_run(grep, pattern="^beta$", path="report.txt")) == "report.txt:2:beta"
    assert _text(_run(grep, pattern="beta", path="sub")) == "sub/notes.md:1:beta again"
    assert _text(_run(grep, pattern="nothing-here", path=".")) == "no matches"

    outside = workdir.parent / f"{workdir.name}-outside.txt"
    assert _run(grep, pattern="secret", path=str(outside))["is_error"] is True
    assert _run(grep, pattern="secret", path="escape.txt")["is_error"] is True
    bad = _run(grep, pattern="(unbalanced", path=".")
    assert bad["is_error"] is True and "invalid regular expression" in _text(bad)
    assert _run(grep, pattern="", path=".")["is_error"] is True


def test_grep_bounds_output(tmp_path: Path, monkeypatch):
    (tmp_path / "many.txt").write_text("".join(f"hit {i}\n" for i in range(50)))
    (tmp_path / "long.txt").write_text("x" * 5000 + " needle\n")
    monkeypatch.setattr(T, "SEARCH_MAX_RESULTS", 10)
    grep = T.readonly_tools(str(tmp_path), ("grep",))[0]

    body = _text(_run(grep, pattern="hit", path="many.txt"))
    assert body.count("\n") == 10 and body.endswith("[limited to 10 matches]")
    long_line = _text(_run(grep, pattern="needle", path="long.txt"))
    assert "[line truncated]" in long_line and len(long_line) < 5000


def test_task_tools_keep_a_per_run_checklist():
    tools = {tool.name: tool for tool in T.task_tools()}
    assert set(tools) == {"TaskCreate", "TaskUpdate", "TaskList", "TaskGet"}
    assert _text(_run(tools["TaskList"])) == "no tasks yet"
    assert _run(tools["TaskCreate"], subject="", description="")["is_error"] is True
    assert _text(_run(tools["TaskCreate"], subject="check markers", description="")) == "created task #1: check markers"
    assert _run(tools["TaskUpdate"], taskId="#1", status="done")["is_error"] is True
    assert _run(tools["TaskUpdate"], taskId="9", status="completed")["is_error"] is True
    assert _text(_run(tools["TaskUpdate"], taskId="#1", status="completed")).startswith("updated #1 [completed]")
    assert _text(_run(tools["TaskGet"], taskId="1")) == "#1 [completed] check markers"
    assert _text(_run(tools["TaskList"])).endswith("(1/1 completed)")
    fresh = {tool.name: tool for tool in T.task_tools()}
    assert _text(_run(fresh["TaskList"])) == "no tasks yet"  # every run_agent call gets its own list


def test_served_tools_and_runtime_instructions(tmp_path: Path):
    async def submit(_args):
        return {"content": [{"type": "text", "text": "ok"}]}

    app = [ToolSpec("submit", "submit", {}, submit)]
    assert [t.name for t in T.served_tools(app, str(tmp_path), ())] == ["submit"]
    assert [t.name for t in T.served_tools(app, str(tmp_path), ("read", "tasks"))] == [
        "submit", "Read", "TaskCreate", "TaskUpdate", "TaskList", "TaskGet",
    ]
    with pytest.raises(ValueError, match="unsupported allowed_builtin"):
        T.served_tools(app, str(tmp_path), ("read", "write"))
    with pytest.raises(FileNotFoundError):
        T.served_tools(app, str(tmp_path / "missing"), ("read",))

    instructions = T.with_runtime_instructions("Be brief.", "/work", "submit")
    assert instructions.startswith("Be brief.\n\nHarness runtime: the working directory is '/work'.")
    assert instructions.endswith("The run is complete only after a successful submit call.")
    assert T.with_runtime_instructions(None, "/work", "submit").startswith("Harness runtime:")
