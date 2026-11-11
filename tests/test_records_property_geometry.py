"""Recognition and property geometry (spec C6)."""

from engagements.records import parse


def test_repeated_marker_identity_and_recognition():
    data = b'---\nengagements: client\nengagements: project\nid: acme-bakery\nname: Acme Bakery\n---\n'
    record = parse(data)
    assert record.is_record
    assert record.kind is None
    assert 'duplicate_key' in record.global_defects
    assert record.properties['engagements'] is None
    assert len(record.property_spans['engagements']) == 2


def test_bounded_frontmatter_recognition():
    assert not parse(b'---\nengagements: client\n' + b'x' * 65536 + b'\n---\n').is_record
    assert not parse(b'---\nname: Acme\n---\nengagements: client\n').is_record
    assert not parse(b'---\nengagements: client\n').is_record


def test_compact_sequence_span_and_alias_span():
    data = (b'---\nengagements: client\nid: acme-bakery\nname: Acme Bakery\n'
            b'aliases:\n- Acme\n- Bakery\nother: &anchor yes\ncopy: *anchor\n---\n')
    record = parse(data)
    start, end = record.property_spans['aliases'][0]
    assert data[start:end] == b'aliases:\n- Acme\n- Bakery\n'
    start, end = record.property_spans['copy'][0]
    assert data[start:end] == b'copy: *anchor\n'
    assert record.property_defects['copy'] == 'unsupported_yaml'


def test_quoted_identity_is_global_and_null():
    data = b'---\nengagements: client\nid: acme-bakery\n"name": Acme Bakery\n---\n'
    record = parse(data)
    assert record.is_record
    assert 'unsupported_yaml' in record.global_defects
    assert record.properties['name'] is None
    assert 'name' in record.property_spans


def test_unknown_yaml_tag_is_targeted_not_parse_failure():
    data = b'---\nengagements: client\nid: acme-bakery\nname: Acme Bakery\nextra: !custom value\n---\n'
    record = parse(data)
    assert record.is_record
    assert 'yaml_parse' not in record.global_defects
    assert record.property_defects['extra'] == 'unsupported_yaml'


def test_non_utf8_body_does_not_hide_record():
    data = b'---\nengagements: client\nid: acme-bakery\nname: Acme Bakery\n---\n\xff'
    record = parse(data)
    assert record.is_record and record.kind == 'client'
    assert record.global_defects == {'encoding'}
    assert record.properties is None and not record.sections


def test_unicode_separator_does_not_shift_later_span():
    data = ('---\nengagements: client\nid: acme-bakery\nname: Acme Bakery\n'
            'details: "first\u2028second"\nstatus: active\n---\n').encode()
    record = parse(data)
    assert data[slice(*record.property_spans['status'][0])] == b'status: active\n'
    assert record.property_defects['details'] == 'unsupported_yaml'
    assert 'status' not in record.property_defects


def test_multiline_sequence_item_is_unsupported():
    data = (b'---\nengagements: client\nid: acme-bakery\nname: Acme Bakery\n'
            b'aliases:\n  - first\n    second\n---\n')
    record = parse(data)
    assert record.property_defects['aliases'] == 'unsupported_yaml'


def test_malformed_yaml_fallback_identity():
    data = (b'---\nengagements: client\nid: "acme-bakery"\nname: Acme Bakery\n'
            b'aliases: [unfinished\n---\n')
    record = parse(data)
    assert record.is_record and record.kind == 'client'
    assert record.properties is None
    assert record.id == 'acme-bakery' and record.name == 'Acme Bakery'


def test_delimiter_must_be_exact_not_extra_carriage_returns():
    data = b'---\nengagements: client\nid: acme-bakery\nname: Acme Bakery\n---\r\r\n'
    assert not parse(data).is_record


def test_malformed_marker_scan_does_not_read_body():
    data = b'---\nid: [broken\n---\nengagements: client\n'
    assert not parse(data).is_record


def test_discovery_boundary_counts_bom_and_entire_closing_line():
    prefix = b'\xef\xbb\xbf---\nengagements: client\nid: acme-bakery\nname: Acme Bakery\n# '
    suffix = b'\n---\n'
    data = prefix + b'x' * (65536 - len(prefix) - len(suffix)) + suffix
    assert len(data) == 65536
    assert parse(data).is_record
    assert not parse(data[:-len(suffix)] + b'x' + suffix).is_record


def test_identity_and_marker_defects_survive_non_identity_duplicates():
    data = (b'---\nengagements: client\nengagements: other\nid: acme-bakery\n'
            b'name: Acme Bakery\nstatus: active\nstatus: paused\n---\n')
    record = parse(data)
    assert record.is_record and record.kind is None
    assert record.properties['engagements'] is None
    assert record.global_defects == {'duplicate_key'}
    assert record.property_defects == {'status': 'duplicate_key'}


def test_scalar_on_line_after_key_is_unsupported():
    data = (b'---\nengagements: client\nid: acme-bakery\nname: Acme Bakery\n'
            b'details:\n  value\n---\n')
    record = parse(data)
    assert record.property_defects['details'] == 'unsupported_yaml'
