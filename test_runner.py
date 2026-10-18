import os
import sys
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
sys.pycache_prefix = os.path.join(BASE_DIR, "pycache")
import time
import re
import shutil
import datetime
import glob
import importlib
from collections import defaultdict
import reporthelper
import appstate
from appstate import progress_state
from dbhelper import db
import stepplan
import runsession
import drafthelper
import apphelpers
import dispatchhelper
import buildhelper
import apphelpers as testid

TESTSRC_HELPERDIR = "/testsrc/helpers"
TESTSRC_BASEDIR = "/testsrc/"
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
REPORT_DIR = os.path.join(BASE_DIR, "reports")
compile_logs_dir = os.path.join(BASE_DIR, "compile_logs")
DB_PATH = os.path.join(BASE_DIR, "report.sqlite")
TESTSRC_ROOT = "/testsrc/sourcedir"
# How many directory levels below TESTSRC_ROOT to look for a __testparent__.py
# before giving up on a branch. Covers both the 1-deep (flask-qemu-automation)
# and 2-deep (c64/cc65) source trees; scanning stops the moment a marker is
# found, so raising this doesn't cost anything for shallower trees.
PROJECT_SCAN_MAX_DEPTH = 3

failed_loads = []
import importlib.util

root_parent = os.path.dirname(TESTSRC_ROOT)
if root_parent not in sys.path:
    sys.path.insert(0, root_parent)
    
context = {
    "sock": None,
    "abort": False
}

TESTLIST_PREFIXES = ("__testlist__")

# A step that succeeded (truthy first element of its (bool, str) return) can
# still opt into WARN instead of PASS by printing a line starting with this
# marker in its message -- same convention reporthelper.py already uses for
# "ARTIFACT:" lines. This keeps the step-function contract untouched: every
# existing dispatch_functions.py function's plain True/False keeps meaning
# exactly PASS/FAIL, and nothing needs to widen its return type to pick up
# WARN. A failing step (success is falsy) is never upgraded by this -- WARN
# describes an expected, measured property of a passing run, not a softened
# failure.
WARN_MARKER = "TESTRUNNER_WARN:"


def _has_warn_marker(text):
    if not text:
        return False
    return any(line.strip().startswith(WARN_MARKER) for line in text.splitlines())


class TestrunnerTimer:
    start_times = {}
    stop_times = {}

    @classmethod
    def set_start(cls, test_name, ts):
        cls.start_times[test_name] = ts

    @classmethod
    def set_stop(cls, test_name, ts):
        cls.stop_times[test_name] = ts

    @classmethod
    def get_start(cls, test_name):
        return cls.start_times.get(test_name)

    @classmethod
    def get_stop(cls, test_name):
        return cls.stop_times.get(test_name)




def load_testfile_from_path(fpath):
    global failed_loads
    rel = os.path.relpath(fpath, TESTSRC_ROOT)
    modname = os.path.splitext(rel)[0].replace(os.sep, ".")
    try:
        spec = importlib.util.spec_from_file_location(modname, fpath)
        mod = importlib.util.module_from_spec(spec)
        sys.modules[modname] = mod
        spec.loader.exec_module(mod)
        return mod
    except Exception as e:
        print(f"IMPORT ERROR: {modname}: {e}")
        failed_loads.append({"module": modname, "error": str(e)})
        if modname in sys.modules:
            del sys.modules[modname]
        return None


def load_config_from_path(fpath):
    """Exec a testlist file in isolation and return its CONFIG dict (or None).

    For a testlist whose CONFIG is computed rather than a pure literal, this is
    the only way to see the real steps -- static parsing cannot run the helper
    that builds them. Executing calls init_test_env, which registers the module
    under whatever __name__ it is given, so the registry is snapshotted and
    restored to keep a throwaway entry out of the test list.
    """
    if not fpath or not os.path.exists(fpath):
        return None

    modname = "_configprobe_%s" % abs(hash(fpath))
    with apphelpers.registry_lock:
        saved_registry = dict(apphelpers.testfile_registry)
        try:
            spec = importlib.util.spec_from_file_location(modname, fpath)
            if not (spec and spec.loader):
                return None
            mod = importlib.util.module_from_spec(spec)
            sys.modules[modname] = mod
            spec.loader.exec_module(mod)
            cfg = getattr(mod, "CONFIG", None)
            return cfg if isinstance(cfg, dict) else None
        except Exception as e:
            print(f"[load_config_from_path] load error for {fpath}: {e}")
            return None
        finally:
            sys.modules.pop(modname, None)
            apphelpers.testfile_registry.clear()
            apphelpers.testfile_registry.update(saved_registry)


# What the last full reload_tests() saw on disk, and the registry it built:
# (signature, frozenset of module names). A reload whose scan matches both is
# skipped -- re-importing every testlist costs hundreds of NFS round-trips and
# every page load used to pay it, often twice.
_last_load = None


def _scan_testlists():
    """Walk TESTSRC_ROOT for project dirs and their testlists.

    Returns ([(project_dir, [testlist fnames])], signature). The signature is
    every .py file's (path, mtime, size) in each project dir -- testlists, the
    __testparent__.py stub and __testplan__.py alike, since an edit to any of
    them changes what a reload produces. A new, removed or edited file changes
    it; nothing else is read.
    """
    # A project dir is any directory under TESTSRC_ROOT that has a
    # __testparent__.py beside its testlists. Different source trees nest
    # these at different depths
    project_dirs = []

    def _scan_for_project_dirs(dirpath, depth):
        if os.path.isfile(os.path.join(dirpath, apphelpers.TESTPARENT_FILE)):
            project_dirs.append(dirpath)
            return
        if depth >= PROJECT_SCAN_MAX_DEPTH:
            return
        try:
            entries = os.scandir(dirpath)
        except OSError:
            return
        with entries:
            subdirs = [e.path for e in entries if e.is_dir(follow_symlinks=False)]
        for subdir in subdirs:
            _scan_for_project_dirs(subdir, depth + 1)

    try:
        top_dirs = sorted(e.path for e in os.scandir(TESTSRC_ROOT)
                           if e.is_dir(follow_symlinks=False))
    except OSError:
        top_dirs = []

    for top_dir in top_dirs:
        _scan_for_project_dirs(top_dir, 1)
    project_dirs.sort()

    projects, signature = [], []
    for project_dir in project_dirs:
        try:
            entries = os.scandir(project_dir)
        except OSError:
            continue
        with entries:
            files = [e for e in entries if e.is_file(follow_symlinks=False)]
        for e in files:
            if e.name.endswith(".py"):
                try:
                    st = e.stat(follow_symlinks=False)
                    signature.append((e.path, st.st_mtime_ns, st.st_size))
                except OSError:
                    signature.append((e.path, None, None))
        projects.append((project_dir, sorted(e.name for e in files)))
    return projects, tuple(sorted(signature))


def ensure_loaded():
    """Full reload only if the registry isn't the one the last reload built.

    No disk scan: for readers that just need a populated registry and the
    last reload's failed_loads (run_testfile() empties both on every run).
    """
    with apphelpers.registry_lock:
        if _last_load is not None and _last_load[1] == frozenset(apphelpers.testfile_registry):
            return
    reload_tests(force=True)


def reload_tests(force=False):
    """Rebuild testfile_registry from disk, unless nothing changed.

    Skipped when the testlist files on disk match the last reload AND the
    registry still holds what that reload built (run_testfile() clears it and
    imports only the module it runs). force=True always rebuilds.
    """
    global failed_loads, _last_load
    # The walk happens outside the lock so concurrent readers aren't held up
    # by directory I/O; only the compare-and-rebuild is serialized.
    projects, signature = _scan_testlists()

    # Serialize the whole clear-then-rebuild against any other thread that
    # reloads or reads the registry -- see apphelpers.registry_lock.
    with apphelpers.registry_lock:
        if (not force and _last_load is not None
                and _last_load[0] == signature
                and _last_load[1] == frozenset(apphelpers.testfile_registry)):
            return

        failed_loads.clear()
        apphelpers.clear_registries()

        if TESTSRC_ROOT not in sys.path:
            sys.path.insert(0, TESTSRC_ROOT)

        seen_modules = set()
        for project_dir, fnames in projects:
            for fname in fnames:
                if fname == "__init__.py":
                    continue
                # Extension check, not just the prefix: a compiled
                # "__testlist__foo.cpython-313.pyc" starts with the marker too.
                if not (fname.startswith(TESTLIST_PREFIXES) and fname.endswith(".py")):
                    continue
                fpath = os.path.join(project_dir, fname)

                # module name from relative path
                rel_path = os.path.relpath(fpath, TESTSRC_ROOT)
                modname = rel_path.replace(os.sep, ".")[:-3]

                if modname not in seen_modules:
                    # Load the module first so decorators populate the registry
                    load_testfile_from_path(fpath)

                # Only set __full_path__ if the module registered itself
                if modname in apphelpers.testfile_registry:
                    apphelpers.testfile_registry[modname]["__full_path__"] = os.path.abspath(fpath)

                seen_modules.add(modname)

        _last_load = (signature, frozenset(apphelpers.testfile_registry))


def run_registered_test(name, registry, context):
    # this runs each individual decorated test step
    for test_func in registry:
        if test_func.test_description == name:
            info = getattr(test_func, "test_info", "")
            try:
                if context.get("abort"):
                    return (name, "FAIL", "red", "Aborted due to previous failure", "", 0.00, info)

                print(f"Running {name}")
                start_time = time.time()
                result = test_func(context)
                duration = time.time() - start_time
                log_output = ""
                stdout_output = ""
                
                if isinstance(result, tuple):
                    success = result[0]
                    log_output = result[1]
                    stdout_output = result[2] if len(result) == 3 else ""
                else:
                    success = result # Fallback if test returns a single value

                if context.get("abort") is True:
                    status = "FAIL"
                    color = "red"
                else:
                    if success:
                        if _has_warn_marker(log_output):
                            status = "WARN"
                            color = "warn"
                        else:
                            status = "PASS"
                            color = "green"
                    else:
                        status = "FAIL"
                        color = "red"

            except Exception as e:
                status = "ERROR"
                log_output = str(e)
                stdout_output = ""
                color = "gray"
                duration = 0.00

            return (name, status, color, log_output, stdout_output, duration, info)

    return (name, "NOT FOUND", "gray", "No matching test found", "", 0.00, "")


def run_testfile(module_name, state=None, options=None):
    """Run one testlist. `options` (all optional) carries the interactive-run
    knobs from POST /api/v1/runs: interactive (False for batch items),
    source (lkg|draft), mode, on_fault, suspend_timeout (minutes),
    suspend_timeout_action -- see runsession.RunSession."""
    global failed_loads
    failed_loads.clear()
    
    all_tests = []
    test_descriptions = []
    context = {"sock": None, "abort": False, "abort_sticky": False}
    # Publish the context so /api/v1/instances can reach the live emulator
    # objects the steps put in it. Deliberately NOT cleared when the run ends:
    # a test commonly leaves its VM up on purpose, and that instance stays
    # viewable until the next run replaces the context.
    appstate.live_context.publish(context, module_name)

    with apphelpers.registry_lock:
        meta = apphelpers.testfile_registry.get(module_name)
        if not meta:
            print(f"ERROR: {module_name} not found in testfile_registry")
            if state:
                state.step, state.test_name = "Error", "No registry entry"
            return []

        full_path = meta.get("__full_path__")
        if not full_path:
            print(f"ERROR: no __full_path__ for {module_name}")
            return []

        apphelpers.clear_registries()
        mod = load_testfile_from_path(full_path)
    
    session = None
    # Which container this run belongs to, for build tracking. A/B runs
    # (options["build_override"]) swap one CONFIG key for a registered
    # build's snapshot; every run, A/B or not, records which build it used.
    container_key = testid.container_id(meta.get("id"), meta.get("system"))
    build_override = (options or {}).get("build_override")
    # "Build A"/"Build B" on a build_tool: the opposite direction from
    # build_override -- there's no existing build to swap in, this run IS
    # what produces one. Slot's stored input (buildhelper.set_build_input)
    # is merged onto CONFIG before the run, and the result is registered
    # straight into that slot below once the run passes.
    build_input_slot = (options or {}).get("build_input_slot")
    build_attr = None
    if mod and hasattr(mod, "CONFIG") and "steps" in mod.CONFIG:
        config = mod.CONFIG
        if build_input_slot:
            input_overrides = buildhelper.get_build_inputs(container_key).get(build_input_slot) or {}
            if input_overrides:
                config = {**config, **input_overrides}
        override_build = None
        if build_override:
            try:
                config, override_build = buildhelper.apply_override(config, build_override)
            except buildhelper.BuildError as e:
                print(f"ERROR: {module_name}: {e}")
                if state:
                    state.step, state.test_name = "Error", str(e)
                return []
        dispatch = dispatchhelper.load_step_dispatch("/testsrc/pyhelpers")
        # The container plan is captured here, once: expansion inside the
        # session always uses this copy, so an edit to __testplan__.py
        # mid-run cannot reach a run already past its boot line.
        try:
            plan = (stepplan.load_plan(os.path.dirname(os.path.abspath(full_path)))
                    if stepplan.has_references(config) else None)
            # strict: a misspelt template id aborts the run rather than
            # quietly running a shorter test, which would look like a pass.
            session = runsession.RunSession(
                run_id=getattr(state, "run_id", "") or module_name,
                module=module_name, full_path=full_path, config=config,
                raw_steps=config.get("steps") or [], plan=plan,
                dispatch=dispatch, context=context,
                interactive=bool((options or {}).get("interactive", True)),
                # No explicit source: an interactive run uses whichever
                # version the user made active; batch items always the .py.
                source=((options or {}).get("source")
                        or ("draft" if (options or {}).get("interactive", True)
                            and drafthelper.is_active(full_path) else "lkg")),
                mode=(options or {}).get("mode"),
                on_fault=(options or {}).get("on_fault"),
                suspend_timeout=(options or {}).get("suspend_timeout"),
                suspend_timeout_action=(options or {}).get("suspend_timeout_action"),
            )
        except (stepplan.PlanError, ValueError) as e:
            print(f"ERROR: {module_name}: {e}")
            if state:
                state.step, state.test_name = "Error", str(e)
            return []
        if override_build is not None:
            try:
                buildhelper.check_referenced(session.config, override_build, session.plan)
            except buildhelper.BuildError as e:
                print(f"ERROR: {module_name}: {e}")
                if state:
                    state.step, state.test_name = "Error", str(e)
                return []
        try:
            build_attr = buildhelper.attribute(container_key, session.config, build_override)
            if build_input_slot:
                # Force the slot label -- attribute()'s auto-detect only
                # matches an EXISTING file's md5 against a registered build,
                # which for a build_tool is still the previous run's binary
                # at this point (this one hasn't compiled yet).
                build_attr = dict(build_attr or {}, slot=build_input_slot)
        except Exception as e:
            print(f"[buildhelper] attribution failed for {module_name}: {e}")
        runsession.sessions.add(session)
    else:
        if build_override:
            print(f"ERROR: {module_name}: A/B build override needs a CONFIG-steps testlist")
            if state:
                state.step, state.test_name = "Error", "build override unsupported"
            return []
        for registry in apphelpers.registry_map.values():
            for f in registry:
                desc = getattr(f, "test_description", None)
                if desc and f not in all_tests:
                    all_tests.append(f)
                    test_descriptions.append(desc)

    config = mod.CONFIG if (mod and hasattr(mod, "CONFIG")) else {}
    if mod and hasattr(mod, "CONFIG"):
        if state:
            # Container name comes from the registry entry, which init_test_env
            # filled from the directory's __testparent__.py -- the child never
            # restates it.
            reg = apphelpers.testfile_registry.get(module_name) or {}
            state.testid = reg.get("id", "")
            state.testtype = config.get("function", "")
            state.testname = module_name

    if session is not None:
        if not session.plan:
            print(f"FINISHED: No tests found after loading {module_name}")
            if state:
                state.step = "Done"
                state.test_name = "No tests found"
            return []
        print(f"Found {len(session.plan)} steps to run for {module_name}.")
        results = session.run()
    else:
        if not all_tests:
            print(f"FINISHED: No tests found after loading {module_name}")
            if state:
                state.step = "Done"
                state.test_name = "No tests found"
            return []
        print(f"Found {len(all_tests)} tests to run for {module_name}.")
        continue_on_failure = bool(config.get("continue_on_failure", False))
        results = run_tests(test_descriptions, all_tests, context, module_name, continue_on_failure)

    # A GIF recorder started by test_start_gif_capture normally stops itself
    # when the next step begins, but one started by the LAST step is still
    # running here. Close it before the report is built, or its .gif lands in
    # reports/ after _move_assets has already swept the directory and never
    # gets attached. Objects are duck-typed off the context so this stays
    # independent of the project's dispatch_functions module.
    for recorder in (context.get("_gif_recorders") or []):
        try:
            if recorder.running:
                ok, msg = recorder.stop(reason="testlist ended")
                print(f"[gifcapture] {msg}")
        except Exception as e:
            print(f"[gifcapture] failed to stop recorder: {type(e).__name__}: {e}")

    def get_start(name):
        ts = session.get_start(name) if session is not None else None
        if ts is None:
            ts = TestrunnerTimer.get_start(name)
        return ts if ts is not None else time.time()

    def get_stop(name):
        ts = session.get_stop(name) if session is not None else None
        if ts is None:
            ts = TestrunnerTimer.get_stop(name)
        if ts is not None:
            return ts
        dur = next((d for n, s, c, o, out, d, info in results if n == name), 0.0)
        return get_start(name) + dur

    total_suite_duration = round(sum(r[5] for r in results), 2)
    # Reuse the run's own run_id rather than generating a second, independent
    # timestamp here -- this is also the directory screenshots/gifs have been
    # written into all along (see appstate.current_reports_dir), so the report
    # and its assets are pinned to the one identifier instead of two.
    run_id = getattr(state, "run_id", "") or datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    subdir_path = os.path.join(REPORT_DIR, run_id)
    os.makedirs(subdir_path, exist_ok=True)

    # Name the report by the same slug the URLs use, so a report on disk and the
    # id in /test/<id> are the one identifier rather than two spellings of it.
    slug = testid.slug_for(module_name, apphelpers.testfile_registry.keys())
    report_path = os.path.join(subdir_path, f"{slug}.html")

    if session is not None:
        # Where the steps came from (LKG / draft revision), what was edited,
        # retried or ignored mid-run -- the report's own rows cannot carry it.
        try:
            import json
            with open(os.path.join(subdir_path, "session.json"), "w", encoding="utf-8") as f:
                json.dump(session.snapshot(), f, indent=2, default=str)
        except Exception as e:
            print(f"[session] could not write session.json: {e}")
    reporthelper.generate_report(
        results=results,
        report_path=report_path,
        report_dir=REPORT_DIR,
        compile_logs_dir=compile_logs_dir,
        testlist_name=module_name,
        build_banner=buildhelper.report_banner_html(build_attr),
    )

    raw_types = meta.get("types", {})
    if isinstance(raw_types, dict):
        test_types = ", ".join(raw_types.keys())
    elif isinstance(raw_types, list):
        test_types = ", ".join(raw_types)
    else:
        test_types = str(raw_types)

    # DB parent name is the container from the directory stub (meta["id"]),
    # not a string each child repeats for itself.
    config_testparentname = meta.get("id") or module_name

    report_id = db.populate_sqlite(
        test_id=module_name,
        testparentname=config_testparentname,
        test_types=test_types,
        results=[(n, s, c, o, out, d) for n, s, c, o, out, d, info in results],
        html_report_path=report_path,
        total_duration=total_suite_duration,
        get_start=get_start,
        get_stop=get_stop
    )

    try:
        buildhelper.record_run(report_id, container_key, module_name, build_attr)
        # A passing tool run registers what it built. A failed one doesn't:
        # the output path usually still holds the PREVIOUS build then.
        passed = not any((r[1] or "").upper() in ("FAIL", "ERROR") for r in results)
        if meta.get("is_tool") and passed and session is not None:
            new_ids = buildhelper.register_from_run(
                container_key, module_name, config.get("function", ""),
                report_id, session.config, slot=build_input_slot)
            if new_ids:
                print(f"[buildhelper] registered build(s) {new_ids} for {container_key}")
    except Exception as e:
        print(f"[buildhelper] build bookkeeping failed for {module_name}: {e}")

    if state:
        state.step = "Done"
        state.test_name = ""

    return results


def _pause_flag(v):
    """Coerce a step's pause_on value -- may be a real bool from the testbuilder
    checkbox, or a string if the .py file was hand-edited."""
    if isinstance(v, bool):
        return v
    return str(v).strip().lower() in ("1", "true", "yes", "on")


def _live_emu_instances(context):
    """Every context value that looks like a running emulator instance --
    QEMU/DOSBox-X/86Box/Basilisk/SIMH all keep a subprocess in .process, so
    duck-type on that rather than importing every backend's class here."""
    live = []
    for key, val in (context or {}).items():
        proc = getattr(val, "process", None)
        if proc is not None and proc.poll() is None:
            live.append(val)
    return live


def _run_pause_step(test_name, context, info=""):
    """A step with pause_on=True: hand control to the user instead of running
    the step's own action. Blocks until every emulator instance launched so
    far exits (the user closing the window), then marks this step AND every
    step after it SKIPPED -- not FAIL, since nothing here was ever asserted
    against automatically; a human looked at it instead. Continuing the
    automated steps past this point wouldn't make sense anyway once the
    instance the user was just driving by hand is gone.
    """
    start = time.time()
    instances = _live_emu_instances(context)

    if not instances:
        msg = "pause_on step reached with no running instance to wait for -- skipping"
        print(f"[pause] {msg}")
        context["abort"] = True
        context["abort_sticky"] = True
        return (test_name, "SKIPPED", "gray", msg, "", time.time() - start, info)

    names = ", ".join(getattr(i, "name", "instance") for i in instances)
    print(f"[pause] waiting for you to close: {names}")
    progress_state.step_name = f"PAUSED -- close {names} to continue"

    heartbeat = time.time()
    while any(getattr(i, "process", None) and i.process.poll() is None for i in instances):
        time.sleep(1)
        if time.time() - heartbeat > 30:
            print(f"[pause] still waiting on: {names}")
            heartbeat = time.time()

    duration = time.time() - start
    msg = f"paused for manual review ({duration:.1f}s) -- resumed after {names} closed"
    print(f"[pause] {msg}")
    # The rest of the run assumed a live instance that's now gone -- same
    # "nothing after this can run for real" signal every other abort uses.
    # abort_sticky marks this as a *manual* abort: the user took control on
    # purpose, so unlike an automatic failure (e.g. an OCR failphrase hit),
    # the teardown step below should NOT auto-run and yank away an instance
    # they may still be looking at.
    context["abort"] = True
    context["abort_sticky"] = True
    return (test_name, "SKIPPED", "gray", msg, "", duration, info)


TEARDOWN_ACTION = "test_terminate_all"


def run_tests(test_descriptions, registry, context, module_name, continue_on_failure=False):
    results = []
    seen_names = set()
    unique_tests = []

    for test_func in registry:
        if hasattr(test_func, "test_description"):
            test_name = test_func.test_description
            if test_name in test_descriptions and test_name not in seen_names:
                unique_tests.append(test_func)
                seen_names.add(test_name)


    total = len(unique_tests)
    if total == 0:
        # reset only these vars at start to explicity set zero/none vals
        progress_state.step = "0/0"
        progress_state.testname = ""
        progress_state.step_name = "No tests found"
        return []

    def _publish(entry):
        results.append(entry)
        progress_state.live_results.append(entry)

    # append steps during testrun
    progress_state.live_results = []
    for index, test_func in enumerate(unique_tests, start=1):
        test_name = getattr(test_func, "test_description", test_func.__name__)
        test_info = getattr(test_func, "test_info", "")
        progress_state.step = f"{index}/{total}"
        progress_state.step_name = test_name
        progress_state.testname = module_name        # dot path
        progress_state.testid = progress_state.testid or test_name  # human-friendly name
        progress_state.testtype = progress_state.testtype or getattr(test_func, "testtype", "")

        if context.get("abort"):
            # Teardown must still run after an automatic abort (e.g. an OCR
            # failphrase hit) so it doesn't leave an emulator orphaned --
            # unless the abort came from a manual pause_on step, where the
            # user deliberately took control and teardown would yank away
            # the instance they're looking at.
            if getattr(test_func, "action", None) == TEARDOWN_ACTION and not context.get("abort_sticky"):
                context["abort"] = False
                result = run_registered_test(test_name, [test_func], context)
                context["abort"] = True
                if result:
                    _publish(result)
                continue

            _publish((test_name, "SKIPPED", "gray", "Skipped", "", 0.00, test_info))
            continue

        if getattr(test_func, "pause_on", False):
            _publish(_run_pause_step(test_name, context, test_info))
            continue

        result = run_registered_test(test_name, [test_func], context)
        if result:
            _publish(result)

        # continue_on_failure ("keep running" mode): a testlist that's a
        # bundle of independent checks rather than a dependent chain opts
        # into this so one failing step doesn't skip the rest. A manual
        # pause_on abort (abort_sticky) is never overridden here -- that's
        # the user deliberately taking control, not a step failure.
        if continue_on_failure and context.get("abort") and not context.get("abort_sticky"):
            context["abort"] = False

    # reset to null after test run completes
    # progress_state.step = f"{total}/{total}"
    progress_state.step = "Idle"
    progress_state.testname = ""
    progress_state.step_name = ""
    progress_state.testname = ""
    progress_state.testtype = ""
    progress_state.testid = ""
    return results
