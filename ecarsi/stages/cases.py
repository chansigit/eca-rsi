"""Freeze the evidence of hard agent steps before the pruner deletes it (decision 0021, #51, #14 step 1).

A run's pool requests hold what its agent sessions read; `container/request-pruner.py` deletes them once
the dataset workflow completes. The display program calls `freeze` with the run's `published` sync, which
the dataset workflow awaits before it completes, so the freeze always runs first and no shared component
changes. A session is hard when it died and restarted (`restart.json`), lost its transcript to a context
reset (`context-reset-N.json`), or had at least REJECTIONS submissions rejected by the host. Its case keeps
the session folder (restart and reset generations included) and every file its records reach within
EVIDENCE_DEPTH references: the tool state, its evidence bundle, the bundle's files and the references inside
the JSON files among them, the model replies, tool results and pinned programs. Each file is stored once per
run as files/<sha256>, checked against its reference while it is copied.

Layout: <archive_root>/_cases/<collection>/<dataset>/<run>/{cases.json, files/<sha256>, <session path>/case.json,
<session path>/session/}. cases.json lists the frozen cases and the skipped ones with the reason (CASE_BYTES,
RUN_BYTES). A later freeze of the same run (a resumed run publishing again) adds what is new.
"""
import hashlib
import os
import shutil
import time
from pathlib import Path

from ..files import read, save

REJECTIONS = 2  # host-rejected submissions in one session
EVIDENCE_DEPTH = 3  # tool state -> evidence bundle -> its files -> the references inside JSON files among them
CASE_BYTES = 24 << 30  # one cross-sample case of a 160k-cell dataset was 7.5 GB (hua-heart, 2026-10-06)
RUN_BYTES = 96 << 30
JSON_LIMIT = 64 << 20  # JSON files larger than this are kept but not followed


def library(record) -> Path:
    """The case folder of a run, beside its work archive: record = ecarsi.display.zone(spec)['record']."""
    return Path(record['work']).parents[2] / '_cases' / record['collection'] / record['dataset'] / record['run']


def _refs(value, out):
    if isinstance(value, dict):
        if isinstance(value.get('path'), str) and isinstance(value.get('sha256'), str):
            out.append((value['path'], value['sha256']))
        for key, item in value.items():
            if key not in ('path', 'sha256'):
                _refs(item, out)
    elif isinstance(value, list):
        for item in value:
            _refs(item, out)
    return out


def _json(path):
    try:
        return read(path) if os.path.getsize(path) <= JSON_LIMIT else None
    except (OSError, ValueError):
        return None


def sessions(root):
    """Session folders; a restart (restart/) or a context reset (generation-N/) belongs to the session it continues."""
    for top, dirs, files in os.walk(root):
        dirs.sort()
        if 'session.json' in files:
            dirs[:] = []
            yield Path(top)


def why(folder) -> dict:
    rejected, restarted, resets = 0, False, 0
    for top, _, files in os.walk(folder):
        restarted |= 'restart.json' in files
        resets += sum(name.startswith('context-reset-') for name in files)
        for name in files:
            if name.endswith('.continuation.json'):
                for result in (_json(Path(top) / name) or {}).get('results', []):
                    if str(result.get('name', '')).startswith('submit_'):
                        output = _json((result.get('output') or {}).get('path', ''))
                        rejected += bool(output and output.get('is_error'))
    return dict(rejected=rejected, restarted=restarted, context_resets=resets)


def evidence(folder) -> dict:
    """path -> sha256 of every file the session's records reach within EVIDENCE_DEPTH references."""
    seeds = []
    for top, _, files in os.walk(folder):
        for name in files:
            if name.endswith('.json'):
                seeds += _refs(_json(Path(top) / name), [])
    found, frontier = {}, [(path, sha, 0) for path, sha in seeds]
    while frontier:
        path, sha, depth = frontier.pop()
        if path in found:
            continue
        found[path] = sha
        if depth < EVIDENCE_DEPTH and path.endswith('.json') and os.path.isfile(path):
            frontier += [(p, s, depth + 1) for p, s in _refs(_json(path), [])]
    return found


def hard(root) -> list[dict]:
    """The run's hard sessions, hardest first: restarted, then reset, then by rejections."""
    root = Path(root)
    cases = []
    for folder in sessions(root):
        reasons = why(folder)
        if reasons['restarted'] or reasons['context_resets'] or reasons['rejected'] >= REJECTIONS:
            cases.append(dict(session=str(folder.relative_to(root)), why=reasons))
    return sorted(cases, key=lambda c: (not c['why']['restarted'], -c['why']['context_resets'], -c['why']['rejected'], c['session']))


def _store(path, sha, files):
    target = files / sha
    if target.is_file():
        return
    partial = files / (sha + '.partial')
    digest = hashlib.sha256()
    with open(path, 'rb') as source, open(partial, 'wb') as copy:
        for chunk in iter(lambda: source.read(16 << 20), b''):
            digest.update(chunk)
            copy.write(chunk)
    if digest.hexdigest() != sha:
        partial.unlink()
        raise ValueError(f'{path} no longer matches its reference')
    os.replace(partial, target)


def freeze(root, dest) -> dict:
    """Copy the hard cases of the run at `root` into `dest` (see `library`), within CASE_BYTES and RUN_BYTES."""
    root, dest = Path(root), Path(dest)
    files = dest / 'files'
    index = read(dest / 'cases.json', {}) if dest.is_dir() else {}
    stored = {p.name: p.stat().st_size for p in files.iterdir() if not p.name.endswith('.partial')} if files.is_dir() else {}
    frozen, skipped = [], []
    for case in hard(root):
        folder = root / case['session']
        found = evidence(folder)
        present = {p: s for p, s in found.items() if os.path.isfile(p)}
        sizes = {s: os.path.getsize(p) for p, s in present.items()}
        new = sum(size for sha, size in sizes.items() if sha not in stored)
        total = sum(sizes.values())
        if total > CASE_BYTES or sum(stored.values()) + new > RUN_BYTES:
            skipped.append(dict(case, bytes=total, reason='case over CASE_BYTES' if total > CASE_BYTES else 'run over RUN_BYTES'))
            continue
        files.mkdir(parents=True, exist_ok=True)
        try:
            for path, sha in present.items():
                _store(path, sha, files)
                stored[sha] = sizes[sha]
        except (OSError, ValueError) as exc:
            skipped.append(dict(case, bytes=total, reason=f'{type(exc).__name__}: {exc}'[:300]))
            continue
        target = dest / case['session']
        shutil.copytree(folder, target / 'session', dirs_exist_ok=True, ignore=shutil.ignore_patterns('*.lock'))
        session = read(folder / 'session.json', {})
        save(target / 'case.json', dict(case, session_id=(session.get('spec') or {}).get('session_id'),
                                        model=session.get('model'), bytes=total,
                                        files={p: dict(sha256=s, size=sizes[s]) for p, s in sorted(present.items())},
                                        missing=sorted(set(found) - set(present))))
        frozen.append(case['session'])
    summary = dict(run=str(root), at=round(time.time()), rejections=REJECTIONS, evidence_depth=EVIDENCE_DEPTH,
                   frozen=sorted(set(index.get('frozen', [])) | set(frozen)), skipped=skipped,
                   bytes=sum(stored.values()))
    if frozen or skipped or index:
        dest.mkdir(parents=True, exist_ok=True)
        save(dest / 'cases.json', summary)
    return dict(frozen=len(frozen), skipped=len(skipped), bytes=summary['bytes'], library=str(dest))
