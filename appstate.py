# this is in its own file so it can be imported easily
import threading

class ProgressState:
    def __init__(self):
        self.step = "Idle"
        self.testname = ""   # dot path old
        self.testid = ""     # new - name with spaces included
        self.testtype = ""   # info like build test or test1 test type - for button label
        self.step_name = ""

progress_state = ProgressState()


class ProcessRegistry:
    _instance = None
    _lock = threading.Lock()

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._procs = {}
            cls._instance._rlock = threading.RLock()
        return cls._instance

# appstate.py - updated ProcessRegistry methods

    def register(self, key: str, proc, source: str = "") -> None:
        with self._rlock:
            self._procs[key] = {"proc": proc, "pid": proc.pid, "source": source}

    def unregister(self, key: str) -> None:
        with self._rlock:
            self._procs.pop(key, None)

    def alive(self) -> dict[str, bool]:
        with self._rlock:
            status = {k: (v["proc"].poll() is None) for k, v in self._procs.items()}
            for k, is_alive in list(status.items()):
                if not is_alive:
                    self._procs.pop(k)
            return status

    def snapshot(self) -> list[dict]:
        """Returns a serializable list of current process info."""
        with self._rlock:
            return [
                {"name": k, "pid": v["pid"], "source": v["source"], "alive": v["proc"].poll() is None}
                for k, v in self._procs.items()
            ]

    def any_alive(self) -> bool:
        return any(self.alive().values())

process_registry = ProcessRegistry()


class RunRegistry:
    """Tracks test runs by run_id so a run started over the API has a durable
    handle that outlives the fire-and-forget worker thread.

    A run row: {run_id, test_id, status, started_at, finished_at,
                report_id, report_path, error}
    status is one of: "running", "done", "error".

    Singleton + thread-locked, mirroring ProcessRegistry above. Kept in memory
    only (cleared on process restart); the durable record of a completed run is
    the report row in report.sqlite, which `report_id` points at.
    """
    _instance = None
    _lock = threading.Lock()

    def __new__(cls):
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = super().__new__(cls)
                    cls._instance._runs = {}
                    cls._instance._rlock = threading.RLock()
        return cls._instance

    def create(self, run_id: str, test_id: str) -> dict:
        import time as _time
        with self._rlock:
            row = {
                "run_id":      run_id,
                "test_id":     test_id,
                "status":      "running",
                "started_at":  _time.time(),
                "finished_at": None,
                "report_id":   None,
                "report_path": None,
                "error":       None,
            }
            self._runs[run_id] = row
            return dict(row)

    def finish(self, run_id: str, status: str, report_id=None,
               report_path=None, error=None) -> None:
        import time as _time
        with self._rlock:
            row = self._runs.get(run_id)
            if not row:
                return
            row["status"]      = status
            row["finished_at"] = _time.time()
            row["report_id"]   = report_id
            row["report_path"] = report_path
            row["error"]       = error

    def get(self, run_id: str):
        with self._rlock:
            row = self._runs.get(run_id)
            return dict(row) if row else None

    def snapshot(self) -> list:
        with self._rlock:
            return [dict(v) for v in self._runs.values()]

run_registry = RunRegistry()


class LiveContextRegistry:
    """manages current run context dict so API request can reach
    the live emulator objects

    some tests are stashed as `context[name] = instance` and the
    context is stored in the test_runner.run_testfile. testlist py file
    """
    _instance = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._ctx = None
            cls._instance._module = None
            cls._instance._rlock = threading.RLock()
        return cls._instance

    def publish(self, context: dict, module_name: str = "") -> None:
        with self._rlock:
            self._ctx = context
            self._module = module_name

    def clear(self) -> None:
        with self._rlock:
            self._ctx = None
            self._module = None

    def context(self) -> dict:
        """The live context dict, or {} when no run is active."""
        with self._rlock:
            return self._ctx if isinstance(self._ctx, dict) else {}

    def module(self) -> str:
        with self._rlock:
            return self._module or ""

live_context = LiveContextRegistry()


# nav bar button builder
def build_nav(app):
    items = []
    for endpoint, view in app.view_functions.items():
        label = getattr(view, "nav_label", None)
        if label:
            items.append({"name": label, "endpoint": endpoint})
    return items

def nav(label):
    def decorator(f):
        f.nav_label = label
        return f
    return decorator