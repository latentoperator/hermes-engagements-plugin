"""Unsupported YAML values and identity: targeted versus global defects, the change-log size
boundary, and encoding recognition."""
from datetime import UTC, datetime

import pytest
from engagements.errors import EngagementsError
from engagements.records import apply_ops, parse

BASE = b'---\nengagements: client\nid: acme-bakery\nname: Acme Bakery\nstatus: active\n---\n## Notes\nold\n## Change log\n'


def apply(data, op, kind='client'):
    return apply_ops(data, kind, [op], clock=lambda: datetime(2026, 11, 7, tzinfo=UTC),
                     profile='test', source='notes', client_exists=lambda _: True)


@pytest.mark.parametrize('value', [b'[1]', b'[true]', b'[2026-11-07]', b'[.nan]'])
def test_unsupported_list_is_targeted_not_global(value):
    data = BASE.replace(b'status: active\n', b'aliases: ' + value + b'\nstatus: active\n')
    record = parse(data)
    assert record.properties['aliases'] == {'unsupported_yaml': 'list'}
    assert record.property_defects['aliases'] == 'unsupported_yaml'
    assert not record.global_defects
    with pytest.raises(EngagementsError) as exc:
        apply(data, {'op': 'set_property', 'key': 'aliases', 'values': ['Acme']})
    assert exc.value.code == 'malformed'
    changed = apply(data, {'op': 'set_property', 'key': 'status', 'value': 'paused'})
    assert b'aliases: ' + value + b'\n' in changed


@pytest.mark.parametrize('value', [b'[1]', b'2026-02-31', b'.nan', b'.inf'])
@pytest.mark.parametrize('key,kind', [('name', 'client'), ('id', 'client'), ('client', 'project')])
def test_unsupported_identity_is_null_and_refuses_all_writes(value, key, kind):
    data = BASE if kind == 'client' else BASE.replace(b'engagements: client\n', b'engagements: project\nclient: acme-bakery\n')
    old = {'name': b'Acme Bakery', 'id': b'acme-bakery', 'client': b'acme-bakery'}[key]
    data = data.replace(key.encode() + b': ' + old + b'\n', key.encode() + b': ' + value + b'\n')
    record = parse(data)
    assert 'unsupported_yaml' in record.global_defects
    assert record.properties[key] is None
    if key in ('id', 'name'):
        assert getattr(record, key) is None
    with pytest.raises(EngagementsError) as exc:
        apply(data, {'op': 'replace_section', 'section': 'Notes', 'body': 'new'}, kind)
    assert exc.value.code == 'malformed'


def test_unsupported_repeated_engagement_marker_is_global():
    data = BASE.replace(b'engagements: client\n', b'engagements: client\nengagements: [1]\n')
    record = parse(data)
    assert record.is_record and 'duplicate_key' in record.global_defects
    assert record.properties['engagements'] is None
    with pytest.raises(EngagementsError) as exc:
        apply(data, {'op': 'replace_section', 'section': 'Notes', 'body': 'new'})
    assert exc.value.code == 'malformed'


def test_change_log_creation_checks_one_mib_before_helpers():
    prefix = b'---\nengagements: client\nid: acme-bakery\nname: Acme Bakery\n---\n# '
    data = prefix + b'a' * (1048576 - len(prefix))
    with pytest.raises(EngagementsError) as exc:
        apply(data, {'op': 'replace_section', 'section': 'Notes', 'body': 'new'})
    assert exc.value.code == 'invalid_op'
    assert exc.value.details == {'field': 'ops', 'reason': 'too_large'}


@pytest.mark.parametrize('eol', [b'\n', b'\r\n'])
@pytest.mark.parametrize('size', [65533, 65534, 65535, 65536])
@pytest.mark.parametrize('op', [
    {'op': 'set_property', 'key': 'status', 'value': 'paused'},
    {'op': 'replace_section', 'section': 'Notes', 'body': 'new'},
])
def test_change_log_boundary(eol, size, op):
    prefix = b'---\nengagements: client\nid: acme-bakery\nname: Acme Bakery\nstatus: active\nupdated: 2026-11-07\n# '.replace(b'\n', eol)
    data = prefix + b'a' * (size - len(prefix) - len(eol) - 3) + eol + b'---'
    assert len(data) == size and parse(data).is_record and not parse(data).global_defects
    if size + len(eol) > 65536:
        with pytest.raises(EngagementsError) as exc:
            apply(data, op)
        assert exc.value.code == 'invalid_op'
        assert exc.value.details == {'field': 'ops', 'reason': 'too_large'}
    else:
        assert parse(apply(data, op)).is_record


@pytest.mark.parametrize('eol', [b'\n', b'\r\n'])
@pytest.mark.parametrize('bom', [b'', b'\xef\xbb\xbf'])
@pytest.mark.parametrize('size', [65535, 65536, 65537])
def test_encoding_recognition_uses_original_bytes(eol, bom, size):
    prefix = bom + b'---\nengagements: client\nid: acme-bakery\nname: Acme Bakery\n# '.replace(b'\n', eol)
    suffix = eol + b'---' + eol
    data = prefix + b'a' * (size - len(prefix) - len(suffix) - 1) + b'\xff' + suffix
    assert len(data) == size
    record = parse(data)
    assert record.global_defects == {'encoding'}
    if size <= 65536:
        assert record.is_record and record.kind == 'client'
        assert record.frontmatter_span == (0, size)
        assert record.properties is None
    else:
        assert not record.is_record
