"""Workflow/driver observation belongs in Overview, independently of compute workers."""
import html
import time
from collections import Counter

from .batch import ACTIVE, monitor


def summary():
    state = monitor()
    # During migration a legacy receipt and the durable queue may describe the
    # same output. The durable (later) record is authoritative.
    rows = list({r["output"]: r for r in state["datasets"] if r.get("output")}.values())
    active = [r for r in rows if r["state"] in ACTIVE]
    kinds = Counter(r.get("activity_kind", "unknown") for r in active)
    return dict(active=active, waiting=sum(r.get("waiting", False) for r in rows),
                phases=dict(kinds), states=dict(Counter(r["state"] for r in rows)))


def render():
    data = summary()
    e = html.escape
    rows = []
    recent = sorted(data["active"], key=lambda r: r.get("last_log_at") or r.get("started_at")
                    or r.get("assigned_at") or r.get("submitted_at") or 0, reverse=True)[:15]
    for r in recent:
        age = max(0, int(time.time() - r["last_log_at"])) if r.get("last_log_at") else None
        host = r.get("node", r.get("placement", {}).get("host", "driver host")).split("/")[0]
        limit = r.get("memory_gb", r.get("placement", {}).get("memory_gb"))
        rss = f'{r["rss_bytes"]/2**30:.1f}' if "rss_bytes" in r else "Unknown"
        memory = f'{rss} / {limit:.0f} GiB' if limit else "Not reported"
        rows.append(f'<tr><td>{e(r["name"])}</td><td>{e(host)}</td>'
                    f'<td title="{e(r.get("last_message", ""))}">{e(r.get("activity", "Starting"))}</td>'
                    f'<td>{memory}</td>'
                    f'<td>{str(age) + "s ago" if age is not None else "No log yet"}</td></tr>')
    counts = data["states"]
    facts = " · ".join(f'{counts.get(k,0)} {v}' for k,v in
                       (("running","running"),("assigned","starting"),("queued","queued"),("paused","paused"),("retry_wait","retry backoff"))
                       if counts.get(k)) or "No active or queued workflows"
    return (f'<p>{e(facts)}</p><p class="muted">Workflows call models and submit individual compute tasks to Warm Pool. '
            'Latest activity comes from logs; a running driver does not mean a busy compute worker.</p>'
            f'<p class="muted">Showing latest {len(recent)} of {len(data["active"])} active workflows.</p>'
            '<div class="wrap"><table><thead><tr><th>Dataset</th><th>Driver host</th><th>Latest activity</th>'
            '<th>RSS / memory limit</th><th>Last log</th></tr></thead><tbody>' + ''.join(rows) + '</tbody></table></div>')
