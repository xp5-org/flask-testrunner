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
    print("failed loads: ", test_runner.failed_loads)
    return jsonify(test_runner.failed_loads)


@app.route("/testfile_list")
def testfile_list():
    test_runner.reload_tests()

    result = []
    for modname, info in apphelpers.testfile_registry.items():
        result.append({
            "id": info["id"],
            "display_name": info["id"],
            "module": modname,
            "types": info["types"],
            "system": info.get("system"),
            "platform": info.get("platform")
        })
    return jsonify(result)


@app.route("/run/<path:testname>")
def run_named_tests(testname):
    print(f"run {testname} called")

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
        "testid": progress_state.testid,
        "testtype": progress_state.testtype,
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
    from newprojecthelper import copybuildtest, copy_sourcedir
    import importlib
    import os

    src_module = request.args.get('src_module')
    target_id = request.args.get('target_id')
    target_type = request.args.get('target_type')
    testlist_name = request.args.get('testlist_name')
    testfile_name = request.args.get('testfile_name')
    testfile_targetdir = request.args.get('target_path')

    #print("testfile name dwebug: ", testfile_name)

    if not all([src_module, target_id, target_type]):
        return jsonify({"status": "error", "message": "Missing parameters"}), 400

    try:
        mod = importlib.import_module(src_module)
        src_file = os.path.abspath(mod.__file__)
        src_dir = os.path.dirname(src_file)
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500

    try:
        dest_dir, testlist_file = copybuildtest(
        src_dir, testfile_name, testlist_name,
        dest_dir=testfile_targetdir,
        cmainfile=request.args.get('cmainfile'),
        testtype=request.args.get('target_type'),
        archtype=request.args.get('archtype'),
        platform=request.args.get('platform'),
        viceconf=request.args.get('viceconf'),
        linkerconf=request.args.get('linkerconf')
        )

        copy_sourcedir(src_dir + '/src', dest_dir + '/src')


        return jsonify({
            "status": "success",
            "path": dest_dir,
            "src_path": src_dir,
            "testlist_file": testlist_file
        }), 200
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500



@app.route("/test/<test_id>")
def test_details(test_id):
    report_name = request.args.get("report_name")
    test_runner.reload_tests()

    matching_tests = []
    # all_summaries = db.get_all_reports_summary()
    all_summaries = db.get_all_reports_summary(test_parent_name=test_id)


    for info in apphelpers.testfile_registry.values():
        if info.get("id", "") != test_id:
            continue

        test_copy = dict(info)
        test_copy['latest_status'] = {}

        for type_name in info.get('types', {}):
            failed_steps = db.get_failed_steps_log(test_id, type_name)
            test_copy['latest_status'][type_name] = 'FAIL' if failed_steps else 'PASS'

        matching_tests.append(test_copy)


    if not matching_tests:
        return "Test not found", 404

    latest_summary = db.get_latest_report_summary(test_id)
    failure_logs = db.get_failed_steps_log(test_id)

    reports = []
    for r in all_summaries:
        filename = os.path.basename(r[0])
        if report_name is None or report_name in filename:
            reports.append({
                "filename": filename,
                "filepath": r[0],
                "duration": r[1],
                "status": r[2],
                "timestamp": r[3]
            })



    reports.sort(key=lambda r: r["timestamp"], reverse=True)
    reports = reports[:5]

    #print("FAILESTEPS DEBUG: ", failure_logs)
    #print("REPORTSDEBUG: ", reports)
    return render_template(
        "test_detail.html",
        testname=test_id,
        test_info=matching_tests,
        reports=reports,
        internal_id=test_id,
        latest_summary=latest_summary,
        failure_logs=failure_logs
    )





if __name__ == "__main__":
    if not os.path.exists(DB_PATH):
        test_runner.db.init_report_db(DB_PATH)
    app.run(host="0.0.0.0", port=8080, debug=False)
