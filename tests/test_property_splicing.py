"""Preserve property style without disturbing neighbouring spans."""
from datetime import UTC, datetime

import pytest
from engagements.errors import EngagementsError
from engagements.records import apply_ops


def update(data, op):
    return apply_ops(data, 'client', [op], clock=lambda: datetime(2026, 11, 7, tzinfo=UTC),
                     profile='studio-dev', source='notes', client_exists=lambda _: True)


@pytest.mark.parametrize(('old', 'new'), [
    (b'aliases: [old] # note', b'aliases: [plain, "yes", "a, b"]'),
    (b'aliases: []', b'aliases: [plain, "yes", "a, b"]'),
])
def test_flow_list_style_exact(old, new):
    data = b'---\nengagements: client\nid: acme-bakery\nname: Acme Bakery\n' + old + b'\n# between\nstatus: active\n---\n\n## Change log\n'
    result = update(data, {'op': 'set_property', 'key': 'aliases', 'values': ['plain', 'yes', 'a, b']})
    assert new + b'\n# between\nstatus: active\n' in result


@pytest.mark.parametrize('eol', ['\n', '\r\n'])
@pytest.mark.parametrize('bom', ['', '\ufeff'])
@pytest.mark.parametrize('indent', ['', '  '])
@pytest.mark.parametrize('between', ['# between', '', '  # indented note', '# one\n\n# two'])
@pytest.mark.parametrize('last_line_comment', ['', ' # note on the last item line'])
def test_block_list_followed_by_comment_or_blank_round_trips(eol, bom, indent, between, last_line_comment):
    # C6: a block sequence's extent ends at its last item, so comment and blank
    # lines after it belong to no property and survive byte-exactly.
    head = '---\nengagements: client\nid: acme-bakery\nname: Acme Bakery\naliases:\n'
    kept = between + '\nstatus: active\n'
    old = bom + head + indent + '- first\n' + indent + '- old' + last_line_comment + '\n' + kept + '---\n\n## Change log\n'
    data = old.replace('\n', eol).encode()
    result = update(data, {'op': 'set_property', 'key': 'aliases', 'values': ['plain', 'yes']})
    expected = (eol.join([indent + '- plain', indent + '- "yes"', '']) + kept.replace('\n', eol)).encode()
    assert expected in result
    assert result.startswith((bom + head).replace('\n', eol).encode())


@pytest.mark.parametrize('between', ['# mid', '', '  # mid'])
def test_block_list_interrupted_between_items_still_refused(between):
    data = ('---\nengagements: client\nid: acme-bakery\nname: Acme Bakery\naliases:\n- a\n'
            + between + '\n- b\n# after\nstatus: active\n---\n\n## Change log\n').encode()
    with pytest.raises(EngagementsError) as exc:
        update(data, {'op': 'set_property', 'key': 'aliases', 'values': ['plain']})
    assert exc.value.code == 'malformed'
    assert exc.value.details['defects'] == [{'kind': 'unsupported_yaml'}]


def test_block_list_trailing_comment_before_closing_delimiter_kept():
    data = b'---\nengagements: client\nid: acme-bakery\nname: Acme Bakery\naliases:\n- old\n# tail\n---\n\n## Change log\n'
    result = update(data, {'op': 'set_property', 'key': 'aliases', 'values': ['plain']})
    assert b'aliases:\n- plain\n# tail\nupdated: ' in result


@pytest.mark.parametrize('item', ['- [x, y]', '- k: v', '- &a old', '- !!str old'])
def test_block_list_with_non_plain_last_item_still_refused(item):
    data = ('---\nengagements: client\nid: acme-bakery\nname: Acme Bakery\naliases:\n- a\n'
            + item + '\n# after\nstatus: active\n---\n\n## Change log\n').encode()
    with pytest.raises(EngagementsError) as exc:
        update(data, {'op': 'set_property', 'key': 'aliases', 'values': ['plain']})
    assert exc.value.code == 'malformed'


@pytest.mark.parametrize('indent', ['', '  '])
def test_block_style_prefix_exact(indent):
    data = ('---\nengagements: client\nid: acme-bakery\nname: Acme Bakery\naliases:\n'
            + indent + '- old\nstatus: active\n---\n\n## Change log\n').encode()
    result = update(data, {'op': 'set_property', 'key': 'aliases', 'values': ['plain', 'yes']})
    assert ('aliases:\n' + indent + '- plain\n' + indent + '- "yes"\nstatus: active').encode() in result


def test_alias_normalized_deduplication():
    data = b'---\nengagements: client\nid: acme-bakery\nname: Acme Bakery\n---\n\n## Change log\n'
    result = update(data, {'op': 'set_property', 'key': 'aliases', 'values': ['ACME!', 'acme', 'Bakery']})
    assert b'aliases: [ACME!, Bakery]' in result
