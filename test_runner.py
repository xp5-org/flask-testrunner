import os
import sys
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
sys.pycache_prefix = os.path.join(BASE_DIR, "pycache")


import time
import re
import shutil
import datetime
import apphelpers
import glob
import importlib
from collections import defaultdict
from appstate import progress_state
from app import db

TESTSRC_HELPERDIR = "/testsrc/helpers"
TESTSRC_BASEDIR = "/testsrc/"
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
REPORT_DIR = os.path.join(BASE_DIR, "reports")
compile_logs_dir = os.path.join(BASE_DIR, "compile_logs")
DB_PATH = os.path.join(BASE_DIR, "report.sqlite")



TESTSRC_ROOT = "/testsrc/sourcedir"

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


failed_loads = []

def _load_or_reload(modname):
    global failed_loads
    try:
        if modname in sys.modules:
            importlib.reload(sys.modules[modname])
        else:
            importlib.import_module(modname)
    except Exception as e:
        # only log 'module' failure
        print(f"Failed to load {modname}: {e}")
        if modname not in failed_loads:
            failed_loads.append(modname)



def reload_tests():
    global failed_loads
    failed_loads.clear()
    apphelpers.clear_registries()

    if TESTSRC_ROOT not in sys.path:
        sys.path.insert(0, TESTSRC_ROOT)

    seen_modules = set()

    # Use recursive glob to find all .py files once
    search_path = os.path.join(TESTSRC_ROOT, "**", "*.py")
    
    for fpath in glob.glob(search_path, recursive=True):
        fname = os.path.basename(fpath)
        
        if fname == "__init__.py":
            continue
            
        if not fname.startswith(TESTLIST_PREFIXES):
            continue

        rel_path = os.path.relpath(fpath, TESTSRC_ROOT)
        # Normalize path separators to dots
        modname = rel_path.replace(os.sep, ".")[:-3]
        
        # Double-check: ensure we don't load a path that ends up being the same module
        if modname not in seen_modules:
            _load_or_reload(modname)
            if modname in sys.modules:
                sys.modules[modname].__full_path__ = os.path.abspath(fpath)
            seen_modules.add(modname)




def run_registered_test(name, registry, context):
    for test_func in registry:
        if test_func.test_description == name:
            try:
                print(f"Running {name}")
                start_time = time.time()
                TestrunnerTimer.set_start(name, start_time)
                result = test_func(context)
                duration = time.time() - start_time
                TestrunnerTimer.set_stop(name, time.time())
                if len(result) == 3:
                    success, log_output, stdout_output = result
                else:
                    success, log_output = result
                    stdout_output = ""
                status = "PASS" if success else "FAIL"
                color = "green" if success else "red"
            except Exception as e:
                status = "ERROR"
                log_output = str(e)
                stdout_output = ""
                color = "gray"
                duration = 0.00
            return (name, status, color, log_output, stdout_output, duration)
    return (name, "NOT FOUND", "gray", "No matching test found", "", 0.00)



# def run_testfile(module_name, state=None):
#     # 1. Clear everything and reload the target module
#     # This ensures the registries ONLY contain tests from this file
#     global failed_loads
#     failed_loads.clear()
#     apphelpers.clear_registries()
    
#     # Reload logic (simplified for the target)
#     _load_or_reload(module_name)

#     all_tests = []
#     test_descriptions = []
    
#     # 2. Grab everything that was just registered
#     registry_map = {k.replace("_registry", ""): v 
#                     for k, v in apphelpers.__dict__.items() 
#                     if k.endswith("_registry") and isinstance(v, list)}

#     for t_type, registry in registry_map.items():
#         for f in registry:
#             desc = getattr(f, "test_description", None)
#             if desc and f not in all_tests:
#                 all_tests.append(f)
#                 test_descriptions.append(desc)

#     if not all_tests:
#         print(f"FINISHED: No tests found in registries after loading {module_name}")
#         if state:
#             state.step, state.test_name = "Done", "No tests found"
#         return []

#     print(f"Found {len(all_tests)} tests to run.")
#     context = {"sock": None, "abort": False}
#     return run_tests(test_descriptions, all_tests, context)



def run_testfile(module_name, state=None):
    # 1. Prepare Environment
    global failed_loads
    failed_loads.clear()
    apphelpers.clear_registries()
    
    # We must reload the specific test list module to populate the registries
    _load_or_reload(module_name)
    
    meta = apphelpers.testfile_registry.get(module_name)
    if not meta:
        print(f"ERROR: {module_name} not found in testfile_registry")
        if state:
            state.step, state.test_name = "Error", "No registry entry"
        return []

    all_tests = []
    test_descriptions = []
    
    # 2. Dynamic Registry Collection
    # After _load_or_reload, the registries in apphelpers should be populated
    registry_map = {k.replace("_registry", ""): v 
                    for k, v in apphelpers.__dict__.items() 
                    if k.endswith("_registry") and isinstance(v, list)}

    for t_type, registry in registry_map.items():
        for f in registry:
            desc = getattr(f, "test_description", None)
            # We don't filter by __module__ here because clear_registries() 
            # ensured only the current module's tests are present.
            if desc and f not in all_tests:
                all_tests.append(f)
                test_descriptions.append(desc)

    if not all_tests:
        print(f"FINISHED: No tests found after loading {module_name}")
        if state:
            state.step = "Done"
            state.test_name = "No tests found (Decorators likely commented out)"
        return []

    print(f"Found {len(all_tests)} tests to run for {module_name}.")
    context = {"sock": None, "abort": False}

    # 3. Execution
    # results = run_tests(test_descriptions, all_tests, context)
    results = run_tests(test_descriptions, all_tests, context, module_name)

    # 4. Timer Helpers for DB
    def get_start(name):
        ts = TestrunnerTimer.get_start(name)
        return ts if ts is not None else time.time()

    def get_stop(name):
        ts = TestrunnerTimer.get_stop(name)
        if ts is not None:
            return ts
        dur = next((d for n, s, c, o, out, d in results if n == name), 0.0)
        return get_start(name) + dur

    # 5. Report Generation
    total_suite_duration = round(sum(r[5] for r in results), 2)
    timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    subdir_path = os.path.join(REPORT_DIR, timestamp)
    os.makedirs(subdir_path, exist_ok=True)

    report_path = os.path.join(subdir_path, f"{module_name}.html")
    generate_report(results, report_path, testlist_name=module_name)

    # 6. Database Population
    db.populate_sqlite(
        test_id=module_name,
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
        progress_state.step = "0/0"
        progress_state.testname = ""
        progress_state.step_name = "No tests found"
        return []

    for index, test_func in enumerate(unique_tests, start=1):
        test_name = getattr(test_func, "test_description", test_func.__name__)
        progress_state.step = f"{index}/{total}"
        progress_state.testname = module_name
        progress_state.step_name = test_name

        if context.get("abort"):
            results.append((test_name, "SKIPPED", "gray", "Skipped", "", 0.00))
            continue

        result = run_registered_test(test_name, [test_func], context)
        if result:
            results.append(result)

    progress_state.step = f"{total}/{total}"
    progress_state.testname = ""
    progress_state.step_name = "Done"
    return results




def generate_report(results, report_path, testlist_name=""):
    subdir_path = os.path.dirname(report_path)
    print(f"Creating directory: '{subdir_path}'")
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
            print("moved image to ", REPORT_DIR, filename)

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
