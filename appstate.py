# this is in its own file so it can be imported easily
import os
import re
import threading
import time
from markupsafe import escape, Markup

class ProgressState:
    def __init__(self):
        self.step = "Idle"
        self.testname = ""   # dot path old
        self.testid = ""     # new - name with spaces included
        self.testtype = ""   # info like build test or test1 test type - for button label
        self.step_name = ""
        self.run_id = ""     # set by runhelper.launch_run(); see current_reports_dir()

progress_state = ProgressState()

REPORTS_DIR = "/testrunnerapp/reports"


def current_reports_dir() -> str:
    """Where in-flight screenshots/gifs/logs for the active run should be written.

    Keyed by run_id into its own subdirectory so two runs with a shared testrunner dir writing to this same
    mount at once do not collide. 
    """
    run_id = progress_state.run_id
    d = os.path.join(REPORTS_DIR, run_id) if run_id else REPORTS_DIR
    os.makedirs(d, exist_ok=True)
    return d


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

    def kill_all(self, escalate_after: float = 3.0) -> list[str]:
        """Terminate every registered process; SIGKILL whatever ignores it.

        register()/unregister() below are what actually populate this now
        (every emulator backend registers on start() / unregisters on
        stop()). a stop-click reaches real PIDs instead
        """
        with self._rlock:
            entries = list(self._procs.items())
            for k, v in entries:
                try:
                    v["proc"].terminate()
                except Exception:
                    pass

        killed = [k for k, v in entries]
        deadline = time.time() + escalate_after
        for k, v in entries:
            remaining = deadline - time.time()
            try:
                if remaining > 0:
                    v["proc"].wait(timeout=max(0, remaining))
            except Exception:
                try:
                    v["proc"].kill()
                    v["proc"].wait(timeout=2)
                except Exception:
                    pass

        with self._rlock:
            for k, _ in entries:
                self._procs.pop(k, None)
        return killed

process_registry = ProcessRegistry()


def register(name, proc, source=""):
    try:
        process_registry.register(name, proc, source=source)
    except Exception:
        pass


def unregister(name):
    try:
        process_registry.unregister(name)
    except Exception:
        pass


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


class BatchRegistry:
    """Tracks in-flight batch runs

    Same shape as RunRegistry (rows keyed by id, singleton + thread-locked)
    but also tracks which item the batch is currently on and whether a stop
    has been requested
    """
    _instance = None
    _lock = threading.Lock()

    def __new__(cls):
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = super().__new__(cls)
                    cls._instance._batches = {}
                    cls._instance._rlock = threading.RLock()
        return cls._instance

    def create(self, batch_id: str, container: str, mode: str, items: list) -> dict:
        with self._rlock:
            row = {
                "batch_id":        batch_id,
                "container":       container,
                "mode":            mode,
                "items":           list(items),
                "index":           -1,
                "current_run_id":  None,
                "results":         [],
                "status":          "running",
                "stop_requested":  False,
            }
            self._batches[batch_id] = row
            return dict(row)

    def set_current(self, batch_id: str, index: int, run_id: str) -> None:
        with self._rlock:
            row = self._batches.get(batch_id)
            if row:
                row["index"] = index
                row["current_run_id"] = run_id

    def record_result(self, batch_id: str, test_id: str, run_id: str, status: str) -> None:
        with self._rlock:
            row = self._batches.get(batch_id)
            if row:
                row["results"].append({"test_id": test_id, "run_id": run_id, "status": status})

    def request_stop(self, batch_id: str) -> bool:
        with self._rlock:
            row = self._batches.get(batch_id)
            if not row:
                return False
            row["stop_requested"] = True
            return True

    def stop_requested(self, batch_id: str) -> bool:
        with self._rlock:
            row = self._batches.get(batch_id)
            return bool(row and row["stop_requested"])

    def finish(self, batch_id: str, status: str) -> None:
        with self._rlock:
            row = self._batches.get(batch_id)
            if row:
                row["status"] = status

    def get(self, batch_id: str):
        with self._rlock:
            row = self._batches.get(batch_id)
            return dict(row) if row else None

    def current(self):
        """The most recently created batch still marked running, or None.
        """
        with self._rlock:
            running = [r for r in self._batches.values() if r["status"] == "running"]
            return dict(running[-1]) if running else None

batch_registry = BatchRegistry()


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
            items.append({
                "name": label,
                "endpoint": endpoint,
                "align": getattr(view, "nav_align", "left"),
            })
    return items

def nav(label, align="left"):
    def decorator(f):
        f.nav_label = label
        f.nav_align = align
        return f
    return decorator


# tiny hand-editable text format for the info page: plain text with
# blank-line paragraphs, "# "/"## " headers, "- " list items,
# **bold**, *italic*, and bare URLs auto-linked. No external deps.
_INFO_URL_RE = re.compile(r"(https?://[^\s<]+)")
_INFO_BOLD_RE = re.compile(r"\*\*(.+?)\*\*")
_INFO_ITALIC_RE = re.compile(r"\*(.+?)\*")

def _render_info_line(line):
    line = str(escape(line))
    line = _INFO_URL_RE.sub(
        lambda m: f'<a href="{m.group(1)}" target="_blank" rel="noopener noreferrer">{m.group(1)}</a>',
        line,
    )
    line = _INFO_BOLD_RE.sub(r"<strong>\1</strong>", line)
    line = _INFO_ITALIC_RE.sub(r"<em>\1</em>", line)
    return line

def render_info_markup(text):
    if not text:
        return Markup("")

    html = []
    list_open = False
    blank_count = 0
    for raw_line in text.splitlines():
        line = raw_line.strip()

        if not line:
            blank_count += 1
            continue

        if blank_count > 1:
            if list_open:
                html.append("</ul>")
                list_open = False
            html.append('<div class="info-spacer"></div>' * (blank_count - 1))
        blank_count = 0

        header_match = re.match(r"^(#{1,3})\s+(.*)$", line)
        if header_match:
            if list_open:
                html.append("</ul>")
                list_open = False
            level = len(header_match.group(1))
            html.append(f"<h{level}>{_render_info_line(header_match.group(2))}</h{level}>")
            continue

        if line.startswith("- "):
            if not list_open:
                html.append("<ul>")
                list_open = True
            html.append(f"<li>{_render_info_line(line[2:])}</li>")
            continue

        if list_open:
            html.append("</ul>")
            list_open = False
        html.append(f"<p>{_render_info_line(line)}</p>")

    if list_open:
        html.append("</ul>")

    return Markup("\n".join(html))