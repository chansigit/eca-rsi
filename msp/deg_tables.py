"""integrate's precomputed DEG tables as a read-only sqlite (DegTables): an agent's deg_lookup and deg_sql.

Standard library only at import: an agent tool call is a fresh process, and msp.evidence's numpy, pandas and
scipy cost ~1.3 s per call (#59). pandas is imported where a table is built from CSVs and where deg_sql formats
its rows."""
import glob
import os


def filter_desc(min_logfc=None, max_padj=None, min_pct1=None, max_pct2=None):
    parts = []
    if min_logfc:
        parts.append(f"logFC>={float(min_logfc):g}")
    if max_padj is not None and 0 < float(max_padj) < 1:
        parts.append(f"padj<={float(max_padj):g}")
    if min_pct1:
        parts.append(f"pct1>={float(min_pct1):g}")
    if max_pct2 is not None and 0 < float(max_pct2) < 1:
        parts.append(f"pct2<={float(max_pct2):g}")
    return ", ".join(parts)


DEG_TOOL_DOC = """Targeted retrieval over integrate's precomputed DEG tables (deg_global_*/deg_local_*, top-50 \
per cluster per view, every leiden key) — the CSVs themselves are larger than Read allows, use this instead. \
Selectors: cluster (its ranked markers), gene (which clusters have it among their top markers), view \
('global' = one-vs-rest, 'local' = vs the cluster's 3 pooled PAGA neighbours, 'both'), key (leiden key; \
default = the base key). Thresholds (0 / empty = off): min_logfc, max_padj, min_pct1, max_pct2 — e.g. \
min_logfc=1, max_padj=1e-10, max_pct2=0.3 returns just the specific positive markers. top_n per view \
(default 20). At least one of cluster/gene is required. For an arbitrary a-vs-b comparison, check_deg \
(same thresholds) may be used only if the session provides that tool."""

DEG_SQL_DOC = """Read-only SQL (one SELECT, ≤200 rows returned) over the working directory's tables: deg(key \
TEXT, view TEXT 'global'|'local', cluster TEXT, rank INTEGER 1=best, gene TEXT, logfc REAL, padj REAL, \
pct1 REAL, pct2 REAL, neighbors TEXT 'a|b|c' for local rows) = every precomputed DEG table, plus one table \
per other CSV in the directory named by its file stem (cluster_qc_msp_leiden_r1_0, paga_neighbors_..., \
stress_clusters, cell_outlier_summary, ...; non-alphanumeric characters in names/columns become '_'). \
Send the query 'schema' to list tables and columns. Example: SELECT cluster, gene, logfc FROM deg WHERE \
key='msp_leiden_r2.0' AND view='global' AND gene IN ('CD3D','MS4A1') ORDER BY cluster, rank"""


def _sql_name(text):
    return "".join(ch if ch.isalnum() or ch == "_" else "_" for ch in str(text))


class DegTables:
    """integrate's precomputed DEG tables (deg_global_<key>.csv / deg_local_<key>.csv,
    every key present in outdir) loaded into an in-memory, read-only sqlite so the
    agent can retrieve exactly what it needs: Claude Code's Read refuses files
    over 25k tokens and a 40-cluster deg_global CSV is ~50k, so before this the
    agent could not read the tables it was told to read and fell back to 35-s
    check_deg calls. Three surfaces: lookup() (structured filters), sql() (one
    SELECT), markers_text() (a compact per-cluster summary cluster_context
    appends). The tables are evidence the agent already had on disk — no new
    computation, same numbers."""

    _COLS = ("key", "view", "cluster", "rank", "gene", "logfc", "padj", "pct1", "pct2", "neighbors")
    # Per-cell tables (one row per cell) are not evidence the agents query by
    # SQL, and loading them costs memory on every session — skip by file stem.
    PER_CELL_TABLES = frozenset({"cell_outliers", "preannotation_removal", "annotation_removed"})
    # Any other CSV above this size is listed in the schema as skipped instead of
    # being loaded: the summaries the agents need are small, and a directory that
    # accumulates large exports must not slow down every session.
    MAX_EXTRA_TABLE_BYTES = 64 << 20

    def __init__(self, outdir=None, base_key=None, *, database=None):
        import json
        import sqlite3
        from pathlib import Path

        self.base_key = base_key
        if database is not None:
            uri = Path(database).resolve(strict=True).as_uri() + "?mode=ro&immutable=1"
            self.conn = sqlite3.connect(uri, uri=True, check_same_thread=False)
            meta = json.loads(self.conn.execute("SELECT value FROM _evidence_meta").fetchone()[0])
            self.keys, self.n_rows = meta["keys"], meta["n_rows"]
            self.extra_tables, self.skipped_tables = meta["extra_tables"], meta["skipped_tables"]
            self._read_only()
            return
        import pandas as pd

        self.keys: list[str] = []
        self.conn = sqlite3.connect(":memory:", check_same_thread=False)
        self.conn.execute(
            "CREATE TABLE deg (key TEXT, view TEXT, cluster TEXT, rank INTEGER, gene TEXT, "
            "logfc REAL, padj REAL, pct1 REAL, pct2 REAL, neighbors TEXT)"
        )
        rows = []
        for path in sorted(glob.glob(os.path.join(outdir, "deg_*_*.csv"))):
            name = os.path.basename(path)[len("deg_") : -len(".csv")]
            view, key = name.split("_", 1)
            if view not in ("global", "local"):
                continue
            df = pd.read_csv(path, dtype={"group": str})
            if "group" not in df:
                continue
            if key not in self.keys:
                self.keys.append(key)
            if df.empty:
                continue
            df = df.rename(columns={"pct_nz_group": "pct1", "pct_nz_reference": "pct2"})
            for c, sub in df.groupby("group", sort=False):
                nbs = str(sub["neighbors"].iloc[0]) if "neighbors" in sub and pd.notna(sub["neighbors"].iloc[0]) else ""
                for rank, r in enumerate(sub.itertuples(index=False), 1):
                    rows.append(
                        (
                            key,
                            view,
                            str(c),
                            rank,
                            str(r.names),
                            float(r.logfoldchanges),
                            float(r.pvals_adj),
                            float(r.pct1),
                            float(r.pct2),
                            nbs,
                        )
                    )
        self.conn.executemany("INSERT INTO deg VALUES (?,?,?,?,?,?,?,?,?,?)", rows)
        self.conn.execute("CREATE INDEX ix_ckv ON deg(key, view, cluster, rank)")
        self.conn.execute("CREATE INDEX ix_gene ON deg(gene)")
        self.n_rows = len(rows)
        # every other CSV in the directory (QC tables, paga_neighbors_*, stress_clusters,
        # fragments, ...) as its own table named by file stem — the agents reach for
        # them in SQL as soon as they see one table exists, and several are large
        self.extra_tables: dict[str, tuple[int, list[str]]] = {}  # name -> (rows, columns)
        self.skipped_tables: list[str] = []
        for path in sorted(glob.glob(os.path.join(outdir, "*.csv"))):
            stem = os.path.basename(path)[: -len(".csv")]
            if stem.startswith("deg_") or stem in self.PER_CELL_TABLES:
                continue
            name = _sql_name(stem)
            if os.path.getsize(path) > self.MAX_EXTRA_TABLE_BYTES:
                self.skipped_tables.append(name)
                continue
            try:
                df = pd.read_csv(path)
            except Exception:
                continue
            if df.empty or df.shape[1] == 0:
                continue
            df.columns = [_sql_name(c) or f"c{i}" for i, c in enumerate(df.columns)]
            df.to_sql(name, self.conn, index=False, if_exists="replace")
            self.extra_tables[name] = (len(df), list(df.columns))
        self.conn.commit()

        self._read_only()

    def _read_only(self):
        import sqlite3

        def _authorizer(action, *_):
            return (
                sqlite3.SQLITE_OK
                if action in (sqlite3.SQLITE_SELECT, sqlite3.SQLITE_READ, sqlite3.SQLITE_FUNCTION)
                else sqlite3.SQLITE_DENY
            )

        self.conn.set_authorizer(_authorizer)

    def write_database(self, path, *, provenance):
        """Publish one immutable snapshot; workers never share a writable database."""
        import json
        import sqlite3
        from pathlib import Path
        from contextlib import closing

        path = Path(path)
        # Exclusive creation prevents replacing evidence already used by a model.
        with path.open("xb"):
            pass
        with closing(sqlite3.connect(path)) as target:
            self.conn.backup(target)
            target.execute("CREATE TABLE _evidence_meta (value TEXT)")
            target.execute("INSERT INTO _evidence_meta VALUES (?)", [json.dumps(dict(
                keys=self.keys, n_rows=self.n_rows, extra_tables=self.extra_tables,
                skipped_tables=self.skipped_tables, provenance=provenance))])
            target.commit()
        path.chmod(0o444)

    def close(self):
        self.conn.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def _fmt_rows(self, rows, header):
        """Grouped per (view, cluster): one terse line per group — gene #rank logFC padj pct1/pct2."""
        groups = {}
        for _key, view, cluster, rank, gene, logfc, padj, pct1, pct2, nbs in rows:
            ref = "rest" if view == "global" else "PAGA nbrs " + nbs.replace("|", ",")
            groups.setdefault((view, cluster, ref), []).append(
                f"{gene} #{rank} {logfc:.1f} {padj:.0e} {pct1:.2f}/{pct2:.2f}"
            )
        lines = [header]
        for (view, cluster, ref), items in groups.items():
            lines.append(f"  {view} cluster {cluster} vs {ref}: " + ", ".join(items))
        return "\n".join(lines)

    def lookup(
        self,
        cluster="",
        gene="",
        view="both",
        key="",
        top_n=20,
        min_logfc=None,
        max_padj=None,
        min_pct1=None,
        max_pct2=None,
    ):
        cluster, gene, key = str(cluster or "").strip(), str(gene or "").strip(), str(key or "").strip()
        view = str(view or "both").strip().lower() or "both"
        if view not in ("global", "local", "both"):
            return "view must be global | local | both"
        if not cluster and not gene:
            return "give cluster and/or gene"
        key = key or self.base_key or (self.keys[0] if self.keys else "")
        if key not in self.keys:
            return f"no precomputed tables for key {key!r}; available: {self.keys}; select an available key or inspect the computation record"
        if not self.clusters(key):
            return (f"precomputed tables for {key!r} contain no marker rows; this is an empty result, "
                    "not a missing computation. Consult QC and check_genes for expression; "
                    "absence from DEG does not establish absence of expression.")
        where, params = ["key = ?"], [key]
        if view != "both":
            where.append("view = ?")
            params.append(view)
        if cluster:
            where.append("cluster = ?")
            params.append(cluster)
        if gene:
            where.append("upper(gene) = ?")
            params.append(gene.upper())
        if min_logfc:
            where.append("logfc >= ?")
            params.append(float(min_logfc))
        if max_padj is not None and 0 < float(max_padj) < 1:
            where.append("padj <= ?")
            params.append(float(max_padj))
        if min_pct1:
            where.append("pct1 >= ?")
            params.append(float(min_pct1))
        if max_pct2 is not None and 0 < float(max_pct2) < 1:
            where.append("pct2 <= ?")
            params.append(float(max_pct2))
        filters = filter_desc(min_logfc, max_padj, min_pct1, max_pct2)
        top_n = max(1, min(int(top_n or 20), 200))
        sql = f"SELECT * FROM deg WHERE {' AND '.join(where)} ORDER BY view, rank"
        rows = self.conn.execute(sql, params).fetchall()
        n_total = len(rows)
        if cluster:  # per view, the best top_n of the rows that pass
            per_view, selected = {}, []
            for row in rows:
                count = per_view.get(row[1], 0)
                if count < top_n:
                    selected.append(row)
                    per_view[row[1]] = count + 1
            rows = selected
        else:
            rows = rows[:top_n]
        if not rows:
            what = f"cluster {cluster!r}" if cluster else f"gene {gene!r}"
            if gene and not cluster:
                return f"{what} is not among any cluster's top-50 markers in {key} ({view}); use check_genes for its expression per cluster"
            return f"nothing for {what} in {key} ({view}); clusters present: {self.clusters(key)}"
        head = (
            f"precomputed DEG, {key}, "
            + (f"cluster {cluster}" if cluster else f"gene {gene}")
            + (f" ∩ gene {gene}" if cluster and gene else "")
            + f", view={view}"
            + (f" [{filters}]" if filters else "")
            + f": {len(rows)} row(s)"
            + (f" of {n_total} passing" if n_total != len(rows) else "")
            + " (tables hold each cluster's top-50 per view; deeper comparisons require an available computation tool):"
        )
        return self._fmt_rows(rows, head)

    def schema_text(self):
        """One line per table for the tool description / a schema query."""
        lines = [f"deg ({self.n_rows} rows): key, view, cluster, rank, gene, logfc, padj, pct1, pct2, neighbors"]
        for name, (n, cols) in self.extra_tables.items():  # (PRAGMA is blocked by the read-only authorizer)
            lines.append(f"{name} ({n} rows): " + ", ".join(cols))
        if self.skipped_tables:
            lines.append(
                f"not loaded (over {self.MAX_EXTRA_TABLE_BYTES >> 20} MB; read the CSV directly): "
                + ", ".join(self.skipped_tables)
            )
        return "\n".join(lines)

    def clusters(self, key):
        return [
            r[0]
            for r in self.conn.execute(
                "SELECT DISTINCT cluster FROM deg WHERE key = ? ORDER BY CAST(cluster AS REAL), cluster", [key]
            )
        ]

    def sql(self, query, max_rows=200):
        from time import monotonic

        import pandas as pd

        q = str(query or "").strip().rstrip(";").strip()
        if q.lower() in ("schema", "tables", ".tables", "show tables"):
            return self.schema_text()
        if not q.lower().startswith(("select", "with")):
            return "only a single SELECT is allowed (or 'schema' to list tables and columns)"
        if ";" in q:
            return "one statement only"
        deadline = monotonic() + 2
        self.conn.set_progress_handler(lambda: monotonic() >= deadline, 10000)
        try:
            cur = self.conn.execute(q)
            rows = cur.fetchmany(max_rows + 1)
        except Exception as exc:  # sqlite3 errors — feed the message back verbatim
            return f"SQL error: {exc}"
        finally:
            self.conn.set_progress_handler(None, 0)
        cols = [d[0] for d in cur.description]
        truncated = len(rows) > max_rows
        rows = rows[:max_rows]
        if not rows:
            return "no rows"
        df = pd.DataFrame(rows, columns=cols)
        out = df.to_string(index=False, float_format=lambda v: f"{v:.3g}")
        if truncated:
            out += f"\n... truncated at {max_rows} rows — narrow the query"
        return out

    def markers_text(self, key, cluster, n=12):
        """Two compact lines for cluster_context: top global and top local
        markers of one cluster from the precomputed tables ('' if absent)."""
        if key not in self.keys:
            return ""
        lines = []
        for view in ("global", "local"):
            rows = self.conn.execute(
                "SELECT gene, logfc, pct1, pct2, neighbors FROM deg WHERE key=? AND view=? AND cluster=? "
                "AND rank<=? ORDER BY rank",
                [key, view, str(cluster), n],
            ).fetchall()
            if not rows:
                continue
            ref = "rest" if view == "global" else "PAGA nbrs " + rows[0][4].replace("|", ",")
            lines.append(
                f"  top {view} markers (vs {ref}, precomputed; gene logFC pct1/pct2): "
                + ", ".join(f"{g} {lf:.1f} {p1:.2f}/{p2:.2f}" for g, lf, p1, p2, _ in rows)
            )
        return "\n".join(lines)
