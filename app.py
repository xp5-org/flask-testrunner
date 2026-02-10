import os
import re
import threading
import json
import sys
import apphelpers
import dispatchhelper
import importlib.util
from appstate import progress_state
from flask import Flask, render_template, send_from_directory, jsonify, request, abort
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




@app.context_processor
def inject_nav_and_paths():
    return {
        "nav_actions": build_nav(app),
        "paths": {}  # default empty dict so tojson never fails
    }



def nav(label):
    def decorator(f):
        f.nav_label = label
        return f
    return decorator




@app.route('/favicon.ico')
def favicon():
    paths = {"mode": "home"}
    return app.send_static_file('favicon.ico')






@app.route("/api/teststeps/<path:module_path>", methods=["GET"])
def get_test_steps(module_path):
    parts = module_path.split('.')
    target_file_base = next((p for p in parts if p.startswith("__testlist__")), None)
    
    if not target_file_base:
        abort(404)

    fname = target_file_base + ".py"
    testlist_path = None

    for root, dirs, files in os.walk(TESTLIST_ROOT):
        if fname in files:
            testlist_path = os.path.join(root, fname)
            break

    if testlist_path is None:
        abort(404)

    # --- STRATEGY 1: Try to Import and read CONFIG['steps'] ---
    found_steps = []
    module_name = "_testlist_%s" % abs(hash(testlist_path))

    try:
        spec = importlib.util.spec_from_file_location(module_name, testlist_path)
        if spec and spec.loader:
            module = importlib.util.module_from_spec(spec)
            sys.modules[module_name] = module
            
            # Execute the module
            spec.loader.exec_module(module)
            
            # extract steps if CONFIG exists
            if hasattr(module, 'CONFIG'):
                found_steps = module.CONFIG.get("steps", [])
                
    except Exception:
        # If the file crashes on import (syntax error, runtime error), 
        # just ignore it and fall through to the text scan.
        found_steps = []
    finally:
        # Always clean up sys.modules to prevent pollution
        if module_name in sys.modules:
            del sys.modules[module_name]

    # If we found valid steps via import, return them immediately
    if found_steps:
        return jsonify(found_steps)

    # We reach here if import failed OR if CONFIG['steps'] was empty/missing
    try:
        with open(testlist_path, "r") as f:
            lines = f.readlines()
    except Exception:
        abort(500)

    step_actions = []
    decorator_active = False

    for line in lines:
        stripped = line.strip()
        # Using the regex from your working live version
        if re.match(r"^@register_mytest\b", stripped):
            decorator_active = True
        elif decorator_active:
            if stripped.startswith("def "):
                match = re.match(r"def (\w+)\s*\(", stripped)
                if match:
                    func_name = match.group(1)
                    step_actions.append({
                        "action": "MANUAL_EDIT_ONLY" + func_name,
                        "param": {"string": ""},
                        "subaction": ""
                    })
                decorator_active = False
            elif stripped.startswith("@") or stripped == "":
                continue
            else:
                decorator_active = False

    return jsonify(step_actions)



@app.route("/")
@nav("Home")
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
@nav("Clone")
def cloneproj():
    return render_template("cloneproj.html")





@app.route("/testbuilder", methods=["GET", "POST"])
@nav("Testbuilder")
def testbuilder():
    proj_dir = "/testsrc/pyhelpers"
    raw_dispatch = dispatchhelper.load_step_dispatch(proj_dir)
    schemas = dispatchhelper.PROJECT_STEP_SCHEMAS.get(proj_dir, {})

    dispatch = {}
    for name, func in raw_dispatch.items():
        if getattr(func, "_is_teststep", False):
            dispatch[name] = func

    if request.method == "POST":
        data = request.json
        test_id = data.get("testid")
        steps = data.get("steps", [])

        if not test_id:
            return jsonify({"status": "error", "message": "No test ID provided"}), 400

        # copy kwargs from schemma
        for step in steps:
            func_name = step.get("action")
            if func_name in schemas:
                for k, v in schemas[func_name].items():
                    step.setdefault("param", {})[k] = step["param"].get(k, v)

        success, message = sync_test_steps(test_id, steps)
        if success:
            return jsonify({"status": "ok", "message": message})
        else:
            return jsonify({"status": "error", "message": message}), 500

    output = {
        "functions": list(dispatch.keys()),
        "schemas": schemas
    }

    action_schema = get_dynamic_action_schema(output)
    return render_template("testbuilder.html", schema=action_schema)









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
    if progress_state.testname:
        print("ERROR ALREADY RUNNING")
        return "Error: test already running", 400

    print(f"run {testname} called")
    progress_state.step = "0/0"
    progress_state.testname = testname

    def run():
        print(f"Thread started for {testname}")
        res = test_runner.run_testfile(testname, progress_state)
        print(f"Thread finished for {testname}. Results found: {len(res)}")
        progress_state.step = "Done"
        progress_state.testname = None

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
    import importlib
    src_module = request.args.get('src_module')
    if not src_module:
        return jsonify({"status": "error", "message": "No module specified"}), 400
    try:
        mod = importlib.import_module(src_module)
        cfg = getattr(mod, "CONFIG", None)
        if cfg is None:
            return jsonify({"status": "error", "message": "No config found in module"}), 404
        return jsonify({"status": "success", "config": cfg})
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
    all_summaries = db.get_all_reports_summary(test_parent_name=test_id)

    # test status for buttons
    # returns testparentname, testtype, status
    latest_summary = db.get_latest_namedteststatus(test_id)
    summary_by_type = {}
    for s in latest_summary:
        if s["testparentname"] != test_id:
            continue

        summary_types = s["types"]
        if isinstance(summary_types, str):
            summary_types = [summary_types]

        for type_name in summary_types:
            summary_by_type.setdefault(type_name, []).append(s)
    
    # search for failed test-steps from the testparentname
    matching_tests = []
    for info in apphelpers.testfile_registry.values():
        if info.get("id") != test_id:
            continue

        test_copy = dict(info)
        test_copy["latest_status"] = {}
        for type_name in info.get("types", {}):
            type_summary = summary_by_type.get(type_name, [])

            if not type_summary:
                test_copy["latest_status"][type_name] = None
            elif any(s["status"] in ("FAIL", "ERROR") for s in type_summary):
                test_copy["latest_status"][type_name] = "FAIL"
            else:
                test_copy["latest_status"][type_name] = "PASS"

        matching_tests.append(test_copy)

    if not matching_tests:
        return "Test not found", 404

    # build list of report statuses with matching testparentname
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

    # sort & return only the most recent 5 results
    reports.sort(key=lambda r: r["timestamp"], reverse=True)
    reports = reports[:5]

    return render_template(
        "test_detail.html",
        testname=test_id,
        test_info=matching_tests,
        reports=reports,
        internal_id=test_id,
        latest_summary=latest_summary,
        failure_logs=failure_logs
    )




# need to move these to helper files
TESTLIST_ROOT = "/testsrc/sourcedir"

def get_dynamic_action_schema(output_data):
    action_schema = {}
    
    for func_name in output_data["functions"]:
        action_schema[func_name] = output_data["schemas"].get(func_name, {})
        
    return action_schema


def sync_test_steps(module_name, new_steps):
    import os
    meta = apphelpers.testfile_registry.get(module_name)
    if not meta or "__full_path__" not in meta:
        return False, f"Registry lookup failed for {module_name}"

    file_path = meta["__full_path__"]
    
    if not os.path.exists(file_path):
        return False, f"File not found on disk: {file_path}"

    try:
        update_test_steps_in_file(file_path, new_steps)
        return True, "Successfully synced steps to disk"
    except Exception as e:
        return False, str(e)


def update_test_steps_in_file(file_path, steps):
    print(f"[DEBUG] Syncing file (No-Regex): {file_path}")
    
    with open(file_path, 'r') as f:
        lines = f.readlines()

    header = []
    footer = []
    found_steps = False
    done_steps = False

    for line in lines:
        if not found_steps:
            if '"steps": [' in line or "'steps': [" in line:
                found_steps = True
                header.append(line.split(':')[0] + ': ')
            else:
                header.append(line)
        elif found_steps and not done_steps:
            # find closing bracket of the steps list
            if line.strip().startswith(']') or line.strip() == ']}':
                done_steps = True
                if '}' in line:
                    footer.append('}\n')
        elif done_steps:
            footer.append(line)

    if not found_steps:
        print("[DEBUG] ERROR: Could not find 'steps' key in file.")
        return

    new_steps_block = json.dumps(steps, indent=8).replace("true", "True").replace("false", "False").replace("null", "None")
    
    with open(file_path, 'w') as f:
        f.writelines(header)
        f.write(new_steps_block)
        if footer:
            f.write(",\n") # Ensure comma if more config follows
            f.writelines(footer)
        else:
            f.write("\n}") # Close the dict if footer was empty

    print(f"[DEBUG] File updated successfully: {file_path}")


def build_nav(app):
    items = []
    for endpoint, view in app.view_functions.items():
        label = getattr(view, "nav_label", None)
        if label:
            items.append({"name": label, "endpoint": endpoint})
    return items




if __name__ == "__main__":
    # dispatch addon debug
    # from dispatchhelper import load_step_dispatch, get_step_schema
    # project_path = "/testsrc/pyhelpers/"
    # dispatch = load_step_dispatch(project_path)
    # schemas = get_step_schema(project_path)
    # output = {
    #     "functions": list(dispatch.keys()),
    #     "schemas": schemas
    # }

    # action_schema = get_dynamic_action_schema(output)
    # print("JSONDEBUG: ", json.dumps(action_schema, indent=4))


    if not os.path.exists(DB_PATH):
        test_runner.db.init_report_db(DB_PATH)
    app.run(host="0.0.0.0", port=8080, debug=False)
