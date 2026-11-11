"""Byte-preserving updates and library defect handling."""
from datetime import UTC, datetime

import pytest
from engagements.errors import EngagementsError
from engagements.records import apply_ops, parse

CLOCK = lambda: datetime(2026, 11, 7, tzinfo=UTC)  # noqa: E731
PROJECT = b'---\nengagements: project\nid: acme-project\nclient: acme-bakery\nname: Project\n---\n\n## Notes\nold\n\n## Change log\n'
CLIENT_OP = {'op': 'set_property', 'key': 'client', 'value': 'acme-bakery'}


def apply(data, ops, **kwargs):
    return apply_ops(data, 'project', ops, clock=CLOCK, profile='studio-dev', source='notes',
                     client_exists=lambda x: x == 'acme-bakery', **kwargs)


def test_unknown_client_repair():
    result = apply(PROJECT, [CLIENT_OP], library_defects=['unknown_client'])
    assert parse(result).properties['client'] == 'acme-bakery'
    assert b'2026-11-07 studio-dev: set client (source: notes)' in result


@pytest.mark.parametrize('defects,ops', [
    (['duplicate_id'], [CLIENT_OP]),
    (['unknown_client'], [CLIENT_OP, {'op': 'append_to_section', 'section': 'Notes', 'text': 'x'}]),
    (['unknown_client'], [{'op': 'set_property', 'key': 'client', 'value': 'unknown-client'}]),
])
def test_global_defects_refuse(defects, ops):
    with pytest.raises(EngagementsError) as exc:
        apply(PROJECT, ops, library_defects=defects)
    assert exc.value.code == 'malformed'


def test_identity_escalation_is_not_repairable():
    data = PROJECT.replace(b'name: Project\n', b'name: Project\nname: Other\n')
    with pytest.raises(EngagementsError) as exc:
        apply(data, [CLIENT_OP], library_defects=['unknown_client'])
    assert exc.value.code == 'malformed'


def test_invalid_library_defect_is_programming_error():
    with pytest.raises(ValueError):
        apply(PROJECT, [CLIENT_OP], library_defects=['mixed_eol'])


def test_apply_operations_in_order():
    result = apply(PROJECT, [
        {'op': 'append_to_section', 'section': 'Notes', 'text': 'new'},
        {'op': 'replace_line', 'section': 'Notes', 'match': 'new', 'text': 'done'},
        {'op': 'remove_line', 'section': 'Notes', 'match': 'old'},
    ])
    assert b'## Notes\ndone\n\n## Change log\n' in result
    assert b'remove line from Notes' in result
