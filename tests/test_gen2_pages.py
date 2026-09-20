"""Periscope reads both generations: gen-2 state comes from publications, not progress.log."""
import json
import os
import re
from pathlib import Path

import pytest

from ecarsi import index, layout as L, serve


def save(path: Path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value))


def gen2_run(root: Path, *, released=True, failed=False) -> Path:
    save(root / 'spec.json', {'run_id': 'r', 'dataset_id': 'D'})
    save(root / '00-organize' / 'publication.json', {'state': 'complete', 'units': [{'name': 'u'}]})
    save(root / '00-organize' / 'organize' / 'manifest.json', {'species': 'human', 'n_cells': 100, 'warnings': []})
    save(root / '00-organize' / 'units' / 'u' / 'input' / 'manifest.json', {'species': 'human', 'n_cells': 100})
    unit = root / 'units' / 'u'
    save(unit / '01-per-sample' / 'publication.json',
         {'state': 'complete', 'samples': [{'path': 'a'}, {'path': 'b'}], 'failed_samples': [],
          'skipped_samples': [{'sample': 'b', 'n_cells': 4, 'error': 'session died twice'}],
          'n_input': 100, 'n_survived': 90, 'n_removed': 10})
    save(unit / 'rounds' / 'round01' / 'publication.json',
         {'round': 1, 'stats': {'n_in': 90, 'n_out': 70, 'removed': 20, 'frac': 0.22,
                                'decision': 'continue', 'reason': 'round 1 never releases'}})
    if released:
        save(unit / 'publication.json', {'state': 'complete', 'n_input': 100, 'n_survived': 70, 'n_removed': 30,
                                         'forced_release': False, 'reason': 'removed 0.5% < 1%', 'rounds': [{}],
                                         'unit': {'name': 'u'}})
        save(unit / 'release' / 'receipt.json', {'state': 'complete'})
        save(unit / 'release' / 'needs_review.json', [])
        (unit / 'release' / 'needs_review.md').write_text('# review\n')
        save(root / 'publication.json', {'state': 'complete', 'dataset_id': 'D', 'units': [], 'failed_units': [],
                                         'n_input': 100, 'n_survived': 70, 'n_removed': 30, 'forced_release': False})
    else:
        save(unit / 'rounds' / 'round02' / '02-cross-sample' / 'publication.json', {'state': 'complete', 'n_input': 70})
        save(root / 'publication.json',
             {'state': 'incomplete', 'dataset_id': 'D', 'units': [],
              'failed_units': [{'unit': 'u', 'error': 'Child Workflow execution failed'}] if failed else [],
              'n_input': 100, 'n_survived': 0, 'n_removed': 0, 'forced_release': False})
    return root


def test_layout_recognises_a_gen2_run_without_mistaking_a_gen1_one(tmp_path):
    root = gen2_run(tmp_path / 'g2')
    assert L.is_gen2_root(root) and L.is_root(root)
    assert L.units(root) == [root / 'units' / 'u'] and L.is_gen2_unit(root / 'units' / 'u')
    assert not L.is_unit(root / 'units' / 'u')      # gen-1 markers (input/manifest.json) are absent
    gen1 = tmp_path / 'g1' / 'units' / 'u'
    save(gen1 / 'input' / 'manifest.json', {'species': 'mouse', 'n_cells': 5})
    assert L.is_unit(gen1) and not L.is_gen2_unit(gen1) and not L.is_gen2_root(tmp_path / 'g1')
    assert serve._check_dataset(root) == root and serve._check_dataset(root / 'units' / 'u')


def test_released_gen2_state_and_page_come_from_the_publications(tmp_path):
    root = gen2_run(tmp_path / 'g2')
    state = index.dataset_state(root)
    assert (state['stage'], state['cls'], state['units'], state['released']) == ('released', 'released', 1, 1)
    assert (state['n_input'], state['final_cells'], state['rounds'], state['species']) == (100, 70, 1, 'human')
    assert state['events']['organize'] and state['events']['release']       # the fleet curve has both ends
    page = index.render_root(root, 'D')
    assert 'round 1 never releases' in page and 'session died twice' in page  # decision reason and the skipped sample
    assert 'release/needs_review.md' in page and '00-organize/organize/manifest.json' in page
    assert index.render_unit(root / 'units' / 'u', 'D')


@pytest.mark.parametrize('failed,expected', [(False, 'running'), (True, 'failed')])
def test_unfinished_gen2_unit_reports_its_round_or_the_datasets_failure(tmp_path, failed, expected):
    root = gen2_run(tmp_path / 'g2', released=False, failed=failed)
    state = index.dataset_state(root)
    assert state['cls'] == expected and state['final_cells'] == 70   # survivors of the last finished round
    unit = index.unit_state(root / 'units' / 'u')   # the aggregate says "failed"; the reason is the unit's
    assert ('Child Workflow execution failed' in unit['stage']) == failed
    if not failed:
        # round 2 has an accepted cross-sample publication, so zoom-in is what is running
        assert state['stage'] == 'round 2 · zoom-in'
        (root / 'units/u/rounds/round03/02-cross-sample').mkdir(parents=True)
        assert index.dataset_state(root)['stage'] == 'round 3 · cross-sample'
    assert index.render_root(root, 'D')


def test_a_unit_still_in_per_sample_is_listed_before_anything_publishes(tmp_path):
    """The control plane creates the stage directory long before it publishes; the page
    renders mid-run the way generation 1 does, instead of claiming nothing was planned."""
    root = tmp_path / 'run'
    save(root / 'spec.json', {'run_id': 'r', 'dataset_id': 'D'})
    save(root / '00-organize' / 'publication.json', {'state': 'complete', 'units': [{'name': 'u'}]})
    save(root / '00-organize' / 'organize' / 'manifest.json', {'species': 'human', 'n_cells': 100, 'warnings': []})
    save(root / '00-organize' / 'units' / 'u' / 'input' / 'manifest.json', {'species': 'human', 'n_cells': 100})
    unit = root / 'units' / 'u'
    (unit / '01-per-sample' / 'agent-abc').mkdir(parents=True)

    assert L.is_gen2_unit(unit) and [u.name for u in L.units(root)] == ['u']
    state = index.unit_state(unit)
    assert state['generation'] == 2 and state['stage'] == 'per-sample running'
    assert state['n_input'] == 100
    assert index.dataset_state(root)['units'] == 1
    assert 'No analysis unit has been planned' not in index.render_root(root)


def test_a_run_whose_files_stopped_moving_is_reported_stopped_not_running(tmp_path, monkeypatch):
    """Nothing on disk records that a driver was killed or a workflow terminated, so a
    'running' state that has not moved for half a day is reported as stopped."""
    root = tmp_path / 'run'
    gen2_run(root, released=False)
    fresh = index.dataset_state(root)
    assert fresh['cls'] == 'running' and not fresh['stage'].startswith('stopped')

    old = fresh['updated'] - index.STALE_AFTER - 60
    for path in root.rglob('*'):
        if path.is_file():
            os.utime(path, (old, old))
    stale = index.dataset_state(root)
    assert stale['cls'] == 'failed' and stale['stage'] == 'stopped · ' + fresh['stage']


def test_a_stage_finishing_inside_a_long_round_counts_as_run_state_moving(tmp_path):
    """A round publishes only when it ends, which can be hours; the stages inside it
    publish as they finish, so the clock keeps moving while the round runs."""
    root = tmp_path / 'run'
    gen2_run(root, released=False)
    old = index.state_mtime(root) - 7200
    for path in root.rglob('*'):
        if path.is_file():
            os.utime(path, (old, old))
    stage = root / 'units' / 'u' / 'rounds' / 'round02' / '02-cross-sample' / 'publication.json'
    os.utime(stage, (old + 3600, old + 3600))
    assert index.state_mtime(root) == old + 3600


def test_a_run_outside_the_fleet_tree_joins_the_collection_its_name_carries(tmp_path):
    """The gen-2 runs live in the control plane's run directory, not next to their
    eca-pp inputs, so the path says nothing; their name does."""
    fleet = tmp_path / 'oak' / 'chondroatlas' / '08_Yan'
    save(fleet / 'standardize' / 'result.json', {})
    (fleet / 'rsi').mkdir()
    items = {'chondroatlas-08_Yanetal': fleet / 'rsi',
             'chondroatlas-g2-08_Yanetal': tmp_path / 'plane' / 'chondro' / '08_Yanetal',
             'somethingelse-42': tmp_path / 'plane' / 'chondro' / 'x'}
    direct = {n: index.collection_of(p) for n, p in items.items()}
    assert index.collections(direct) == {'chondroatlas-08_Yanetal': 'chondroatlas',
                                        'chondroatlas-g2-08_Yanetal': 'chondroatlas',
                                        'somethingelse-42': ''}


def test_publish_copies_the_sample_reports_into_the_unit_and_the_page_links_them(tmp_path):
    """The pool request that produced a sample is a replay cache; the report a person
    reads belongs in the unit directory, and the unit page lists it."""
    from ecarsi.control.persample import materialize_reports

    pool = tmp_path / 'pool' / 'req' / 'outputs' / 'sample'
    (pool / 'figures').mkdir(parents=True)
    (pool / 'report.html').write_text('<html>osp</html>')
    (pool / 'figures' / 'umap.png').write_bytes(b'png')
    (pool / 'qc_summary.csv').write_text('n,1\n')
    (pool / 'clustered.h5ad').write_bytes(b'x' * 1000)
    record = {'sample': 'study__s1__abc',
              'files': {n: {'path': str(pool / n)} for n in
                        ('report.html', 'figures/umap.png', 'qc_summary.csv', 'clustered.h5ad')}}

    root = tmp_path / 'run'
    gen2_run(root, released=False)
    per = root / 'units' / 'u' / L.GEN2_PERSAMPLE
    materialize_reports(per, [record])
    assert (per / 'study__s1__abc' / 'report.html').read_text() == '<html>osp</html>'
    assert (per / 'study__s1__abc' / 'figures' / 'umap.png').is_file()
    assert not (per / 'study__s1__abc' / 'clustered.h5ad').exists()  # matrices stay in the pool
    assert not list((per / 'study__s1__abc').glob('*.part'))

    from ecarsi.control.persample import sample_summary
    records = [dict(record, validation={'n_input': 100, 'n_survived': 90, 'n_removed': 10}, empty=False)]
    save(per / 'samples.json', sample_summary(records, [], []))
    page = index.render_unit(root / 'units' / 'u')
    assert '01-per-sample/study__s1__abc/report.html' in page
    assert '>100<' in page  # the counts gen-1 pages have always shown

    save(root / 'units' / 'u' / 'rounds' / 'round01' / L.GEN2_CROSS / 'inclusion.json',
         {'notes': 'n', 'samples': [{'sample': 'study__s1__abc', 'include': False, 'reason': 'ambient RNA'}]})
    page = index.render_unit(root / 'units' / 'u')
    assert '>exclude<' in page and 'ambient RNA' in page  # the integration column, as in generation 1

    materialize_reports(per, [record])  # a retried publish copies nothing twice


def test_a_gen2_unit_under_a_root_gets_its_page_not_a_directory_listing(tmp_path):
    root = tmp_path / 'run'
    gen2_run(root, released=False)
    assert serve._render_index(root, 'units/u/', 'd') is not None
    assert serve._render_index(root, '', 'd') is not None
    assert serve._render_index(root, 'units/u/rounds/', 'd') is None  # still a plain directory


def test_a_round_shows_its_own_reports_ledger_and_sankey(tmp_path):
    """Generation 1 published a report and a Sankey per round; the page shows whichever
    of them a round has produced, and the newest round's Sankey before any release."""
    root = tmp_path / 'run'
    gen2_run(root, released=False)
    rdir = root / 'units' / 'u' / 'rounds' / 'round01'
    (rdir / L.GEN2_CROSS).mkdir(parents=True, exist_ok=True)
    (rdir / L.GEN2_CROSS / 'report.html').write_text('<html>msp</html>')
    (rdir / L.GEN2_ZOOM).mkdir(parents=True, exist_ok=True)
    (rdir / L.GEN2_ZOOM / 'report.html').write_text('<html>zmip</html>')
    (rdir / L.LEDGER).mkdir(parents=True, exist_ok=True)
    (rdir / L.LEDGER / 'cell_ledger.csv.gz').write_bytes(b'')
    save(rdir / L.LEDGER / 'sankey.json', {'nodes': [], 'links': []})

    page = index.render_unit(root / 'units' / 'u')
    for link in (f'rounds/round01/{L.GEN2_CROSS}/report.html', f'rounds/round01/{L.GEN2_ZOOM}/report.html',
                 f'rounds/round01/{L.LEDGER}/cell_ledger.csv.gz'):
        assert link in page
    assert 'through round 1' in page and 'SANKEY_DATA' in page


def test_a_resumed_unit_is_not_still_failed_from_the_attempt_before(tmp_path):
    """A dataset records a unit's failure when it gives up, and rewrites that record only
    when it finishes. A resume that has already published newer work is not failed."""
    root = tmp_path / 'run'
    gen2_run(root, released=False)
    unit = root / 'units' / 'u'
    save(root / 'publication.json', {'state': 'incomplete', 'dataset_id': 'D', 'units': [],
                                     'failed_units': [{'unit': 'u', 'error': 'Child Workflow execution failed'}],
                                     'n_input': 100, 'n_survived': 0, 'n_removed': 0, 'forced_release': False})
    assert index.unit_state(unit)['stage_class'] == 'failed'

    later = (root / 'publication.json').stat().st_mtime + 60
    resumed = unit / 'rounds' / 'round02' / L.GEN2_CROSS / 'publication.json'
    save(resumed, {'state': 'complete', 'n_input': 70})
    os.utime(resumed, (later, later))
    state = index.unit_state(unit)
    assert state['stage_class'] == 'running' and 'failed' not in state['stage']


def test_a_running_round_shows_the_stage_subtotal_and_the_trend_marks_it_unsettled(tmp_path):
    """Cross-sample publishes its half of a round long before the round ends. It is shown as a
    subtotal -- labelled, and hollow in the sparkline -- never as the round's removal: on the
    chondrocyte batch cross-sample removed 61 cells of a round that went on to remove 3,239."""
    root = tmp_path / 'run'
    gen2_run(root, released=False)
    unit = root / 'units' / 'u'
    save(unit / 'rounds' / 'round02' / '02-cross-sample' / 'publication.json',
         {'state': 'complete', 'n_input': 70, 'n_survived': 69, 'n_removed': 1})

    rounds = index._gen2_rounds(unit)
    assert rounds[0]['stats']['frac'] == pytest.approx(0.22)      # round 1 finished
    assert rounds[1]['stats'] is None and rounds[1]['partial']['stage'] == 'cross-sample'
    assert rounds[1]['partial'] == {'stage': 'cross-sample', 'removed': 1, 'frac': pytest.approx(1 / 70)}

    body = index.render_unit(unit)
    assert '>1</td>' in body and '1.43%' in body and 'so far' in body

    trend = index.round_trend([index.unit_state(unit)])
    assert [(p['n'], p['settled']) for p in trend] == [(1, True), (2, False)]
    svg = index.sparkline(trend)
    assert svg.count('class="sp ') == 2 and 'class="sp failed"' in svg   # 22 % is over 3 %
    assert svg.count('class="sp-hit"') == 2                              # one pointer target per round
    # the target carries the reading and is followed by its dot, so CSS can grow the one hovered
    assert svg.index('class="sp-hit"') < svg.index('class="sp failed"')
    assert 'cursor:help' not in index.CSS                                # a ? over the number, not the number
    assert 'circle.sp{pointer-events:none' in index.CSS and 'circle.sp-hit:hover+circle.sp{r:4}' in index.CSS
    assert 'class="sp released open"' in svg                             # 1.43 % is under 1.5 %: green, still removing
    assert 'round 2: 1.43% removed so far' in svg
    assert index.sparkline([]) == '<span class="muted">–</span>'
    # above the ceiling every point sits on the top edge, and the tooltip still tells the truth
    big = index.sparkline([{'n': 1, 'frac': .36, 'settled': True}, {'n': 2, 'frac': .12, 'settled': True},
                           {'n': 3, 'frac': .009, 'settled': True}])
    assert big.count('cy="3.0" r="2.6"') == 2 and 'cx="3.0"' in big and 'cx="14.0"' in big and 'round 1: 36.00% removed' in big and 'class="sp released"' in big
    assert (index.trend_band(0.0099), index.trend_band(0.02), index.trend_band(0.05)) == ('released', 'running', 'failed')


def test_a_short_run_is_drawn_short(tmp_path):
    """A round is a fixed step from the left edge, not a fraction of the frame: two rounds
    stretched across the whole width would read like a long history of two states."""
    two = index.sparkline([{'n': 1, 'frac': .2, 'settled': True}, {'n': 2, 'frac': .01, 'settled': True}])
    assert 'cx="3.0"' in two and 'cx="14.0"' in two and 'cx="105.0"' not in two
    one = index.sparkline([{'n': 1, 'frac': .2, 'settled': True}])
    assert 'cx="3.0"' in one                                    # left-aligned, not centred
    many = index.sparkline([{'n': i, 'frac': .02, 'settled': True} for i in range(1, 16)])
    xs = [float(v) for v in re.findall(r'cx="([0-9.]+)" cy="[0-9.]+" r="2.6"', many)]
    assert len(xs) == 15 and xs[0] == 3.0 and xs[-1] <= 105.0   # the safety cap still fits
    assert xs[1] - xs[0] < index.TREND_STEP                     # compressed only because it must be


def test_a_round_knows_its_input_before_cross_sample_restates_it(tmp_path):
    """A round's input is settled when the round opens -- it is what the round before left.
    Cross-sample only restates it, so waiting for that publication left the cells-in column
    blank for the first half of every round, which reads as unknown rather than pending."""
    root = gen2_run(tmp_path / 'g2', released=False)
    unit = root / 'units' / 'u'
    # round 2 is open but nothing of it has published yet: no cross-sample, no zoom-in
    (unit / 'rounds' / 'round02' / '02-cross-sample' / 'publication.json').unlink()
    (unit / 'rounds' / 'round03').mkdir()
    rounds = index._gen2_rounds(unit)
    assert [(r['n'], r.get('n_in')) for r in rounds] == [(1, None), (2, 70), (3, 70)]
    assert rounds[0]['stats']['n_in'] == 90 and rounds[0]['stats']['n_out'] == 70
    assert f'<td class="num">{index._n(70)}</td>' in index.render_unit(unit)
    # a first round reads it from per-sample, which is the only thing published before it
    (unit / 'rounds' / 'round01' / 'publication.json').unlink()
    assert [(r['n'], r.get('n_in')) for r in index._gen2_rounds(unit)] == [(1, 90), (2, 90), (3, 90)]
