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
import apphelpers
import dispatchhelper
import apphelpers as testid

TESTSRC_HELPERDIR = "/testsrc/helpers"
TESTSRC_BASEDIR = "/testsrc/"
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
REPORT_DIR = os.path.join(BASE_DIR, "reports")
compile_logs_dir = os.path.join(BASE_DIR, "compile_logs")
DB_PATH = os.path.join(BASE_DIR, "report.sqlite")
TESTSRC_ROOT = "/testsrc/sourcedir"

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
        failed_loads.append(modname)
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


def reload_tests():
    global failed_loads
    failed_loads.clear()
    apphelpers.clear_registries()

    if TESTSRC_ROOT not in sys.path:
        sys.path.insert(0, TESTSRC_ROOT)

    seen_modules = set()
    # Every testlist lives directly inside its project directory, one level
    # under TESTSRC_ROOT (sourcedir/<project>/__testlist__*.py), right beside
    # the __testparent__.py stub that names its container -- never nested any
    # deeper (verified: every __testlist__*/__testparent__*.py in this repo
    # sits at that exact depth). So scan project dirs non-recursively instead
    # of walking the whole tree: a full os.walk used to also descend into
    # whatever huge, testlist-free content happened to sit inside a project
    # dir -- vendored git checkouts, their build-output trees (a compiled
    # llvm-project alone is thousands of files), stale __pycache__ dirs --
    # none of which can ever contain a testlist, so there was never anything
    # to find down there. Not walking past depth 1 makes every one of those
    # a non-issue for free, without needing an exclude list for any of them.
    try:
        project_dirs = sorted(e.path for e in os.scandir(TESTSRC_ROOT)
                               if e.is_dir(follow_symlinks=False))
    except OSError:
        project_dirs = []

    for project_dir in project_dirs:
        try:
            entries = os.scandir(project_dir)
        except OSError:
            continue
        with entries:
            fnames = sorted(e.name for e in entries if e.is_file(follow_symlinks=False))

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


def run_testfile(module_name, state=None):
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
    
    if mod and hasattr(mod, "CONFIG") and "steps" in mod.CONFIG:
            config = mod.CONFIG
            proj_dir = "/testsrc/pyhelpers"
            dispatch = dispatchhelper.load_step_dispatch(proj_dir)
            
            for i, step in enumerate(config["steps"], 1):
                action = step.get('action', 'unknown')
                subaction = step.get('subaction', 'unknown')
                func_name = f"{action}"
                func = dispatch.get(func_name)

                step_desc = step.get("description")
                step_title = step.get("title")
                unique_name = f"{i}_{func_name}"
                if step_title:
                    unique_name = f"{unique_name}: {step_title}"

                if func:
                    kwargs = step.get("param", {}).copy()
                    kwargs['context'] = context
                    kwargs['config'] = config
                    
                    def step_wrapper(test_meta=None, f=func, kw=kwargs):
                        try:
                            return f(**kw)
                        except Exception as e:
                            return False, (
                                f"Testrunner.py: PYTHON CRASH in {f.__name__}:\n"
                                f"kwargs={kw}\n"
                                f"{type(e).__name__}: {str(e)}"
                            )

                    step_wrapper.test_description = unique_name
                    step_wrapper.test_info = step_desc or ""
                    step_wrapper.my_test_type = config.get("function", "dispatchtest")
                    step_wrapper.pause_on = _pause_flag(step.get("pause_on", False))
                    step_wrapper.action = action

                    all_tests.append(step_wrapper)
                    test_descriptions.append(step_wrapper.test_description)
                else:
                    print(f"ERROR: Function {func_name} not found")

                    def fail_wrapper(test_meta=None, *args, name=func_name, **kwargs):
                        return False, f"Function {name} not found in dispatch"

                    fail_wrapper.test_description = f"{unique_name} (Missing)"
                    fail_wrapper.test_info = step_desc or ""
                    fail_wrapper.my_test_type = config.get("function", "dispatchtest")

                    all_tests.append(fail_wrapper)
                    test_descriptions.append(fail_wrapper.test_description)

    else:
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
        ts = TestrunnerTimer.get_start(name)
        return ts if ts is not None else time.time()

    def get_stop(name):
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
    reporthelper.generate_report(
        results=results,
        report_path=report_path,
        report_dir=REPORT_DIR,
        compile_logs_dir=compile_logs_dir,
        testlist_name=module_name,
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

    db.populate_sqlite(
        test_id=module_name,
        testparentname=config_testparentname,
        test_types=test_types,
        results=[(n, s, c, o, out, d) for n, s, c, o, out, d, info in results],
        html_report_path=report_path,
        total_duration=total_suite_duration,
        get_start=get_start,
        get_stop=get_stop
    )

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

    # append steps during testrun
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
                    results.append(result)
                continue

            results.append((test_name, "SKIPPED", "gray", "Skipped", "", 0.00, test_info))
            continue

        if getattr(test_func, "pause_on", False):
            results.append(_run_pause_step(test_name, context, test_info))
            continue

        result = run_registered_test(test_name, [test_func], context)
        if result:
            results.append(result)

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
