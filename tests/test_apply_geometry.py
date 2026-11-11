"""Exact section geometry and property rewrite contract."""
from datetime import UTC, datetime

import pytest
import yaml
from engagements.errors import EngagementsError
from engagements.records import apply_ops, parse


def clock():
    return datetime(2026, 11, 7, tzinfo=UTC)


def run(data, op):
    return apply_ops(data, 'client', [op], clock=clock, profile='studio-dev', source='notes',
                     client_exists=lambda _: True)


@pytest.mark.parametrize('eol', [b'\n', b'\r\n'])
@pytest.mark.parametrize('final', [True, False])
@pytest.mark.parametrize(('op', 'expected'), [
    ({'op': 'replace_section', 'section': 'Notes', 'body': 'new'}, b'## Notes\nnew\n\n# Other'),
    ({'op': 'append_to_section', 'section': 'Notes', 'text': 'new'}, b'## Notes\nold\nnew\n\n# Other'),
    ({'op': 'replace_line', 'section': 'Notes', 'match': 'old', 'text': 'new'}, b'## Notes\nnew\n\n# Other'),
    ({'op': 'remove_line', 'section': 'Notes', 'match': 'old'}, b'## Notes\n\n# Other'),
])
def test_section_edits_preserve_heading_and_level1(eol, final, op, expected):
    data = b'---\nengagements: client\nid: acme-bakery\nname: Acme Bakery\n---\n\n## Notes\nold\n\n# Other\nuntouched\n\n## Change log\nprevious\n'
    data = data.replace(b'\n', eol)
    if not final:
        data = data[:-len(eol)]
    result = run(data, op)
    assert expected.replace(b'\n', eol) in result
    assert result.endswith(eol) == final
    assert b'untouched' + eol in result


@pytest.mark.parametrize('eol', [b'\n', b'\r\n'])
@pytest.mark.parametrize('final', [True, False])
def test_create_missing_section_and_log(eol, final):
    data = b'---\nengagements: client\nid: acme-bakery\nname: Acme Bakery\n---\n\n# Acme\ntext\n'.replace(b'\n', eol)
    if not final:
        data = data[:-len(eol)]
    result = run(data, {'op': 'append_to_section', 'section': 'Notes', 'text': 'new'})
    assert b'text' + eol + eol + b'## Notes' + eol + b'new' + eol + eol + b'## Change log' in result
    assert result.endswith(eol) == final


@pytest.mark.parametrize('style', ['aliases: [old]', 'aliases:\n  - old', 'aliases:\n- old', 'aliases: []'])
def test_list_quoting(style):
    data = ('---\nengagements: client\nid: acme-bakery\nname: Acme Bakery\n' + style + '\n---\n\n## Change log\n').encode()
    values = ['plain', 'yes', 'null', '123', 'Acme: Bakery', '#launch', 'a, b', '[x]', ' leading', '🍞', 'a"\\c']
    result = run(data, {'op': 'set_property', 'key': 'aliases', 'values': values})
    assert parse(result).properties['aliases'] == values
    assert yaml.safe_load(result.decode().split('---')[1])['aliases'] == values


@pytest.mark.parametrize('value', ['\ud800', '\ufffe', '\uffff'])
def test_invalid_yaml_characters_refused(value):
    data = b'---\nengagements: client\nid: acme-bakery\nname: Acme Bakery\n---\n'
    with pytest.raises(EngagementsError) as exc:
        run(data, {'op': 'set_property', 'key': 'aliases', 'values': [value]})
    assert exc.value.code == 'invalid_op'
