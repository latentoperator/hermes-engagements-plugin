from pathlib import Path

import pytest
from engagements.records import parse

ROOT = Path(__file__).parent / 'fixtures' / 'records'

EXTRA_PROPERTIES = {
    'nested-mapping': {'details': {'unsupported_yaml': 'dict'}},
    'multiline-plain': {'details': 'first second'},
    'block-scalar': {'details': 'one\ntwo\n'},
    'anchor-alias': {'aliases': ['Acme'], 'other': ['Acme']},
    'tag': {'aliases': ['Acme']},
    'compact-sequence': {'aliases': ['Acme', 'Bakery']},
    'block-list': {'aliases': ['Acme', 'Bakery']},
    'block-list-comment': {'aliases': ['Acme', 'Bakery']},
    'flow-list-multiline': {'aliases': ['Acme', 'Bakery']},
    'trailing-comment': {'status': 'active'},
    'unicode-line-separator': {'aliases': ['Acme'], 'status': 'paused'},
    'comments-between': {'aliases': ['Acme'], 'status': 'active'},
    'unknown-keys': {'unfamiliar': 'value'},
}


@pytest.mark.parametrize(('filename', 'kind', 'global_defect', 'targeted', 'section_keys'), [
    ('bom', 'client', None, {}, ['summary', 'change log']),
    ('crlf', 'client', None, {}, ['summary', 'change log']),
    ('no-final-newline', 'client', None, {}, ['summary', 'change log']),
    ('mixed-endings', 'client', 'mixed_eol', {}, ['summary', 'change log']),
    ('duplicate-key', 'client', None, {'status': 'duplicate_key'}, ['summary', 'change log']),
    ('duplicate-identity', 'client', 'duplicate_key', {}, ['summary', 'change log']),
    ('repeated-marker-agrees', 'client', 'duplicate_key', {}, ['summary', 'change log']),
    ('repeated-marker-conflicts', None, 'duplicate_key', {}, ['summary', 'change log']),
    ('nested-mapping', 'client', None, {'details': 'unsupported_yaml'}, ['summary', 'change log']),
    ('multiline-plain', 'client', None, {'details': 'unsupported_yaml'}, ['summary', 'change log']),
    ('block-scalar', 'client', None, {'details': 'unsupported_yaml'}, ['summary', 'change log']),
    ('anchor-alias', 'client', None, {'aliases': 'unsupported_yaml', 'other': 'unsupported_yaml'}, ['summary', 'change log']),
    ('tag', 'client', None, {'aliases': 'unsupported_yaml'}, ['summary', 'change log']),
    ('compact-sequence', 'client', None, {}, ['summary', 'change log']),
    ('block-list', 'client', None, {}, ['summary', 'change log']),
    ('block-list-comment', 'client', None, {'aliases': 'unsupported_yaml'}, ['summary', 'change log']),
    ('flow-list-multiline', 'client', None, {}, ['summary', 'change log']),
    ('trailing-comment', 'client', None, {}, ['summary', 'change log']),
    ('unicode-line-separator', 'client', None, {'aliases': 'unsupported_yaml', 'status': 'unsupported_yaml'}, ['summary', 'change log']),
    ('comments-between', 'client', None, {}, ['summary', 'change log']),
    ('unknown-keys', 'client', None, {}, ['summary', 'change log']),
    ('unknown-sections', 'client', None, {}, ['custom', 'change log']),
    ('backtick-fence', 'client', None, {}, ['summary', 'change log']),
    ('tilde-fence', 'client', None, {}, ['summary', 'change log']),
    ('longer-closing-fence', 'client', None, {}, ['summary', 'change log']),
    ('unclosed-fence', 'client', None, {}, ['summary']),
    ('level-three', 'client', None, {}, ['summary', 'change log']),
    ('level-one', 'client', None, {}, ['summary', 'change log']),
    ('duplicate-sections', 'client', None, {}, ['summary', 'summary', 'change log']),
    ('no-change-log', 'client', None, {}, ['summary']),
    ('malformed-yaml', 'client', 'yaml_parse', {}, ['summary', 'change log']),
])
def test_fixture_structure(filename, kind, global_defect, targeted, section_keys):
    data = (ROOT / (filename + '.md')).read_bytes()
    original = bytes(data)
    record = parse(data)
    assert data == original
    assert record.is_record and record.kind == kind
    assert record.bom == data.startswith(b'\xef\xbb\xbf')
    assert record.final_newline == data.endswith(b'\n')
    assert record.global_defects == ({global_defect} if global_defect else set())
    assert record.property_defects == targeted
    assert [s.key for s in record.sections] == section_keys
    assert record.section_defects == ({'summary': 'duplicate_section'} if filename == 'duplicate-sections' else {})
    assert data[slice(*record.frontmatter_span)].startswith(b'\xef\xbb\xbf---' if record.bom else b'---')
    if filename == 'malformed-yaml':
        assert record.properties is None
        assert record.id == 'acme-bakery' and record.name == 'Acme Bakery'
    else:
        assert record.properties['name'] == 'Acme Bakery'
        assert record.properties['id'] == (None if filename == 'duplicate-identity' else 'acme-bakery')
        assert record.properties['engagements'] == (
            None if filename.startswith('repeated-marker-') else 'client'
        )
        assert {k: v for k, v in record.properties.items() if k not in {'name', 'id', 'engagements'}} == (
            EXTRA_PROPERTIES.get(filename, {})
        )
    for spans in record.property_spans.values():
        for start, end in spans:
            assert 0 <= start < end <= record.frontmatter_span[1]
    for section in record.sections:
        assert data[section.start:section.body_start].lstrip().startswith(b'## ')
        assert section.body_start <= section.end <= len(data)
    # Include gaps explicitly; merge overlapping unsupported spans before rebuilding.
    spans = [span for group in record.property_spans.values() for span in group]
    spans.extend((section.start, section.end) for section in record.sections)
    rebuilt = bytearray()
    cursor = 0
    for start, end in sorted(spans):
        if end <= cursor:
            continue
        rebuilt.extend(data[cursor:start] if cursor < start else b'')
        rebuilt.extend(data[max(start, cursor):end])
        cursor = end
    rebuilt.extend(data[cursor:])
    assert bytes(rebuilt) == data


def test_unclosed_fixture_is_not_record():
    record = parse((ROOT / 'unclosed-frontmatter.md').read_bytes())
    assert not record.is_record and record.kind is None
