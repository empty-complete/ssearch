"""Atomic settings writes preserve the prior document on storage failure."""
import json
import os
from pathlib import Path

import pytest

from atom_search.settings.atomic import atomic_write_json


def test_atomic_writer_uses_unique_same_directory_temp(tmp_path, monkeypatch):
    target = tmp_path / 'настройки.json'
    target.write_bytes(b'{"previous":true}')
    replacements = []
    events = []
    real_replace, real_fsync = os.replace, os.fsync

    def fsync(descriptor):
        temporary, = set(tmp_path.iterdir()) - {target}
        assert json.loads(temporary.read_text(encoding='utf-8')) == {'root': 'Документы'}
        events.append('fsync')
        real_fsync(descriptor)

    def replace(source, destination):
        temporary = Path(source)
        assert temporary.parent == target.parent
        assert temporary != target
        assert Path(destination) == target
        assert 'Документы'.encode('utf-8') in temporary.read_bytes()
        assert json.loads(temporary.read_text(encoding='utf-8')) == {'root': 'Документы'}
        events.append('replace')
        replacements.append(temporary)
        real_replace(source, destination)

    monkeypatch.setattr(os, 'fsync', fsync)
    monkeypatch.setattr(os, 'replace', replace)
    atomic_write_json(target, {'root': 'Документы'})
    atomic_write_json(target, {'root': 'Документы'})

    assert replacements[0] != replacements[1]
    assert events == ['fsync', 'replace', 'fsync', 'replace']
    assert json.loads(target.read_text(encoding='utf-8')) == {'root': 'Документы'}
    assert set(tmp_path.iterdir()) == {target}


@pytest.mark.parametrize('failure', ['write', 'fsync', 'replace'])
def test_atomic_writer_failure_preserves_target_and_cleans_only_own_temp(tmp_path, monkeypatch, failure):
    target = tmp_path / 'settings.json'
    previous = b'{"previous": true}\n'
    target.write_bytes(previous)
    other_temp = tmp_path / 'settings.tmp'
    other_temp.write_bytes(b'belongs to another writer')

    def fail(*args, **kwargs):
        if failure == 'write':
            args[1].write('{"partial":')
        raise OSError('injected storage failure')

    with monkeypatch.context() as patch:
        if failure == 'write':
            patch.setattr(json, 'dump', fail)
        else:
            patch.setattr(os, failure, fail)
        with pytest.raises(OSError, match='injected storage failure'):
            atomic_write_json(target, {'next': True})

    assert target.read_bytes() == previous
    assert other_temp.read_bytes() == b'belongs to another writer'
    assert set(tmp_path.iterdir()) == {target, other_temp}
    atomic_write_json(target, {'next': True})
    assert json.loads(target.read_text(encoding='utf-8')) == {'next': True}
