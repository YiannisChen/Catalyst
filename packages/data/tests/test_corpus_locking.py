from __future__ import annotations

import multiprocessing

from catalyst_data.corpus.locking import acquire_corpus_lock, release_corpus_lock


def _hold_lock(db_path: str, acquired, release) -> None:
    fd = acquire_corpus_lock(db_path)
    if fd is None:
        acquired.set()
        return
    acquired.set()
    release.wait(10)
    release_corpus_lock(fd)


def test_corpus_lock_is_exclusive_across_processes(tmp_path):
    db_path = tmp_path / "served.sqlite3"
    db_path.touch()
    context = multiprocessing.get_context("spawn")
    acquired, release = context.Event(), context.Event()
    process = context.Process(target=_hold_lock, args=(str(db_path), acquired, release))
    process.start()
    try:
        assert acquired.wait(5)
        assert acquire_corpus_lock(db_path) is None
    finally:
        release.set()
        process.join(5)
    assert process.exitcode == 0


def test_lock_path_is_adjacent_to_served_corpus(tmp_path):
    from catalyst_data.corpus.locking import corpus_lock_path

    assert corpus_lock_path(tmp_path / "served.sqlite3") == tmp_path / "served.sqlite3.fts.lock"
