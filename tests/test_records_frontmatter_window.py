"""Tag constructor failures, the 65,536-byte frontmatter window, and section titles that must
round-trip (spec C6)."""
from datetime import UTC, datetime

import pytest
from engagements.errors import EngagementsError
from engagements.records import apply_ops, parse, render_client, render_project

BASE = b'---\nengagements: client\nid: acme-bakery\nname: Acme Bakery\nstatus: active\n---\n## Notes\nold\n## Change log\n'
PROJECT = BASE.replace(b'engagements: client\n', b'engagements: project\nclient: acme-bakery\n')
CLOCK = lambda: datetime(2026, 11, 7, tzinfo=UTC)  # noqa: E731

# Explicit tags whose SafeLoader constructors fail with exception types other
# than ValueError/TypeError/KeyError (AttributeError, IndexError), plus nested
# and neighbouring variants of the same family.
FAILING_TAGS = [
    b'!!timestamp nope',
    b'!!timestamp ""',
    b'!!int ""',
    b'!!float ""',
    b'!!int "0x"',
    b'[!!int ""]',
    b'[ok, !!timestamp nope]',
]


def apply(data, op, kind='client'):
    return apply_ops(data, kind, [op], clock=CLOCK, profile='test', source='notes',
                     client_exists=lambda _: True)


# Constructor failures are targeted unsupported_yaml defects.

@pytest.mark.parametrize('value', FAILING_TAGS)
def test_failing_tag_constructor_is_targeted_defect(value):
    data = BASE.replace(b'status: active\n', b'aliases: ' + value + b'\nstatus: active\n')
    record = parse(data)
    assert 'unsupported_yaml' in record.properties['aliases']
    assert record.property_defects['aliases'] == 'unsupported_yaml'
    assert not record.global_defects
    # Properties after the failing one still construct normally.
    assert record.properties['status'] == 'active'
    assert record.id == 'acme-bakery' and record.name == 'Acme Bakery'


@pytest.mark.parametrize('value', FAILING_TAGS)
def test_failing_tag_allows_unrelated_writes_and_refuses_the_key(value):
    line = b'aliases: ' + value + b'\n'
    data = BASE.replace(b'status: active\n', line + b'status: active\n')
    changed = apply(data, {'op': 'replace_section', 'section': 'Notes', 'body': 'new'})
    assert line in changed and b'## Notes\nnew\n' in changed
    changed = apply(data, {'op': 'set_property', 'key': 'status', 'value': 'paused'})
    assert line in changed and b'status: paused\n' in changed
    with pytest.raises(EngagementsError) as exc:
        apply(data, {'op': 'set_property', 'key': 'aliases', 'values': ['Acme']})
    assert exc.value.code == 'malformed'


@pytest.mark.parametrize('value', FAILING_TAGS)
@pytest.mark.parametrize('key,kind', [('name', 'client'), ('id', 'client'), ('client', 'project')])
def test_failing_tag_on_identity_is_global_and_refuses_writes(value, key, kind):
    data = BASE if kind == 'client' else PROJECT
    old = {'name': b'Acme Bakery', 'id': b'acme-bakery', 'client': b'acme-bakery'}[key]
    data = data.replace(key.encode() + b': ' + old + b'\n', key.encode() + b': ' + value + b'\n')
    record = parse(data)
    assert record.is_record
    assert 'unsupported_yaml' in record.global_defects
    assert 'yaml_parse' not in record.global_defects
    assert record.properties[key] is None
    if key in ('id', 'name'):
        assert getattr(record, key) is None
    with pytest.raises(EngagementsError) as exc:
        apply(data, {'op': 'replace_section', 'section': 'Notes', 'body': 'new'}, kind)
    assert exc.value.code == 'malformed'


def test_failing_tag_on_marker_still_recognizes_valid_marker():
    data = BASE.replace(b'engagements: client\n', b'engagements: client\nengagements: !!int ""\n')
    record = parse(data)
    assert record.is_record and 'duplicate_key' in record.global_defects
    assert record.properties['engagements'] is None


def test_supported_explicit_str_tag_control():
    data = BASE.replace(b'status: active\n', b'aliases: !!str hello\nstatus: active\n')
    record = parse(data)
    assert record.property_defects['aliases'] == 'unsupported_yaml'
    assert not record.global_defects
    apply(data, {'op': 'replace_section', 'section': 'Notes', 'body': 'new'})


# The 1 MiB limit measures the resulting file only.

PREFIX = b'---\nengagements: client\nid: acme-bakery\nname: Acme Bakery\nupdated: 2026-11-07\n---\n## Notes\n'


def test_shrinking_edit_to_full_record_without_change_log_succeeds():
    data = PREFIX + b'x' * (1048576 - len(PREFIX) - 1) + b'\n'
    assert len(data) == 1048576
    assert not parse(data).global_defects
    result = apply(data, {'op': 'replace_section', 'section': 'Notes', 'body': 'short note'})
    assert len(result) < 1024
    assert b'## Notes\nshort note\n' in result
    assert b'## Change log\n- 2026-11-07 test: replace section Notes (source: notes)' in result


def test_shrinking_edit_to_full_record_with_change_log_succeeds():
    suffix = b'\n## Change log\n'
    data = PREFIX + b'x' * (1048576 - len(PREFIX) - len(suffix)) + suffix
    assert len(data) == 1048576
    result = apply(data, {'op': 'replace_section', 'section': 'Notes', 'body': 'short note'})
    assert len(result) < 1024


def test_growing_edit_to_full_record_is_still_too_large():
    data = PREFIX + b'x' * (1048576 - len(PREFIX) - 1) + b'\n'
    with pytest.raises(EngagementsError) as exc:
        apply(data, {'op': 'append_to_section', 'section': 'Notes', 'text': 'more'})
    assert exc.value.code == 'invalid_op' and exc.value.details['reason'] == 'too_large'


# Rendered section titles must round-trip to the requested keys.

@pytest.mark.parametrize('title', ['Change log ###', 'Change log #', 'Notes ###', 'Purpose ##', 'Custom ###'])
def test_render_project_refuses_titles_that_do_not_round_trip(title):
    with pytest.raises(EngagementsError) as exc:
        render_project('acme-site', 'Site', 'acme-bakery', clock=CLOCK, profile='test', source='notes',
                       sections=[{'section': title, 'body': 'review note'}])
    assert exc.value.code == 'invalid_op'
    assert exc.value.details == {'field': 'sections', 'reason': 'structure_changed'}


@pytest.mark.parametrize('title', ['Change log ###', 'History ##', 'Custom #'])
def test_render_client_refuses_titles_that_do_not_round_trip(title):
    with pytest.raises(EngagementsError) as exc:
        render_client('acme-bakery', 'Acme Bakery', clock=CLOCK, profile='test', source='notes',
                      sections=[{'section': title, 'body': 'note'}])
    assert exc.value.details['reason'] == 'structure_changed'


@pytest.mark.parametrize('title', ['Custom', 'Custom # notes', 'C# tips', 'Review ### notes'])
def test_render_keeps_ordinary_titles_and_later_updates_work(title):
    data = render_project('acme-site', 'Site', 'acme-bakery', clock=CLOCK, profile='test', source='notes',
                          sections=[{'section': title, 'body': 'review note'}])
    record = parse(data)
    assert [s.key for s in record.sections][-2:] == [' '.join(title.casefold().split()), 'change log']
    assert not record.section_defects and not record.global_defects
    changed = apply(data, {'op': 'set_property', 'key': 'status', 'value': 'historical'}, 'project')
    assert b'status: historical\n' in changed


@pytest.mark.parametrize('title', ['Custom ###', 'Change log ###', 'Notes ##'])
def test_apply_ops_section_creation_refuses_titles_that_do_not_round_trip(title):
    data = BASE.replace(b'## Notes\nold\n', b'')
    with pytest.raises(EngagementsError) as exc:
        apply(data, {'op': 'replace_section', 'section': title, 'body': 'x'})
    assert exc.value.code == 'invalid_op'
    assert exc.value.details == {'op_index': 0, 'field': 'section', 'reason': 'structure_changed'}


# TOOLS.md "Ordered updates". The 65,536-byte frontmatter limit is
# checked after each op, so a request that crosses it mid-way is refused even
# if a later op would shrink it; the shrink-first order succeeds.

def _near_window_record(eol, bom, log):
    lines = [b'---', b'engagements: client', b'id: acme-bakery', b'name: Acme Bakery',
             b'aliases: [' + b'x' * 200 + b']', b'updated: 2026-11-07']
    head = (b'\xef\xbb\xbf' if bom else b'') + eol.join(lines) + eol
    tail = eol + b'---' + eol
    front = head + b'#' + b'p' * (65520 - len(head) - 1 - len(tail)) + tail
    body = eol.join([b'## Notes', b'old', b'## Change log', b''] if log else [b'## Notes', b'old', b''])
    data = front + body
    record = parse(data)
    assert record.frontmatter_span[1] == 65520 and not record.global_defects
    return data


GROW = {'op': 'set_property', 'key': 'name', 'value': 'N' * 100}
SHRINK = {'op': 'set_property', 'key': 'aliases', 'values': []}
VARIANTS = [(eol, bom, log) for eol in (b'\n', b'\r\n') for bom in (False, True) for log in (False, True)]


def apply_many(data, ops):
    return apply_ops(data, 'client', ops, clock=CLOCK, profile='test', source='notes',
                     client_exists=lambda _: True)


@pytest.mark.parametrize('eol,bom,log', VARIANTS)
def test_shrink_first_order_near_the_window_succeeds(eol, bom, log):
    data = _near_window_record(eol, bom, log)
    result = apply_many(data, [SHRINK, GROW, {'op': 'replace_section', 'section': 'Notes', 'body': 'new'}])
    final = parse(result)
    assert final.frontmatter_span[1] <= 65536 and not final.global_defects
    assert final.properties['name'] == 'N' * 100 and final.properties['aliases'] == []
    assert b'## Notes' + eol + b'new' + eol in result


@pytest.mark.parametrize('eol,bom,log', VARIANTS)
def test_op_crossing_the_window_is_too_large_even_if_a_later_op_shrinks(eol, bom, log):
    data = _near_window_record(eol, bom, log)
    with pytest.raises(EngagementsError) as exc:
        apply_many(data, [GROW, SHRINK])
    assert exc.value.details == {'field': 'ops', 'reason': 'too_large'}


@pytest.mark.parametrize('eol,bom,log', VARIANTS)
def test_frontmatter_still_past_the_window_is_too_large(eol, bom, log):
    data = _near_window_record(eol, bom, log)
    with pytest.raises(EngagementsError) as exc:
        apply_many(data, [GROW])
    assert exc.value.details == {'field': 'ops', 'reason': 'too_large'}
    with pytest.raises(EngagementsError) as exc:
        apply_many(data, [SHRINK, GROW, {'op': 'set_property', 'key': 'aliases', 'values': ['y' * 300]}])
    assert exc.value.details == {'field': 'ops', 'reason': 'too_large'}


def test_worst_case_oversized_request_fails_fast():
    """Per-op window checks bound parsing: 50 maximal list ops stay cheap."""
    import time
    base = PROJECT
    big = ['\u00e9' * 1000 + str(i) for i in range(50)]
    ops = [{'op': 'set_property', 'key': k, 'values': big} for k in ('aliases', 'locations') * 25]
    started = time.monotonic()
    with pytest.raises(EngagementsError) as exc:
        apply_ops(base, 'project', ops, clock=CLOCK, profile='test', source='notes',
                  client_exists=lambda _: True)
    assert exc.value.details == {'field': 'ops', 'reason': 'too_large'}
    assert time.monotonic() - started < 5
