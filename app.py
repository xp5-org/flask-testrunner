import os
import re
import glob
from flask import Flask, render_template, send_from_directory
import test_runner
import threading
from flask import jsonify
import importlib
import pkgutil
from collections import defaultdict
import sys
import apphelpers
from appstate import ProgressState

#######################################
### config stuff #####################
app = Flask(__name__)
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
REPORT_DIR = os.path.join(BASE_DIR, "reports")
FLASKRUNNER_HELPERDIR = "/testrunnerapp/helpers"
TESTSRC_HELPERDIR = "/testsrc/helpers"
TESTSRC_TESTLISTDIR = "/testsrc/mytests"
progress_state = ProgressState()
#######################################
if TESTSRC_TESTLISTDIR not in sys.path:
    sys.path.insert(0, TESTSRC_TESTLISTDIR)






@app.route('/favicon.ico')
def favicon():
    return app.send_static_file('favicon.ico')


def get_all_report_summaries():
    # scans reports dir, gets table tag for status on each report
    # used to get list of most recent status of each test type founf
    if not os.path.exists(REPORT_DIR):
        return []

    summaries = []
    pattern = os.path.join(REPORT_DIR, "*", "*.html")
    reports = sorted(glob.glob(pattern), reverse=True)

    for path in reports:
        report_name = os.path.relpath(path, REPORT_DIR)
        try:
            with open(path, "r") as f:
                content = f.read()

            if "<table>" not in content.lower():
                summaries.append((report_name, "", "UNKNOWN"))
                continue

            status = "PASS"
            duration_total = 0.0
            
            rows = re.findall(r"<tr>(.*?)</tr>", content, re.DOTALL | re.IGNORECASE)
            
            for row in rows[1:]:
                cols = re.findall(r"<td>(.*?)</td>", row, re.DOTALL | re.IGNORECASE)
                if len(cols) >= 3:
                    d_text = re.sub(r"<[^>]*>", "", cols[1]).strip().rstrip("s")
                    s_text = re.sub(r"<[^>]*>", "", cols[2]).strip().upper()

                    try:
                        duration_total += float(d_text)
                    except ValueError:
                        pass

                    if "FAIL" in s_text:
                        status = "FAIL"

            duration_str = f"{duration_total:.2f}s" if duration_total > 0 else "0.00s"
            summaries.append((report_name, duration_str, status))

        except Exception:
            summaries.append((report_name, "", "ERROR"))

    return summaries


@app.route("/testfile_list")
def testfile_list():
    # scan for .py files in the 'mytests' dir and import them
    for _, modname, _ in pkgutil.iter_modules([TESTSRC_TESTLISTDIR]):
        importlib.import_module(modname)

    grouped = defaultdict(lambda: {
        "types": {},
        "files": [],
        "description": None,
        "system": None,
        "platform": None,
    })

    for modname, meta in apphelpers.testfile_registry.items():
        file = modname.split('.')[-1]
        for t in meta["types"]:
            grouped[meta["id"]]["types"][t] = file
        grouped[meta["id"]]["files"].append(file)
        # Only set description, system, platform if not set yet
        if grouped[meta["id"]]["description"] is None:
            grouped[meta["id"]]["description"] = meta.get("description")
        if grouped[meta["id"]]["system"] is None:
            grouped[meta["id"]]["system"] = meta.get("system")
        if grouped[meta["id"]]["platform"] is None:
            grouped[meta["id"]]["platform"] = meta.get("platform")

    result = []
    for test_id, info in grouped.items():
        result.append({
            "id": test_id,
            "types": info["types"],
            "description": info["description"],
            "system": info["system"],
            "platform": info["platform"],
        })

    #print("Returning testfile list:", result)  # debug print
    return jsonify(result)


def get_latest_report_summary():
    if not os.path.exists(REPORT_DIR):
        return []
    pattern = os.path.join(REPORT_DIR, "*", "*.html")
    all_reports = sorted(glob.glob(pattern), reverse=True)
    if not all_reports:
        return []
    latest_path = all_reports[0]

    try:
        with open(latest_path, "r") as f:
            content = f.read()

        rows = re.findall(r"<tr>(.*?)</tr>", content, re.DOTALL | re.IGNORECASE)
        if not rows:
            return []

        summary = []
        for row in rows[1:]:
            cols = re.findall(r"<td>(.*?)</td>", row, re.DOTALL | re.IGNORECASE)
            if len(cols) >= 3:
                name = re.sub(r"<[^>]*>", "", cols[0]).strip()
                dur = re.sub(r"<[^>]*>", "", cols[1]).strip()
                stat_raw = re.sub(r"<[^>]*>", "", cols[2]).strip().upper()

                if "PASS" in stat_raw:
                    status = "PASS"
                elif "FAIL" in stat_raw:
                    status = "FAIL"
                else:
                    status = stat_raw
                
                summary.append((name, dur, status))
        return summary
    except Exception:
        return []


@app.route("/")
def index():
    if not os.path.exists(REPORT_DIR):
        os.makedirs(REPORT_DIR)

    # repopulate test registry before showing the page
    test_runner.reload_tests()
    available_tests = list(apphelpers.testfile_registry.keys())

    summaries = get_all_report_summaries()
    latest_summary = get_latest_report_summary()
    return render_template(
        "index.html",
        summaries=summaries,
        latest_summary=latest_summary,
        available_tests=available_tests
    )


@app.route("/run/<testname>")
def run_named_tests(testname):
    print(f"run {testname} called")

    progress_state.step = "0/0"
    progress_state.test_name = testname

    def run():
        test_runner.run_testfile(testname, progress_state)
        progress_state.step = "Done"
        progress_state.test_name = ""

    threading.Thread(target=run, daemon=True).start()
    return "Started"





@app.route("/progress")
def progress():
    return jsonify({
        "step": progress_state.step,
        "test_name": progress_state.test_name
    })



@app.route("/reports/<path:filepath>")
def view_report(filepath):
    # filepath could be "timestamp/filename.html" or "filename.html"
    full_path = os.path.join(REPORT_DIR, filepath)
    if not os.path.isfile(full_path):
        return "File not found", 404
    directory, filename = os.path.split(full_path)
    return send_from_directory(directory, filename)




if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080, debug=False)
