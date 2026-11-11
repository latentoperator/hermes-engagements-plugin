"""A malformed .engagements/library.yaml is invalid metadata, whatever failed.

YAML constructors raise whatever the tag's own code raises, so a library file
carrying an explicit tag used to escape the open as a raw AttributeError or
IndexError instead of the documented ``library_missing`` error.
"""

import uuid

import pytest
import yaml
from conftest import write
from engagements import library as library_module
from engagements.doctor import run_doctor
from engagements.errors import EngagementsError
from engagements.library import open_library, open_library_readonly

HOME = "/home/tester"
FAILING_VALUES = ['!!timestamp nope', '!!int ""', '!!float ""']


class _Boom(Exception):
    """A constructor failure whose type is in no list of expected exceptions."""


def write_metadata(root, extra):
    write(root, ".engagements/library.yaml",
          f"format: 1\nlibrary_id: {uuid.uuid4()}\nother: {extra}\n")
    return root


def doctor(root):
    return run_doctor(root, home=HOME, os_name="posix", exists=lambda path: False, realpath=lambda path: path)


@pytest.mark.parametrize("value", FAILING_VALUES)
def test_a_constructor_failure_is_invalid_metadata_for_every_opener(tmp_path, value):
    root = write_metadata(tmp_path / "library", value)

    for opener in (open_library_readonly, lambda path: open_library(path, create_default=False)):
        with pytest.raises(EngagementsError) as excinfo:
            opener(root)
        assert excinfo.value.code == "library_missing"
        assert excinfo.value.details["reason"] == "invalid_library_yaml"


@pytest.mark.parametrize("value", FAILING_VALUES)
def test_doctor_reports_a_constructor_failure_as_an_invalid_metadata_finding(tmp_path, value):
    root = write_metadata(tmp_path / "library", value)
    write(root, "Acme Bakery/Client.md",
          b"---\nengagements: client\nid: acme-bakery\nname: Acme Bakery\n---\n\n## Summary\n")

    report = doctor(root)

    (finding,) = [item for item in report.findings if item.code == "library_yaml"]
    assert finding.severity == "error"
    assert finding.path == ".engagements/library.yaml"
    assert not report.clean


def test_a_constructor_failure_of_an_unlisted_type_is_invalid_metadata(tmp_path):
    # The documented behaviour is "any constructor failure", so the catch cannot be a
    # list of the exception types seen so far. A registered tag that raises its own
    # class proves the containment: narrowing the catch to AttributeError, IndexError
    # and friends would let this one escape.
    def boom(loader, node):
        raise _Boom("constructor failed")

    yaml.SafeLoader.add_constructor("!boom", boom)
    try:
        root = write_metadata(tmp_path / "library", "!boom value")
        for opener in (open_library_readonly, lambda path: open_library(path, create_default=False)):
            with pytest.raises(EngagementsError) as excinfo:
                opener(root)
            assert excinfo.value.code == "library_missing"
            assert excinfo.value.details["reason"] == "invalid_library_yaml"
    finally:
        yaml.SafeLoader.yaml_constructors.pop("!boom", None)


def test_an_out_of_memory_load_is_not_turned_into_invalid_metadata(tmp_path, monkeypatch):
    root = write_metadata(tmp_path / "library", "value")

    def exhausted(*args, **kwargs):
        raise MemoryError

    monkeypatch.setattr(library_module.yaml, "safe_load", exhausted)
    with pytest.raises(MemoryError):
        open_library_readonly(root)


def test_an_interrupted_load_is_not_turned_into_invalid_metadata(tmp_path, monkeypatch):
    root = write_metadata(tmp_path / "library", "value")

    def interrupted(*args, **kwargs):
        raise KeyboardInterrupt

    monkeypatch.setattr(library_module.yaml, "safe_load", interrupted)
    with pytest.raises(KeyboardInterrupt):
        open_library_readonly(root)


def test_a_well_formed_metadata_file_still_opens(tmp_path):
    root = tmp_path / "library"
    identifier = str(uuid.uuid4())
    write(root, ".engagements/library.yaml", f"format: 1\nlibrary_id: {identifier}\n")

    assert open_library_readonly(root).library_id == identifier
