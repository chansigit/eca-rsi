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
    if frame is None:raise ValueError('A planned DEG comparison has no eligible reference')
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
    if len(set(indices))!=len(indices):raise ValueError('A DEG batch lists each comparison once')
    bundle,plan,loaded=_deg_buffers(prepared_ref);results=[]
    for index in indices:
        folder=destination/('deg-'+str(index));folder.mkdir()
        results.append(_deg_one(prepared_ref,bundle,plan,loaded,index,folder))
    immutable(destination/'results.json',dict(prepared=prepared_ref,indices=indices,results=results))


def deg_results(prepared_ref,results):
    """The per-comparison result documents behind `results`: single result.json refs or deg_batch
    results.json manifests, in any mix."""
    rows=[]
    for r in results:
        doc=verified(r)
        if 'indices' in doc:
            if doc['prepared']!=prepared_ref:raise ValueError('DEG batch belongs to different evidence')
            rows.extend(verified(x) for x in doc['results'])
        else:
            rows.append(doc)
    return rows


def assemble(prepared_ref, results, destination):
    import pandas as pd
    from msp.api import write_deg_results
    from msp.api import DegTables
    bundle=verified(prepared_ref);plan=read(artifact(bundle,'deg_plan.json'));out={**plan,'results':[]}
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
