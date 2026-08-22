"""
runhelper.py
------------
Single source of truth for launching a test run.

Both the legacy HTML route (`/run/<testname>` in app.py) and the REST surface
(`POST /api/v1/runs` in apiv1.py) call `launch_run()` so a run behaves
identically no matter how it was started — this is what keeps the app
"consistent from run to report".

The test engine is single-run (one global `progress_state`). `launch_run`
enforces that: if a run is already in flight it raises `RunBusyError`, which
the API layer turns into HTTP 409.
"""

import datetime
import threading

import apphelpers
import test_runner
import testid
from appstate import progress_state, run_registry
from dbhelper import db


class RunBusyError(Exception):
    """Raised when a run is requested while another is still in progress."""
    pass


def is_busy() -> bool:
    return bool(progress_state.testname)


def _new_run_id(test_id: str) -> str:
    # Timestamp keeps run ids sortable and human-readable; the short module
    # tail disambiguates runs started within the same second (rare given the
    # single-run guard, but harmless).
    ts   = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    tail = test_id.rsplit(".", 1)[-1]
    return f"{ts}_{tail}"


def launch_run(test_id: str) -> str:
    """Start `test_id` in a background thread and return a run_id handle.

    Raises RunBusyError if another run is already active.

    The worker records its outcome (status + report_id/report_path) into
    run_registry on completion, so `GET /api/v1/runs/<run_id>` can move from
    live progress to a finished result with a link to the JSON report.
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

    def _worker():
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
            run_registry.finish(run_id, "error", error=str(e))
        finally:
            progress_state.step     = "Done"
            progress_state.testname = None

    threading.Thread(target=_worker, daemon=True).start()
    return run_id
