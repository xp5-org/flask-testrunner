import os
import time
import sqlite3
import re
import datetime
import shutil
import datetime
import apphelpers
import datetime
import importlib
import sys
from collections import defaultdict
from appstate import progress_state

from app import db





TESTSRC_HELPERDIR = "/testsrc/helpers"
TESTSRC_BASEDIR = "/testsrc/"
TESTSRC_TESTLISTDIR = "/testsrc/mytests"
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
REPORT_DIR = os.path.join(BASE_DIR, "reports")
compile_logs_dir = os.path.join(BASE_DIR, "compile_logs")
DB_PATH = os.path.join(BASE_DIR, "report.sqlite")

#if TESTSRC_TESTLISTDIR not in sys.path:
#    sys.path.insert(0, TESTSRC_TESTLISTDIR)
parent_dir = os.path.dirname(TESTSRC_TESTLISTDIR)
if parent_dir not in sys.path:
    sys.path.insert(0, parent_dir)


context = {
    "sock": None,
    "abort": False
}



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





def reload_tests():
    apphelpers.clear_registries()
    pkg_name = "mytests"
    try:
        importlib.import_module(pkg_name)
    except Exception as e:
        print(f"Failed to import package {pkg_name}: {e}")

    for fname in os.listdir(TESTSRC_TESTLISTDIR):
        if fname.endswith(".py") and not fname.startswith("__"):
            modname = f"{pkg_name}.{fname[:-3]}"
            if modname in sys.modules:
                importlib.reload(sys.modules[modname])
            else:
                importlib.import_module(modname)


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
                duration = 0.0
            return (name, status, color, log_output, stdout_output, duration)
    return (name, "NOT FOUND", "gray", "No matching test found", "", 0.0)


def run_testfile(module_name, state=None):
    reload_tests()

    # Determine exact module string (e.g., 'mytests.test1')
    target_module = f"mytests.{module_name.split('.')[-1]}"
    
    meta = apphelpers.testfile_registry.get(target_module)
    if not meta:
        if state:
            state.step = "Error"
            state.test_name = "No registry entry"
        return []

    registry_map = {
        "build": apphelpers.buildtest_registry,
        "play": apphelpers.playtest_registry,
        "package": apphelpers.packagetest_registry,
    }

    all_tests = []
    test_descriptions = []
    
    test_types = meta.get("types", {})

    for t, mod_path in test_types.items():
        registry = registry_map.get(t)
        if registry:
            for f in registry:
                # 1. Check if the function belongs to the specific file being run
                # 2. Check if the decorator actually added the 'test_description'
                # If the decorator is commented out, hasattr(f, "test_description") will be False
                is_correct_mod = (f.__module__ == mod_path)
                desc = getattr(f, "test_description", None)

                if is_correct_mod and desc is not None:
                    if f not in all_tests:
                        all_tests.append(f)
                        test_descriptions.append(desc)

    if not all_tests:
        if state:
            state.step = "Done"
            state.test_name = "No tests found (Decorators likely commented out)"
        return []

    context = {"sock": None, "abort": False}
    results = run_tests(test_descriptions, all_tests, context)

    # Calculate TOTAL duration for the whole suite
    total_suite_duration = sum(r[5] for r in results)

    timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    subdir_path = os.path.join(REPORT_DIR, timestamp)
    os.makedirs(subdir_path, exist_ok=True)

    report_path = os.path.join(subdir_path, f"{module_name}.html")
    generate_report(results, report_path)
    
    # Updated to pass the calculated total duration
    db.populate_sqlite(results, report_path, total_suite_duration)

    if state:
        state.step = "Done"
        state.test_name = ""

    return results


def run_tests(test_descriptions, registry, context):
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
        progress_state.test_name = "No tests found"
        return []

    for index, test_func in enumerate(unique_tests, start=1):
        test_name = getattr(test_func, "test_description", test_func.__name__)
        progress_state.step = f"{index}/{total}"
        progress_state.test_name = test_name

        if context.get("abort"):
            results.append((test_name, "SKIPPED", "gray", "Skipped", "", 0.0))
            continue

        result = run_registered_test(test_name, [test_func], context)
        if result:
            results.append(result)

    progress_state.step = f"{total}/{total}"
    progress_state.test_name = "Done"
    return results


def generate_report(results, report_path):
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
        f.write("""<html>
    <head>
    <title>Test Report</title>
    <style>
    body { font-family: sans-serif; }
    table { border-collapse: collapse; width: 100%; }
    th, td { border: 1px solid #ccc; padding: 8px; text-align: left; }
    .green { background-color: #c8f7c5; }
    .red { background-color: #f7c5c5; }
    .gray { background-color: #eeeeee; }

    .flex-container {
        display: flex;
        gap: 20px;
        flex-wrap: nowrap;
        max-width: 100%;
    }

    .output-column {
        flex: 1;
        min-width: 0;
        max-width: 50%;
        overflow: hidden;
        background-color: #f0f0f0;
        padding: 10px;
    }

    .image-column {
        flex: 1;
        max-width: 50%;
    }

    pre {
        background-color: #eee;
        padding: 10px;
        white-space: pre-wrap;
        word-wrap: break-word;
        overflow-wrap: break-word;
        max-width: 100%;
        overflow-x: auto;
    }
    hr { margin: 40px 0; }
    </style>
    </head>
    <body>
    <h1>Test Report</h1>
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


