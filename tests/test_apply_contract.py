"""K1c apply_ops contract: byte preservation, geometry and failure behaviour.

Expectations here are literal documents so every assertion is byte-exact.  The
contract source is TOOLS.md rev 2 ("Section editing rules", "Parsing, fences and
filenames" and "Value and text rules") plus SPEC.md rev 5 section 13 C6.
"""
from copy import deepcopy
from datetime import UTC, date, datetime
from pathlib import Path

import pytest
import yaml
from engagements.errors import EngagementsError
from engagements.records import apply_ops, parse

EOL = b'\n'
UPDATED = 'updated: 2026-11-07'
LOG = '- 2026-11-07 studio-dev: '


def clock():
    return datetime(2026, 11, 7, tzinfo=UTC)


def run(data, ops, kind='client', **kwargs):
    return apply_ops(data, kind, ops, clock=clock, profile='studio-dev', source='notes',
                     client_exists=lambda _: True, **kwargs)


def doc(text, *, eol=EOL, final=True):
    data = text.encode().replace(b'\n', eol)
    return data if final else data[:-len(eol)]


def failure(data, ops, kind='client', **kwargs):
    with pytest.raises(EngagementsError) as exc:
        run(data, ops, kind, **kwargs)
    return exc.value


BASE = """---
engagements: client
id: acme-bakery
name: Acme Bakery
status: active
---

## Notes
old

## Change log
previous
"""

STATUS_PAUSED = """---
engagements: client
id: acme-bakery
name: Acme Bakery
status: paused
""" + UPDATED + """
---

## Notes
old

## Change log
previous
""" + LOG + """set status (source: notes)
"""


# --- Byte preservation ------------------------------------------------------

def test_bom_is_preserved_and_preamble_bytes_untouched():
    result = run(b'\xef\xbb\xbf' + doc(BASE),
                 [{'op': 'set_property', 'key': 'status', 'value': 'paused'}])
    assert result == b'\xef\xbb\xbf' + doc(STATUS_PAUSED)


@pytest.mark.parametrize('final', [True, False])
def test_crlf_and_no_final_newline_preserved(final):
    result = run(doc(BASE, eol=b'\r\n', final=final),
                 [{'op': 'set_property', 'key': 'status', 'value': 'paused'}])
    assert result == doc(STATUS_PAUSED, eol=b'\r\n', final=final)


@pytest.mark.parametrize('eol', [b'\n', b'\r\n'])
def test_frontmatter_only_record_without_final_eol(eol):
    data = doc('---\nengagements: client\nid: acme-bakery\nname: Acme Bakery\n---\n', eol=eol)[:-len(eol)]
    result = run(data, [{'op': 'set_property', 'key': 'aliases', 'values': ['Acme']}])
    assert result == doc('---\nengagements: client\nid: acme-bakery\nname: Acme Bakery\n'
                         'aliases: [Acme]\n' + UPDATED + '\n---\n\n## Change log\n'
                         + LOG + 'set aliases (source: notes)\n', eol=eol, final=False)


@pytest.mark.parametrize('final', [True, False])
def test_lf_no_final_newline_preserved(final):
    result = run(doc(BASE, final=final), [{'op': 'set_property', 'key': 'status', 'value': 'paused'}])
    assert result == doc(STATUS_PAUSED, final=final)


def test_block_list_property_stays_block():
    data = doc("""---
engagements: client
id: acme-bakery
name: Acme Bakery
aliases:
  - Acme
status: active
---

## Change log
previous
""")
    result = run(data, [{'op': 'set_property', 'key': 'aliases', 'values': ['Acme Bakery', 'yes']}])
    assert result == doc("""---
engagements: client
id: acme-bakery
name: Acme Bakery
aliases:
  - Acme Bakery
  - "yes"
status: active
""" + UPDATED + """
---

## Change log
previous
""" + LOG + """set aliases (source: notes)
""")


def test_flow_list_property_stays_flow():
    data = doc("""---
engagements: client
id: acme-bakery
name: Acme Bakery
aliases: [Acme]
status: active
---

## Change log
previous
""")
    result = run(data, [{'op': 'set_property', 'key': 'aliases', 'values': ['Acme Bakery', 'yes']}])
    assert result == doc("""---
engagements: client
id: acme-bakery
name: Acme Bakery
aliases: [Acme Bakery, "yes"]
status: active
""" + UPDATED + """
---

## Change log
previous
""" + LOG + """set aliases (source: notes)
""")


def test_compact_block_sequence_stays_compact():
    data = doc("""---
engagements: client
id: acme-bakery
name: Acme Bakery
aliases:
- Acme
status: active
---

## Change log
previous
""")
    result = run(data, [{'op': 'set_property', 'key': 'aliases', 'values': ['Acme Bakery', 'yes']}])
    assert result == doc("""---
engagements: client
id: acme-bakery
name: Acme Bakery
aliases:
- Acme Bakery
- "yes"
status: active
""" + UPDATED + """
---

## Change log
previous
""" + LOG + """set aliases (source: notes)
""")


def test_comment_between_properties_is_never_rewritten():
    data = doc("""---
engagements: client
id: acme-bakery
name: Acme Bakery
# keep this comment
status: active
---

## Change log
""")
    result = run(data, [{'op': 'set_property', 'key': 'status', 'value': 'paused'}])
    assert result == doc("""---
engagements: client
id: acme-bakery
name: Acme Bakery
# keep this comment
status: paused
""" + UPDATED + """
---

## Change log
""" + LOG + """set status (source: notes)
""")


def test_unknown_property_with_trailing_comment_is_untouched():
    data = doc("""---
engagements: client
id: acme-bakery
name: Acme Bakery
custom: keep  # note
status: active
---

## Change log
""")
    result = run(data, [{'op': 'set_property', 'key': 'status', 'value': 'paused'}])
    assert result.startswith(doc("""---
engagements: client
id: acme-bakery
name: Acme Bakery
custom: keep  # note
status: paused
"""))


def test_unknown_section_is_preserved_and_editable():
    data = doc("""---
engagements: client
id: acme-bakery
name: Acme Bakery
---

## Unique section
body text

## Change log
""")
    result = run(data, [{'op': 'append_to_section', 'section': 'Unique section', 'text': 'new'}])
    assert result == doc("""---
engagements: client
id: acme-bakery
name: Acme Bakery
""" + UPDATED + """
---

## Unique section
body text
new

## Change log
""" + LOG + """append to Unique section (source: notes)
""")


def test_fenced_heading_lines_are_not_sections():
    data = doc("""---
engagements: client
id: acme-bakery
name: Acme Bakery
---

## Notes
```
## not a heading
```

## Change log
""")
    result = run(data, [{'op': 'append_to_section', 'section': 'Notes', 'text': 'new'}])
    assert result == doc("""---
engagements: client
id: acme-bakery
name: Acme Bakery
""" + UPDATED + """
---

## Notes
```
## not a heading
```
new

## Change log
""" + LOG + """append to Notes (source: notes)
""")


def test_level_three_heading_belongs_to_parent_section():
    data = doc("""---
engagements: client
id: acme-bakery
name: Acme Bakery
---

## Notes
old
### Deeper
detail

## Change log
""")
    result = run(data, [{'op': 'append_to_section', 'section': 'Notes', 'text': 'new'}])
    assert b'## Notes\nold\n### Deeper\ndetail\nnew\n\n## Change log\n' in result


def test_level_one_heading_ends_section_and_its_content_is_never_edited():
    data = doc("""---
engagements: client
id: acme-bakery
name: Acme Bakery
---

## Notes
old

# Loose
loose text

## Change log
""")
    result = run(data, [{'op': 'append_to_section', 'section': 'Notes', 'text': 'new'}])
    assert b'## Notes\nold\nnew\n\n# Loose\nloose text\n\n## Change log\n' in result
    assert failure(data, [{'op': 'remove_line', 'section': 'Notes', 'match': 'loose text'}]).details['count'] == 0


# --- Defect refusal ---------------------------------------------------------

OPS = [{'op': 'set_property', 'key': 'status', 'value': 'paused'},
       {'op': 'replace_section', 'section': 'Notes', 'body': 'new'},
       {'op': 'append_to_section', 'section': 'Notes', 'text': 'new'},
       {'op': 'replace_line', 'section': 'Notes', 'match': 'old', 'text': 'new'},
       {'op': 'remove_line', 'section': 'Notes', 'match': 'old'}]


@pytest.mark.parametrize('op', OPS)
def test_mixed_eol_refuses_every_op(op):
    data = doc(BASE).replace(b'## Notes\nold\n', b'## Notes\r\nold\n')
    assert failure(data, [op]).code == 'malformed'


@pytest.mark.parametrize('op', OPS)
def test_non_utf8_refuses_every_op(op):
    data = doc(BASE).replace(b'## Notes\nold\n', b'## Notes\nold\xff\n')
    assert failure(data, [op]).code == 'malformed'


def test_targeted_duplicate_key_refuses_only_that_property():
    data = doc("""---
engagements: client
id: acme-bakery
name: Acme Bakery
status: active
status: paused
---

## Notes
old

## Change log
""")
    error = failure(data, [{'op': 'set_property', 'key': 'status', 'value': 'former'}])
    assert (error.code, error.details['defects']) == ('malformed', [{'kind': 'duplicate_key'}])
    result = run(data, [{'op': 'append_to_section', 'section': 'Notes', 'text': 'new'}])
    assert b'status: active\nstatus: paused\n' in result


def test_identity_escalated_defect_refuses_every_op():
    data = doc("""---
engagements: client
id: acme-bakery
name: Acme Bakery
name: Other
---

## Notes
old

## Change log
""")
    for op in OPS:
        assert failure(data, [op]).code == 'malformed'


def test_unsupported_identity_property_is_global():
    # A quoted key is not canonical, so C6 makes it unsupported_yaml, and on an
    # identity field that escalates to a global defect.
    data = doc('---\nengagements: client\nid: acme-bakery\n"name": Acme Bakery\n---\n\n## Change log\n')
    error = failure(data, [{'op': 'append_to_section', 'section': 'Notes', 'text': 'new'}])
    assert (error.code, error.details['defects']) == ('malformed', [{'kind': 'unsupported_yaml'}])


def test_set_client_on_a_project():
    data = doc("""---
engagements: project
id: acme-website-2026
client: acme-bakery
name: Website 2026
---

## Change log
""")
    result = run(data, [{'op': 'set_property', 'key': 'client', 'value': 'riverbend-dental'}], kind='project')
    assert b'client: riverbend-dental\n' in result
    assert parse(result).properties['client'] == 'riverbend-dental'


def test_unsupported_property_refuses_only_that_property():
    data = doc("""---
engagements: project
id: acme-website-2026
client: acme-bakery
name: Website 2026
locations:
  - nested: value
status: active
---

## Notes
old

## Change log
""")
    error = failure(data, [{'op': 'set_property', 'key': 'locations', 'values': ['~/acme']}], kind='project')
    assert (error.code, error.details['defects']) == ('malformed', [{'kind': 'unsupported_yaml'}])
    result = run(data, [{'op': 'set_property', 'key': 'status', 'value': 'historical'}], kind='project')
    assert b'locations:\n  - nested: value\n' in result


def test_duplicate_section_refuses_only_that_section():
    data = doc("""---
engagements: client
id: acme-bakery
name: Acme Bakery
---

## Notes
a

## Notes
b

## Contacts
- Jane Rivera | owner | jane@acme-bakery.example

## Change log
""")
    error = failure(data, [{'op': 'append_to_section', 'section': 'Notes', 'text': 'x'}])
    assert (error.code, error.details['defects']) == ('malformed', [{'kind': 'duplicate_section'}])
    result = run(data, [{'op': 'append_to_section', 'section': 'Contacts', 'text': '- Sam Ortiz | manager'}])
    assert b'## Notes\na\n\n## Notes\nb\n' in result


# --- Operation coverage and errors -----------------------------------------

def test_every_op_succeeds_in_one_request():
    result = run(doc(BASE), [
        {'op': 'set_property', 'key': 'status', 'value': 'paused'},
        {'op': 'set_property', 'key': 'aliases', 'values': ['Acme']},
        {'op': 'replace_section', 'section': 'Notes', 'body': 'replaced'},
        {'op': 'append_to_section', 'section': 'Notes', 'text': 'appended'},
        {'op': 'replace_line', 'section': 'Notes', 'match': 'appended', 'text': 'appended line'},
        {'op': 'remove_line', 'section': 'Notes', 'match': 'replaced'},
    ])
    assert b'## Notes\nappended line\n\n## Change log\n' in result
    assert result.endswith((LOG + 'set status; set aliases; replace section Notes; append to Notes; '
                            'replace line in Notes; remove line from Notes (source: notes)\n').encode())


@pytest.mark.parametrize(('op', 'reason', 'field'), [
    ({'op': 'set_property', 'key': 'status', 'value': 'historical'}, 'status_not_allowed_for_kind', 'value'),
    ({'op': 'set_property', 'key': 'locations', 'values': ['~/x']}, 'key_not_allowed_for_kind', 'key'),
    ({'op': 'set_property', 'key': 'status', 'value': 'paused', 'extra': 'x'}, 'unexpected_field', 'extra'),
    ({'op': 'replace_section', 'section': 'Change log', 'body': 'x'}, 'plugin_owned', 'section'),
    ({'op': 'set_property', 'key': 'updated', 'value': '2026-01-01'}, 'plugin_owned', 'key'),
    ({'op': 'append_to_section', 'section': 'Notes', 'text': '## Heading'}, 'heading_in_text', 'text'),
    ({'op': 'replace_section', 'section': 'Notes', 'body': '```\nunclosed'}, 'unbalanced_fence', 'body'),
    ({'op': 'append_to_section', 'section': 'Notes', 'text': '   '}, 'empty_text', 'text'),
    ({'op': 'replace_line', 'section': 'Missing', 'match': 'x', 'text': 'y'}, 'section_missing', 'section'),
])
def test_documented_invalid_op_reasons(op, reason, field):
    error = failure(doc(BASE), [op])
    assert error.code == 'invalid_op'
    assert (error.details['field'], error.details['reason']) == (field, reason)


def test_status_reason_respects_project_kind():
    data = doc("""---
engagements: project
id: acme-project
client: acme-bakery
name: Project
---

## Change log
""")
    error = failure(data, [{'op': 'set_property', 'key': 'status', 'value': 'paused'}], kind='project')
    assert error.details['reason'] == 'status_not_allowed_for_kind'


@pytest.mark.parametrize(('body', 'match', 'count'), [
    ('old\n', 'absent', 0),
    ('old\nolder\n', 'old', 2),
])
def test_match_count_reports_hits(body, match, count):
    data = doc('---\nengagements: client\nid: acme-bakery\nname: Acme Bakery\n---\n\n## Notes\n'
               + body + '\n## Change log\n')
    error = failure(data, [{'op': 'replace_line', 'section': 'Notes', 'match': match, 'text': 'new'}])
    assert (error.code, error.details['reason'], error.details['count']) == ('invalid_op', 'match_count', count)


def test_structure_changed_when_applied_heading_is_fenced():
    data = doc("""---
engagements: client
id: acme-bakery
name: Acme Bakery
---

## Notes
```
old
""")
    error = failure(data, [{'op': 'set_property', 'key': 'status', 'value': 'paused'}])
    assert (error.code, error.details['reason']) == ('invalid_op', 'structure_changed')


def test_too_large_frontmatter_reports_result_level_details():
    data = b'---\nengagements: client\nid: acme-bakery\nname: Acme Bakery\n#' + b'x' * 65455 + b'\n---\n'
    error = failure(data, [{'op': 'set_property', 'key': 'status', 'value': 'paused'}])
    assert error.code == 'invalid_op'
    assert error.details == {'field': 'ops', 'reason': 'too_large'}


def test_too_large_file_reports_result_level_details():
    data = b'---\nengagements: client\nid: acme-bakery\nname: Acme Bakery\n---\n\n## Notes\n' + b'x' * 1048510
    error = failure(data, [{'op': 'append_to_section', 'section': 'Notes', 'text': 'new'}])
    assert error.details == {'field': 'ops', 'reason': 'too_large'}


def test_too_large_when_inserting_a_property_crosses_the_frontmatter_window():
    data = b'---\nengagements: client\nid: acme-bakery\nname: Acme Bakery\n#' + b'x' * 65440 + b'\n---\n'
    assert parse(data).is_record
    error = failure(data, [{'op': 'set_property', 'key': 'aliases', 'values': ['Acme']}])
    assert error.details['reason'] == 'too_large'


# --- Section geometry -------------------------------------------------------

GEOMETRY_BASE = """---
engagements: client
id: acme-bakery
name: Acme Bakery
---

## Notes
old

## Change log
previous
"""


@pytest.mark.parametrize('eol', [b'\n', b'\r\n'])
@pytest.mark.parametrize('final', [True, False])
@pytest.mark.parametrize(('op', 'body'), [
    ({'op': 'replace_section', 'section': 'Notes', 'body': 'new\nline'},
     '## Notes\nnew\nline\n\n## Change log\n'),
    ({'op': 'replace_section', 'section': 'Notes', 'body': ''},
     '## Notes\n\n## Change log\n'),
    ({'op': 'append_to_section', 'section': 'Notes', 'text': 'new'},
     '## Notes\nold\nnew\n\n## Change log\n'),
    ({'op': 'append_to_section', 'section': 'Notes', 'text': 'new\nline'},
     '## Notes\nold\nnew\nline\n\n## Change log\n'),
    ({'op': 'replace_line', 'section': 'Notes', 'match': 'old', 'text': 'new'},
     '## Notes\nnew\n\n## Change log\n'),
    ({'op': 'remove_line', 'section': 'Notes', 'match': 'old'},
     '## Notes\n\n## Change log\n'),
])
def test_section_geometry_matrix(eol, final, op, body):
    result = run(doc(GEOMETRY_BASE, eol=eol, final=final), [op])
    assert doc(body, eol=eol, final=final) in result
    assert result.endswith(eol) == final


@pytest.mark.parametrize('eol', [b'\n', b'\r\n'])
@pytest.mark.parametrize('final', [True, False])
@pytest.mark.parametrize(('op', 'body'), [
    ({'op': 'replace_section', 'section': 'Zeta', 'body': 'body'},
     '## Zeta\nbody\n\n## Change log\n'),
    ({'op': 'append_to_section', 'section': 'Zeta', 'text': 'line'},
     '## Zeta\nline\n\n## Change log\n'),
    ({'op': 'replace_section', 'section': 'Zeta', 'body': ''},
     '## Zeta\n\n## Change log\n'),
])
def test_missing_section_is_created_before_change_log(eol, final, op, body):
    result = run(doc(GEOMETRY_BASE, eol=eol, final=final), [op])
    assert doc(body, eol=eol, final=final) in result


@pytest.mark.parametrize('eol', [b'\n', b'\r\n'])
@pytest.mark.parametrize('final', [True, False])
def test_replace_last_section_keeps_final_newline_state(eol, final):
    data = doc("""---
engagements: client
id: acme-bakery
name: Acme Bakery
---

## Change log
previous

## Notes
old
""", eol=eol, final=final)
    result = run(data, [{'op': 'replace_section', 'section': 'Notes', 'body': 'new'}])
    assert doc('## Change log\nprevious\n' + LOG + 'replace section Notes (source: notes)\n\n## Notes\nnew\n',
               eol=eol, final=final) in result
    assert result.endswith(eol) == final


def test_heading_line_text_is_never_rewritten():
    data = doc("""---
engagements: client
id: acme-bakery
name: Acme Bakery
---

##  Contacts
- Jane Rivera

## Change log
""").replace(b'##  Contacts\n', b'##  Contacts  \n')
    result = run(data, [{'op': 'replace_section', 'section': 'contacts', 'body': '- Sam Ortiz'}])
    assert b'##  Contacts  \n- Sam Ortiz\n\n## Change log\n' in result


@pytest.mark.parametrize('eol', [b'\n', b'\r\n'])
@pytest.mark.parametrize(('ending', 'expected'), [
    # TOOLS.md: ensure a final EOL, add one blank line only when the last line
    # is not blank, then the heading; the final-EOL state is preserved.
    ('old\n', 'old\n\n## Change log\n'),
    ('old\n\n', 'old\n\n## Change log\n'),
    ('old\n   \n', 'old\n   \n## Change log\n'),
    ('old', 'old\n\n## Change log\n'),
    ('old\n   ', 'old\n   \n## Change log\n'),
])
def test_change_log_creation_geometry(eol, ending, expected):
    data = ('---\nengagements: client\nid: acme-bakery\nname: Acme Bakery\n---\n\n## Notes\n'
            + ending).encode().replace(b'\n', eol)
    final = ending.endswith('\n')
    result = run(data, [{'op': 'set_property', 'key': 'status', 'value': 'paused'}])
    assert doc(expected, eol=eol, final=final) in result
    assert result.endswith(eol) == final


# --- Property rewriting and quoting ----------------------------------------

QUOTED_VALUES = ['plain', 'yes', 'null', '123', 'Acme: Bakery', '#launch', 'a, b', '[x]',
                 ' leading', '\U0001f35e', 'a"\\c', 'Riverbend Dental']
QUOTED_FLOW = ('[plain, "yes", "null", "123", "Acme: Bakery", "#launch", "a, b", "[x]", '
               '" leading", \U0001f35e, a"\\c, Riverbend Dental]')


def test_flow_list_quoting_is_exact():
    data = doc('---\nengagements: client\nid: acme-bakery\nname: Acme Bakery\naliases: [old]\n---\n\n## Change log\n')
    result = run(data, [{'op': 'set_property', 'key': 'aliases', 'values': QUOTED_VALUES}])
    assert ('aliases: ' + QUOTED_FLOW + '\n').encode() in result
    assert parse(result).properties['aliases'] == QUOTED_VALUES


def test_block_list_quoting_is_exact_and_mixed():
    data = doc('---\nengagements: client\nid: acme-bakery\nname: Acme Bakery\naliases:\n  - old\n---\n\n## Change log\n')
    result = run(data, [{'op': 'set_property', 'key': 'aliases', 'values': QUOTED_VALUES}])
    expected = ('aliases:\n'
                '  - plain\n  - "yes"\n  - "null"\n  - "123"\n  - "Acme: Bakery"\n  - "#launch"\n'
                '  - a, b\n  - "[x]"\n  - " leading"\n  - \U0001f35e\n  - a"\\c\n  - Riverbend Dental\n')
    assert expected.encode() in result
    assert parse(result).properties['aliases'] == QUOTED_VALUES


def test_scalar_quoting_is_exact():
    data = doc('---\nengagements: client\nid: acme-bakery\nname: Acme Bakery\n---\n\n## Change log\n')
    result = run(data, [{'op': 'set_property', 'key': 'name', 'value': 'Acme: Bakery'}])
    assert b'name: "Acme: Bakery"\n' in result
    assert parse(result).properties['name'] == 'Acme: Bakery'


def test_updated_stays_a_plain_yaml_date_on_insert_and_rewrite():
    # SPEC 4.2/4.3 define updated as a date and the create template writes it
    # plain; TOOLS.md "JSON property values" reads YAML dates back as ISO
    # strings. The plugin's own edit must preserve that type rather than quote
    # the date into a string. The date is still injected through the clock.
    inserted = run(doc('---\nengagements: client\nid: acme-bakery\nname: Acme Bakery\n---\n\n'
                       '## Change log\n'),
                   [{'op': 'set_property', 'key': 'status', 'value': 'paused'}])
    rewritten = run(doc('---\nengagements: client\nid: acme-bakery\nname: Acme Bakery\n'
                        'updated: 2026-11-01\n---\n\n## Change log\n'),
                    [{'op': 'set_property', 'key': 'status', 'value': 'paused'}])
    for result in (inserted, rewritten):
        assert b'updated: 2026-11-07\n' in result
        assert b'"2026-11-07"' not in result
        assert yaml.safe_load(result.split(b'---')[1])['updated'] == date(2026, 11, 7)
        assert parse(result).properties['updated'] == '2026-11-07'


def test_new_and_empty_list_are_flow():
    data = doc('---\nengagements: client\nid: acme-bakery\nname: Acme Bakery\n---\n\n## Change log\n')
    assert b'aliases: [Acme]\n' in run(data, [{'op': 'set_property', 'key': 'aliases', 'values': ['Acme']}])
    assert b'aliases: []\n' in run(data, [{'op': 'set_property', 'key': 'aliases', 'values': []}])


def test_trailing_comment_on_rewritten_span_is_not_kept():
    data = doc('---\nengagements: client\nid: acme-bakery\nname: Acme Bakery\naliases: [old] # note\n---\n\n## Change log\n')
    result = run(data, [{'op': 'set_property', 'key': 'aliases', 'values': ['new']}])
    assert b'aliases: [new]\n' in result
    assert b'# note' not in result


@pytest.mark.parametrize('eol', [b'\n', b'\r\n'])
def test_append_to_heading_only_section_at_eof_without_final_eol(eol):
    data = doc('---\nengagements: client\nid: acme-bakery\nname: Acme Bakery\n---\n\n'
               '## Change log\nprevious\n\n## Notes\n', eol=eol)[:-len(eol)]
    result = run(data, [{'op': 'append_to_section', 'section': 'Notes', 'text': 'new'}])
    assert result.endswith(doc('## Notes\nnew', eol=eol))
    assert not result.endswith(eol)


@pytest.mark.parametrize('eol', [b'\n', b'\r\n'])
@pytest.mark.parametrize('final', [True, False])
def test_append_to_empty_section_before_heading(eol, final):
    data = doc('---\nengagements: client\nid: acme-bakery\nname: Acme Bakery\n---\n\n'
               '## Notes\n\n## Change log\nprevious\n', eol=eol, final=final)
    result = run(data, [{'op': 'append_to_section', 'section': 'Notes', 'text': 'new'}])
    assert doc('## Notes\nnew\n\n## Change log\n', eol=eol, final=final) in result
    assert result.endswith(eol) == final


@pytest.mark.parametrize('eol', [b'\n', b'\r\n'])
@pytest.mark.parametrize(('op', 'expected'), [
    ({'op': 'replace_line', 'section': 'Notes', 'match': 'last', 'text': 'new'}, '## Notes\nold\nnew'),
    ({'op': 'remove_line', 'section': 'Notes', 'match': 'last'}, '## Notes\nold'),
])
def test_line_edits_on_last_body_line_without_final_eol(eol, op, expected):
    data = doc('---\nengagements: client\nid: acme-bakery\nname: Acme Bakery\n---\n\n'
               '## Change log\nprevious\n\n## Notes\nold\nlast\n', eol=eol)[:-len(eol)]
    result = run(data, [op])
    assert result.endswith(doc(expected, eol=eol))
    assert not result.endswith(eol)


def test_heading_line_is_never_matched_by_line_edits():
    data = doc('---\nengagements: client\nid: acme-bakery\nname: Acme Bakery\n---\n\n'
               '## Notes\nold\n\n## Change log\n')
    error = failure(data, [{'op': 'remove_line', 'section': 'Notes', 'match': 'Notes'}])
    assert (error.code, error.details['reason'], error.details['count']) == ('invalid_op', 'match_count', 0)


def test_project_locations_block_list_rewrite():
    data = doc("""---
engagements: project
id: acme-website-2026
client: acme-bakery
name: Website 2026
locations:
  - ~/clients/acme/website-2026
status: active
---

## Change log
""")
    result = run(data, [{'op': 'set_property', 'key': 'locations',
                         'values': ['~/clients/acme/website-2026', 'https://git.example/studio/acme-site']}],
                 kind='project')
    assert (b'locations:\n  - ~/clients/acme/website-2026\n'
            b'  - https://git.example/studio/acme-site\nstatus: active\n') in result
    assert parse(result).properties['locations'] == ['~/clients/acme/website-2026',
                                                     'https://git.example/studio/acme-site']


def test_disagreeing_markers_are_a_global_defect_before_the_kind_check():
    data = doc('---\nengagements: client\nengagements: project\nid: acme-bakery\n'
               'client: acme-bakery\nname: Acme Bakery\n---\n\n## Change log\n')
    error = failure(data, [{'op': 'set_property', 'key': 'status', 'value': 'paused'}])
    assert (error.code, error.details['defects']) == ('malformed', [{'kind': 'duplicate_key'}])


def test_non_record_and_kind_mismatch_are_caller_bugs():
    with pytest.raises(ValueError):
        run(b'plain text', [{'op': 'set_property', 'key': 'status', 'value': 'paused'}])
    with pytest.raises(ValueError):
        run(doc("""---
engagements: project
id: acme-website-2026
client: acme-bakery
name: Website 2026
---

## Change log
"""), [{'op': 'set_property', 'key': 'status', 'value': 'paused'}], kind='client')
    with pytest.raises(ValueError):
        run(doc(BASE), [{'op': 'set_property', 'key': 'status', 'value': 'paused'}], kind='unknown')


# --- Fixture robustness ----------------------------------------------------

FIXTURES = sorted((Path(__file__).parent / 'fixtures' / 'records').glob('*.md'))
LIBRARY = sorted((Path(__file__).parent / 'fixtures' / 'library').rglob('*.md'))
DOCUMENTED_CODES = {'invalid_op', 'malformed', 'not_found', 'conflict', 'io_error'}


def _lines(data):
    return [line.rstrip(b'\r') for line in data.split(b'\n')[:-1]]


def _assert_lines_preserved(original, result, label):
    kept = _lines(result)
    position = 0
    for line in _lines(original):
        if line.startswith(b'updated:'):
            continue  # plugin-owned; rewritten in place
        while position < len(kept) and kept[position] != line:
            position += 1
        assert position < len(kept), f'{label}: lost line {line!r}'
        position += 1


@pytest.mark.parametrize('path', FIXTURES, ids=lambda path: path.stem)
def test_apply_over_parser_fixtures_preserves_every_original_line(path):
    """Every parser edge case either refuses with a documented error or keeps
    every original line, so no fixture splice can drop untouched bytes."""
    data = path.read_bytes()
    record = parse(data)
    if not record.is_record:
        return
    try:
        result = run(data, [{'op': 'append_to_section', 'section': 'Matrix notes', 'text': 'appended'}],
                     kind=record.kind or 'client')
    except EngagementsError as exc:
        assert exc.code in DOCUMENTED_CODES
        assert isinstance(exc.details, dict)
        return
    final = parse(result)
    assert final.is_record
    assert not final.defects
    _assert_lines_preserved(data, result, path.stem)


@pytest.mark.parametrize('path', LIBRARY, ids=lambda path: f'{path.parent.name}/{path.stem}')
def test_apply_over_library_records(path):
    data = path.read_bytes()
    record = parse(data)
    assert record.kind in ('client', 'project')
    assert not record.defects
    status = 'paused' if record.kind == 'client' else 'historical'
    result = run(data, [{'op': 'append_to_section', 'section': 'Notes', 'text': 'New note'},
                        {'op': 'set_property', 'key': 'status', 'value': status}], kind=record.kind)
    final = parse(result)
    assert not final.defects
    assert final.properties['status'] == status
    assert [section.key for section in final.sections].count('notes') == 1
    _assert_lines_preserved(data[record.frontmatter_span[1]:], result, path.stem)


# --- Ordering, atomicity and precedence ------------------------------------

def test_ops_apply_in_order_and_summarise_in_order():
    data = doc('---\nengagements: client\nid: acme-bakery\nname: Acme Bakery\n---\n\n## Change log\n')
    result = run(data, [
        {'op': 'append_to_section', 'section': 'Notes', 'text': 'alpha'},
        {'op': 'append_to_section', 'section': 'Notes', 'text': 'beta'},
        {'op': 'replace_line', 'section': 'Notes', 'match': 'beta', 'text': 'gamma'},
    ])
    assert b'## Notes\nalpha\ngamma\n\n## Change log\n' in result
    assert result.endswith((LOG + 'append to Notes; append to Notes; replace line in Notes '
                            '(source: notes)\n').encode())


def test_failing_op_returns_no_bytes_and_leaves_input_untouched():
    data = doc(BASE)
    original = bytes(data)
    with pytest.raises(EngagementsError):
        run(data, [{'op': 'append_to_section', 'section': 'Notes', 'text': 'new'},
                   {'op': 'remove_line', 'section': 'Notes', 'match': 'absent'}])
    assert data == original


def test_operations_argument_is_not_mutated():
    ops = [{'op': 'set_property', 'key': 'aliases', 'values': ['Acme!', 'acme', 'Bakery']},
           {'op': 'append_to_section', 'section': 'Notes', 'text': 'new'}]
    snapshot = deepcopy(ops)
    run(doc(BASE), ops)
    assert ops == snapshot


def test_stage_one_shape_errors_precede_global_defects():
    data = doc("""---
engagements: client
id: acme-bakery
name: Acme Bakery
name: Other
---

## Change log
""")
    error = failure(data, [{'op': 'remove_line', 'section': 'Notes'}])
    assert (error.code, error.details['op_index'], error.details['field'], error.details['reason']) == (
        'invalid_op', 0, 'match', 'missing_field')


def test_stage_four_value_errors_precede_targeted_defects():
    data = doc("""---
engagements: client
id: acme-bakery
name: Acme Bakery
status: active
status: paused
---

## Change log
""")
    error = failure(data, [{'op': 'set_property', 'key': 'status', 'value': 'historical'}])
    assert (error.code, error.details['reason']) == ('invalid_op', 'status_not_allowed_for_kind')
