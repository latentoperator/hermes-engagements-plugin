import pytest
from engagements.records import footprint, parse


@pytest.mark.parametrize('eol', [b'\n', b'\r\n'])
def test_duplicate_property_footprint_contains_all_spans(eol):
    data = eol.join([b'---', b'engagements: client', b'id: acme-bakery', b'name: Acme',
                     b'status: active', b'# keep', b'status: paused', b'---', b''])
    assert footprint(data, {'op': 'set_property', 'key': 'status'}) == (
        'status: active\nstatus: paused\n')
    assert footprint(data, {'op': 'set_property', 'key': 'missing'}) is None


def test_invalid_backtick_info_does_not_open_fence():
    data = (b'---\nengagements: client\nid: acme-bakery\nname: Acme\n---\n'
            b'``` bad`info\n## Visible ###\nbody\n')
    assert [s.title for s in parse(data).sections] == ['Visible']


def test_fence_lengths_tildes_indentation_and_level_one_geometry():
    data = (b'---\nengagements: client\nid: acme-bakery\nname: Acme\n---\n'
            b'   ## First ###\n~~~lang\n## hidden\n~~~~\n### nested\n'
            b'    ## indented\n# Outside\nno section\n## Second\n'
            b'````\n## hidden too\n')
    result = parse(data)
    assert [s.title for s in result.sections] == ['First', 'Second']
    assert data[result.sections[0].end:].startswith(b'# Outside')
    assert result.sections[1].end == len(data)
