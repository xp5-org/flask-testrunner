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

from appstate import progress_state
from app import db
import apphelpers
import dispatchhelper

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


def reload_tests():
    global failed_loads
    failed_loads.clear()
    apphelpers.clear_registries()

    if TESTSRC_ROOT not in sys.path:
        sys.path.insert(0, TESTSRC_ROOT)

    seen_modules = set()
    search_path = os.path.join(TESTSRC_ROOT, "**", "*.py")
    for fpath in glob.glob(search_path, recursive=True):
        fname = os.path.basename(fpath)
        if fname == "__init__.py":
            continue
        if not fname.startswith(TESTLIST_PREFIXES):
            continue

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
            try:
                # 1. Stop if we are already in an aborted state
                if context.get("abort"):
                    return (name, "FAIL", "red", "Aborted due to previous failure", "", 0.00)

                print(f"Running {name}")
                start_time = time.time()
                
                # 2. Run the test
                result = test_func(context)

                duration = time.time() - start_time
                
                # 3. Unpack result
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

            return (name, status, color, log_output, stdout_output, duration)

    return (name, "NOT FOUND", "gray", "No matching test found", "", 0.00)


def run_testfile(module_name, state=None):
    global failed_loads
    failed_loads.clear()
    
    all_tests = []
    test_descriptions = []
    context = {"sock": None, "abort": False}

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
                func_name = f"{action}_{subaction}"
                func = dispatch.get(func_name)
                
                # create a unique name with stepnum appended , temp fix
                # list steps names must be unique or will get errors like test with 4 steps but only 3 run
                unique_name = f"{i}_{func_name}"
                
                if func:
                    kwargs = step.get("param", {}).copy()
                    kwargs['context'] = context
                    kwargs['config'] = config
                    
                    def step_wrapper(test_meta=None, f=func, kw=kwargs):
                        try:
                            return f(**kw)
                        except Exception as e:
                            return False, f"PYTHON CRASH in {f.__name__}: {str(e)}"
                    
                    step_wrapper.test_description = unique_name
                    step_wrapper.my_test_type = config.get("testtype", "dispatchtest")
                    
                    all_tests.append(step_wrapper)
                    test_descriptions.append(step_wrapper.test_description)
                else:
                    print(f"ERROR: Function {func_name} not found")
                    
                    def fail_wrapper(test_meta=None, *args, name=func_name, **kwargs):
                        return False, f"Function {name} not found in dispatch"
                    
                    fail_wrapper.test_description = f"{unique_name} (Missing)"
                    fail_wrapper.my_test_type = config.get("testtype", "dispatchtest")
                    
                    all_tests.append(fail_wrapper)
                    test_descriptions.append(fail_wrapper.test_description)

    else:
        for registry in apphelpers.registry_map.values():
            for f in registry:
                desc = getattr(f, "test_description", None)
                if desc and f not in all_tests:
                    all_tests.append(f)
                    test_descriptions.append(desc)

    if mod and hasattr(mod, "CONFIG"):
        config = mod.CONFIG
        if state:
            state.testid = config.get("testname", "")
            state.testtype = config.get("testtype", "")
            state.testname = module_name

    if not all_tests:
        print(f"FINISHED: No tests found after loading {module_name}")
        if state:
            state.step = "Done"
            state.test_name = "No tests found"
        return []

    print(f"Found {len(all_tests)} tests to run for {module_name}.")
    results = run_tests(test_descriptions, all_tests, context, module_name)

    def get_start(name):
        ts = TestrunnerTimer.get_start(name)
        return ts if ts is not None else time.time()

    def get_stop(name):
        ts = TestrunnerTimer.get_stop(name)
        if ts is not None:
            return ts
        dur = next((d for n, s, c, o, out, d in results if n == name), 0.0)
        return get_start(name) + dur

    total_suite_duration = round(sum(r[5] for r in results), 2)
    timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    subdir_path = os.path.join(REPORT_DIR, timestamp)
    os.makedirs(subdir_path, exist_ok=True)

    report_path = os.path.join(subdir_path, f"{module_name}.html")
    generate_report(results, report_path, testlist_name=module_name)

    raw_types = meta.get("types", {})
    if isinstance(raw_types, dict):
        test_types = ", ".join(raw_types.keys())
    elif isinstance(raw_types, list):
        test_types = ", ".join(raw_types)
    else:
        test_types = str(raw_types)

    config_testparentname = mod.CONFIG.get("testname") if hasattr(mod, "CONFIG") else module_name

    db.populate_sqlite(
        test_id=module_name,
        testparentname=config_testparentname,
        test_types=test_types,
        results=[(n, s, c, o, out, d) for n, s, c, o, out, d in results],
        html_report_path=report_path,
        total_duration=total_suite_duration,
        get_start=get_start,
        get_stop=get_stop
    )

    if state:
        state.step = "Done"
        state.test_name = ""

    return results


def parse_step_params(param_str):
    params = {}
    if not param_str:
        return params
    pairs = param_str.split(',')
    for pair in pairs:
        if '=' in pair:
            key, value = pair.split('=', 1)
            params[key.strip()] = value.strip()
        else:
            params['string'] = pair.strip()
    return params


def create_step_wrapper(func, args_payload):
    def wrapped_step(ctx):
        merged_args = args_payload.copy()
        sig = inspect.signature(func)
        if "context" in sig.parameters:
            merged_args["context"] = ctx
        return func(**merged_args)
    return wrapped_step


def run_tests(test_descriptions, registry, context, module_name):
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
        progress_state.step = f"{index}/{total}"
        progress_state.step_name = test_name
        progress_state.testname = module_name        # dot path
        progress_state.testid = progress_state.testid or test_name  # human-friendly name
        progress_state.testtype = progress_state.testtype or getattr(test_func, "testtype", "")

        if context.get("abort"):
            results.append((test_name, "SKIPPED", "gray", "Skipped", "", 0.00))
            continue

        result = run_registered_test(test_name, [test_func], context)
        if result:
            results.append(result)

    # reset to null after test run completes
    # progress_state.step = f"{total}/{total}"
    progress_state.step = "Idle"
    progress_state.testname = ""
    progress_state.step_name = ""
    progress_state.testname = ""
    progress_state.testtype = ""
    progress_state.testid = ""
    return results


def generate_report(results, report_path, testlist_name=""):
    subdir_path = os.path.dirname(report_path)
   # print(f"Creating directory: '{subdir_path}'")
    if subdir_path and not os.path.exists(subdir_path):
        os.makedirs(subdir_path, exist_ok=True)

    # Move compile logs into report subdir
    if os.path.exists(compile_logs_dir):
        for filename in os.listdir(compile_logs_dir):
            shutil.move(os.path.join(compile_logs_dir, filename), subdir_path)

    # Move screenshots from REPORT_DIR into subdir
    for filename in os.listdir(REPORT_DIR):
        if (re.match(r"test\d+\.(png|ppm|gif)$", filename) or
            filename.startswith("screenshot-")):
            shutil.move(os.path.join(REPORT_DIR, filename), subdir_path)

    # Collect screenshots by integer test step
    screenshot_map = defaultdict(list)
    for fname in os.listdir(subdir_path):
        if fname.endswith((".png", ".gif")):
            # Match filenames like screenshot-vice1-6-1.png or screenshot-vice2-6.png
            m = re.match(r"screenshot-[^-]+-(\d+)(?:-\d+)?\.(png|gif)$", fname)
            if m:
                step_num = int(m.group(1))
                screenshot_map[step_num].append(fname)

    # Write HTML report
    with open(report_path, "w") as f:
        f.write(f"""<html>
        <head>
        <title>Test Report - {testlist_name}</title>
        <style>
        body {{ font-family: sans-serif; }}
        table {{ border-collapse: collapse; width: 100%; }}
        th, td {{ border: 1px solid #ccc; padding: 8px; text-align: left; }}
        .green {{ background-color: #c8f7c5; }}
        .red {{ background-color: #f7c5c5; }}
        .gray {{ background-color: #eeeeee; }}

        .flex-container {{
            display: flex;
            gap: 20px;
            flex-wrap: nowrap;
            max-width: 100%;
        }}

        .output-column {{
            flex: 1;
            min-width: 0;
            max-width: 50%;
            overflow: hidden;
            background-color: #f0f0f0;
            padding: 10px;
        }}

        .image-column {{
            flex: 1;
            max-width: 50%;
        }}

        pre {{
            background-color: #eee;
            padding: 10px;
            white-space: pre-wrap;
            word-wrap: break-word;
            overflow-wrap: break-word;
            max-width: 100%;
            overflow-x: auto;
        }}
        hr {{ margin: 40px 0; }}
        </style>
        </head>
        <body>
        <h1>Test Report: {testlist_name}</h1>
        <table>
        <tr><th>Test Name</th><th>Duration (s)</th><th>Result</th></tr>
        """)


        # Summary table
        for name, status, color, _, _, duration in results:
            f.write(f'<tr><td>{name}</td><td>{duration:.2f}</td><td class="{color}">{status}</td></tr>\n')

        f.write("</table><h2>Detailed Output</h2>\n")

        # Detailed sections with screenshots linked by index (starting at 1)
        for idx, (name, status, color, output, stdout, duration) in enumerate(results, start=1):
            matching_images = screenshot_map.get(idx, [])
            if matching_images:
                img_tags = "\n".join(
                    f'<img src="{img}" alt="{img}" style="max-width: 100%; border: 1px solid #ccc;">'
                    for img in matching_images
                )
            else:
                img_tags = "<p>No screenshot available.</p>"

            f.write(f"""<hr>
    <div class="flex-container">
    <div class="output-column">
        <h3>{name}</h3>
        <p><strong>Duration:</strong> {duration:.2f} seconds</p>
        <pre>OUTPUT:
        {output}

    STDOUT:
    {stdout}</pre>
        </div>
        <div class="image-column">
            <h4>Screenshot</h4>
            {img_tags}
        </div>
        </div>
        """)

        f.write("</body></html>")

    print(f"Wrote report to {report_path}")


def movethefiles(results):
    subdir_path = os.path.dirname(DB_PATH)
    if subdir_path and not os.path.exists(subdir_path):
        os.makedirs(subdir_path, exist_ok=True)

    if os.path.exists(compile_logs_dir):
        for filename in os.listdir(compile_logs_dir):
            shutil.move(os.path.join(compile_logs_dir, filename), subdir_path)

    for filename in os.listdir(REPORT_DIR):
        if (re.match(r"test\d+\.(png|ppm|gif)$", filename) or
            filename.startswith("screenshot-")):
            shutil.move(os.path.join(REPORT_DIR, filename), subdir_path)

    screenshot_map = defaultdict(list)
    for fname in os.listdir(subdir_path):
        if fname.endswith((".png", ".gif")):
            m = re.match(r"screenshot-[^-]+-(\d+)(?:-\d+)?\.(png|gif)$", fname)
            if m:
                step_num = int(m.group(1))
                screenshot_map[step_num].append(fname)
