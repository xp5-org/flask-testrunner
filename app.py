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


@app.route("/testfile_list")
def testfile_list():
    test_runner.reload_tests()

    result = []
    for modname, info in apphelpers.testfile_registry.items():
        result.append({
            "id": info["id"],
            "module": modname,
            "types": info["types"],
            "system": info.get("system"),
            "platform": info.get("platform")
        })
    return jsonify(result)


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


@app.route("/clone_as_new")
def clone_as_new():
    from newprojecthelper import copybuildtest, newprojdir
    
    testname = request.args.get('testname')
    outputname = request.args.get('outputname')

    if not testname or not outputname:
        return jsonify({"status": "error", "message": "testname and outputname required"}), 400

    try:
        newprojdir(outputname)
        copybuildtest(testname, outputname)
        return jsonify({"status": "success", "path": f"/testsrc/src/{outputname}"}), 200
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500




if __name__ == "__main__":
    if not os.path.exists(DB_PATH):
        test_runner.db.init_report_db(DB_PATH)
    app.run(host="0.0.0.0", port=8080, debug=False)
