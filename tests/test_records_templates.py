from datetime import UTC, datetime

from engagements.records import parse, render_client, render_project


def clock():
    return datetime(2026, 11, 7, tzinfo=UTC)


def test_minimal_client_template():
    data = render_client('acme-bakery', 'Acme Bakery', clock=clock, profile='studio-dev')
    assert data == (b'---\nengagements: client\nid: acme-bakery\nname: Acme Bakery\n'
                    b'aliases: []\nstatus: active\nupdated: 2026-11-07\n---\n\n'
                    b'# Acme Bakery\n\n## Summary\n\n## Current work\n\n## Contacts\n\n'
                    b'## Communication preferences\n\n## Locations\n\n'
                    b'## Systems of record\n\n## History\n\n## Unconfirmed\n\n'
                    b'## Change log\n- 2026-11-07 studio-dev: created client (source: conversation)\n')
    assert len(parse(data).sections) == 9


def test_project_template_custom_sections_and_properties():
    data = render_project('website-2026', 'Website 2026', 'acme-bakery', clock=clock,
                          profile='studio-dev', source='kickoff',
                          properties={'aliases': ['yes'], 'status': 'historical',
                                      'locations': ['https://git.example/acme']},
                          sections=[{'section': 'notes', 'body': 'Hello'},
                                    {'section': 'Details', 'body': 'World'}])
    record = parse(data)
    assert record.properties['aliases'] == ['yes']
    assert record.properties['locations'] == ['https://git.example/acme']
    assert [s.key for s in record.sections] == ['purpose', 'notes', 'details', 'change log']
    assert b'## Notes\nHello\n\n## Details\nWorld\n\n' in data
    assert b'- 2026-11-07 studio-dev: created project (source: kickoff)\n' in data
