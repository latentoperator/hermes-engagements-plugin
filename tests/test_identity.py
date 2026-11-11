import re

import pytest
from engagements.records import derive_filename, derive_id


@pytest.mark.parametrize(('name', 'expected'), [
    ('Ｃａｆé Ñandú', 'cafe-nandu'), ('Straße', 'strasse'),
    (' -- A...B!! ', 'a-b'), ('X', 'client-x'), ('', 'client'),
])
def test_ids(name, expected):
    result = derive_id(name, 'client', set())
    assert result == expected
    assert re.fullmatch(r'[a-z0-9][a-z0-9-]{1,62}', result)


def test_id_collision_truncation_and_retrim():
    name = 'a' * 60 + '-bbbbbbbb'
    first = derive_id(name, 'client', set())
    second = derive_id(name, 'client', {first})
    third = derive_id(name, 'client', {first, second})
    assert first == 'a' * 60 + '-bb'
    assert second == 'a' * 60 + '-2'
    assert third == 'a' * 60 + '-3'
    assert all(re.fullmatch(r'[a-z0-9][a-z0-9-]{1,62}', x) for x in (first, second, third))


@pytest.mark.parametrize(('name', 'id_', 'expected'), [
    ('e\u0301', 'xx', 'é'), ('  a\t b  ', 'xx', 'a b'),
    ('..', 'xx', 'xx'), ('.hidden', 'xx', 'hidden'),
    ('...hello... ', 'xx', 'hello'), ('a..b', 'xx', 'a..b'),
    ('a\x00b', 'xx', 'a b'), ('CON', 'xx', 'CON_'),
    ('aux.txt', 'xx', 'aux_.txt'), ('com1', 'xx', 'com1_'),
    ('LPT¹', 'xx', 'LPT¹_'), ('', 'con', 'con_'),
])
def test_filenames(name, id_, expected):
    assert derive_filename(name, id_, set()) == expected


@pytest.mark.parametrize('char', '<>:"/\\|?*')
def test_forbidden_characters_become_spaces(char):
    assert derive_filename('a' + char + 'b', 'xx', set()) == 'a b'


def test_filename_caps_and_collisions():
    assert derive_filename('a' * 81, 'xx', set()) == 'a' * 80
    assert derive_filename('😀' * 60, 'xx', set()) == '😀' * 50
    assert derive_filename('a' * 79 + '.' + 'z', 'xx', set()) == 'a' * 79
    assert derive_filename('acme bakery', 'xx', {'Acme Bakery', 'ACME BAKERY (2)'}) == 'acme bakery (3)'
    assert derive_filename('é', 'xx', {'e\u0301'}) == 'é (2)'
    assert derive_filename('a' * 80, 'xx', {'a' * 80}) == 'a' * 80 + ' (2)'
