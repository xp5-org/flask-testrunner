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
    print("DEBUGL LATEST SUMMARY: ", latest_summary)

    return render_template(
        "index.html",
        summaries=summaries,
        latest_summary=latest_summary
    )


@app.route("/cloneproj")
def cloneproj():
    return render_template("cloneproj.html")



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
    return jsonify(result)


@app.route('/test-logs/<test_id>')
def view_test_logs(test_id):
    db = ReportDB()
    failed_logs = db.get_failed_steps_log(test_id)
    return render_template('logs.html', logs=failed_logs, test_id=test_id)



@app.route("/run/<testname>")
def run_named_tests(testname):
    # print(f"run {testname} called") - testname is the reg key

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


@app.route("/clone_as_new")
def clone_as_new():
    from newprojecthelper import copybuildtest
    
    testname = request.args.get('testname')
    outputname = request.args.get('outputname')

    if not testname or not outputname:
        return jsonify({"status": "error", "message": "testname and outputname required"}), 400

    try:
        copybuildtest(testname, outputname)
        return jsonify({"status": "success", "path": f"/testsrc/src/{outputname}"}), 200
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500
    



@app.route("/test/<test_id>")
def test_details(test_id):
    test_runner.reload_tests()

    test_info = next(
        (
            info for info in apphelpers.testfile_registry.values()
            if info.get("id", "").replace(" ", "_") == test_id
        ),
        {}
    )

    internal_id = None
    if test_info.get('types'):
        internal_id = next(iter(test_info['types'].values()))

    all_summaries = db.get_all_reports_summary()
    print("allsumarydebug: ", all_summaries)
    reports = [
        {
            "filename": os.path.basename(r[0]),
            "filepath": r[0],
            "duration": r[1],
            "status": r[2],
            "timestamp": r[3]
        }
        for r in all_summaries
        if internal_id and internal_id in os.path.basename(r[0])
    ]

    latest = db.get_latest_report_summary()
    latest_summary = []
    for r in latest:
        filepath, duration, status = r
        step_name = os.path.basename(filepath)
        latest_summary.append((step_name, duration, status))


    failure_logs = []
    if internal_id:
        failure_logs = db.get_failed_steps_log(internal_id)

    # example log to test js/html template
    # failure_logs = [
    #         {
    #             "name": "STEP_FAILURE",
    #             "output": "Error: Test error message"
    #         },
    #         {
    #             "name": "TIMEOUT_WARN",
    #             "output": "Warning: Test warning messsage"
    #         } ]
    # print("failurelogsdb: ", failure_logs)

    reports.sort(key=lambda r: r["timestamp"], reverse=True)
    reports = reports[:5]
    
    return render_template(
        "test_detail.html",
        testname=test_id,
        test_info=test_info,
        reports=reports,
        internal_id=internal_id,
        latest_summary=latest_summary,
        failure_logs=failure_logs
    )











if __name__ == "__main__":
    if not os.path.exists(DB_PATH):
        test_runner.db.init_report_db(DB_PATH)
    app.run(host="0.0.0.0", port=8080, debug=False)
