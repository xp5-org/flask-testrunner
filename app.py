import os
import threading
import sys
import apphelpers
from appstate import progress_state
from flask import Flask, render_template, send_from_directory, jsonify, request
import apphelpers, test_runner
from dbhelper import ReportDB
db = ReportDB()


#######################################
### config stuff #####################
app = Flask(__name__)
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
REPORT_DIR = os.path.join(BASE_DIR, "reports")
FLASKRUNNER_HELPERDIR = "/testrunnerapp/helpers"
TESTSRC_HELPERDIR = "/testsrc/helpers"
TESTSRC_TESTLISTDIR = "/testsrc/mytests"
DB_PATH = os.path.join(BASE_DIR, "report.sqlite")
#######################################
if TESTSRC_TESTLISTDIR not in sys.path:
    sys.path.insert(0, TESTSRC_TESTLISTDIR)




@app.route('/favicon.ico')
def favicon():
    return app.send_static_file('favicon.ico')


@app.route("/")
def index():
    if not os.path.exists(REPORT_DIR):
        os.makedirs(REPORT_DIR)

    db = ReportDB()
    summaries = db.get_all_reports_summary()
    latest_summary = db.get_latest_report_summary()

    return render_template(
        "index.html",
        summaries=summaries,
        latest_summary=latest_summary
    )


@app.route("/cloneproj")
def cloneproj():
    return render_template("cloneproj.html")


@app.route("/failed_tests")
def failedtestsinfo():
    test_runner.reload_tests()
    # gets test modules which failed to load
    print("failed loads1: ", test_runner.failed_loads)
    return jsonify(test_runner.failed_loads)


@app.route("/testfile_list")
def testfile_list():
    test_runner.reload_tests()

    result = []
    for modname, info in apphelpers.testfile_registry.items():
        result.append({
            "id": info["id"].replace(" ", "_"),  # internal ID, underscores only
            "display_name": info["id"],          # human-readable
            "module": modname,
            "types": info["types"],
            "system": info.get("system"),
            "platform": info.get("platform")
        })
        #print("testfile list result debug: ", result)
    return jsonify(result)


@app.route("/run/<testname>")
def run_named_tests(testname):
    print(f"run {testname} called") # testname is the reg key

    progress_state.step = "0/0"
    progress_state.testname = testname

    def run():
        print(f"Thread started for {testname}")
        res = test_runner.run_testfile(testname, progress_state)
        print(f"Thread finished for {testname}. Results found: {len(res)}")
        progress_state.step = "Done"
    threading.Thread(target=run, daemon=True).start()
    return "Started"


@app.route("/progress")
def progress():
    return jsonify({
        "step": progress_state.step,
        "testname": progress_state.testname,
        "step_name": progress_state.step_name
    })


@app.route("/reports/<path:filepath>")
def view_report(filepath):
    # filepath could be "timestamp/filename.html" or "filename.html"
    full_path = os.path.join(REPORT_DIR, filepath)
    if not os.path.isfile(full_path):
        return "File not found", 404
    directory, filename = os.path.split(full_path)
    return send_from_directory(directory, filename)


@app.route('/module_path')
def module_path():
    import importlib, os
    src_module = request.args.get('src_module')
    if not src_module:
        return jsonify({"status": "error", "message": "No module specified"}), 400
    try:
        mod = importlib.import_module(src_module)
        src_file = os.path.abspath(mod.__file__)
        src_dir = os.path.dirname(src_file)
        return jsonify({"status": "success", "src_path": src_dir})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500


@app.route('/clone_as_new')
def clone_as_new():
    from newprojecthelper import copybuildtest
    import importlib
    import os

    src_module = request.args.get('src_module')
    target_id = request.args.get('target_id')
    target_type = request.args.get('target_type')

    if not all([src_module, target_id, target_type]):
        return jsonify({"status": "error", "message": "Missing parameters"}), 400

    try:
        mod = importlib.import_module(src_module)
        src_file = os.path.abspath(mod.__file__)
        src_dir = os.path.dirname(src_file)
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500

    try:
        copybuildtest(src_dir, target_id)
        return jsonify({
            "status": "success",
            "path": f"/testsrc/src/{target_id}",
            "src_path": src_dir
        }), 200
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500





@app.route("/test/<test_id>")
def test_details(test_id):
    test_runner.reload_tests()

    matching_tests = []
    for info in apphelpers.testfile_registry.values():
        test_id_normalized = info.get("id", "").replace(" ", "_")
        if test_id_normalized == test_id:
            test_copy = dict(info)
            test_copy['latest_status'] = {}
            # for each type, fetch the most recent report status
            for type_name, internal_id in info.get('types', {}).items():
                all_summaries = db.get_all_reports_summary()
                # find reports matching this internal_id
                matching_reports = [
                    r for r in all_summaries
                    if internal_id in os.path.basename(r[0])
                ]
                if matching_reports:
                    # take the most recent
                    latest = max(matching_reports, key=lambda r: r[3])
                    test_copy['latest_status'][type_name] = latest[2]  # r[2] is status
                else:
                    test_copy['latest_status'][type_name] = None
            matching_tests.append(test_copy)

    if not matching_tests:
        return "Test not found", 404

    # single internal_id report filtering
    internal_id = next(iter(matching_tests[0]['types'].values())) if matching_tests[0].get('types') else None

    latest_summary = db.get_latest_report_summary()
    all_logs = db.get_failed_steps_log(internal_id) if internal_id else []
    failed_step_names = {step["step_name"] for step in latest_summary if step["status"] == "FAIL"}
    # keep only logs corresponding to failed steps
    failure_logs = [log for log in all_logs if log.get("name") in failed_step_names]

    reports = [
        {
            "filename": os.path.basename(r[0]),
            "filepath": r[0],
            "duration": r[1],
            "status": r[2],
            "timestamp": r[3]
        }
        for r in db.get_all_reports_summary()
        if internal_id and internal_id in os.path.basename(r[0])
    ]
    reports.sort(key=lambda r: r["timestamp"], reverse=True)
    reports = reports[:5]

    return render_template(
        "test_detail.html",
        testname=test_id,
        test_info=matching_tests,
        reports=reports,
        internal_id=internal_id,
        latest_summary=latest_summary,
        failure_logs=failure_logs
    )




if __name__ == "__main__":
    if not os.path.exists(DB_PATH):
        test_runner.db.init_report_db(DB_PATH)
    app.run(host="0.0.0.0", port=8080, debug=False)
