"""read_record: full-bytes revision, atomic snapshot, boundaries, pruning."""
import os
from datetime import timedelta

import pytest
from engagements import writer
from engagements.errors import EngagementsError
from engagements.fsutil import rev
from writer_support import ACME, fixed_clock, make_library


def test_read_returns_bytes_and_rev_over_the_full_file(lib):
    data, revision = writer.read_record(lib.root, lib.path)
    assert data == ACME.encode()
    assert revision == rev(ACME.encode())


def test_read_snapshots_the_exact_bytes_under_the_rev(lib):
    _, revision = writer.read_record(lib.root, lib.path)
    snapshot = lib.meta / "snapshots" / f"{revision}.md"
    assert snapshot.read_bytes() == ACME.encode()
    assert [p.name for p in snapshot.parent.iterdir()] == [f"{revision}.md"]  # no temp left


def test_rev_covers_bom_and_crlf_bytes(tmp_path):
    raw = b"\xef\xbb\xbf" + ACME.replace("\n", "\r\n").encode()
    lib = make_library(tmp_path, raw)
    data, revision = writer.read_record(lib.root, lib.path)
    assert data == raw and revision == rev(raw)
    assert (lib.meta / "snapshots" / f"{revision}.md").read_bytes() == raw


def test_existing_valid_snapshot_is_not_rewritten(lib):
    _, revision = writer.read_record(lib.root, lib.path)
    snapshot = lib.meta / "snapshots" / f"{revision}.md"
    before = snapshot.stat()
    writer.read_record(lib.root, lib.path)
    after = snapshot.stat()
    assert (before.st_ino, before.st_mtime_ns) == (after.st_ino, after.st_mtime_ns)


def test_a_snapshot_whose_hash_does_not_match_its_name_is_replaced_on_read(lib):
    revision = rev(ACME.encode())
    directory = lib.meta / "snapshots"
    directory.mkdir()
    (directory / f"{revision}.md").write_bytes(b"tampered")
    assert writer.load_snapshot(lib.root, revision) is None
    writer.read_record(lib.root, lib.path)
    assert writer.load_snapshot(lib.root, revision) == ACME.encode()


def test_load_snapshot_missing_or_malformed_rev_is_absent(lib):
    assert writer.load_snapshot(lib.root, "0" * 16) is None
    assert writer.load_snapshot(lib.root, "../../etc/passwd") is None
    assert writer.load_snapshot(lib.root, "ABCDEF0123456789") is None


def test_snapshot_failure_still_returns_with_a_warning(lib, tmp_path):
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (lib.meta / "snapshots").symlink_to(elsewhere)
    warnings = []
    data, revision = writer.read_record(lib.root, lib.path, warnings=warnings)
    assert data == ACME.encode() and revision == rev(ACME.encode())
    assert [w["code"] for w in warnings] == ["snapshot_failed"]
    assert list(elsewhere.iterdir()) == []  # nothing followed the symlink


def test_snapshot_failure_without_a_collector_does_not_raise(lib, tmp_path):
    (lib.meta / "snapshots").symlink_to(tmp_path)
    assert writer.read_record(lib.root, lib.path)[1] == rev(ACME.encode())


def test_metadata_directory_is_created_on_demand(tmp_path):
    lib = make_library(tmp_path)
    assert not (lib.meta / "snapshots").exists()
    writer.read_record(lib.root, lib.path)
    assert (lib.meta / "snapshots").is_dir()


def test_relative_paths_are_taken_from_the_root(lib):
    data, _ = writer.read_record(lib.root, "Acme Bakery/Client.md")
    assert data == ACME.encode()


@pytest.mark.parametrize("code,reason,build", [
    ("not_found", "no_match", lambda lib: lib.path.with_name("Missing.md")),
    ("not_found", "excluded", lambda lib: lib.meta / "library.yaml"),
])
def test_unreadable_targets_are_refused_without_a_traceback(lib, code, reason, build):
    with pytest.raises(EngagementsError) as exc:
        writer.read_record(lib.root, build(lib))
    assert exc.value.code == code and exc.value.details["reason"] == reason


def test_a_path_outside_the_root_is_outside_root(lib, tmp_path):
    outside = tmp_path / "other.md"
    outside.write_bytes(ACME.encode())
    with pytest.raises(EngagementsError) as exc:
        writer.read_record(lib.root, outside)
    assert exc.value.code == "outside_root"


def test_dotdot_escape_is_outside_root(lib, tmp_path):
    (tmp_path / "other.md").write_bytes(ACME.encode())
    with pytest.raises(EngagementsError) as exc:
        writer.read_record(lib.root, lib.root / "Acme Bakery" / ".." / ".." / "other.md")
    assert exc.value.code == "outside_root"


def test_symlinked_file_is_excluded(lib, tmp_path):
    link = lib.path.with_name("Link.md")
    link.symlink_to(lib.path)
    with pytest.raises(EngagementsError) as exc:
        writer.read_record(lib.root, link)
    assert (exc.value.code, exc.value.details["reason"]) == ("not_found", "excluded")


def test_symlinked_directory_component_is_excluded(lib, tmp_path):
    (lib.root / "Alias").symlink_to(lib.path.parent)
    with pytest.raises(EngagementsError) as exc:
        writer.read_record(lib.root, lib.root / "Alias" / "Client.md")
    assert (exc.value.code, exc.value.details["reason"]) == ("not_found", "excluded")


def test_dot_directory_components_are_excluded(lib):
    hidden = lib.root / ".hidden"
    hidden.mkdir()
    (hidden / "Client.md").write_bytes(ACME.encode())
    with pytest.raises(EngagementsError) as exc:
        writer.read_record(lib.root, hidden / "Client.md")
    assert (exc.value.code, exc.value.details["reason"]) == ("not_found", "excluded")


def test_a_directory_is_not_a_record(lib):
    with pytest.raises(EngagementsError) as exc:
        writer.read_record(lib.root, lib.path.parent)
    assert exc.value.code == "not_found"


def test_oversize_files_are_not_records(lib):
    lib.path.write_bytes(b"x" * (1024 * 1024 + 1))
    with pytest.raises(EngagementsError) as exc:
        writer.read_record(lib.root, lib.path)
    assert (exc.value.code, exc.value.details["reason"]) == ("not_found", "not_a_record")


def test_os_errors_become_io_error_with_a_relative_path(lib, monkeypatch):
    def broken(*args, **kwargs):
        raise OSError(5, "boom: /secret/place")

    monkeypatch.setattr(writer.os, "read", broken)
    with pytest.raises(EngagementsError) as exc:
        writer.read_record(lib.root, lib.path)
    assert exc.value.code == "io_error"
    assert exc.value.details["path"] == "Acme Bakery/Client.md"
    assert "secret" not in exc.value.message


def test_prune_removes_snapshots_older_than_14_days(lib):
    _, old = writer.read_record(lib.root, lib.path, clock=fixed_clock)
    directory = lib.meta / "snapshots"
    stale = directory / ("a" * 16 + ".md")
    fresh = directory / ("b" * 16 + ".md")
    stale.write_bytes(b"old")
    fresh.write_bytes(b"new")
    now = fixed_clock().timestamp()
    os.utime(stale, (now - 14 * 86400 - 60, now - 14 * 86400 - 60))
    os.utime(fresh, (now - 13 * 86400, now - 13 * 86400))
    writer.prune_snapshots(lib.root, clock=fixed_clock)
    assert not stale.exists() and fresh.exists()
    assert (directory / f"{old}.md").exists()


def test_reading_prunes_with_an_injected_clock_and_refreshes_what_it_reads(lib):
    _, revision = writer.read_record(lib.root, lib.path, clock=fixed_clock)
    snapshot = lib.meta / "snapshots" / f"{revision}.md"
    long_ago = fixed_clock().timestamp() - 20 * 86400
    os.utime(snapshot, (long_ago, long_ago))
    writer.read_record(lib.root, lib.path, clock=fixed_clock)  # re-read of a still-needed rev
    assert snapshot.exists()
    assert abs(snapshot.stat().st_mtime - fixed_clock().timestamp()) < 1
    later = lambda: fixed_clock() + timedelta(days=15)  # noqa: E731
    writer.prune_snapshots(lib.root, clock=later)
    assert not snapshot.exists()


def test_prune_failure_on_read_is_a_maintenance_warning(lib, monkeypatch):
    def broken(*args, **kwargs):
        raise OSError("no scan")

    monkeypatch.setattr(writer.os, "scandir", broken)
    warnings = []
    _, revision = writer.read_record(lib.root, lib.path, clock=fixed_clock, warnings=warnings)
    assert revision == rev(ACME.encode())
    assert [w["code"] for w in warnings] == ["maintenance_warning"]
