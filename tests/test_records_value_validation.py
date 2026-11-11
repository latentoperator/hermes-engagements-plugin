"""Value and identity validation, and descriptor cleanup when a write cannot start."""
import errno
import json
import os
from contextlib import suppress
from datetime import UTC, datetime

import pytest
from engagements import fsutil
from engagements.errors import EngagementsError
from engagements.records import apply_ops, parse, render_client, validate_ops


def CLOCK():
    return datetime(2026, 11, 7, tzinfo=UTC)
BASE = b'---\nengagements: client\nid: acme-bakery\nname: Acme Bakery\nstatus: active\n---\n## Notes\nold\n## Change log\n'


def apply(data, op):
    return apply_ops(data, 'client', [op], clock=CLOCK, profile='test', source='notes', client_exists=lambda _: True)


@pytest.mark.parametrize(('line', 'op', 'expected'), [
    (b'', {'op': 'set_property', 'key': 'name', 'value': '2026-02-31'}, b'name: "2026-02-31"'),
    (b'aliases: [old]\n', {'op': 'set_property', 'key': 'aliases', 'values': ['2026-02-31']}, b'aliases: ["2026-02-31"]'),
    (b'aliases:\n  - old\n', {'op': 'set_property', 'key': 'aliases', 'values': ['2026-02-31']}, b'  - "2026-02-31"'),
])
def test_invalid_date_string_is_quoted_in_each_update_style(line, op, expected):
    result = apply(BASE.replace(b'status: active\n', line + b'status: active\n'), op)
    assert expected in result
    assert parse(result).properties[op['key']] == op.get('value', op.get('values'))


def test_invalid_date_string_is_quoted_in_template():
    result = render_client('acme-bakery', '2026-02-31', clock=CLOCK, properties={'aliases': ['2026-02-31']})
    assert b'name: "2026-02-31"' in result
    assert b'aliases: ["2026-02-31"]' in result
    assert parse(result).name == '2026-02-31'


@pytest.mark.parametrize('value', [b'0xFF', b'0123', b'1_23', b'true', b'null'])
def test_non_string_id_is_invalid_and_refuses_writes(value):
    data = BASE.replace(b'acme-bakery', value)
    assert 'invalid_id' in parse(data).global_defects
    with pytest.raises(EngagementsError) as exc:
        apply(data, {'op': 'set_property', 'key': 'status', 'value': 'paused'})
    assert exc.value.code == 'malformed'


def test_quoted_numeric_id_is_valid():
    data = BASE.replace(b'acme-bakery', b'"0123"')
    assert parse(data).id == '0123'
    assert not parse(data).global_defects
    assert parse(apply(data, {'op': 'set_property', 'key': 'status', 'value': 'paused'})).id == '0123'


def test_deep_yaml_recursion_uses_marker_fallback_for_read_and_discovery(tmp_path):
    data = b'---\nengagements: client\nid: acme-bakery\nname: Acme Bakery\nx: ' + b'[' * 500 + b'a' + b']' * 500 + b'\n---\n'
    assert len(data) == 1067
    path = tmp_path / 'record.md'
    path.write_bytes(data)
    with path.open('rb') as file:
        prefix = file.read(65536)
    for content in (data, prefix):
        record = parse(content)
        assert record.is_record and record.kind == 'client'
        assert record.global_defects == {'yaml_parse'}
        with pytest.raises(EngagementsError) as exc:
            apply(content, {'op': 'set_property', 'key': 'status', 'value': 'paused'})
        assert exc.value.code == 'malformed'


@pytest.mark.parametrize('value', [b'.nan', b'.inf', b'-.inf'])
def test_nonfinite_float_is_targeted_and_json_safe(value):
    data = BASE.replace(b'status: active\n', b'x: ' + value + b'\nstatus: active\n')
    record = parse(data)
    assert record.properties['x'] == {'unsupported_yaml': 'float'}
    assert record.property_defects['x'] == 'unsupported_yaml'
    json.dumps(record.properties, allow_nan=False)
    assert parse(apply(data, {'op': 'set_property', 'key': 'status', 'value': 'paused'})).properties['x'] == {'unsupported_yaml': 'float'}


@pytest.mark.parametrize(('action', 'field'), [('replace_section', 'body'), ('append_to_section', 'text')])
def test_multiline_surrogate_is_invalid_op(action, field):
    op = {'op': action, 'section': 'Notes', field: 'first\n\ud800'}
    for call in (lambda: validate_ops('client', [op], client_exists=lambda _: True), lambda: apply(BASE, op)):
        with pytest.raises(EngagementsError) as exc:
            call()
        assert exc.value.code == 'invalid_op'
        assert exc.value.details['reason'] == 'invalid_value'
        assert exc.value.details['field'] == field
    op[field] = 'first\nsecond café'
    assert 'second café'.encode() in apply(BASE, op)


@pytest.mark.skipif(os.name == 'nt', reason='flock injection is POSIX-specific')
def test_unlock_failure_closes_retained_handle_and_releases_lock(tmp_path, monkeypatch):
    path = tmp_path / 'record.lock'
    lock = fsutil.FileLock(path, timeout=0.1)
    original_flock = fsutil.fcntl.flock
    def fail_unlock(fd, operation):
        if operation == fsutil.fcntl.LOCK_UN:
            raise OSError(errno.EIO, 'injected')
        return original_flock(fd, operation)
    monkeypatch.setattr(fsutil.fcntl, 'flock', fail_unlock)
    with pytest.raises(EngagementsError) as exc, lock:
        handle = lock._file
    assert exc.value.code == 'io_error'
    assert handle.closed and lock._file is None
    monkeypatch.setattr(fsutil.fcntl, 'flock', original_flock)
    with fsutil.FileLock(path, timeout=0.1):
        pass


def test_fdopen_failure_closes_mkstemp_fd(tmp_path, monkeypatch):
    path = tmp_path / 'record.md'
    path.write_bytes(b'old')
    original_mkstemp = fsutil.tempfile.mkstemp
    fds = []
    def mkstemp(*args, **kwargs):
        fd, name = original_mkstemp(*args, **kwargs)
        fds.append(fd)
        return fd, name
    def fail_fdopen(*args, **kwargs):
        raise OSError(errno.EIO, 'injected')
    monkeypatch.setattr(fsutil.tempfile, 'mkstemp', mkstemp)
    monkeypatch.setattr(fsutil.os, 'fdopen', fail_fdopen)
    try:
        with pytest.raises(EngagementsError) as exc:
            fsutil.atomic_write(path, b'new')
        assert exc.value.code == 'io_error'
        with pytest.raises(OSError) as error:
            os.fstat(fds[0])
        assert error.value.errno == errno.EBADF
        assert path.read_bytes() == b'old'
        assert list(tmp_path.iterdir()) == [path]
    finally:
        with suppress(OSError):
            os.close(fds[0])
