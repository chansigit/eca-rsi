"""Private resume receipts; public CSV, JSON and H5AD contracts stay unchanged."""

import hashlib
import json
import os
from pathlib import Path



def file_digest(path):
    """Hash incrementally so large H5ADs do not need a second in-memory copy."""
    digest = hashlib.sha256()
    with open(path, "rb") as stream:
        for block in iter(lambda: stream.read(8 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def write_json(path, value):
    path = Path(path)
    temporary = path.with_name(path.name + ".tmp")
    with temporary.open("w") as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2)
    os.replace(temporary, path)


def run_id(outdir):
    path = Path(outdir) / ".zmip-run.json"
    return json.loads(path.read_text())["run_id"] if path.exists() else None


def seal(outdir, stage, generation, files):
    """Publish completion only after all files have been successfully written."""
    write_json(
        Path(outdir) / f".zmip-{stage}.json",
        {
            "run_id": generation,
            "files": {name: file_digest(Path(outdir) / name) for name in files},
        },
    )
