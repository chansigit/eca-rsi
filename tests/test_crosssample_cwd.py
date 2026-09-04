"""Regression test for sample-inclusion figure access."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import ecarsi
from ecarsi import crosssample
from ecarsi import harness


def test_sample_inclusion_uses_persample_tree_as_agent_cwd(monkeypatch, tmp_path):
    persample = tmp_path / "persample"
    sample_a = persample / "A"
    sample_b = persample / "B"
    sample_a.mkdir(parents=True)
    sample_b.mkdir()
    inventories = [
        {"sample": "A", "dir": str(sample_a), "figures": [str(sample_a / "umap.png")]},
        {"sample": "B", "dir": str(sample_b), "figures": [str(sample_b / "umap.png")]},
    ]
    captured = {}

    async def fake_run_agent(**kwargs):
        captured.update(kwargs)
        return SimpleNamespace(
            submitted={
                "samples": [
                    {"sample": "A", "include": True, "reason": "ok"},
                    {"sample": "B", "include": True, "reason": "ok"},
                ],
                "notes": "ok",
            },
            cost_usd=None,
        )

    monkeypatch.setattr(harness, "run_agent", fake_run_agent)
    monkeypatch.setattr(ecarsi, "model", lambda: "test-model")

    result = asyncio.run(crosssample._propose(inventories))

    assert result["notes"] == "ok"
    assert captured["cwd"] == str(persample.resolve())
