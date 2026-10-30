"""Store transactions remain mutually exclusive across real processes."""
import multiprocessing

import pytest

from stardew_ai_runtime.file_lock import lock_file, unlock_file
from stardew_ai_runtime.work_state import WorkStore


def _record_requests(path, prefix, ready, finished):
    store = WorkStore(path)
    ready.set()
    for number in range(12):
        store.record_request("farm", f"{prefix}-{number}", "Keep this request")
    finished.set()


def test_exception_releases_lock_and_concurrent_stores_keep_both_writers(tmp_path):
    path = tmp_path / "work-state.json"
    ctx = multiprocessing.get_context("spawn")
    signals = [(ctx.Event(), ctx.Event()) for _ in range(2)]
    processes = [ctx.Process(target=_record_requests, args=(path, str(i), *signals[i]))
                 for i in range(2)]
    try:
        with path.with_suffix(".json.lock").open("a+b") as handle:
            with pytest.raises(RuntimeError, match="transaction failed"):
                lock_file(handle)
                try:
                    for process in processes:
                        process.start()
                    for ready, finished in signals:
                        assert ready.wait(10)
                        assert not finished.wait(0.1), "writer escaped the held file lock"
                    raise RuntimeError("transaction failed")
                finally:
                    unlock_file(handle)
        for process in processes:
            process.join(15)
            assert process.exitcode == 0
        requests = WorkStore(path).state("farm").requests
        assert {request["id"] for request in requests} == {
            f"{prefix}-{number}" for prefix in ("0", "1") for number in range(12)
        }
    finally:
        for process in processes:
            if process.is_alive():
                process.terminate()
                process.join(5)
