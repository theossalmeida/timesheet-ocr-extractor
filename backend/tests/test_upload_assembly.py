from threading import Barrier, Event, Lock

import pytest

import jobs


def test_upload_parts_download_concurrently_in_original_order(monkeypatch):
    first_batch = Barrier(4)
    later_part_finished = Event()
    lock = Lock()
    active = 0
    peak = 0
    completed = []

    def read(key):
        nonlocal active, peak
        key = int(key)
        with lock:
            active += 1
            peak = max(peak, active)
        if key < 4:
            first_batch.wait(timeout=5)
        if key == 0:
            assert later_part_finished.wait(5)
        with lock:
            completed.append(key)
            active -= 1
        if key != 0:
            later_part_finished.set()
        return str(key).encode()

    monkeypatch.setattr(jobs.storage, 'read', read)
    parts = [{'object_key': str(i), 'content': None} for i in range(8)]
    assert jobs._read_upload_parts(parts) == b'01234567'
    assert peak == 4
    assert completed[0] != 0


def test_upload_parts_support_legacy_content_and_empty_uploads(monkeypatch):
    monkeypatch.setattr(jobs.storage, 'read', lambda key: b'remote')
    assert jobs._read_upload_parts([]) == b''
    assert jobs._read_upload_parts([{'object_key': None, 'content': memoryview(b'local')}]) == b'local'
    assert jobs._read_upload_parts([
        {'object_key': 'part', 'content': None},
        {'object_key': None, 'content': memoryview(b'local')},
    ]) == b'remotelocal'


def test_upload_part_read_failure_does_not_return_partial_content(monkeypatch):
    def read(key):
        if key == 'missing':
            raise OSError('Missing upload part')
        return b'available'

    monkeypatch.setattr(jobs.storage, 'read', read)
    with pytest.raises(OSError, match='Missing upload part'):
        jobs._read_upload_parts([
            {'object_key': 'available', 'content': None},
            {'object_key': 'missing', 'content': None},
        ])
