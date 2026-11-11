"""Byte-splice regressions at frontmatter and document boundaries."""
from datetime import UTC, datetime

import pytest
from engagements.errors import EngagementsError
from engagements.records import apply_ops, parse


def run(data, op):
    return apply_ops(data, 'client', [op], clock=lambda: datetime(2026, 11, 7, tzinfo=UTC),
                     profile='studio-dev', source='notes', client_exists=lambda _: True)


@pytest.mark.parametrize('eol', [b'\n', b'\r\n'])
def test_frontmatter_only_no_final_eol(eol):
    data = b'---\nengagements: client\nid: acme-bakery\nname: Acme Bakery\n---'.replace(b'\n', eol)
    result = run(data, {'op': 'set_property', 'key': 'status', 'value': 'paused'})
    assert parse(result).properties['status'] == 'paused'
    assert not result.endswith(eol)


def test_frontmatter_limit_refuses_growth():
    base = b'---\nengagements: client\nid: acme-bakery\nname: Acme Bakery\n#' + b'x' * 65455 + b'\n---\n'
    assert parse(base).is_record
    with pytest.raises(EngagementsError) as exc:
        run(base, {'op': 'set_property', 'key': 'status', 'value': 'paused'})
    assert exc.value.details['reason'] == 'too_large'


def test_large_result_refused():
    data = b'---\nengagements: client\nid: acme-bakery\nname: Acme Bakery\n---\n\n## Notes\n' + b'x' * 1048510
    with pytest.raises(EngagementsError) as exc:
        run(data, {'op': 'append_to_section', 'section': 'Notes', 'text': 'new'})
    assert exc.value.details['reason'] == 'too_large'


def test_empty_lines_stripped_from_inserted_body():
    data = b'---\nengagements: client\nid: acme-bakery\nname: Acme Bakery\n---\n\n## Notes\nold\n\n## Change log\n'
    result = run(data, {'op': 'replace_section', 'section': 'Notes', 'body': 'new\n  \n\n'})
    assert b'## Notes\nnew\n\n## Change log' in result
