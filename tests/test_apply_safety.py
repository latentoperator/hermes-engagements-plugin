"""Refuse unsafe edits and preserve unrelated bytes."""
from datetime import UTC, datetime

import pytest
from engagements.errors import EngagementsError
from engagements.records import apply_ops


def apply(data, op):
    return apply_ops(data, 'client', [op], clock=lambda: datetime(2026, 11, 7, tzinfo=UTC),
                     profile='studio-dev', source='notes', client_exists=lambda _: True)


DATA = b'---\nengagements: client\nid: acme-bakery\nname: Acme Bakery\nstatus: active\n---\n\n## Notes\nold\n\n## Change log\n'


@pytest.mark.parametrize(('op', 'reason'), [
    ({'op': 'replace_line', 'section': 'Missing', 'match': 'x', 'text': 'y'}, 'section_missing'),
    ({'op': 'remove_line', 'section': 'Notes', 'match': 'x'}, 'match_count'),
    ({'op': 'append_to_section', 'section': 'Notes', 'text': '  '}, 'empty_text'),
    ({'op': 'append_to_section', 'section': 'Notes', 'text': '## heading'}, 'heading_in_text'),
    ({'op': 'append_to_section', 'section': 'Notes', 'text': '```'}, 'unbalanced_fence'),
])
def test_reject_bad_ops(op, reason):
    with pytest.raises(EngagementsError) as exc:
        apply(DATA, op)
    assert exc.value.code == 'invalid_op'
    assert exc.value.details['reason'] == reason
    if reason == 'match_count':
        assert exc.value.details['count'] == 0


@pytest.mark.parametrize('data', [DATA.replace(b'old', b'\xff'), DATA.replace(b'old\n', b'old\r\n')])
def test_invalid_encoding_and_mixed_eol(data):
    with pytest.raises(EngagementsError) as exc:
        apply(data, {'op': 'set_property', 'key': 'status', 'value': 'paused'})
    assert exc.value.code == 'malformed'


def test_unclosed_fence_cannot_consume_log():
    data = DATA.replace(b'## Notes\nold\n\n## Change log\n', b'## Notes\n```\nold')
    with pytest.raises(EngagementsError) as exc:
        apply(data, {'op': 'set_property', 'key': 'status', 'value': 'paused'})
    assert exc.value.code == 'invalid_op'
    assert exc.value.details['reason'] == 'structure_changed'


def test_untargeted_duplicate_key_preserved():
    data = DATA.replace(b'status: active\n', b'status: active\nstatus: paused\n')
    result = apply(data, {'op': 'append_to_section', 'section': 'Notes', 'text': 'new'})
    assert b'status: active\nstatus: paused\n' in result
    with pytest.raises(EngagementsError) as exc:
        apply(data, {'op': 'set_property', 'key': 'status', 'value': 'former'})
    assert exc.value.code == 'malformed'


def test_append_to_empty_heading_at_eof():
    data = DATA[:DATA.index(b'## Notes')] + b'## Notes'
    result = apply(data, {'op': 'append_to_section', 'section': 'Notes', 'text': 'new'})
    assert b'## Notes\nnew\n\n## Change log\n' in result
    assert not result.endswith(b'\n')
