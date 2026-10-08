"""What the stage programs share: sealed bundles and their artifacts, figures for the model, and the DEG
comparisons cross-sample and zoom-in both run. Stage programs import from here, never from each other, so a
change to one stage's file pins and invalidates only that stage's requests (and this file pins them all)."""
import base64
import io
import shutil
from pathlib import Path

from ..files import immutable, read, reference, verified

BASE = 'msp_leiden_r2.0'  # the cross-sample annotation resolution


def sealed(directory, destination, **metadata):
    files = {str(p.relative_to(directory)): reference(p) for p in sorted(directory.rglob("*"))
             if p.is_file() and not p.name.startswith(".")}
    return immutable(destination, {**metadata, "files": files})


def check_bundle(ref):
    bundle = verified(ref)
    for item in bundle["files"].values():
        if reference(item["path"]) != item:
            raise ValueError("Sample artifact changed")
    return bundle


def png_url(path):
    """A figure as a data URL for the model. A PNG above 512 KiB goes to a 256-colour palette at full size:
    the 418k-cell umap__ann_coarse.png fell from 1.66 MB to 533 KiB. Turns carrying the original timed out
    or failed to parse at the provider for 10 minutes at a time (scale test 2026-10-05)."""
    data = Path(path).read_bytes()
    if len(data) > 512 * 1024:
        from PIL import Image
        buffer = io.BytesIO()
        Image.open(io.BytesIO(data)).convert("RGB").quantize(256).save(buffer, "PNG", optimize=True)
        data = min(data, buffer.getvalue(), key=len)
    return "data:image/png;base64," + base64.b64encode(data).decode()


def artifact(bundle, name):
    ref = bundle['files'][name]
    if reference(ref['path']) != ref:
        raise ValueError('Evidence artifact changed: ' + name)
    return Path(ref['path'])


def publish_bundle(destination, name, parent=None, **metadata):
    files = {k:v for k,v in verified(parent)['files'].items() if not any(p.startswith('.') for p in Path(k).parts)} if parent else {}
    files.update({str(p.relative_to(destination)): reference(p) for p in sorted(destination.rglob('*'))
                  if p.is_file() and p.name != name and not any(part.startswith('.') for part in p.relative_to(destination).parts)})
    return immutable(destination / name, {**metadata, 'files': files})


def _deg_buffers(prepared_ref):
    """The verified bundle, its DEG plan and the mapped shared buffers: verified once per process
    (each process maps them without loading counts or graphs)."""
    from msp.api import load_deg_input
    bundle=verified(prepared_ref)
    for name in bundle['files']:
        if name.startswith('deg_input/'):
            artifact(bundle,name)
    directory=artifact(bundle,'deg_input/metadata.h5ad').parent
    return bundle,read(artifact(bundle,'deg_plan.json'))['plan'],load_deg_input(directory)


def _deg_one(prepared_ref,bundle,plan,loaded,index,destination):
    from msp.api import compute_deg_task
    task=bundle['tasks'][index]
    frame=compute_deg_task(loaded,plan[task['plan_index']],task['cluster'])
    if frame is None:
        raise ValueError('A planned DEG comparison has no eligible reference')
    # Stress uses only top 10; annotation keeps the documented top 50 per view.
    frame.groupby('group',observed=True).head(50).to_csv(destination/'deg.csv',index=False)
    return immutable(destination/'result.json',dict(prepared=prepared_ref,index=index,task=task,table=reference(destination/'deg.csv')))


def deg(prepared_ref, index, destination):
    bundle,plan,loaded=_deg_buffers(prepared_ref)
    _deg_one(prepared_ref,bundle,plan,loaded,index,destination)


def deg_batch(prepared_ref, indices, destination):
    """Several comparisons in one pool request: the buffers are verified and mapped once, each
    comparison writes the deg-<i>/result.json a single request would, and results.json lists them so
    assemble takes the batch in place of its members. One request per comparison cost each a process
    start, a numba warm-up and a SHA pass over the buffers, and a lineage made N of them (2026-09-27)."""
    indices=[int(i) for i in indices]
    if len(set(indices)) != len(indices):
        raise ValueError('A DEG batch lists each comparison once')
    bundle, plan, loaded = _deg_buffers(prepared_ref)
    results = []
    for index in indices:
        folder = destination / ('deg-' + str(index))
        folder.mkdir()
        results.append(_deg_one(prepared_ref,bundle,plan,loaded,index,folder))
    immutable(destination/'results.json',dict(prepared=prepared_ref,indices=indices,results=results))


def deg_results(prepared_ref,results):
    """The per-comparison result documents behind `results`: single result.json refs or deg_batch
    results.json manifests, in any mix."""
    rows=[]
    for r in results:
        doc=verified(r)
        if 'indices' in doc:
            if doc['prepared'] != prepared_ref:
                raise ValueError('DEG batch belongs to different evidence')
            rows.extend(verified(x) for x in doc['results'])
        else:
            rows.append(doc)
    return rows


def assemble(prepared_ref, results, destination):
    import pandas as pd
    from msp.api import write_deg_results
    from msp.api import DegTables

    bundle = verified(prepared_ref)
    plan = read(artifact(bundle, 'deg_plan.json'))
    out = {**plan, 'results': []}
    rows=deg_results(prepared_ref,results)
    if sorted(r['index'] for r in rows)!=list(range(len(bundle['tasks']))):
        raise ValueError('Missing or duplicate DEG results')
    by_index={r['index']:r for r in rows}
    for i,task in enumerate(bundle['tasks']):
        r=by_index[i]
        if r['prepared']!=prepared_ref or r['task']!=task or reference(r['table']['path'])!=r['table']:
            raise ValueError('DEG result belongs to different evidence')
        item=plan['plan'][task['plan_index']]
        out['results'].append((item['key'],'global' if task['cluster'] is None else 'local',task['cluster'],pd.read_csv(r['table']['path'],dtype={'group':str},keep_default_na=False)))
    for name in bundle['files']:
        if '/' not in name and name.endswith('.csv') and not name.startswith(('deg_','stress_clusters')):
            shutil.copyfile(artifact(bundle,name),destination/name)
    write_deg_results(out,plan['keys'],str(destination),top_n_de=50)
    with DegTables(destination,BASE) as tables:
        tables.write_database(destination/'deg.sqlite',provenance={'prepared':prepared_ref,'mask':bundle['files']['preannotation_removal.csv'],'coverage':'top 50 per cluster and view'})
    publish_bundle(destination,'evidence.json',prepared_ref,**{k:v for k,v in bundle.items() if k!='files'},prepared=prepared_ref,comparisons=results)


# Stress, dissociation and dying cells (decision 0017). Under the default policy "remove" a removal of at
# least CHECK_MIN_CELLS cells stands only on evidence the code checks; otherwise, and always under "keep",
# its cells stay with their identity labels and obs['retained_state'] names the state.
STRESS_STATES = ('stress', 'dissociation', 'dying')
STRESS_POLICIES = ('remove', 'keep')
CHECK_MIN_CELLS = 10  # below this an agent's removal stands on its reason (owner, 2026-10-05); msp has no DEG there
DYING_AUC, DYING_P = 0.7, 0.05
FRAGMENT_DROP_PCT = 50.0  # msp DROP_PCT_THRESH: a fragment more than half dropped upstream is not a state
STRESS_HOST_POLICY = 'stress_policy_v1'
RETAINED = 'retained_state'


def stress_policy(spec):
    return spec.get('config', {}).get('stress_policy', 'remove')


def stress_flags(bundle, column='stress'):
    """(key, cluster) pairs msp marks in stress_clusters.csv. 'stress': more than 3 of the cluster's top 10 DEG
    genes are heat-shock or immediate-early genes, in its global or its local view. 'mito': a small cluster (under a
    quarter of its local siblings' cells) has more than 3 mitochondrial genes among its top 10 against them (owner,
    2026-10-06); a table from before has no such column."""
    import pandas as pd
    if 'stress_clusters.csv' not in bundle['files']:
        return set()
    table = pd.read_csv(artifact(bundle, 'stress_clusters.csv'), dtype=str, keep_default_na=False)
    if column not in table:
        return set()
    hit = table[table[column].str.lower().eq('true')]
    return set(zip(hit.key, hit.cluster))


def dying_evidence(obs, target, comparison):
    """(supported, note): the target cells have a clearly higher mitochondrial fraction or clearly fewer
    genes than the comparison cells, one-sided Mann-Whitney with AUC >= 0.7 and p < 0.05."""
    from scipy.stats import mannwhitneyu
    notes = []
    for column, sign in (('pct_counts_mt', 1), ('n_genes_by_counts', -1)):
        if column not in obs:
            notes.append(column + ' missing')
            continue
        a = sign * obs.loc[target, column].astype(float).dropna().to_numpy()
        b = sign * obs.loc[comparison, column].astype(float).dropna().to_numpy()
        if min(len(a), len(b)) < CHECK_MIN_CELLS:
            notes.append(column + ': too few measured cells')
            continue
        u, p = mannwhitneyu(a, b, alternative='greater')
        auc = u / (len(a) * len(b))
        notes.append(f'{column} AUC {auc:.2f} p {p:.2g}')
        if auc >= DYING_AUC and p < DYING_P:
            return True, '; '.join(notes)
    return False, '; '.join(notes)


def comparison_cells(obs, same_identity, removing):
    """Cells of the same identity that no decision removes; all cells no decision removes when fewer than 10."""
    near = same_identity & ~removing
    return near if near.sum() >= CHECK_MIN_CELLS else ~removing


def guard_stress(entry, policy, n_cells, flagged, dying_check, mito=False):
    """Apply the stress policy to one removal in place; True when it became a keep. `flagged`: the stress-gene
    rule marks the target's cluster; `mito`: the mitochondrial rule does, which supports a dying removal as
    `dying_check()` -> (supported, note) does. The tool reply and needs_review read host_adjustment;
    requested_* keep what the agent asked for."""
    state = entry.get('remove_reason')
    if entry.get('action') != 'remove' or state not in STRESS_STATES:
        return False
    if policy == 'remove' and n_cells < CHECK_MIN_CELLS:
        return False
    if policy == 'keep':
        supported, note = False, 'stress policy keep'
    elif state == 'dying':
        supported, note = ((True, 'stress_clusters.csv marks the cluster mito (more than 3 of its top 10 genes against '
                            'its local siblings are mitochondrial genes)') if mito else dying_check())
    else:
        supported, note = flagged, ('stress_clusters.csv marks the cluster' if flagged else
                                    'stress_clusters.csv does not mark the cluster (more than 3 of its top 10 DEG genes are stress genes)')
    if supported:
        entry['host_evidence'] = note
        return False
    entry.update(requested_action='remove', requested_remove_reason=state, action='keep', remove_reason=None,
                 review_required=True, host_adjustment=dict(policy=STRESS_HOST_POLICY, stress_policy=policy, state=state,
                 n_cells=int(n_cells), evidence=note, reason=(f'Stress policy keep: {state} cells stay, labelled.' if policy == 'keep'
                 else f'The {state} removal is not supported by the code check; its cells stay, labelled.')))
    return True


def guard_retained_drops(proposal, retained):
    """#30: cross-sample inspect may not drop a cluster the type phase kept under the stress policy: its drops
    become flags, which needs_review lists. In place, like msp's batch guard; the clusters adjusted."""
    adjusted = set()
    for entry in [*proposal['clusters'], *proposal.get('cell_actions', [])]:
        if str(entry['cluster']) in retained and entry.get('action') == 'drop':
            entry.update(requested_action='drop', action='flag', review_required=True, host_adjustment=dict(
                policy=STRESS_HOST_POLICY, reason='The type phase kept this cluster under the stress policy (decision 0017); '
                                                 'quality flags it for review instead of dropping it.'))
            adjusted.add(str(entry['cluster']))
    return sorted(adjusted)


FRAGMENT_TESTS = ('decontX', 'dissociation', 'doublet', 'mt')  # msp minor-sibling QC, columns <test>_significant


def fragment_reasons(table):
    """{fragment: {'tests': [...]}} for msp's removed minor-sibling fragments: the tests that hit, and 'dropped
    upstream' when more than half its cells were (#27: needs_review lists fragment removals by test)."""
    out = {}
    for row in table.to_dict('records'):
        if str(row.get('recommend_removal', '')).lower() != 'true':
            continue
        tests = [t for t in FRAGMENT_TESTS if str(row.get(t + '_significant', '')).lower() == 'true']
        dropped = str(row.get('pct_drop_upstream', '')).strip()
        if dropped and dropped != 'nan' and float(dropped) > FRAGMENT_DROP_PCT:
            tests.append('dropped upstream')
        out[str(row['subcluster'])] = dict(tests=tests)
    return out


def soft_fragments(table):
    """{fragment: state} of msp minor-sibling fragments removed only by their dissociation and/or mitochondrial
    test: no decontX or doublet hit and not more than half dropped upstream. Kept under the policy keep."""
    def hit(row, name):
        return str(row.get(name + '_significant', '')).lower() == 'true'
    soft = {}
    for row in table.to_dict('records'):
        if str(row.get('recommend_removal', '')).lower() != 'true' or hit(row, 'decontX') or hit(row, 'doublet'):
            continue
        dropped = str(row.get('pct_drop_upstream', '')).strip()
        if dropped and float(dropped) > FRAGMENT_DROP_PCT:
            continue
        if hit(row, 'dissociation') or hit(row, 'mt'):
            soft[str(row['subcluster'])] = 'dissociation' if hit(row, 'dissociation') else 'dying'
    return soft


def fragment_table(bundle):
    import pandas as pd
    try:
        return pd.read_csv(artifact(bundle, 'minor_sibling_qc.csv'), dtype=str, keep_default_na=False)
    except pd.errors.EmptyDataError:  # msp writes a bare newline when no minor sibling fragment exists
        return pd.DataFrame(columns=['subcluster', 'recommend_removal'])


def mark_retained(obs, cells):
    """obs['retained_state'] from {cell: state}; a cell keeps the state it was first retained for."""
    import pandas as pd
    current = obs[RETAINED].astype(object).fillna('').astype(str) if RETAINED in obs else pd.Series('', index=obs.index)
    new = pd.Series(cells, dtype=object).reindex(obs.index).fillna('')
    obs[RETAINED] = pd.Categorical(current.where(current != '', new))
