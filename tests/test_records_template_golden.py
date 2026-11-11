from datetime import UTC, datetime

import pytest
from engagements.records import parse, render_client, render_project


def clock():
    return datetime(2026, 11, 7, tzinfo=UTC)


CLIENT_SECTIONS = ['summary', 'current work', 'contacts', 'communication preferences',
                   'locations', 'systems of record', 'history', 'unconfirmed', 'change log']


@pytest.mark.parametrize(('kind', 'full', 'expected', 'keys'), [
    ('client', False, b'---\nengagements: client\nid: acme-bakery\nname: Acme Bakery\n'
     b'aliases: []\nstatus: active\nupdated: 2026-11-07\n---\n\n# Acme Bakery\n\n'
     b'## Summary\n\n## Current work\n\n## Contacts\n\n## Communication preferences\n\n'
     b'## Locations\n\n## Systems of record\n\n## History\n\n## Unconfirmed\n\n'
     b'## Change log\n- 2026-11-07 default: created client (source: conversation)\n', CLIENT_SECTIONS),
    ('client', True, b'---\nengagements: client\nid: acme-bakery\nname: Acme Bakery\n'
     b'aliases: ["yes", Acme]\nstatus: paused\nupdated: 2026-11-07\n---\n\n# Acme Bakery\n\n'
     b'## Summary\nBakery\n\n## Current work\n\n## Contacts\n\n## Communication preferences\n\n'
     b'## Locations\n\n## Systems of record\n\n## History\n\n## Unconfirmed\n\n'
     b'## Extra\nDetail\n\n## Change log\n- 2026-11-07 studio-dev: created client (source: kickoff)\n',
     CLIENT_SECTIONS[:-1] + ['extra', 'change log']),
    ('project', False, b'---\nengagements: project\nid: website-2026\nclient: acme-bakery\n'
     b'name: Website 2026\naliases: []\nstatus: active\nlocations: []\nupdated: 2026-11-07\n'
     b'---\n\n## Purpose\n\n## Notes\n\n## Change log\n'
     b'- 2026-11-07 default: created project (source: conversation)\n', ['purpose', 'notes', 'change log']),
    ('project', True, b'---\nengagements: project\nid: website-2026\nclient: acme-bakery\n'
     b'name: Website 2026\naliases: ["yes", Acme]\nstatus: historical\n'
     b'locations: [https://git.example/acme]\nupdated: 2026-11-07\n---\n\n'
     b'## Purpose\nBakery\n\n## Notes\n\n## Extra\nDetail\n\n## Change log\n'
     b'- 2026-11-07 studio-dev: created project (source: kickoff)\n',
     ['purpose', 'notes', 'extra', 'change log']),
])
def test_template_golden_and_parseback(kind, full, expected, keys):
    kwargs = {'clock': clock}
    if full:
        kwargs.update(profile='studio-dev', source='kickoff',
                      properties={'aliases': ['yes', 'Acme'], 'status': 'paused' if kind == 'client' else 'historical'},
                      sections=[{'section': 'summary' if kind == 'client' else 'purpose', 'body': 'Bakery'},
                                {'section': 'Extra', 'body': 'Detail'}])
        if kind == 'project':
            kwargs['properties']['locations'] = ['https://git.example/acme']
    data = (render_client('acme-bakery', 'Acme Bakery', **kwargs) if kind == 'client'
            else render_project('website-2026', 'Website 2026', 'acme-bakery', **kwargs))
    assert data == expected
    record = parse(data)
    assert record.is_record and record.kind == kind
    assert not record.global_defects and not record.property_defects and not record.section_defects
    assert [section.key for section in record.sections] == keys


def test_create_section_body_normalizes_line_endings_and_trailing_blank_lines():
    data = render_client('acme-bakery', 'Acme Bakery', clock=clock,
                         sections=[{'section': 'Summary', 'body': 'First\r\nSecond\r\n  \r\n\r\n'}])
    assert b'## Summary\nFirst\nSecond\n\n## Current work' in data
