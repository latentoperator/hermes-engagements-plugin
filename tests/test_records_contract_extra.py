import pytest
from engagements.records import footprint, parse


@pytest.mark.parametrize('eol', [b'\n', b'\r\n'])
def test_footprint_complete_and_absent(eol):
    data = eol.join([b'---', b'engagements: client', b'id: acme-bakery', b'name: Acme Bakery',
                     b'aliases:', b'- Acme', b'- Bakery', b'---', b'', b'## Notes', b'text',
                     b'  ', b'', b'## Other', b'end', b''])
    assert footprint(data, {'op': 'set_property', 'key': 'aliases'}) == 'aliases:\n- Acme\n- Bakery\n'
    assert footprint(data, {'op': 'replace_section', 'section': 'notes'}) == '## Notes\ntext'
    assert footprint(data, {'op': 'replace_section', 'section': 'missing'}) is None
    assert footprint(data, {'op': 'set_property', 'key': 'missing'}) is None


@pytest.mark.parametrize(('key', 'kind'), [('engagements', 'client'), ('id', 'client'),
                                            ('name', 'client'), ('client', 'project')])
def test_noncanonical_identity_is_global(key, kind):
    pairs = {'engagements': kind, 'id': 'acme-bakery', 'name': 'Acme Bakery'}
    if kind == 'project':
        pairs['client'] = 'acme-bakery'
    data = ('---\n' + ''.join(f'{chr(34) + k + chr(34) if k == key else k}: {v}\n'
                              for k, v in pairs.items()) + '---\n').encode()
    record = parse(data)
    assert record.is_record
    assert 'unsupported_yaml' in record.global_defects
    assert record.properties[key] is None


@pytest.mark.parametrize('key', ['engagements', 'id', 'name', 'client'])
def test_duplicate_identity_escalates(key):
    kind = 'project' if key == 'client' else 'client'
    prefix = f'---\nengagements: {kind}\nid: acme-bakery\nname: Acme Bakery\n'
    if kind == 'project':
        prefix += 'client: acme-bakery\n'
    value = {'engagements': kind, 'id': 'acme-bakery', 'name': 'Acme Bakery', 'client': 'acme-bakery'}[key]
    result = parse((prefix + f'{key}: {value}\n---\n').encode())
    assert 'duplicate_key' in result.global_defects
    assert result.properties[key] is None


@pytest.mark.parametrize(('frontmatter', 'defect'), [
    ('engagements: client\nname: Acme Bakery\n', 'missing_field'),
    ('engagements: project\nid: acme-bakery\nname: Acme Bakery\n', 'missing_field'),
    ('engagements: client\nid: Bad_ID\nname: Acme Bakery\n', 'invalid_id'),
])
def test_global_identity_defects(frontmatter, defect):
    result = parse(('---\n' + frontmatter + '---\n').encode())
    assert defect in result.global_defects


def test_full_reconstruction_from_sections_properties_and_gaps():
    data = (b'\xef\xbb\xbf---\r\nengagements: client\r\nid: acme-bakery\r\n'
            b'name: Acme Bakery\r\n# keep\r\naliases:\r\n- Acme\r\n---\r\n'
            b'\r\n# Title\r\nOutside\r\n## Summary\r\nBody\r\n\r\n## Notes\r\nTail')
    record = parse(data)
    spans = [span for group in record.property_spans.values() for span in group]
    spans += [(s.start, s.end) for s in record.sections]
    rebuilt = bytearray()
    cursor = 0
    for start, end in sorted(spans):
        assert start >= cursor
        rebuilt += data[cursor:start] + data[start:end]
        cursor = end
    rebuilt += data[cursor:]
    assert bytes(rebuilt) == data


def test_json_value_conversion_and_targeted_nested_value():
    data = (b'---\nengagements: client\nid: acme-bakery\nname: Acme Bakery\n'
            b'date: 2026-11-07\nflag: true\ncount: 12\nempty: null\n'
            b'aliases: [Acme, Bakery]\ncustom: {location: Acme}\n---\n')
    result = parse(data)
    assert result.properties['date'] == '2026-11-07'
    assert result.properties['flag'] is True
    assert result.properties['count'] == 12
    assert result.properties['empty'] is None
    assert result.properties['aliases'] == ['Acme', 'Bakery']
    assert result.properties['custom'] == {'unsupported_yaml': 'dict'}
    assert result.property_defects == {'custom': 'unsupported_yaml'}


def test_shared_line_keys_are_both_unsupported_and_keep_byte_spans():
    data = ('---\nengagements: client\nid: acme-bakery\nname: Acme Bakery\n'
            'aliases: [Acme]\u2028status: paused\n---\n').encode()
    result = parse(data)
    assert result.property_defects == {'aliases': 'unsupported_yaml', 'status': 'unsupported_yaml'}
    assert result.property_spans['aliases'][0] == result.property_spans['status'][0]
