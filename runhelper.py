"""
runhelper.py
------------
"""

import datetime
import threading

import appstate
import apphelpers
import test_runner
import apphelpers as testid
from appstate import progress_state, run_registry, batch_registry, process_registry
from dbhelper import db


class RunBusyError(Exception):
    """Raised when a run is requested while another is still in progress."""
    pass


def is_busy() -> bool:
    # A batch holds the engine busy between its items too, even though
    # progress_state briefly goes idle there -- otherwise a run launched from
    # elsewhere could jump the queue mid-batch.
    return bool(progress_state.testname) or bool(batch_registry.current())


def _new_run_id(test_id: str) -> str:
    # Timestamp keeps run ids sortable and human-readable; the short module
    # tail disambiguates runs started within the same second (rare given the
    # single-run guard, but harmless).
    ts   = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    tail = test_id.rsplit(".", 1)[-1]
    return f"{ts}_{tail}"


def _new_batch_id(container: str) -> str:
    ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    return f"{ts}_{container}_batch"


def _execute(test_id: str, run_id: str) -> str:
    """Run one already-registered run_id and record its outcome.

    Shared by launch_run's worker and the batch worker in launch_batch so a
    single test behaves identically whether it's run alone or as one item in
    a batch. Assumes the caller has already called progress_state for this run_id
    """
    try:
        results = test_runner.run_testfile(test_id, progress_state)

        # The run just inserted (at most) one report row for this test.
        # Since runs are serialised, the newest report is ours.
        report_id = report_path = None
        if results:
            recent = db.list_reports(limit=1)
            if recent:
                report_id   = recent[0]["report_id"]
                report_path = recent[0]["path"]

        status = "done"
        if results:
            status = "error" if any(
                (r[1] or "").upper() in ("FAIL", "ERROR") for r in results
            ) else "done"

        run_registry.finish(run_id, status,
                             report_id=report_id, report_path=report_path)
    except Exception as e:
        status = "error"
        run_registry.finish(run_id, "error", error=str(e))
    finally:
        progress_state.step     = "Done"
        progress_state.testname = None
        progress_state.run_id   = ""
    return status


def launch_run(test_id: str) -> str:
    """Start `test_id` in a background thread and return a run_id handle.

    Raises RunBusyError if another run is already active.

    The worker records its outcome into
    run_registry on completion
    """
    if is_busy():
        raise RunBusyError(f"A run is already in progress: {progress_state.testname}")

    # Accept either id form. test_runner.run_testfile() looks the id up in
    # testfile_registry, which is keyed by module dot-path, so a slug that
    # reaches it finds nothing and the run dies with no usable error. Resolve
    # here, at the shared choke point, so every caller behaves the same:
    # apiv1 already resolves before calling in, and resolving a dot-path is a
    # no-op, so this only ever helps.
    test_id = testid.resolve(test_id, apphelpers.testfile_registry.keys()) or test_id

    run_id = _new_run_id(test_id)
    run_registry.create(run_id, test_id)

    # Seed progress the same way the old route did.
    progress_state.step     = "0/0"
    progress_state.testname = test_id
    progress_state.run_id   = run_id

    def _worker():
        _execute(test_id, run_id)

    threading.Thread(target=_worker, daemon=True).start()
    return run_id


def launch_batch(container: str, mode: str, items: list) -> str:
    """Run several test_ids sequentially (a container's Run All / Run Failed)
    and return a batch_id handle.
    """
    if is_busy():
        raise RunBusyError(f"A run is already in progress: {progress_state.testname}")
    if not items:
        raise ValueError("No tests to run for this batch")

    resolved = [testid.resolve(t, apphelpers.testfile_registry.keys()) or t for t in items]
    batch_id = _new_batch_id(container)
    batch_registry.create(batch_id, container, mode, resolved)

    def _worker():
        for i, test_id in enumerate(resolved):
            if batch_registry.stop_requested(batch_id):
                break
            test_runner.reload_tests()

            run_id = _new_run_id(test_id)
            run_registry.create(run_id, test_id)
            batch_registry.set_current(batch_id, i, run_id)

            progress_state.step     = "0/0"
            progress_state.testname = test_id
            progress_state.run_id   = run_id

            status = _execute(test_id, run_id)
            batch_registry.record_result(batch_id, test_id, run_id, status)

        batch_registry.finish(
            batch_id, "stopped" if batch_registry.stop_requested(batch_id) else "done")

    threading.Thread(target=_worker, daemon=True).start()
    return batch_id


def request_stop() -> bool:
    """stop running process called by test. returns false if nothing found
    """
    stopped_something = False

    # live_context is deliberately never cleared when a run ends (a test may
    # leave its VM up on purpose), so only touch its `abort` flag while a run
    # is actually in progress -- otherwise this would report "stopped" for a
    # stale context left over from the last finished run.
    if progress_state.testname:
        ctx = appstate.live_context.context()
        if ctx:
            ctx["abort"] = True
            stopped_something = True

    batch = batch_registry.current()
    if batch:
        batch_registry.request_stop(batch["batch_id"])
        stopped_something = True

    process_registry.kill_all()

    return stopped_something
