"""run_state.write_json: an atomic, durable record."""


def test_write_json_syncs_contents_and_directory_and_keeps_its_bytes(tmp_path, monkeypatch):
    import os
    from ecarsi import run_state

    synced = []
    real = os.fsync
    monkeypatch.setattr(run_state.os, "fsync", lambda fd: (synced.append(os.path.isdir(f"/proc/self/fd/{fd}")), real(fd)))
    path = tmp_path / "m" / "manifest.json"
    run_state.write_json(path, {"b": 1, "a": "é"})
    assert synced == [False, True]  # the file, then its directory (the rename)
    assert path.read_text() == '{\n  "b": 1,\n  "a": "é"\n}\n'
    assert [p.name for p in path.parent.iterdir()] == ["manifest.json"]
