import re
from pathlib import Path

import yaml

ROOT = Path(__file__).parent / 'fixtures'
CASES = ['bom', 'crlf', 'no-final-newline', 'mixed-endings', 'non-utf8', 'unclosed-frontmatter', 'malformed-yaml', 'duplicate-key', 'duplicate-identity', 'repeated-marker-agrees', 'repeated-marker-conflicts', 'nested-mapping', 'multiline-plain', 'block-scalar', 'anchor-alias', 'tag', 'compact-sequence', 'block-list', 'block-list-comment', 'flow-list-multiline', 'trailing-comment', 'unicode-line-separator', 'comments-between', 'unknown-keys', 'unknown-sections', 'backtick-fence', 'tilde-fence', 'longer-closing-fence', 'unclosed-fence', 'level-three', 'level-one', 'duplicate-sections', 'no-change-log']


def test_edge_fixture_inventory():
    for name in CASES:
        assert (ROOT / 'records' / f'{name}.md').is_file(), name
    assert (ROOT / 'records' / 'README.md').is_file()


def test_byte_sensitive_edge_fixtures():
    root = ROOT / 'records'
    assert (root / 'bom.md').read_bytes().startswith(b'\xef\xbb\xbf')
    crlf = (root / 'crlf.md').read_bytes()
    assert b'\r\n' in crlf and b'\n' not in crlf.replace(b'\r\n', b'')
    assert not (root / 'no-final-newline.md').read_bytes().endswith(b'\n')
    mixed = (root / 'mixed-endings.md').read_bytes()
    assert b'\r\n' in mixed and b'\n' in mixed.replace(b'\r\n', b'')
    assert b'\xff' in (root / 'non-utf8.md').read_bytes()
    assert 'aliases: [Acme]\u2028status: paused' in (root / 'unicode-line-separator.md').read_text(encoding='utf-8')


def test_library_records_and_fictional_domains():
    root = ROOT / 'library'
    metadata = yaml.safe_load((root / '.engagements/library.yaml').read_text())
    assert metadata == {'format': 1, 'library_id': '11111111-1111-4111-8111-111111111111'}
    paths = {
        'Acme Bakery/Client.md': 'acme-bakery',
        'Acme Bakery/Projects/Website 2026.md': 'acme-website-2026',
        'Riverbend Dental/Client.md': 'riverbend-dental',
        'Riverbend Dental/Projects/Recall Campaign.md': 'riverbend-recall-campaign',
        'Riverbend Dental/Projects/Brand Assets.md': 'riverbend-brand-assets',
    }
    for path, id_ in paths.items():
        text = (root / path).read_text()
        assert f'id: {id_}\n' in text
    domain = re.compile(r'\b(?:[a-z0-9-]+\.)+[a-z]{2,}\b', re.I)
    for file in ROOT.rglob('*'):
        if file.is_file():
            text = file.read_bytes().decode('utf-8', errors='replace')
            for match in domain.findall(text):
                assert match.lower().endswith('.example'), (file, match)
