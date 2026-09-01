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
from jinja2 import ChoiceLoader, FileSystemLoader
import apphelpers, test_runner
import runhelper
import apphelpers as testid
from dbhelper import ReportDB, db
from appstate import build_nav, nav, render_info_markup
from appstate import process_registry
from apiv1 import api_v1
from step_validator import validate_config_steps, format_results_text, has_blocking_issues
from repair_config import repair_config, apply_step_param_fixes
from newprojecthelper import copybuildtest, copy_sourcedir, update_config_in_file


#######################################
### config stuff #####################
app = Flask(__name__)
app.jinja_env.filters["linkify"] = apphelpers.linkify
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
REPORT_DIR = os.path.join(BASE_DIR, "reports")
FLASKRUNNER_HELPERDIR = "/testrunnerapp/helpers"

TESTLIST_ROOT     = "/testsrc/sourcedir"
TESTSRC_HELPERDIR = "/testsrc/pyhelpers"
TESTSRC_TESTLISTDIR = "/testsrc/mytests"
PROJFILES_ROOT    = "/testsrc"   # project tree served read-only at /projfiles/
DB_PATH = os.path.join(BASE_DIR, "report.sqlite")
#######################################
if TESTSRC_TESTLISTDIR not in sys.path:
    sys.path.insert(0, TESTSRC_TESTLISTDIR)


@app.context_processor
def inject_nav_and_paths():
    return {
        "nav_actions": build_nav(app),
        "paths": {}
    }


@app.route('/favicon.ico')
def favicon():
    return app.send_static_file('favicon.ico')


# inspection route for test steps to get their filesystem path
@app.route("/api/teststeps/<path:module_path>", methods=["GET"])
def get_test_steps(module_path):
    test_runner.reload_tests()
    # Callers take ids from /api/v1/tests, which hands out slugs
    # ("98se-isapc.98se-boot"), while the registry is keyed by module dot-path.
    # Resolve first, or a slug misses the registry, finds no "__testlist__"
    # segment to fall back on, and silently reports no steps at all.
    modname = testid.resolve(module_path, apphelpers.testfile_registry.keys()) or module_path
    meta = apphelpers.testfile_registry.get(modname)
    testlist_path = meta.get("__full_path__") if meta else None

    if not testlist_path:
        parts = modname.split('.')
        target_file_base = next((p for p in parts if p.startswith("__testlist__")), None)
        if not target_file_base:
            return jsonify([]), 200
        fname = target_file_base + ".py"
        for root, dirs, files in os.walk(TESTLIST_ROOT):
            if fname in files:
                testlist_path = os.path.join(root, fname)
                break

    if not testlist_path or not os.path.exists(testlist_path):
        return jsonify([]), 200

    config = test_runner.load_config_from_path(testlist_path)
    return jsonify(config.get("steps", []) if config else [])



# attempt to look over steps for json errors or missing params. doesnt catch it all
@app.route("/api/validate_steps", methods=["GET", "POST"])
def api_validate_steps():
    import ast

    dispatch_dir = TESTSRC_HELPERDIR

    config        = None
    resolved_vars = None

    if request.method == "POST":
        body = request.get_json(silent=True) or {}
        config = body.get("config")
        resolved_vars = body.get("resolved_vars")
        if not config:
            return jsonify({"status": "error",
                            "message": "POST body must contain 'config' key"}), 400
    else:
        src_module = request.args.get("src_module")
        if not src_module:
            return jsonify({"status": "error",
                            "message": "src_module param required"}), 400

        if not apphelpers.testfile_registry:
            test_runner.reload_tests()

        meta      = apphelpers.testfile_registry.get(src_module)
        full_path = meta.get("__full_path__") if meta else None

        if not full_path or not os.path.exists(full_path):
            return jsonify({"status": "error",
                            "message": f"Module not found: {src_module}"}), 404

        try:
            with open(full_path, "r") as f:
                source = f.read()
            tree = ast.parse(source)
            config = None
            for node in ast.walk(tree):
                if (isinstance(node, ast.Assign)
                        and any(isinstance(t, ast.Name) and t.id == "CONFIG"
                                for t in node.targets)):
                    config = ast.literal_eval(node.value)
                    break
            if config is None:
                return jsonify({"status": "error",
                                "message": "No CONFIG found in file"}), 404
        except Exception as e:
            return jsonify({"status": "error", "message": str(e)}), 500

    try:
        results    = validate_config_steps(config, dispatch_dir,
                                           resolved_vars=resolved_vars)
        has_errors = has_blocking_issues(results)
        summary    = format_results_text(results)
        return jsonify({
            "status":     "ok",
            "results":    results,
            "has_errors": has_errors,
            "summary":    summary,
        })
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500




# index front page stuff

FRONT_PAGE_REPORTS_PER_TESTLIST = 3
_index_cache_lock = threading.Lock()
_index_cache = {"report_id": None, "data": None}

def get_index_summaries(db_conn):
    latest_report_id = db_conn.get_max_report_id()

    with _index_cache_lock:
        if _index_cache["data"] is not None and _index_cache["report_id"] == latest_report_id:
            return _index_cache["data"]

    if not apphelpers.testfile_registry:
        test_runner.reload_tests()

    data = (
        db_conn.get_all_reports_summary(per_parent_limit=FRONT_PAGE_REPORTS_PER_TESTLIST),
        db_conn.get_latest_report_summary(known_test_ids=apphelpers.testfile_registry.keys()),
    )

    with _index_cache_lock:
        _index_cache["report_id"] = latest_report_id
        _index_cache["data"] = data

    return data


@app.route("/")
@nav("Home")
def index():
    if not os.path.exists(REPORT_DIR):
        os.makedirs(REPORT_DIR)
    db_conn = ReportDB()
    summaries, latest_summary = get_index_summaries(db_conn)
    return render_template("index.html",
                           summaries=summaries, latest_summary=latest_summary)







# tools and other things
@app.route("/cloneproj")
@nav("Clone")
def cloneproj():
    test_runner.reload_tests()
    return render_template("cloneproj.html")


@app.route("/testbuilder", defaults={'test_id': None}, methods=["GET", "POST"])
@app.route("/testbuilder/<path:test_id>", methods=["GET", "POST"])
@nav("Testbuilder")
def testbuilder(test_id):
    test_runner.reload_tests()
    proj_dir    = TESTSRC_HELPERDIR
    raw_dispatch = dispatchhelper.load_step_dispatch(proj_dir)
    schemas     = dispatchhelper.PROJECT_STEP_SCHEMAS.get(proj_dir, {})

    dispatch = {
        name: func for name, func in raw_dispatch.items()
        if getattr(func, "_is_teststep", False)
    }

    if request.method == "POST":
        data    = request.json
        test_id = data.get("testid")
        steps   = data.get("steps", [])
        CONFIG  = data.get("build_config", {})

        # if it gets None, it will be cleared or emptied since javascript doesnt like "None" in the same way python does
        if data.get("description") is not None:
            CONFIG["description"] = data["description"]

        if not test_id:
            return jsonify({"status": "error", "message": "No test ID provided"}), 400

        for step in steps:
            func_name = step.get("action")
            if func_name in schemas:
                for k, v in schemas[func_name].items():
                    step.setdefault("param", {})[k] = step["param"].get(k, v)

        success, message = sync_test_data(test_id, steps, CONFIG)
        return jsonify({"status": "ok" if success else "error", "message": message}), \
               200 if success else 500

    # match test module name "98SE_isapc.__testlist__98se_boot" or a slug
    # "98se-isapc.98se-boot" as example
    if test_id:
        mods = list(apphelpers.testfile_registry.keys())
        modname = testid.resolve(test_id, mods)
        if modname:
            test_id = testid.slug_for(modname, mods)

    output       = {"functions": list(dispatch.keys()), "schemas": schemas}
    action_schema = get_dynamic_action_schema(output)
    return render_template("testbuilder.html", schema=action_schema, initial_testid=test_id)







# test registry
@app.route("/failed_tests")
def failedtestsinfo():
    test_runner.reload_tests()
    return jsonify(test_runner.failed_loads)


@app.route("/testfile_list")
def testfile_list():
    test_runner.reload_tests()
    _, mod_to_slug = testid.build_index(apphelpers.testfile_registry.keys())
    result = []
    for modname, info in apphelpers.testfile_registry.items():
        latest_status = {}
        rows = db.get_latest_namedteststatus(info["id"])
        if rows:
            for r in rows:
                latest_status[r["types"]] = r["status"]
        result.append({
            "id":           mod_to_slug.get(modname, modname),
            "display_name": info["id"],
            "module":       modname,
            "path":         apphelpers.project_relpath(info),
            "container_id": testid.container_id(info["id"], info.get("system")),
            "types":        info["types"],
            "system":       info.get("system"),
            "platform":     info.get("platform"),
            "description":  info.get("description") or "",
            "tags":          info.get("tags") or [],
            "latest_status": latest_status,
        })
    return jsonify(result)









# test runner launcher

@app.route("/run/<path:testname>")
def run_named_tests(testname):
    # old can get rid of soon, old api junk
    try:
        runhelper.launch_run(testname)
    except runhelper.RunBusyError:
        return "Error: test already running", 400
    return "Started"


@app.route("/progress")
# probably need to add a timer return on this , current step runtime vs all steps runtime? 
def progress():
    return jsonify({
        "step":      progress_state.step,
        "testname":  progress_state.testname,
        "testid":    progress_state.testid,
        "testtype":  progress_state.testtype,
        "step_name": progress_state.step_name,
        "processes": process_registry.snapshot(),
    })


# generate the reports
@app.route("/reports/<path:filepath>")
def view_report(filepath):
    full_path = os.path.join(REPORT_DIR, filepath)
    if not os.path.isfile(full_path):
        return "File not found", 404
    directory, filename = os.path.split(full_path)
    return send_from_directory(directory, filename)


@app.route("/projfiles/<path:filepath>")
# i forget what is using this, todo
def serve_projfile(filepath):
    """pull file from the project dir"""
    full_path = os.path.normpath(os.path.join(PROJFILES_ROOT, filepath))
    # Reject path-traversal that escapes the project root.
    if full_path != PROJFILES_ROOT and not full_path.startswith(PROJFILES_ROOT + os.sep):
        abort(403)
    if not os.path.isfile(full_path):
        return "File not found", 404
    directory, filename = os.path.split(full_path)
    return send_from_directory(directory, filename)



# a way to reload whole app after python code changes without having to use docker or rdp
@app.route("/reload", methods=["GET", "POST"])
def reload_app():
    """Re-exec this process in place to pick up code changes to app.py """
    import time as _time

    def _reexec():
        _time.sleep(0.5)
        try:
            import fcntl
            for fd in range(3, 4096):
                try:
                    flags = fcntl.fcntl(fd, fcntl.F_GETFD)
                    fcntl.fcntl(fd, fcntl.F_SETFD, flags | fcntl.FD_CLOEXEC)
                except OSError:
                    pass
        except Exception:
            pass
        os.execv(sys.executable, [sys.executable] + sys.argv)

    threading.Thread(target=_reexec, daemon=True).start()
    return jsonify({"status": "reloading", "pid": os.getpid()}), 200






# loading test configs
@app.route('/module_path')
def module_path():
    import ast

    src_module = request.args.get('src_module')
    if not src_module:
        return jsonify({"status": "error", "message": "No module specified"}), 400

    if not apphelpers.testfile_registry:
        test_runner.reload_tests()

    meta      = apphelpers.testfile_registry.get(src_module)
    full_path = meta.get("__full_path__") if meta else None

    if not full_path or not os.path.exists(full_path):
        return jsonify({"status": "error",
                        "message": f"Module not found in registry: {src_module}"}), 404

    try:
        with open(full_path, 'r') as f:
            source = f.read()
        tree = ast.parse(source)
        cfg  = None
        for node in ast.walk(tree):
            if (isinstance(node, ast.Assign)
                    and any(isinstance(t, ast.Name) and t.id == 'CONFIG'
                            for t in node.targets)):
                cfg = ast.literal_eval(node.value)
                break
        if cfg is None:
            return jsonify({"status": "error", "message": "No CONFIG found in file"}), 404

        # run the same repair pass used at clone time
        repaired_cfg, repair_log = repair_config(cfg, cfg)
        if repair_log:
            print(f"[module_path] {len(repair_log)} repair(s) for {src_module}:")
            for e in repair_log:
                print(f"  [{e['scope']}] {e['key']}: {e['old']!r} → {e['new']!r}")

        return jsonify({"status": "success", "config": repaired_cfg,
                        "repairs": repair_log})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500






# project clonetool
# this is a good start but has many missign features around managing disks in qemu

@app.route('/clone_as_new')
def clone_as_new():
    import importlib

    src_module = request.args.get('src_module')
    target_id  = request.args.get('target_id')

    if not all([src_module, target_id]):
        return jsonify({"status": "error", "message": "Missing parameters"}), 400

    try:
        mod              = importlib.import_module(src_module)
        testfile_path    = os.path.abspath(mod.__file__)
        testfile_src_dir = os.path.dirname(testfile_path)
        module_config    = getattr(mod, 'CONFIG', {})

        dst_dir_from_client = request.args.get('target_path')

        # Build the identity overrides from the client
        SKIP = {'src_module', 'target_id', 'target_path', 'testlist_name', 'step_param_fixes'}
        client_overrides = {k: v for k, v in request.args.to_dict().items()
                            if k not in SKIP}

        # Merge: module config as base, client values on top
        new_vars = {**module_config, **client_overrides}

        # Apply step param fixes from the clone UI's Step 4 
        step_fixes = []
        raw_step_fixes = request.args.get('step_param_fixes')
        if raw_step_fixes:
            try:
                step_fixes = json.loads(raw_step_fixes)
            except (TypeError, ValueError) as e:
                print(f"[clone_as_new] could not parse step_param_fixes: {e}")
                step_fixes = []
        fixed_config, ui_fix_log = apply_step_param_fixes(module_config, step_fixes)

        # Run repair pass — fixes broken templates, known legacy step-param
        # renames, and clears stale step params
        repaired_config, repair_log = repair_config(fixed_config, new_vars)
        repair_log = ui_fix_log + repair_log

        if repair_log:
            print(f"[clone_as_new] {len(repair_log)} repair(s) applied:")
            for entry in repair_log:
                print(f"  [{entry['scope']}] {entry['key']}: "
                      f"{entry['old']!r} → {entry['new']!r}")

        # Apply client overrides on top of repaired config
        for k, v in client_overrides.items():
            if k in repaired_config and k not in ('structure', 'steps'):
                repaired_config[k] = v

        dest_dir, testlist_file = copybuildtest(
            src_dir=testfile_src_dir,
            outputname=target_id,
            dest_dir=dst_dir_from_client,
            testlist_name=request.args.get('testlist_name'),
            repaired_config=repaired_config,
        )

    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({"status": "error", "message": str(e)}), 500

    return jsonify({
        "status":        "success",
        "src_path":      testfile_src_dir,
        "dst_dir":       dest_dir,
        "testlist_file": testlist_file,
        "config_used":   module_config,
        "repairs":       repair_log,
    }), 200






# The container view: one page per __testparent__.py, listing its children.
@app.route("/test/<path:container>")
def test_details(container):
    report_name = request.args.get("report_name")
    test_runner.reload_tests()

    children = testid.resolve_container(container, apphelpers.testfile_registry)
    if not children:
        return "Container not found", 404

    meta        = apphelpers.testfile_registry[children[0]]
    human_label = meta["id"]

    container_dirpath = None
    full_path = meta.get("__full_path__")
    if full_path:
        container_dirpath = os.path.dirname(os.path.abspath(full_path))
    container_parent = apphelpers.load_parent(container_dirpath) if container_dirpath else None
    container_description = (container_parent or {}).get("description")
    container_batch_all    = bool((container_parent or {}).get("batch_run_all"))
    container_batch_failed = bool((container_parent or {}).get("batch_run_failed"))

    all_summaries  = db.get_all_reports_summary(test_parent_name=human_label, per_parent_limit=3)
    latest_summary = db.get_latest_namedteststatus(human_label)

    summary_by_type = {}
    for s in latest_summary:
        if s["testparentname"] != human_label:
            continue
        types_field = s.get("types")
        summary_types = types_field if isinstance(types_field, list) else [
            t.strip() for t in (types_field or "").split(",") if t.strip()
        ]
        for type_name in summary_types:
            summary_by_type.setdefault(type_name, []).append(s)

    all_mods = list(apphelpers.testfile_registry.keys())
    matching_tests = []
    for modname in children:
        info = apphelpers.testfile_registry[modname]
        test_copy = dict(info)
        test_copy["module"] = modname
        test_copy["slug"]   = testid.slug_for(modname, all_mods)
        test_copy["path"]   = apphelpers.project_relpath(info)
        test_copy["latest_status"] = {}
        for type_name in info.get("types", {}):
            type_summary = summary_by_type.get(type_name, [])
            if not type_summary:
                test_copy["latest_status"][type_name] = None
            elif any(s["status"] in ("FAIL", "ERROR") for s in type_summary):
                test_copy["latest_status"][type_name] = "FAIL"
            else:
                test_copy["latest_status"][type_name] = "PASS"
        test_copy["pause_label"] = _pause_step_label(modname)
        matching_tests.append(test_copy)

    latest_summary = db.get_latest_report_summary(human_label)
    failure_logs   = db.get_failed_steps_log(human_label)

    build_artifacts = []
    seen_artifacts  = set()
    for modname in children:
        found = db.get_latest_build_artifacts(modname)
        if not found:
            try:
                cfg = _load_module_config(modname)
                if cfg:
                    from repair_config import repair_config
                    repaired, _ = repair_config(cfg, cfg)
                    found = db.extract_build_artifacts(repaired)
            except Exception as e:
                print(f"[test_details] artifact fallback failed for {modname}: {e}")
        for a in (found or []):
            key = repr(a)
            if key not in seen_artifacts:
                seen_artifacts.add(key)
                build_artifacts.append(a)

    reports_by_variant = {}
    for r in all_summaries:
        filename = os.path.basename(r[0])
        if report_name is not None and report_name not in filename:
            continue
        variant_id = r[4]
        reports_by_variant.setdefault(variant_id, []).append({
            "filename":  filename,
            "filepath":  r[0],
            "duration":  r[1],
            "status":    r[2],
            "timestamp": r[3],
        })

    for variant_reports in reports_by_variant.values():
        variant_reports.sort(key=lambda r: r["timestamp"], reverse=True)

    for test_copy in matching_tests:
        test_copy["reports"] = reports_by_variant.get(test_copy["module"], [])

    return render_template(
        "test_detail.html",
        testname=human_label,
        test_info=matching_tests,
        slug=container,
        project_path=apphelpers.project_relpath(meta),
        container_description=container_description,
        container_batch_all=container_batch_all,
        container_batch_failed=container_batch_failed,
        latest_summary=latest_summary,
        failure_logs=failure_logs,
        build_artifacts=build_artifacts,
    )


def _pause_step_label(modname: str) -> str | None:
    """First 'pause_on' step in a testlist's CONFIG

    Step numbering matches test_runner.run_tests' enumerate(steps, 1), so
    STEPx here lines up with the unique_name test_runner logs/reports for
    that same step. None if the test has no pause_on step.
    """
    cfg = _load_module_config(modname)
    if not cfg:
        return None
    for i, step in enumerate(cfg.get("steps", []), 1):
        if test_runner._pause_flag(step.get("pause_on", False)):
            return f"PAUSE - STEP{i}"
    return None


# for testbuilder
def _load_module_config(src_module: str) -> dict | None:
    """
    Accepts slug or module dot-path
    Load and return the CONFIG dict from a testlist module.
    Shared by module_path, test_details, and api_build_artifacts.
    """
    import apiv1
    full_path = apiv1._full_path(src_module)
    if not full_path:
        return None
    try:
        return apiv1._extract_config(full_path)
    except ValueError:
        return None


# test desc

@app.route("/api/test_description/<path:test_id>", methods=["GET", "POST"])
def api_test_description(test_id):
    import apiv1
    full_path = apiv1._full_path(test_id)
    if not full_path or not os.path.exists(full_path):
        return jsonify({"status": "error",
                        "message": f"No testlist file on disk for: {test_id}"}), 404

    if request.method == "GET":
        cfg = _load_module_config(test_id)
        if cfg is None:
            return jsonify({"status": "error",
                            "message": f"Could not read CONFIG for: {test_id}"}), 404
        return jsonify({"status": "ok", "id": test_id,
                        "description": cfg.get("description", "")})

    body = request.get_json(silent=True) or {}
    if "description" not in body:
        return jsonify({"status": "error",
                        "message": "POST body must contain 'description'"}), 400
    description = body["description"]
    if description is None:
        description = ""
    if not isinstance(description, str):
        return jsonify({"status": "error",
                        "message": "'description' must be a string"}), 400

    try:
        # update_config_in_file appends the key when the testlist predates it,
        # so this works on a test that has never carried a description.
        update_config_in_file(full_path, {"description": description})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500

    return jsonify({"status": "ok", "id": test_id, "description": description,
                    "written_to": full_path})


@app.route("/api/build_artifacts/<path:test_id>")
def api_build_artifacts(test_id):
    """
    Return build artifacts for a test — from DB if available (most recent
    run), otherwise derived live from the testlist's CONFIG.
    Response: {artifacts: [{artifact_key, artifact_path, exists_on_disk, file_size}]}

    Accepts a slug or a module dot-path. Both the DB and testfile_registry are
    keyed by dot-path, so the id has to be resolved first

    See GET /api/v1/tests/<id>/artifacts, which does the same thing.
    """
    test_runner.reload_tests()
    test_id = testid.resolve(test_id, apphelpers.testfile_registry.keys()) or test_id

    artifacts = db.get_latest_build_artifacts(test_id)

    if not artifacts:
        try:
            cfg = _load_module_config(test_id)
            if cfg:
                from repair_config import repair_config
                repaired, _ = repair_config(cfg, cfg)
                artifacts = db.extract_build_artifacts(repaired)
        except Exception as e:
            return jsonify({"error": str(e), "artifacts": []}), 500

    return jsonify({"artifacts": artifacts})


@app.route("/api/teststep_dirlist", methods=["POST"])
def teststep_dirlist():
    config    = request.json
    base_dir  = config.get("projbasedir", "")
    proj_dir  = config.get("projdir", "")
    full_path = os.path.abspath(os.path.join(base_dir, proj_dir))

    # Multi-pass resolution via apphelpers (single source of truth)
    resolved_paths = apphelpers.resolve_meta(config, config)

    files_found = []
    if os.path.exists(full_path):
        for root, dirs, files in os.walk(full_path):
            for file in files:
                rel_path = os.path.relpath(os.path.join(root, file), full_path)
                files_found.append(rel_path)

    return jsonify({
        "project_root":    full_path,
        "exists":          os.path.exists(full_path),
        "files_on_disk":   files_found,
        "resolved_config": resolved_paths,
    })


@app.route("/api/scan_drivers", methods=["POST"])
def scan_drivers():
    """
    Scan the project's src/ directory for cc65 driver binary files
    (.emd, .mou, .ser, .joy, .tgi) and return pre-computed driver1_path /
    driver1_label suggestions for each one found.

    Expects the full CONFIG dict (or resolved vars) in the request body.
    Returns:
        {
          "src_dir": "/resolved/src/path",
          "exists":  true,
          "drivers": [
            {
              "filename":    "c64-reu.emd",
              "path_token":  "{src}c64-reu.emd",
              "label":       "c64-reu",
              "ext":         ".emd"
            },
            ...
          ]
        }
    """
    config = request.json or {}

    # Resolve {tokens} in src using apphelpers' multi-pass resolver
    resolved = apphelpers.resolve_meta(config, config)
    src_dir  = resolved.get("src", "").rstrip("/")

    DRIVER_EXTS = {".emd", ".mou", ".ser", ".joy", ".tgi"}

    drivers = []
    exists  = os.path.isdir(src_dir)
    if exists:
        for fname in sorted(os.listdir(src_dir)):
            ext = os.path.splitext(fname)[1].lower()
            if ext not in DRIVER_EXTS:
                continue
            stem = os.path.splitext(fname)[0]          # e.g. "c64-reu"
            drivers.append({
                "filename":   fname,
                "path_token": "{src}" + fname,          # e.g. "{src}c64-reu.emd"
                "label":      stem,                     # e.g. "c64-reu"  (sanitized at assemble time)
                "ext":        ext,
            })

    return jsonify({
        "src_dir": src_dir,
        "exists":  exists,
        "drivers": drivers,
    })


@app.route("/api/make_disk_image", methods=["POST"])
def make_disk_image_route():
    """Create a blank FAT disk image of a given size for the current project.

    Body: {"size_mb": 16, "path": "...optional..."} or
          {"size_mb": 16, "config": {...}}  (path derived as
          {projbasedir}{projdir}/{hdd1_img}). Optional: label, overwrite,
          image_name (override hdd1_img). Delegates to the project's
          qemuhelpers.create_fat_disk_image (loaded from TESTSRC_HELPERDIR).
    """
    data      = request.json or {}
    size_mb   = data.get("size_mb")
    path      = data.get("path")
    label     = data.get("label", "")
    overwrite = bool(data.get("overwrite", False))

    if not path:
        config   = data.get("config") or {}
        resolved = apphelpers.resolve_meta(config, config)
        base     = str(resolved.get("projbasedir", "")).rstrip("/")
        projdir  = str(resolved.get("projdir", "")).strip("/")
        img      = data.get("image_name") or resolved.get("hdd1_img") or "hdd.img"
        if base and projdir:
            path = f"{base}/{projdir}/{img}"

    if not path or not size_mb:
        return jsonify({"status": "error",
                        "message": "need size_mb and (path or config with projbasedir/projdir)"}), 400

    abspath = os.path.abspath(path)
    if not abspath.startswith("/testsrc/sourcedir/"):
        return jsonify({"status": "error",
                        "message": f"path must be under /testsrc/sourcedir/: {abspath}"}), 400

    helper_file = os.path.join(TESTSRC_HELPERDIR, "qemuhelpers.py")
    if not os.path.isfile(helper_file):
        return jsonify({"status": "error", "message": "project has no qemuhelpers.py"}), 400

    spec = importlib.util.spec_from_file_location("qemuhelpers_diskutil", helper_file)
    mod  = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(mod)
    except Exception as e:
        return jsonify({"status": "error", "message": f"failed to load qemuhelpers: {e}"}), 500
    if not hasattr(mod, "create_fat_disk_image"):
        return jsonify({"status": "error",
                        "message": "create_fat_disk_image not found in qemuhelpers"}), 400

    ok, msg = mod.create_fat_disk_image(abspath, size_mb, label=label, overwrite=overwrite)
    return jsonify({"status": "ok" if ok else "error", "message": msg, "path": abspath}), \
           (200 if ok else 500)


@app.route("/api/list_disk_images", methods=["GET"])
def list_disk_images():
    """Scan every registered testlist's CONFIG for disk-image file references
    (values ending .img/.qcow2/…), resolve them to absolute paths, and return
    the de-duplicated list with existence + size — so the UI can offer real
    images to clone instead of typing paths."""
    import ast
    test_runner.reload_tests()
    IMG_EXTS = (".img", ".qcow2", ".qcow", ".vfd", ".ima")
    seen = {}
    for modname, meta in apphelpers.testfile_registry.items():
        full_path = meta.get("__full_path__")
        if not full_path or not os.path.exists(full_path):
            continue
        try:
            tree = ast.parse(open(full_path).read())
            cfg = None
            for node in ast.walk(tree):
                if (isinstance(node, ast.Assign)
                        and any(isinstance(t, ast.Name) and t.id == "CONFIG" for t in node.targets)):
                    cfg = ast.literal_eval(node.value)
                    break
        except Exception:
            continue
        if not cfg:
            continue
        resolved = apphelpers.resolve_meta(cfg, cfg)
        base    = str(resolved.get("projbasedir", "")).rstrip("/")
        projdir = str(resolved.get("projdir", "")).strip("/")
        projpath = f"{base}/{projdir}" if base and projdir else None
        for k, v in resolved.items():
            if not isinstance(v, str) or not v.lower().endswith(IMG_EXTS):
                continue
            p = v if v.startswith("/") else (f"{projpath}/{v}" if projpath else None)
            if not p:
                continue
            ap = os.path.abspath(p)
            if ap in seen:
                continue
            exists = os.path.isfile(ap)
            seen[ap] = {
                "path": ap, "filename": os.path.basename(ap), "key": k,
                "exists": exists,
                "size_mb": round(os.path.getsize(ap) / (1024 * 1024), 1) if exists else None,
                "project": projdir or modname,
            }
    images = sorted(seen.values(), key=lambda x: (not x["exists"], x["path"]))
    return jsonify({"images": images})


@app.route("/api/clone_disk_image", methods=["POST"])
def clone_disk_image_route():
    """Clone an existing disk image into the project. Body:
    {"source_path": "...", "config": {...}}  (dest = {projbasedir}{projdir}/{hdd1_img})
    or {"source_path": "...", "path": "..."}. Optional: size_mb (grow — file-copy,
    not bootable), image_name, overwrite. Exact copy when size_mb is omitted."""
    data      = request.json or {}
    src       = data.get("source_path")
    size_mb   = data.get("size_mb") or None
    overwrite = bool(data.get("overwrite", False))
    dst       = data.get("path")

    if not dst:
        config   = data.get("config") or {}
        resolved = apphelpers.resolve_meta(config, config)
        base     = str(resolved.get("projbasedir", "")).rstrip("/")
        projdir  = str(resolved.get("projdir", "")).strip("/")
        img      = data.get("image_name") or resolved.get("hdd1_img") or "hdd.img"
        if base and projdir:
            dst = f"{base}/{projdir}/{img}"

    if not src or not dst:
        return jsonify({"status": "error",
                        "message": "need source_path and (path or config with projbasedir/projdir)"}), 400
    if not os.path.abspath(dst).startswith("/testsrc/sourcedir/"):
        return jsonify({"status": "error",
                        "message": f"destination must be under /testsrc/sourcedir/: {dst}"}), 400

    helper_file = os.path.join(TESTSRC_HELPERDIR, "qemuhelpers.py")
    spec = importlib.util.spec_from_file_location("qemuhelpers_diskutil", helper_file)
    mod  = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(mod)
    except Exception as e:
        return jsonify({"status": "error", "message": f"failed to load qemuhelpers: {e}"}), 500
    if not hasattr(mod, "clone_disk_image"):
        return jsonify({"status": "error", "message": "clone_disk_image not found in qemuhelpers"}), 400

    ok, msg = mod.clone_disk_image(src, os.path.abspath(dst), size_mb=size_mb, overwrite=overwrite)
    return jsonify({"status": "ok" if ok else "error", "message": msg,
                    "path": os.path.abspath(dst)}), (200 if ok else 500)





# Disk artifact repo
# this is a lazy attempt at making media reuseable by identifying it and tracking it throughout the registered tests
def _load_artifacthelpers():
    """Load the projects artifacthelpers module fresh.
    Returns module, error_str."""
    helper_file = os.path.join(TESTSRC_HELPERDIR, "artifacthelpers.py")
    if not os.path.isfile(helper_file):
        return None, "project has no artifacthelpers.py"
    spec = importlib.util.spec_from_file_location("artifacthelpers_diskutil", helper_file)
    mod  = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(mod)
    except Exception as e:
        return None, f"failed to load artifacthelpers: {e}"
    return mod, None


def _guard_sourcedir(path):
    """return abspath if it's under /testsrc/sourcedir/, else None."""
    ap = os.path.abspath(str(path))
    return ap if ap.startswith("/testsrc/sourcedir/") else None


@app.route("/diskbuilder")
@nav("Disk Builder")
def diskbuilder():
    """Diskbuilder workspace, browse captured artifacts"""
    test_runner.reload_tests()
    return render_template("diskbuilder.html")


@app.route("/api/list_artifacts", methods=["GET"])
def list_artifacts_route():
    mod, err = _load_artifacthelpers()
    if err:
        return jsonify({"status": "error", "message": err}), 400
    return jsonify({"status": "ok", "artifacts": mod.list_artifacts()})


@app.route("/api/capture_artifact", methods=["POST"])
def capture_artifact_route():
    """freeze a DOS dir from a disk image into a reusable artifact. Body:
    {"name","source_img","dos_dir", optional "dest","description","overwrite"}."""
    data = request.json or {}
    mod, err = _load_artifacthelpers()
    if err:
        return jsonify({"status": "error", "message": err}), 400
    src = _guard_sourcedir(data.get("source_img", ""))
    if not src:
        return jsonify({"status": "error",
                        "message": "source_img must be under /testsrc/sourcedir/"}), 400
    if not data.get("name") or not data.get("dos_dir"):
        return jsonify({"status": "error", "message": "need name and dos_dir"}), 400
    ok, msg = mod.capture_artifact(
        data["name"], src, data["dos_dir"],
        dest=data.get("dest"), description=data.get("description", ""),
        overwrite=bool(data.get("overwrite", False)))
    return jsonify({"status": "ok" if ok else "error", "message": msg}), (200 if ok else 500)


@app.route("/api/inject_artifact", methods=["POST"])
def inject_artifact_route():
    """copy an artifact into a disk image. Body: {"name","hdd_img_path",
    optional "dest"} or {"name","config"} (path from projbasedir/projdir/hdd1_img)."""
    data = request.json or {}
    mod, err = _load_artifacthelpers()
    if err:
        return jsonify({"status": "error", "message": err}), 400
    path = data.get("hdd_img_path")
    if not path:
        config   = data.get("config") or {}
        resolved = apphelpers.resolve_meta(config, config)
        base     = str(resolved.get("projbasedir", "")).rstrip("/")
        projdir  = str(resolved.get("projdir", "")).strip("/")
        img      = data.get("image_name") or resolved.get("hdd1_img") or "hdd.img"
        if base and projdir:
            path = f"{base}/{projdir}/{img}"
    hdd = _guard_sourcedir(path or "")
    if not hdd or not data.get("name"):
        return jsonify({"status": "error",
                        "message": "need name and (hdd_img_path or config) under /testsrc/sourcedir/"}), 400
    ok, msg = mod.inject_artifact(data["name"], hdd, dest=data.get("dest"),
                                  apply_bootfiles=bool(data.get("apply_bootfiles", False)))
    return jsonify({"status": "ok" if ok else "error", "message": msg}), (200 if ok else 500)


@app.route("/api/remove_from_disk", methods=["POST"])
def remove_from_disk_route():
    """delete a directory tree off a disk image. Body: {"dest","hdd_img_path"}."""
    data = request.json or {}
    mod, err = _load_artifacthelpers()
    if err:
        return jsonify({"status": "error", "message": err}), 400
    hdd = _guard_sourcedir(data.get("hdd_img_path", ""))
    if not hdd or not data.get("dest"):
        return jsonify({"status": "error",
                        "message": "need dest and hdd_img_path under /testsrc/sourcedir/"}), 400
    ok, msg = mod.remove_dir_from_disk(data["dest"], hdd)
    return jsonify({"status": "ok" if ok else "error", "message": msg}), (200 if ok else 500)


def _load_qemuhelpers():
    """load the project's qemuhelpers module fresh. Returns (module, error)."""
    helper_file = os.path.join(TESTSRC_HELPERDIR, "qemuhelpers.py")
    if not os.path.isfile(helper_file):
        return None, "project has no qemuhelpers.py"
    spec = importlib.util.spec_from_file_location("qemuhelpers_diskutil", helper_file)
    mod  = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(mod)
    except Exception as e:
        return None, f"failed to load qemuhelpers: {e}"
    return mod, None


@app.route("/api/list_projects", methods=["GET"])
def list_projects_route():
    """list registered testlists with parent projectdir"""
    test_runner.reload_tests()
    out = []
    for modname, meta in apphelpers.testfile_registry.items():
        fp = meta.get("__full_path__")
        if not fp:
            continue
        out.append({
            "testlist": fp,
            "dir": os.path.dirname(fp),
            "name": os.path.basename(os.path.dirname(fp)),
            "testlist_file": os.path.basename(fp),
        })
    out.sort(key=lambda x: x["name"])
    return jsonify({"status": "ok", "projects": out})


@app.route("/api/create_disk", methods=["POST"])
def create_disk_route():
    """Create a new disk image in a testlist's parent dir. Body:
    {"testlist_path","disk_name","media","fat_bits", "size_mb" or "size_kb",
     optional "bootable","make_src_dir","overwrite"}.
    or pass an explicit "dir" instead of testlist_path."""
    data = request.json or {}
    mod, err = _load_qemuhelpers()
    if err:
        return jsonify({"status": "error", "message": err}), 400

    tl = data.get("testlist_path")
    target_dir = data.get("dir") or (os.path.dirname(tl) if tl else None)
    if not target_dir:
        return jsonify({"status": "error", "message": "need testlist_path or dir"}), 400
    target_dir = os.path.abspath(target_dir)
    if not target_dir.startswith("/testsrc/sourcedir/") or not os.path.isdir(target_dir):
        return jsonify({"status": "error",
                        "message": f"dir must be an existing dir under /testsrc/sourcedir/: {target_dir}"}), 400

    disk_name = os.path.basename(str(data.get("disk_name", "")).strip())
    if not disk_name or disk_name in (".", ".."):
        return jsonify({"status": "error", "message": "invalid disk_name"}), 400

    path = os.path.join(target_dir, disk_name)
    ok, msg = mod.create_disk_image(
        path,
        size_mb=data.get("size_mb") or None,
        size_kb=data.get("size_kb") or None,
        fat_bits=data.get("fat_bits"),
        media=data.get("media", "hdd"),
        bootable=bool(data.get("bootable", False)),
        make_src_dir=bool(data.get("make_src_dir", False)),
        overwrite=bool(data.get("overwrite", False)),
    )
    return jsonify({"status": "ok" if ok else "error", "message": msg, "path": path}), \
           (200 if ok else 500)


@app.route("/api/disk_toplevel", methods=["GET"])
def disk_toplevel_route():
    """list the top level dirs on a disk image. use: ?path=..."""
    mod, err = _load_artifacthelpers()
    if err:
        return jsonify({"status": "error", "message": err}), 400
    hdd = _guard_sourcedir(request.args.get("path", ""))
    if not hdd:
        return jsonify({"status": "error", "message": "path must be under /testsrc/sourcedir/"}), 400
    ok, res = mod.list_disk_toplevel(hdd)
    if not ok:
        return jsonify({"status": "error", "message": res}), 500
    return jsonify({"status": "ok", "dirs": res})


# ── Media library (whole floppy/ISO/hdd images dropped into /testsrc/images) ───

def _load_medialibhelpers():
    """Load the project's medialibhelpers module fresh. Returns (module, error)."""
    helper_file = os.path.join(TESTSRC_HELPERDIR, "medialibhelpers.py")
    if not os.path.isfile(helper_file):
        return None, "project has no medialibhelpers.py"
    spec = importlib.util.spec_from_file_location("medialibhelpers_diskutil", helper_file)
    mod  = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(mod)
    except Exception as e:
        return None, f"failed to load medialibhelpers: {e}"
    return mod, None


@app.route("/api/list_media", methods=["GET"])
def list_media_route():
    """List indexed media. Query: ?kind=floppy|iso|hdd (optional)."""
    mod, err = _load_medialibhelpers()
    if err:
        return jsonify({"status": "error", "message": err}), 400
    return jsonify({"status": "ok", "root": mod.IMAGES_ROOT,
                    "media": mod.list_media(kind=request.args.get("kind") or None)})


@app.route("/api/scan_media", methods=["POST"])
def scan_media_route():
    """Rescan /testsrc/images for media files, preserving existing annotations."""
    mod, err = _load_medialibhelpers()
    if err:
        return jsonify({"status": "error", "message": err}), 400
    ok, msg = mod.scan_media()
    return jsonify({"status": "ok" if ok else "error", "message": msg}), (200 if ok else 500)


@app.route("/api/update_media", methods=["POST"])
def update_media_route():
    """Annotate an indexed item. Body: {"id", optional "shortname","description","kind"}."""
    data = request.json or {}
    mod, err = _load_medialibhelpers()
    if err:
        return jsonify({"status": "error", "message": err}), 400
    if not data.get("id"):
        return jsonify({"status": "error", "message": "need id"}), 400
    ok, msg = mod.update_media(data["id"], shortname=data.get("shortname"),
                              description=data.get("description"), kind=data.get("kind"))
    return jsonify({"status": "ok" if ok else "error", "message": msg}), (200 if ok else 400)


@app.route("/api/forget_media", methods=["POST"])
def forget_media_route():
    """Drop the index row for an item whose file is gone. Body: {"id"}.
    Never touches files on disk — index only."""
    data = request.json or {}
    mod, err = _load_medialibhelpers()
    if err:
        return jsonify({"status": "error", "message": err}), 400
    if not data.get("id"):
        return jsonify({"status": "error", "message": "need id"}), 400
    ok, msg = mod.forget_media(data["id"])
    return jsonify({"status": "ok" if ok else "error", "message": msg}), (200 if ok else 400)


@app.route("/api/export_media", methods=["POST"])
def export_media_route():
    """Copy a floppy from the library into a project dir. Body:
    {"id", "testlist_path" or "dir", optional "dest_name","overwrite"}."""
    data = request.json or {}
    mod, err = _load_medialibhelpers()
    if err:
        return jsonify({"status": "error", "message": err}), 400
    tl = data.get("testlist_path")
    target_dir = data.get("dir") or (os.path.dirname(tl) if tl else None)
    if not data.get("id") or not target_dir:
        return jsonify({"status": "error", "message": "need id and testlist_path or dir"}), 400
    dest_dir = _guard_sourcedir(target_dir)
    if not dest_dir:
        return jsonify({"status": "error",
                        "message": "destination must be under /testsrc/sourcedir/"}), 400
    ok, msg = mod.export_media(data["id"], dest_dir, dest_name=data.get("dest_name"),
                               overwrite=bool(data.get("overwrite", False)))
    return jsonify({"status": "ok" if ok else "error", "message": msg}), (200 if ok else 500)


# liveview helper
@app.route("/instances")
@nav("Instances")
def instances_page():
    """return every process liveview.py can see, anything started by the test framework"""
    return render_template("instances.html")


INFO_CONTENT_PATH = os.path.join(BASE_DIR, "info_content.txt")

@app.route("/info")
@nav("Info", align="right")
def info_page():
    try:
        with open(INFO_CONTENT_PATH, "r", encoding="utf-8") as f:
            raw = f.read()
    except FileNotFoundError:
        raw = ""
    return render_template("info.html", content=render_info_markup(raw))





# testbuilder helpers
def get_dynamic_action_schema(output_data):
    return {
        func_name: output_data["schemas"].get(func_name, {})
        for func_name in output_data["functions"]
    }


def sync_test_data(module_name, new_steps, new_build_config):
    meta = apphelpers.testfile_registry.get(module_name)
    if not meta or "__full_path__" not in meta:
        return False, f"Registry lookup failed for {module_name}"

    file_path = meta["__full_path__"]
    if not os.path.exists(file_path):
        return False, f"File not found on disk: {file_path}"

    try:
        updates = {**new_build_config, "steps": new_steps}
        update_config_in_file(file_path, updates)
        return True, "Successfully synced steps and build config to disk"
    except Exception as e:
        return False, str(e)



app.register_blueprint(api_v1)


route_file = os.path.join(TESTSRC_HELPERDIR, 'customflaskroutes.py')
if os.path.isfile(route_file):
    spec   = importlib.util.spec_from_file_location('custom_routes', route_file)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    app.jinja_loader = ChoiceLoader([
        app.jinja_loader,
        FileSystemLoader("/testsrc/flasktemplates"),
    ])
    if hasattr(module, 'register_routes'):
        module.register_routes(app)


if __name__ == "__main__":
    test_runner.db.init_report_db()
    test_runner.reload_tests()
    app.run(host="0.0.0.0", port=8080, debug=True, use_reloader=False)