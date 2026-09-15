"""Publication relocates paths while retaining the immutable worker receipt."""
import pytest

from ecarsi import layout as L, organize_v2 as organize
from ecarsi.run_state import digest, file_identity, read_json, write_json
from ecarsi.warm_pool.state import read, save


def outputs(tmp_path):
    output, destination = tmp_path / 'worker', tmp_path / 'published'
    unit = L.unit_dir(output / 'run', 'unit')
    L.input_h5ad(unit).parent.mkdir(parents=True)
    L.input_h5ad(unit).write_bytes(b'unchanged scientific output')
    mapping = unit / 'input' / 'samples.csv'
    mapping.write_text('cell_id,sample\n001,A\n')
    write_json(L.input_manifest(unit), {'sample_mapping': {
        'path': mapping.name, 'identity': file_identity(mapping)}})
    item = dict(name='unit', dir=str(unit), identity=file_identity(L.input_h5ad(unit)),
                manifest_identity=file_identity(L.input_manifest(unit)))
    manifest = dict(state='complete', input_identity='input', plan={'test': True},
                    units_written=[item], warnings=[])
    write_json(L.organize_manifest(output / 'run'), manifest)
    save(output / 'completion.json', dict(input_identity='input', plan_digest=digest(manifest['plan']),
         manifest=file_identity(L.organize_manifest(output / 'run')),
         units=[dict(name='unit', input=item['identity'], manifest=item['manifest_identity'])]))
    return output, destination


def test_relocation_crash_retry_and_receipt_integrity(tmp_path, monkeypatch):
    output, destination = outputs(tmp_path)
    original_write = organize.write_json

    def crash(path, value):
        if path == L.organize_manifest(destination):
            raise OSError('interrupted after rename')
        original_write(path, value)

    monkeypatch.setattr(organize, 'write_json', crash)
    with pytest.raises(OSError, match='interrupted'):
        organize.publish(output, destination)
    monkeypatch.setattr(organize, 'write_json', original_write)
    assert organize.publish(output, destination) == str(destination)
    publication = read(destination / 'publication.json')
    assert publication['manifest'] == file_identity(L.organize_manifest(destination))
    assert publication['worker_manifest'] == file_identity(L.organize_manifest(output / 'run'))
    assert publication['worker_manifest'] != publication['manifest']
    assert read_json(L.organize_manifest(destination))['units_written'][0]['dir'] == str(L.unit_dir(destination, 'unit'))
    assert organize.publish(output, destination) == str(destination)
    assert read(destination / 'publication.json') == publication
    manifest = read_json(L.organize_manifest(destination))
    manifest['warnings'].append('unaccepted alteration')
    write_json(L.organize_manifest(destination), manifest)
    with pytest.raises(ValueError, match='published manifest changed'):
        organize.publish(output, destination)


def test_reject_modified_worker_manifest(tmp_path):
    output, destination = outputs(tmp_path)
    path = L.organize_manifest(output / 'run')
    manifest = read_json(path)
    manifest['warnings'].append('unaccepted alteration')
    write_json(path, manifest)
    with pytest.raises(ValueError, match='worker manifest changed'):
        organize.publish(output, destination)
    assert not destination.exists()
