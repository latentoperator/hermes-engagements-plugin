"""Concurrent creates from separate processes and threads (spec 4.4, A2)."""

import multiprocessing
import threading
from datetime import datetime, timedelta, timezone

from engagements.errors import EngagementsError
from engagements.library import open_library


def _clock():
    return datetime(2026, 11, 7, 14, 2, tzinfo=timezone(timedelta(hours=-6)))


def _worker(root, allow_similar, barrier, queue):
    lib = open_library(root, create_default=True)
    barrier.wait(10)
    try:
        result = lib.create("client", "Acme Bakery", allow_similar=allow_similar, profile="studio-dev", today=_clock)
        queue.put(("ok", result.id, result.path))
    except EngagementsError as exc:
        queue.put(("error", exc.code, None))


def _race(root, allow_similar, workers=2):
    ctx = multiprocessing.get_context("spawn")
    barrier = ctx.Barrier(workers)
    queue = ctx.Queue()
    procs = [ctx.Process(target=_worker, args=(str(root), allow_similar, barrier, queue)) for _ in range(workers)]
    for p in procs:
        p.start()
    results = [queue.get(timeout=30) for _ in procs]
    for p in procs:
        p.join(30)
        assert p.exitcode == 0
    return results


def test_two_processes_creating_the_same_client_with_allow_similar_get_distinct_records(tmp_path):
    for round_ in range(4):
        root = tmp_path / f"library-{round_}"
        open_library(root, create_default=True)
        results = _race(root, allow_similar=True)
        assert [r[0] for r in results] == ["ok", "ok"], results
        ids = {r[1] for r in results}
        paths = {r[2] for r in results}
        assert ids == {"acme-bakery", "acme-bakery-2"}
        assert paths == {"Acme Bakery/Client.md", "Acme Bakery (2)/Client.md"}
        for path in paths:
            assert (root / path).is_file()
        lib = open_library(root, create_default=False)
        assert sorted(e.id for e in lib.discover().entries) == ["acme-bakery", "acme-bakery-2"]
        assert all(e.valid for e in lib.discover().entries)


def test_two_processes_without_allow_similar_exactly_one_wins(tmp_path):
    for round_ in range(4):
        root = tmp_path / f"library-{round_}"
        open_library(root, create_default=True)
        results = _race(root, allow_similar=False)
        assert sorted(r[0] for r in results) == ["error", "ok"], results
        assert next(r for r in results if r[0] == "error")[1] == "similar_exists"
        assert len(open_library(root, create_default=False).discover().entries) == 1


def test_first_use_race_between_processes_creates_one_library(tmp_path):
    root = tmp_path / "fresh"
    results = _race(root, allow_similar=True, workers=4)  # each worker opens with create_default=True
    assert [r[0] for r in results] == ["ok"] * 4
    lib = open_library(root, create_default=False)
    assert len(lib.discover().entries) == 4
    assert sorted(e.id for e in lib.discover().entries) == [
        "acme-bakery", "acme-bakery-2", "acme-bakery-3", "acme-bakery-4"]


def test_threads_in_one_process_exclude_each_other(tmp_path):
    lib = open_library(tmp_path / "library", create_default=True)
    outcomes = []
    barrier = threading.Barrier(6)

    def go():
        barrier.wait()
        try:
            outcomes.append(lib.create("client", "Acme Bakery", profile="p", today=_clock).id)
        except EngagementsError as exc:
            outcomes.append(exc.code)

    threads = [threading.Thread(target=go) for _ in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(outcomes) == ["acme-bakery"] + ["similar_exists"] * 5


def test_threads_with_allow_similar_all_succeed_with_distinct_ids(tmp_path):
    lib = open_library(tmp_path / "library", create_default=True)
    outcomes = []
    barrier = threading.Barrier(6)

    def go():
        barrier.wait()
        outcomes.append(lib.create("client", "Acme Bakery", allow_similar=True, profile="p", today=_clock))

    threads = [threading.Thread(target=go) for _ in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len({o.id for o in outcomes}) == 6 and len({o.path for o in outcomes}) == 6
