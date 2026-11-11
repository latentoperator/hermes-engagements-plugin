"""Create's exclusive fallback write publishes complete bytes even while an updater holds
the record lock. Fictional data only.

When hard links are unsupported, create writes the final record directly with
O_EXCL. The library lock does not exclude the writer's record lock: an updater could
read a valid prefix, replace that inode, and succeed before create finished
writing its now-unlinked inode. Both reported success but the Summary and
remaining template sections were lost, and create's revision was not durable.

Pause the real fallback write after ``## Summary\n`` and run the real resolver,
read/snapshot and updater against that prefix. Record-lock coordination must
make an updater either fail cleanly or re-read and rebase onto complete bytes;
exclusive create must still refuse pre-existing destinations.
"""

import errno
import os
import threading

import pytest
from engagements import fsutil, writer
from engagements import library as library_module
from engagements.errors import EngagementsError
from engagements.library import open_library
from writer_support import fixed_clock, op_status

pytestmark = pytest.mark.skipif(os.name == "nt", reason="POSIX descriptor-relative writes and flock")
SUMMARY = b"Confirmed synthetic summary."


@pytest.fixture
def lib(tmp_path):
    return open_library(tmp_path / "library", create_default=True)


def force_fallback(monkeypatch):
    def unsupported(self, source, destination):
        raise OSError(errno.EOPNOTSUPP, "links unsupported")

    monkeypatch.setattr(fsutil._Dir, "link", unsupported)


def create(lib):
    return lib.create("client", "Acme Bakery", profile="studio-dev", today=fixed_clock,
                      sections=[{"section": "Summary", "body": SUMMARY.decode()}])


def pause_final_write(monkeypatch, action):
    """Call action after a valid prefix, only for the final Client.md descriptor."""
    real_open, real_write = fsutil._Dir.open, library_module._write_all
    final_fds, published = set(), []

    def opening(self, name, flags, *args, **kwargs):
        fd = real_open(self, name, flags, *args, **kwargs)
        if name == "Client.md" and flags & os.O_CREAT and flags & os.O_WRONLY:
            final_fds.add(fd)
        return fd

    def writing(fd, data):
        if fd not in final_fds:
            return real_write(fd, data)
        final_fds.remove(fd)
        published.append(data)
        end = data.index(b"## Summary\n") + len(b"## Summary\n")
        real_write(fd, data[:end])
        action(data[:end])
        real_write(fd, data[end:])

    monkeypatch.setattr(fsutil._Dir, "open", opening)
    monkeypatch.setattr(library_module, "_write_all", writing)
    return published


def update(lib, path, base, ops, **kwargs):
    return writer.update(lib, path, base, "confirmed synthetic notes", ops, kind="client",
                         profile="studio-ops", clock=fixed_clock, client_exists=lambda _: False, **kwargs)


def test_fallback_prefix_update_cannot_lose_created_content(lib, monkeypatch):
    """The synchronous interleaving either updates safely or fails cleanly."""
    force_fallback(monkeypatch)
    outcomes = []

    def during_prefix(prefix):
        entry = lib.resolve("Acme Bakery")
        data, base = writer.read_record(lib, entry.relpath, clock=fixed_clock)
        assert data == prefix and SUMMARY not in data
        try:
            outcomes.append(update(lib, entry.relpath, base, [op_status()], lock_timeout=0.01))
        except EngagementsError as exc:
            outcomes.append(exc)

    published = pause_final_write(monkeypatch, during_prefix)
    result = create(lib)
    durable = (lib.root / result.path).read_bytes()
    assert len(published) == len(outcomes) == 1
    assert SUMMARY in durable  # both successes previously lost this text
    assert result.rev == fsutil.rev(published[0])
    if isinstance(outcomes[0], EngagementsError):
        assert outcomes[0].code in {"lock_timeout", "malformed", "conflict"}
        assert durable == published[0] and result.rev == fsutil.rev(durable)
    else:
        assert b"status: paused\n" in durable
        assert b"## Unconfirmed\n" in durable
        assert outcomes[0].data["new_rev"] == fsutil.rev(durable)
        [history] = (lib.root / ".engagements/history/acme-bakery").glob("*.md")
        assert history.read_bytes() == published[0]


@pytest.mark.parametrize("target", ["status", "summary"])
def test_waiting_prefix_update_rereads_complete_bytes(lib, monkeypatch, target):
    """A disjoint op rebases; a Summary op conflicts instead of dropping new text."""
    force_fallback(monkeypatch)
    attempted, finished = threading.Event(), threading.Event()
    blocked, outcomes, threads = [], [], []
    real_flock = fsutil.fcntl.flock

    def flock(fd, operation):
        if threading.current_thread() in threads and operation & fsutil.fcntl.LOCK_NB:
            try:
                return real_flock(fd, operation)
            except BlockingIOError:
                blocked.append(True)
                raise
            finally:
                attempted.set()
        return real_flock(fd, operation)

    monkeypatch.setattr(fsutil.fcntl, "flock", flock)

    def during_prefix(prefix):
        entry = lib.resolve("Acme Bakery")
        data, base = writer.read_record(lib, entry.relpath, clock=fixed_clock)
        assert data == prefix
        ops = [op_status()] if target == "status" else [
            {"op": "replace_section", "section": "Summary", "body": "Replacement synthetic summary."}]

        def worker():
            try:
                outcomes.append(update(lib, entry.relpath, base, ops, lock_timeout=5))
            except BaseException as exc:
                outcomes.append(exc)
            finally:
                finished.set()

        thread = threading.Thread(target=worker)
        threads.append(thread)
        thread.start()
        assert attempted.wait(5)
        if not blocked:  # On the unfixed head, force the successful replace before create resumes.
            assert finished.wait(5)

    published = pause_final_write(monkeypatch, during_prefix)
    try:
        result = create(lib)
    finally:
        for thread in threads:
            thread.join(10)
    assert threads and all(not thread.is_alive() for thread in threads)
    assert len(published) == len(outcomes) == 1
    durable = (lib.root / result.path).read_bytes()
    assert SUMMARY in durable
    assert result.rev == fsutil.rev(published[0])
    assert blocked  # The real record lock contended with fallback publication.
    outcome = outcomes[0]
    if target == "status":
        assert not isinstance(outcome, BaseException), repr(outcome)
        assert outcome.data["rebased"] is True
        assert outcome.data["new_rev"] == fsutil.rev(durable)
        assert b"status: paused\n" in durable
        [history] = (lib.root / ".engagements/history/acme-bakery").glob("*.md")
        assert history.read_bytes() == published[0]  # update applied atop the whole create
    else:
        assert isinstance(outcome, EngagementsError)
        assert (outcome.code, outcome.details["reason"]) == ("conflict", "footprint_changed")
        assert durable == published[0]
        assert result.rev == fsutil.rev(durable)
    assert b"## Unconfirmed\n" in durable


def test_fallback_without_update_returns_the_durable_revision(lib, monkeypatch):
    force_fallback(monkeypatch)
    result = create(lib)
    durable = (lib.root / result.path).read_bytes()
    assert SUMMARY in durable and b"## Unconfirmed\n" in durable
    assert result.rev == fsutil.rev(durable)
    assert not list(lib.root.rglob(".engagements-new-*.tmp"))


@pytest.mark.parametrize("fallback", [False, True])
def test_only_fallback_needs_the_record_lock(lib, monkeypatch, fallback):
    """A planted record-lock symlink refuses fallback, not atomic hard-link publication."""
    outside = lib.root.parent / "never-created.lock"
    (lib.root / ".engagements/locks/acme-bakery.lock").symlink_to(outside)
    if fallback:
        force_fallback(monkeypatch)
        with pytest.raises(EngagementsError) as exc:
            create(lib)
        assert (exc.value.code, exc.value.details["reason"]) == ("io_error", "symlink")
        assert not (lib.root / "Acme Bakery").exists()
    else:
        result = create(lib)
        assert result.rev == fsutil.rev((lib.root / result.path).read_bytes())
    assert not outside.exists()


@pytest.mark.skipif(not os.path.isdir("/proc/self/fd"), reason="needs /proc/self/fd")
@pytest.mark.parametrize("fail_write", [False, True])
def test_fallback_leaks_no_descriptors(lib, monkeypatch, fail_write):
    force_fallback(monkeypatch)
    if fail_write:
        def fail(prefix):
            raise OSError(errno.EIO, "synthetic final-write failure")

        pause_final_write(monkeypatch, fail)
    before = len(os.listdir("/proc/self/fd"))
    for _ in range(3):
        if fail_write:
            with pytest.raises(EngagementsError) as exc:
                create(lib)
            assert exc.value.code == "io_error"
            assert not (lib.root / "Acme Bakery").exists()
        else:
            result = lib.create("client", "Riverbend Dental", allow_similar=True,
                                profile="studio-dev", today=fixed_clock)
            assert result.rev == fsutil.rev((lib.root / result.path).read_bytes())
    assert len(os.listdir("/proc/self/fd")) == before
