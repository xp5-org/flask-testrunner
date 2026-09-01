import ast
import os

from flask import Blueprint, jsonify, request, redirect, url_for
from werkzeug.exceptions import HTTPException

import apphelpers
import test_runner
import dispatchhelper
import runhelper
import apphelpers as testid
from dbhelper import db
from appstate import progress_state, process_registry, run_registry, batch_registry
from repair_config import repair_config, apply_step_param_fixes
from step_validator import validate_config_steps, format_results_text, has_blocking_issues
from newprojecthelper import copybuildtest, update_config_in_file

TESTLIST_ROOT       = "/testsrc/sourcedir"
TESTSRC_HELPERDIR   = "/testsrc/pyhelpers"

api_v1 = Blueprint("api_v1", __name__, url_prefix="/api/v1")


def ok(data, links=None, status=200):
    payload = {"data": data}
    if links:
        payload["links"] = links
    return jsonify(payload), status


def err(code, message, status):
    return jsonify({"error": {"code": code, "message": message}}), status


@api_v1.errorhandler(Exception)
def _uniform_error(e):
    if isinstance(e, HTTPException):
        return err("http_error", e.description or e.name, e.code or 500)
    import traceback
    traceback.print_exc()
    return err("internal_error", str(e), 500)


def _ensure_registry():
    if not apphelpers.testfile_registry:
        test_runner.reload_tests()


def _slug_maps():
    """(slug -> module, module -> slug) for the current registry."""
    _ensure_registry()
    return testid.build_index(apphelpers.testfile_registry.keys())


def _resolve(test_id):
    """Accept either a slug or a module dot-path; return the module name."""
    _ensure_registry()
    if test_id in apphelpers.testfile_registry:
        return test_id
    slug_to_mod, _ = _slug_maps()
    return slug_to_mod.get(test_id)


def _slug(modname):
    _, mod_to_slug = _slug_maps()
    return mod_to_slug.get(modname, modname)


def _meta(test_id):
    modname = _resolve(test_id)
    return apphelpers.testfile_registry.get(modname) if modname else None


def _full_path(test_id):
    meta = _meta(test_id)
    p = meta.get("__full_path__") if meta else None
    return p if (p and os.path.exists(p)) else None


def _extract_config(full_path):
    """handling for config with broken steps
    """
    with open(full_path, "r", encoding="utf-8") as f:
        tree = ast.parse(f.read())
    for node in ast.walk(tree):
        if (isinstance(node, ast.Assign)
                and any(isinstance(t, ast.Name) and t.id == "CONFIG"
                        for t in node.targets)):
            try:
                return ast.literal_eval(node.value)
            except (ValueError, TypeError, SyntaxError,
                    MemoryError, RecursionError):
                cfg = test_runner.load_config_from_path(full_path)
                if cfg is None:
                    raise ValueError(
                        f"CONFIG in {full_path} is computed and could not be "
                        f"evaluated by import")
                return cfg
    raise ValueError("No CONFIG found in file")


def _raw_config(test_id):
    """read config as is on disk
    """
    full_path = _full_path(test_id)
    if not full_path:
        return None
    return _extract_config(full_path)


def _test_links(test_id):
    base = f"/api/v1/tests/{test_id}"
    return {
        "self":      base,
        "steps":     f"{base}/steps",
        "validate":  f"{base}/validate",
        "artifacts": f"{base}/artifacts",
        "reports":   f"{base}/reports",
        "run":       "/api/v1/runs",
    }


def _dispatch_and_schemas():
    dispatch = dispatchhelper.load_step_dispatch(TESTSRC_HELPERDIR)
    schemas  = dispatchhelper.PROJECT_STEP_SCHEMAS.get(TESTSRC_HELPERDIR, {})
    return dispatch, schemas





# api discovery , needs a lot of improvement. probably should look at swagger or some other package

@api_v1.route("", strict_slashes=False)
def index():
    """info about api. need to make this dynamic at some point and not a generated list of garbage"""
    data = {
        "name":         "flask-testrunner API",
        "version":      "v1",
        "canonical_id": "module dot-path, e.g. "
                        "c64src.mcmdemo1.__testlist__C64_mcmgradient",
        "workflow": [
            "GET  /api/v1/tests                     — discover tests",
            "GET  /api/v1/config-schema             — learn the CONFIG vocabulary",
            "GET  /api/v1/actions                   — learn the step vocabulary",
            "GET  /api/v1/tests/{id}                — read a test's config + steps",
            "PUT  /api/v1/tests/{id}/steps          — edit steps/build config",
            "POST /api/v1/tests/{id}/validate       — validate before running",
            "POST /api/v1/runs                       — execute {\"test\": id}",
            "GET  /api/v1/runs/{run_id}             — poll until status=done",
            "GET  /api/v1/reports/{report_id}       — read structured results",
            "GET  /api/v1/instances                 — see what is running NOW",
            "GET  /api/v1/instances/{id}/screenshot — look at it (PNG)",
            "GET  /api/v1/instances/{id}/ocr        — read its screen as text",
        ],
        "data_model": {
            "test": "a __testlist__*.py file exposing a CONFIG dict (metadata + "
                    "resolved paths). Steps come either from CONFIG['steps'] or "
                    "from @register_mytest-decorated functions — see "
                    "authoring_modes in GET /api/v1/config-schema.",
            "step": {"action": "<name from GET /api/v1/actions>",
                     "param":  {"<schema key>": "<value>"},
                     "subaction": ""},
            "id": "URL-safe slug (owc-clonetest2.86box). The module dot-path is "
                  "also accepted anywhere an id is taken.",
            "container": "A source directory declaring a __testparent__.py. "
                         "Tests sharing container_id are its children and "
                         "belong on its one row, one button each. Only a "
                         "container has a page (/test/<container_id>); a "
                         "testlist is addressed only by the run/edit APIs.",
        },
        "envelope": {
            "success": {"data": "<payload>", "links": {"<rel>": "<url>"}},
            "error":   {"error": {"code": "<machine_code>", "message": "<text>"}},
        },
        "resources": [
            {"rel": "actions", "method": "GET", "href": "/api/v1/actions",
             "summary": "Available step actions and their param schemas."},
            {"rel": "config-schema", "method": "GET", "href": "/api/v1/config-schema",
             "summary": "CONFIG field reference for authoring a new testlist: "
                        "what each key controls, the identity/grouping rules, "
                        "and how to add a variant of an existing test."},
            {"rel": "tests", "method": "GET", "href": "/api/v1/tests",
             "summary": "List all tests with latest status."},
            {"rel": "test", "method": "GET", "href": "/api/v1/tests/{id}",
             "summary": "Read one test: raw on-disk config, steps, artifacts, status."},
            {"rel": "edit-steps", "method": "PUT", "href": "/api/v1/tests/{id}/steps",
             "summary": "Write steps + build config back to the .py file.",
             "example": {"steps": [{"action": "test_sendrun", "param": {}, "subaction": ""}],
                         "build_config": {"warpmode": "True"}}},
            {"rel": "validate", "method": "POST", "href": "/api/v1/tests/{id}/validate",
             "summary": "Validate steps (on-disk CONFIG, or a posted config)."},
            {"rel": "clone", "method": "POST", "href": "/api/v1/tests",
             "summary": "Clone an existing test into a new one.",
             "example": {"src_module": "<id>", "target_id": "mynewtest"}},
            {"rel": "run", "method": "POST", "href": "/api/v1/runs",
             "summary": "Start a run.", "example": {"test": "<id>"}},
            {"rel": "run-status", "method": "GET", "href": "/api/v1/runs/{run_id}",
             "summary": "Run progress, then final result + report link."},
            {"rel": "reports", "method": "GET", "href": "/api/v1/reports",
             "summary": "List reports (most recent first)."},
            {"rel": "report", "method": "GET", "href": "/api/v1/reports/{report_id}",
             "summary": "Structured per-step results + build artifacts."},
            {"rel": "instances", "method": "GET", "href": "/api/v1/instances",
             "summary": "Emulator instances running right now — from the current "
                        "run and from a process scan. Each carries a "
                        "`capabilities` list; do not assume an instance has a "
                        "screen (a SimH Nova has only `tty`)."},
            {"rel": "instance", "method": "GET", "href": "/api/v1/instances/{id}",
             "summary": "One instance, with a link per capability it offers."},
            {"rel": "instance-screenshot", "method": "GET",
             "href": "/api/v1/instances/{id}/screenshot",
             "summary": "PNG of the instance's display. ?format=json returns the "
                        "path instead of the image bytes."},
            {"rel": "instance-ocr", "method": "GET",
             "href": "/api/v1/instances/{id}/ocr",
             "summary": "Text read off the instance's display. Optional ?phrase= "
                        "adds a found:true/false match; the text is returned "
                        "either way."},
            {"rel": "instance-keys", "method": "POST",
             "href": "/api/v1/instances/{id}/keys",
             "summary": "Type into the guest. A newline is never implied — ask "
                        "for one with enter:true, key:\"ret\", or \\n in text.",
             "example": {"text": "dir /w", "enter": True}},
        ],
    }
    links = {
        "actions":   "/api/v1/actions",
        "tests":     "/api/v1/tests",
        "runs":      "/api/v1/runs",
        "reports":   "/api/v1/reports",
        "instances": "/api/v1/instances",
    }
    return ok(data, links)


@api_v1.route("/actions")
def actions():
    """Catalog of step actions (dispatch functions) + their param schemas —
    the vocabulary needed to author a step's `action` and `param`."""
    dispatch, schemas = _dispatch_and_schemas()
    catalog = []
    for name in sorted(dispatch):
        func = dispatch[name]
        catalog.append({
            "action":      name,
            "is_teststep": bool(getattr(func, "_is_teststep", False)),
            "params":      schemas.get(name, {}),
        })
    return ok({"actions": catalog, "helper_dir": TESTSRC_HELPERDIR})




@api_v1.route("/tests")
def list_tests():
    test_runner.reload_tests()
    _, mod_to_slug = _slug_maps()
    result = []
    for modname, info in apphelpers.testfile_registry.items():
        latest_status = {}
        rows = db.get_latest_namedteststatus(info["id"])
        for r in rows:
            latest_status[r["types"]] = r["status"]

        slug = mod_to_slug.get(modname, modname)
        system = info.get("system")
        #group clients by key
        result.append({
            "id":            slug,
            "module":        modname,
            "display_name":  info["id"],
            "path":          apphelpers.project_relpath(info),
            "container_id":  testid.container_id(info["id"], system),
            "group_label":   info["id"],
            "variants":      [
                {"variant": t,
                 "id":      mod_to_slug.get(m, m),
                 "module":  m,
                 "path":    apphelpers.project_relpath(
                                apphelpers.testfile_registry.get(m)),
                 "status":  latest_status.get(t, "")}
                for t, m in (info["types"] or {}).items()
            ],
            "types":         info["types"],
            "system":        system,
            "platform":      info.get("platform"),
            "description":   info.get("description") or "",
            "tags":          info.get("tags") or [],
            "latest_status": latest_status,
            "links":         {"self": f"/api/v1/tests/{slug}"},
        })
    return ok({"tests": result, "count": len(result)},
              links={"actions": "/api/v1/actions",
                     "config-schema": "/api/v1/config-schema"})


@api_v1.route("/config-schema")
def config_schema():
    """list available config actions for the instance type
    """
    _ensure_registry()
    known_types = sorted({
        t for info in apphelpers.testfile_registry.values()
        for t in (info.get("types") or {})
    })
    known_systems = sorted({
        info.get("system") for info in apphelpers.testfile_registry.values()
        if info.get("system")
    })

    known_parents = sorted({
        info["id"] for info in apphelpers.testfile_registry.values() if info.get("id")
    })

    # need to organize this
    data = {
        "summary": "Fields of the CONFIG dict in a __testlist__*.py file.",
        "containers": {
            "how": "Every source directory holds one __testparent__.py "
                   "declaring PARENT = {name, archtype, platform}. called "
                   "container. It is a bookmark",
            "children": "Each __testlist__*.py in that directory names the "
                        "container in CONFIG['parent'] and contributes"
                        "one button, labelled by CONFIG['function'].",
            "rule": "CONFIG['parent'] must equal the directory stub's name "
                    "exactly, or the testlist fails to load.",
            "stub_fields": {
                "name": "Container name. The row's display_name/group_label.",
                "archtype": "Uppercased into `system`, the second half of the "
                            "row identity. In use: %s" % ", ".join(known_systems),
                "platform": "Section heading the row is filed under.",
            },
            "in_use": known_parents,
        },
        "authoring_modes": {
            "steps": {
                "how": "CONFIG['steps'] is a list of {action, param, subaction}.",
                "actions_from": "/api/v1/actions",
                "use_when": "The test is expressible with existing dispatch "
                            "actions. Editable through the testbuilder UI and "
                            "PUT /api/v1/tests/{id}/steps.",
            },
            "decorator": {
                "how": "No CONFIG['steps']. Functions decorated with "
                       "@register_mytest(testtype, step_name) become the steps, "
                       "in decoration order.",
                "use_when": "The test drives a helper directly (dosboxhelpers, "
                            "box86helpers, basiliskhelpers). Not editable via "
                            "the steps API",
                "note": "run_testfile() clears the step registry and imports "
                        "only the target module, so decorated steps from other "
                        "testlists never leak into a run.",
            },
        },
        "fields": [
            {"key": "parent", "required": True, "type": "string",
             "controls": "Which container this testlist belongs to. Supplies "
                         "the row's display_name/group_label and, via the "
                         "stub, its system and platform.",
             "info": "Must match the name in this directory's "
                       "__testparent__.py character for character; a mismatch "
                       "is a load error",
             "in_use": known_parents,
             "example": "OWC_VGAPLAY_v1"},
            {"key": "function", "required": True, "type": "string",
             "controls": "This test's button under its parent, and the key "
                         "that pass/fail status is stored under.",
             "info": "Must be unique among the children of one container, or "
                       "they overwrite each other's status on that row. Also "
                       "prefixes decorated step names as '<function> N - <step>'.",
             "in_use": known_types,
             "example": "build | dosbox | 86box"},
            {"key": "description", "required": False, "type": "string",
             "controls": "saying what the test is for or why it exists, on the "
                         "testbuilder page and in the registry listing.",
             "info": "Documentation only. Never referenced by a step, and "
                       "not a {token} anything resolves against. For why an "
                       "individual STEP is there rather than the whole test, "
                       "see the per-step \"description\" key instead (sibling "
                       "of a step's action/param/subaction, not part of "
                       "CONFIG)."},
            {"key": "tags", "required": False, "type": "list[string]",
             "controls": "searchable labels (e.g. "
                         "\"floppy\", \"sound\", \"boot\") on /tests "
                         "and filterable in the testbuilder's test picker.",
             "info": "Documentation only, same as description. Free text, "
                       "no fixed vocabulary yet.",
             "example": ["floppy", "boot", "watcom"]},
            {"key": "projbasedir", "required": True, "type": "string",
             "controls": "Root that `structure` paths resolve against.",
             "example": "/testsrc/sourcedir/"},
            {"key": "structure", "required": True, "type": "object",
             "controls": "Declarative path tree. init_test_env() walks it and "
                         "returns PATHS; '_rel' is the directory name and any "
                         "other key becomes a PATHS entry.",
             "info": "Values interpolate {placeholders} from other CONFIG "
                       "keys, so '{projname}' or '{config_file}' resolve "
                       "against this same dict.",
             "example": {"project": {"_rel": "{projname}",
                                     "out_dir": {"_rel": "output"}}}},
            {"key": "steps", "required": False, "type": "array",
             "controls": "Selects the 'steps' authoring mode. Omit it to use "
                         "the decorator mode.",
             "actions_from": "/api/v1/actions"},
            {"key": "instance_name", "required": False, "type": "string",
             "controls": "Handle the emulator instance is stored under in the "
                         "run context; helpers read it back by this name.",
             "example": "qemu1 | dosbox1 | box86_1"},
        ],
        "file_rules": {
            "location": "Anywhere under /testsrc/sourcedir/.",
            "filename": "Must start with '__testlist__' or reload_tests() will "
                        "not discover it.",
            "id": "Derived from the module dot-path; see the 'identity' block.",
            "must_call": "PATHS = init_test_env(CONFIG, __name__) at import ",
        },
        "identity": {
            "canonical": "module dot-path, e.g. "
                         "OWC_CLONETEST2.__testlist__OWC_CLONETEST2_86box",
            "id": "URL/HTML-safe slug derived from it, e.g. "
                  "owc-clonetest2.86box. Every endpoint accepts either form. "
                  "HTML reports on disk are named by the slug too.",
            "path": "The testlist's directory, relative to /testsrc, so you can "
                    "edit its code. it matches the real directory name "
                    "exactly.",
            "container_id": "The declared container this testlist belongs to, "
                            "slugified. Clients should group rows on this "
                            "rather than recomputing it, and it is the id the "
                            "container's page is served under.",
        },
        "reload": {
            "testlists": "Re-imported on every /api/v1/tests call.",
            "helpers": "NOT re-imported. Editing anything in /testsrc/pyhelpers "
                       "(dosboxhelpers, box86helpers, qemuhelpers...) requires "
                       "restarting the runner process; reload_tests() only "
                       "evicts testlist modules.",
        },
        "create_new": {
            "recommended": "POST /api/v1/tests to clone an existing test.",
            "body": {"src_module": "<id of the test to copy>",
                     "target_id": "<new testlist name>",
                     "…": "any other key is applied as a CONFIG override"},
            "variant_example": {
                "src_module": "OWC_VGAPLAY_v1.__testlist__OWC_CLONETEST2_build",
                "target_id": "OWC_CLONETEST2_dosbox",
                "function": "dosbox"},
            "variant_note": "Cloning into the same directory adds another "
                            "button to that directory's container and treats them like groped tests"
                            "__testparent__.py  names its parent container. the new "
                            "child's parent is pinned to it. only 'function' "
                            "needs to change (and must not collide with a "
                            "sibling's). Cloning into a NEW directory creates a "
                            "new container: pass 'parent' to name it, or it "
                            "takes the target_id slug.",
        },
    }
    return ok(data, links={"actions": "/api/v1/actions",
                           "tests": "/api/v1/tests",
                           "clone": "/api/v1/tests"})


@api_v1.route("/failed-loads")
def failed_loads():
    test_runner.reload_tests()
    return ok({"failed": test_runner.failed_loads,
               "warnings": apphelpers.registry_warnings()})


@api_v1.route("/tests/<path:test_id>")
def get_test(test_id):
    meta = _meta(test_id)
    if not meta:
        return err("not_found", f"Unknown test: {test_id}", 404)
    cfg = _raw_config(test_id)
    if cfg is None:
        return err("not_found", f"No config file on disk for: {test_id}", 404)

    # DB rows are keyed by module dot-path, so look them up with the resolved
    # name even when the caller addressed the test by slug.
    modname = _resolve(test_id)
    artifacts = db.get_latest_build_artifacts(modname)
    if not artifacts:
        try:
            artifacts = db.extract_build_artifacts(cfg)
        except Exception:
            artifacts = []

    latest_status = {}
    for r in db.get_latest_namedteststatus(meta["id"]):
        latest_status[r["types"]] = r["status"]

    data = {
        "id":            _slug(modname),
        "module":        modname,
        "display_name":  meta["id"],
        "path":          apphelpers.project_relpath(meta),
        "container_id":  testid.container_id(meta["id"], meta.get("system")),
        "group_label":   meta["id"],
        "system":        meta.get("system"),
        "platform":      meta.get("platform"),
        "types":         meta.get("types"),
        "description":   cfg.get("description", "") or "",
        "tags":          cfg.get("tags", []) or [],
        "config":        cfg,
        "steps":         cfg.get("steps", []),
        "build_artifacts": artifacts,
        "latest_status": latest_status,
    }
    return ok(data, links=_test_links(test_id))


@api_v1.route("/tests/<path:test_id>/steps", methods=["GET", "PUT"])
def test_steps(test_id):
    meta = _meta(test_id)
    if not meta:
        return err("not_found", f"Unknown test: {test_id}", 404)

    if request.method == "GET":
        cfg = _raw_config(test_id)
        if cfg is None:
            return err("not_found", f"No config file on disk for: {test_id}", 404)
        return ok({"id": test_id, "steps": cfg.get("steps", [])},
                  links=_test_links(test_id))

    # PUT — write steps (+ optional build_config) back to the .py file.
    body = request.get_json(silent=True) or {}
    steps        = body.get("steps")
    build_config = body.get("build_config", {})
    if steps is None:
        return err("bad_request", "PUT body must contain 'steps'", 400)

    full_path = _full_path(test_id)
    if not full_path:
        return err("not_found", f"No config file on disk for: {test_id}", 404)
    updates = {**build_config, "steps": steps}
    update_config_in_file(full_path, updates)
    return ok({"id": test_id, "steps": steps, "written_to": full_path},
              links=_test_links(test_id))


@api_v1.route("/tests/<path:test_id>/validate", methods=["POST"])
def validate_test(test_id):
    body          = request.get_json(silent=True) or {}
    config        = body.get("config")
    resolved_vars = body.get("resolved_vars")

    if config is None:
        # Validate the on-disk CONFIG.
        full_path = _full_path(test_id)
        if not full_path:
            return err("not_found", f"No config file on disk for: {test_id}", 404)
        config = _extract_config(full_path)

    results    = validate_config_steps(config, TESTSRC_HELPERDIR,
                                       resolved_vars=resolved_vars)
    return ok({
        "id":         test_id,
        "results":    results,
        "has_errors": has_blocking_issues(results),
        "summary":    format_results_text(results),
    }, links=_test_links(test_id))


@api_v1.route("/tests/<path:test_id>/artifacts")
def test_artifacts(test_id):
    if not _meta(test_id):
        return err("not_found", f"Unknown test: {test_id}", 404)
    # The DB is keyed by module dot-path
    artifacts = db.get_latest_build_artifacts(_resolve(test_id) or test_id)
    if not artifacts:
        cfg = _raw_config(test_id)
        artifacts = db.extract_build_artifacts(cfg) if cfg else []
    return ok({"id": test_id, "artifacts": artifacts},
              links=_test_links(test_id))


@api_v1.route("/tests/<path:test_id>/reports")
def test_reports(test_id):
    if not _meta(test_id):
        return err("not_found", f"Unknown test: {test_id}", 404)
    reports = []
    for r in db.get_reports_by_test_id(_resolve(test_id) or test_id):
        report_id = db.get_report_id_for_path(r["filepath"])
        reports.append({
            "report_id": report_id,
            "filename":  r["filename"],
            "timestamp": r["timestamp"],
            "links":     {"self": f"/api/v1/reports/{report_id}"} if report_id else {},
        })
    return ok({"id": test_id, "reports": reports}, links=_test_links(test_id))


@api_v1.route("/tests", methods=["POST"])
def clone_test():
    """clone an existing test into a new one. options are parent (new subdir, new testname) 
    or new variant (same parent dir, same parent name, but different built-type button label)
    """
    import importlib

    body       = request.get_json(silent=True) or {}
    src_module = body.get("src_module")
    target_id  = body.get("target_id")
    if not src_module or not target_id:
        return err("bad_request", "Body must include 'src_module' and 'target_id'", 400)

    mod              = importlib.import_module(src_module)
    testfile_src_dir = os.path.dirname(os.path.abspath(mod.__file__))
    module_config    = getattr(mod, "CONFIG", {})

    SKIP = {"src_module", "target_id", "target_path", "testlist_name", "step_param_fixes"}
    client_overrides = {k: v for k, v in body.items() if k not in SKIP}
    new_vars = {**module_config, **client_overrides}

    step_fixes = body.get("step_param_fixes") or []
    fixed_config, ui_fix_log = apply_step_param_fixes(module_config, step_fixes)
    repaired_config, repair_log = repair_config(fixed_config, new_vars)
    repair_log = ui_fix_log + repair_log

    for k, v in client_overrides.items():
        if k in repaired_config and k not in ("structure", "steps"):
            repaired_config[k] = v

    dest_dir, testlist_file = copybuildtest(
        src_dir=testfile_src_dir,
        outputname=target_id,
        dest_dir=body.get("target_path"),
        testlist_name=body.get("testlist_name"),
        repaired_config=repaired_config,
    )

    new_id = None
    try:
        rel = os.path.relpath(os.path.join(dest_dir, testlist_file), TESTLIST_ROOT)
        new_id = os.path.splitext(rel)[0].replace(os.sep, ".")
        test_runner.reload_tests()
    except Exception:
        pass

    data = {
        "src_module":    src_module,
        "new_id":        new_id,
        "dest_dir":      dest_dir,
        "testlist_file": testlist_file,
        "repairs":       repair_log,
    }
    links = {"self": f"/api/v1/tests/{new_id}"} if new_id else {}
    return ok(data, links=links, status=201)






# test runs , routes

@api_v1.route("/runs", methods=["GET", "POST"])
def runs():
    if request.method == "GET":
        return ok({
            "runs":           run_registry.snapshot(),
            "recent_reports": db.list_reports(limit=20),
        }, links={"reports": "/api/v1/reports"})

    body    = request.get_json(silent=True) or {}
    test_id = body.get("test")
    if not test_id:
        return err("bad_request", "POST body must contain 'test' (a test id)", 400)
    if not _meta(test_id):
        return err("not_found", f"Unknown test: {test_id}", 404)

    # convert dopath to module slug name
    modname = _resolve(test_id)

    try:
        run_id = runhelper.launch_run(modname)
    except runhelper.RunBusyError as e:
        return err("run_in_progress", str(e), 409)

    return ok(
        {"run_id": run_id, "status": "running",
         "test": _slug(modname), "module": modname},
        links={"self": f"/api/v1/runs/{run_id}",
               "test": f"/api/v1/tests/{_slug(modname)}"},
        status=202,
    )


@api_v1.route("/runs/current")
def runs_current():
    return ok({
        "step":      progress_state.step,
        "testname":  progress_state.testname,
        "testid":    progress_state.testid,
        "testtype":  progress_state.testtype,
        "step_name": progress_state.step_name,
        "processes": process_registry.snapshot(),
        "busy":      runhelper.is_busy(),
        "batch":     batch_registry.current(),
    })


@api_v1.route("/runs/current/stop", methods=["POST"])
def stop_current_run():
    stopped = runhelper.request_stop()
    return ok({"stop_requested": stopped})


@api_v1.route("/runs/<run_id>")
def run_status(run_id):
    row = run_registry.get(run_id)
    if not row:
        return err("not_found", f"Unknown run: {run_id}", 404)

    links = {"self": f"/api/v1/runs/{run_id}",
             "test": f"/api/v1/tests/{row['test_id']}"}

    if row["status"] == "running":
        row["progress"] = {
            "step":      progress_state.step,
            "step_name": progress_state.step_name,
            "processes": process_registry.snapshot(),
        }
    elif row.get("report_id"):
        links["report"] = f"/api/v1/reports/{row['report_id']}"

    return ok(row, links=links)


def _failed_container_children(children):
    """module names whose latest run had a FAIL/ERROR status for
    at least one of their test types 
    """
    meta        = apphelpers.testfile_registry[children[0]]
    human_label = meta["id"]

    summary_by_type = {}
    for s in db.get_latest_namedteststatus(human_label):
        if s["testparentname"] != human_label:
            continue
        types_field = s.get("types")
        types = types_field if isinstance(types_field, list) else [
            t.strip() for t in (types_field or "").split(",") if t.strip()
        ]
        for type_name in types:
            summary_by_type.setdefault(type_name, []).append(s)

    failed = []
    for modname in children:
        info = apphelpers.testfile_registry[modname]
        for type_name in info.get("types", {}):
            type_summary = summary_by_type.get(type_name, [])
            if any(s["status"] in ("FAIL", "ERROR") for s in type_summary):
                failed.append(modname)
                break
    return failed


@api_v1.route("/containers/<path:container>/batch-runs", methods=["POST"])
def container_batch_runs(container):
    _ensure_registry()

    body = request.get_json(silent=True) or {}
    mode = body.get("mode", "all")
    if mode not in ("all", "failed"):
        return err("bad_request", "mode must be 'all' or 'failed'", 400)

    children = testid.resolve_container(container, apphelpers.testfile_registry)
    if not children:
        return err("not_found", f"Unknown container: {container}", 404)

    items = children if mode == "all" else _failed_container_children(children)
    if not items:
        return err("bad_request", "No failed tests to re-run", 400)

    try:
        batch_id = runhelper.launch_batch(container, mode, items)
    except runhelper.RunBusyError as e:
        return err("run_in_progress", str(e), 409)

    return ok(
        {"batch_id": batch_id, "status": "running", "mode": mode, "count": len(items)},
        links={"self": f"/api/v1/batches/{batch_id}"},
        status=202,
    )


@api_v1.route("/batches/<batch_id>")
def batch_status(batch_id):
    row = batch_registry.get(batch_id)
    if not row:
        return err("not_found", f"Unknown batch: {batch_id}", 404)
    return ok(row)








# routes for reports access

@api_v1.route("/reports")
def list_reports():
    limit = request.args.get("limit", default=50, type=int)
    reports = db.list_reports(limit=limit)
    for r in reports:
        r["links"] = {
            "self": f"/api/v1/reports/{r['report_id']}",
            "html": f"/api/v1/reports/{r['report_id']}/html",
        }
    return ok({"reports": reports, "count": len(reports)})


@api_v1.route("/reports/<int:report_id>")
def get_report(report_id):
    report = db.get_report_json(report_id)
    if report is None:
        return err("not_found", f"Unknown report: {report_id}", 404)
    return ok(report, links={
        "self": f"/api/v1/reports/{report_id}",
        "html": f"/api/v1/reports/{report_id}/html",
    })


@api_v1.route("/reports/<int:report_id>/html")
def get_report_html(report_id):
    report = db.get_report_json(report_id)
    if report is None:
        return err("not_found", f"Unknown report: {report_id}", 404)
    path = report["path"]
    try:
        return redirect(url_for("view_report", filepath=path))
    except Exception:
        return redirect(f"/reports/{path}")










# extra junk to help with looking for drivers, CC65 compile uses some of this

@api_v1.route("/tests/<path:test_id>/dirlist", methods=["POST"])
def teststep_dirlist(test_id):
    config = request.get_json(silent=True) or _raw_config(test_id) or {}
    base_dir  = config.get("projbasedir", "")
    proj_dir  = config.get("projdir", "")
    full_path = os.path.abspath(os.path.join(base_dir, proj_dir))

    resolved_paths = apphelpers.resolve_meta(config, config)
    files_found = []
    if os.path.exists(full_path):
        for root, _dirs, files in os.walk(full_path):
            for f in files:
                files_found.append(os.path.relpath(os.path.join(root, f), full_path))

    return ok({
        "project_root":    full_path,
        "exists":          os.path.exists(full_path),
        "files_on_disk":   files_found,
        "resolved_config": resolved_paths,
    })


@api_v1.route("/tests/<path:test_id>/scan-drivers", methods=["POST"])
def scan_drivers(test_id):
    config = request.get_json(silent=True) or _raw_config(test_id) or {}
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
            stem = os.path.splitext(fname)[0]
            drivers.append({
                "filename":   fname,
                "path_token": "{src}" + fname,
                "label":      stem,
                "ext":        ext,
            })

    return ok({"src_dir": src_dir, "exists": exists, "drivers": drivers})










# helpers for live instances

_TEXT_CAPABILITIES = ("ocr", "tty", "screentext")
_WRITE_CAPABILITIES = ("keys",)


def _load_liveview():
    """refresh once on each veiw for whatever running instance is queried
    """
    helper_file = os.path.join(TESTSRC_HELPERDIR, "liveview.py")
    if not os.path.isfile(helper_file):
        return None, ("this project has no pyhelpers/liveview.py, so it cannot "
                      "report running instances")
    import importlib.util
    spec = importlib.util.spec_from_file_location("project_liveview", helper_file)
    mod  = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(mod)
    except Exception as e:
        return None, f"failed to load liveview.py: {type(e).__name__}: {e}"
    return mod, None


def _instance_links(iid, capabilities):
    """limit it to 1 connection per emu or vm instance"""
    links = {"self": f"/api/v1/instances/{iid}"}
    for cap in capabilities or []:
        links[cap] = f"/api/v1/instances/{iid}/{cap}"
    return links


@api_v1.route("/instances")
def list_instances():
    """show everything started by the test framework"""
    mod, e = _load_liveview()
    if e:
        return err("not_supported", e, 501)

    instances = mod.list_instances()
    for inst in instances:
        inst["links"] = _instance_links(inst["id"], inst.get("capabilities"))

    return ok({
        "instances": instances,
        "count":     len(instances),
        "sources": {
            "run":  "live objects of the loaded run",
            "scan": "scanns /proc on host container",
        },
        "capability_reference": getattr(mod, "CAPABILITY_DOC", {}),
    }, links={"self": "/api/v1/instances", "runs": "/api/v1/runs"})


@api_v1.route("/instances/<path:iid>/screenshot")
def instance_screenshot(iid):
    """return screen capture as png to browser
    """
    mod, e = _load_liveview()
    if e:
        return err("not_supported", e, 501)

    okflag, res = mod.screenshot(iid)
    if not okflag:
        if str(res).startswith("unknown instance"):
            return err("not_found", res, 404)
        return err("capture_failed", res, 409)

    if request.args.get("format") == "json":
        return ok({"instance": iid, "image": res,
                   "bytes": os.path.getsize(res)},
                  links=_instance_links(iid, ["screenshot", "ocr"]))

    from flask import send_file
    # A live view is the current frame; a cached one is worse than useless.
    resp = send_file(res, mimetype="image/png", max_age=0)
    resp.headers["Cache-Control"] = "no-store"
    return resp


@api_v1.route("/instances/<path:iid>/media", methods=["GET", "POST"])
def instance_media(iid):
    """GET  lists drive slots. POST swaps:
        {"op": "attach", "device": "floppy0", "path": "/testsrc/images/x.img"}
        {"op": "detach", "device": "ide1-cd0"}
    """
    mod, e = _load_liveview()
    if e:
        return err("not_supported", e, 501)
    if not hasattr(mod, "media_list"):
        return err("not_supported",
                   "this project exposes no media capability", 501)

    if request.method == "GET":
        okflag, res = mod.media_list(iid)
        if not okflag:
            return err("not_found" if str(res).startswith("unknown instance")
                       else "no_capability", res,
                       404 if str(res).startswith("unknown instance") else 409)
        return ok({"instance": iid, "blocks": res,
                   "allowed_roots": mod.media_sources()
                   if hasattr(mod, "media_sources") else []},
                  links=_instance_links(iid, ["media"]))

    body   = request.get_json(silent=True) or {}
    op     = (body.get("op") or "").lower()
    device = body.get("device")
    if not device:
        return err("bad_request", "body must contain 'device'", 400)

    if op == "attach":
        path = body.get("path")
        if not path:
            return err("bad_request", "attach needs 'path'", 400)
        okflag, res = mod.media_attach(iid, device, path,
                                       read_only=body.get("read_only"))
    elif op == "detach":
        okflag, res = mod.media_detach(iid, device, force=bool(body.get("force")))
    else:
        return err("bad_request", "'op' must be 'attach' or 'detach'", 400)

    if not okflag:
        return err("media_failed", res, 409)
    return ok({"instance": iid, "op": op, "device": device, "result": res},
              links=_instance_links(iid, ["media"]))


@api_v1.route("/instances/<path:iid>/keys", methods=["POST"])
def instance_keys(iid):
    """Type into a running guest.

    any combination, applied in this order:
        {"text":  "dir /w"}            type a string, character by character
        {"key":   "f3"}                one key token. modifiers allowed:
                                       "ret", "ctrl-alt-del", "shift-a"
        {"keys":  ["f3", "ret"]}       a sequence , list, or "f3, ret"
        {"enter": true}                append a newline/carriagereturn
        {"delay": 0.05}                delay seconds between keysend

        newlines not sent by default must be specified enter-true.
    """
    mod, e = _load_liveview()
    if e:
        return err("not_supported", e, 501)
    if not hasattr(mod, "send_keys"):
        return err("not_supported",
                   "this project exposes no keys capability", 501)

    body = request.get_json(silent=True) or {}
    unknown = set(body) - {"text", "key", "keys", "enter", "delay"}
    if unknown:
        return err("bad_request",
                   f"unknown field(s): {', '.join(sorted(unknown))}. "
                   "Accepted: text, key, keys, enter, delay", 400)

    okflag, res = mod.send_keys(
        iid,
        text=body.get("text"),
        key=body.get("key"),
        keys=body.get("keys"),
        enter=bool(body.get("enter")),
        delay=body.get("delay", 0.05),
    )
    if not okflag:
        msg = str(res)
        if msg.startswith("unknown instance"):
            return err("not_found", msg, 404)
        if "nothing to send" in msg:
            return err("bad_request", msg, 400)
        return err("no_capability" if "capability" in msg else "send_failed",
                   msg, 409)

    inst = mod.get_instance(iid) or {}
    return ok(res, links=_instance_links(iid, inst.get("capabilities")))


@api_v1.route("/instances/<path:iid>/<capability>")
def instance_capability(iid, capability):
    """text mode returns from liveview. mostly screen OCR for now
    """
    mod, e = _load_liveview()
    if e:
        return err("not_supported", e, 501)

    # capture "unknown" return val
    if capability in _WRITE_CAPABILITIES:
        return err("method_not_allowed",
                   f"'{capability}' is a write capability — use "
                   f"POST /api/v1/instances/{iid}/{capability}", 405)

    if capability not in _TEXT_CAPABILITIES or not hasattr(mod, capability):
        return err("not_found",
                   f"unknown capability '{capability}'. This project offers: "
                   f"{', '.join(sorted(set(_TEXT_CAPABILITIES) & set(dir(mod))))}",
                   404)

    kwargs = {}
    if capability == "ocr" and request.args.get("phrase"):
        kwargs["phrase"] = request.args["phrase"]
    if capability == "tty" and request.args.get("tail"):
        kwargs["tail"] = request.args["tail"]

    okflag, res = getattr(mod, capability)(iid, **kwargs)
    if not okflag:
        if str(res).startswith("unknown instance"):
            return err("not_found", res, 404)
        # old bug maybe fixed now. "has no X capability" . a state conflict
        return err("no_capability" if "capability" in str(res) else "read_failed",
                   res, 409)

    payload = {"instance": iid, "capability": capability}
    payload.update(res if isinstance(res, dict) else {"text": res})
    inst = mod.get_instance(iid) or {}
    return ok(payload, links=_instance_links(iid, inst.get("capabilities")))


@api_v1.route("/instances/<path:iid>")
def get_instance(iid):
    """get info about one instance"""
    mod, e = _load_liveview()
    if e:
        return err("not_supported", e, 501)

    inst = mod.get_instance(iid)
    if not inst:
        return err("not_found", f"Unknown instance: {iid}", 404)

    caps = inst.get("capabilities") or []
    inst["capability_reference"] = {
        c: getattr(mod, "CAPABILITY_DOC", {}).get(c, "") for c in caps
    }
    return ok(inst, links=_instance_links(iid, caps))
