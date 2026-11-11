"""Golden tool cases (format and rules: tests/golden/README.md).

One parametrized test per ``tests/golden/<tool>/<case>.json`` runs the case
through ``engagements.tools.call_tool`` and compares the complete envelope.
The harness lives in ``tests/golden_support.py``; it imports the tool module
inside the test, so collection works before ``engagements/tools.py`` exists and
every case fails with ImportError until it does.
"""

import golden_support as support
import pytest

CASES = support.case_files()


def _id(tool, path):
    return f"{tool}/{path.stem}"


@pytest.mark.parametrize(
    "tool,path",
    [pytest.param(tool, path, id=_id(tool, path)) for tool, path in CASES],
)
def test_golden_case(tool, path):
    if tool not in support.KNOWN_TOOLS:
        pytest.fail(f"tests/golden/{tool}/ is not a known tool; expected one of {support.KNOWN_TOOLS}")
    support.run_case(tool, path)


def test_every_golden_case_is_well_formed():
    problems = support.check_cases()
    assert not problems, "golden case files have problems:\n" + "\n".join(problems)


def test_golden_cases_exist():
    assert CASES, "no cases under tests/golden/<tool>/"
