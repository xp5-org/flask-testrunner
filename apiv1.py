import ast
import copy
import os

from flask import Blueprint, jsonify, request, redirect, url_for
from werkzeug.exceptions import HTTPException

import apphelpers
import stepplan
import test_runner
import dispatchhelper
import runhelper
import runsession
import drafthelper
import buildhelper
import apphelpers as testid
from dbhelper import db
from appstate import progress_state, process_registry, run_registry, batch_registry
from repair_config import repair_config, apply_step_param_fixes
from step_validator import validate_config_steps, format_results_text, has_blocking_issues
from newprojecthelper import (copybuildtest, update_config_in_file,
                             parent_literal_keys, update_parent_in_file)

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


# One definition, shared with the run path -- see runhelper.ensure_registry.
_ensure_registry = runhelper.ensure_registry


def _slug_maps():
    """(slug -> module, module -> slug) for the current registry."""
    _ensure_registry()
    with apphelpers.registry_lock:
        return testid.build_index(apphelpers.testfile_registry.keys())


def _resolve(test_id):
    """Accept either a slug or a module dot-path; return the module name."""
    _ensure_registry()
    with apphelpers.registry_lock:
        if test_id in apphelpers.testfile_registry:
            return test_id
        slug_to_mod, _ = _slug_maps()
        return slug_to_mod.get(test_id)


def _slug(modname):
    _, mod_to_slug = _slug_maps()
    return mod_to_slug.get(modname, modname)


def _meta(test_id):
    modname = _resolve(test_id)
    with apphelpers.registry_lock:
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

    Deliberately UNexpanded: this is what the editor loads and what gets
    written back, so it must stay the literal the file holds -- template and
    matrix references included. Anything that needs the steps that actually
    run wants _expanded_config() instead.
    """
    full_path = _full_path(test_id)
    if not full_path:
        return None
    return _extract_config(full_path)


def _expanded_config(test_id, strict=False):
    """CONFIG with plan references resolved -- the steps that actually run.

    Returns (config, notes). Non-strict by default: a read should still show
    the test, with the broken reference visible as an unresolved step, rather
    than 500 on a typo in a template id.
    """
    cfg = _raw_config(test_id)
    if cfg is None:
        return None, []
    return stepplan.resolve_config(cfg, _full_path(test_id), strict=strict)


def _plan_summary(test_id):
    """What the container offers this testlist: templates, matrices, machines."""
    full_path = _full_path(test_id)
    if not full_path:
        return None
    try:
        plan = stepplan.load_plan(os.path.dirname(os.path.abspath(full_path)))
    except stepplan.PlanError as e:
        return {"error": str(e)}
    if not plan:
        return None
    return {
        "templates": {
            tid: {
                "label": (t or {}).get("label") or tid,
                "description": (t or {}).get("description"),
                "params": sorted((t or {}).get("params") or {}),
                "step_count": len((t or {}).get("steps") or []),
                "step_ids": [s.get("id") for s in ((t or {}).get("steps") or [])],
                "overridable": {
                    s.get("id") or str(i): s.get("overridable")
                    for i, s in enumerate(((t or {}).get("steps") or []), 1)
                },
            }
            for tid, t in (plan.get("templates") or {}).items()
        },
        "matrices": {
            mid: {
                "label": (m or {}).get("label") or mid,
                "description": (m or {}).get("description"),
                "axes": (m or {}).get("axes"),
                # None when the plan defines only the cell shape and each
                # use site brings its own cells.
                "cell_count": (len(stepplan.matrix_cells({"matrix": mid}, plan))
                               if ((m or {}).get("axes") or (m or {}).get("cells"))
                               else None),
                "step_count": len((m or {}).get("steps") or []),
                "step_ids": [s.get("id") for s in ((m or {}).get("steps") or [])],
                # Per-step, so the editor's grid can put one column under each
                # (step, param) pair rather than one column per param name --
                # two steps of a cell can both take "successphrase" and mean
                # entirely different things.
                "overridable": {
                    s.get("id") or str(i): s.get("overridable")
                    for i, s in enumerate(((m or {}).get("steps") or []), 1)
                },
                "columns": sorted({
                    c for s in ((m or {}).get("steps") or [])
                    for c in (s.get("overridable") or [])
                }),
                # The plan's own cells, so the editor's matrix grid has
                # something to render for a use site that -- as almost every
                # real one does -- brings none of its own ({"matrix": mid}
                # alone). Without this the grid has no row data at all: it
                # would build its "cell ci" columns against an empty list and
                # render zero rows, even though the matrix plainly runs N
                # cells. Explicit block-level cells still take priority
                # client-side, matching stepplan._matrix_spec's "an explicit
                # block.cells replaces the plan's, it doesn't merge" rule.
                "cells": (m or {}).get("cells"),
            }
            for mid, m in (plan.get("matrices") or {}).items()
        },
        "machines": {
            k: {"backend": (v or {}).get("backend"),
                "params": sorted((v or {}).get("params") or {})}
            for k, v in (plan.get("machines") or {}).items()
        },
    }


def _test_links(test_id):
    base = f"/api/v1/tests/{test_id}"
    return {
        "self":      base,
        "steps":     f"{base}/steps",
        "validate":  f"{base}/validate",
        "artifacts": f"{base}/artifacts",
        "reports":   f"{base}/reports",
        "matrix":    f"{base}/matrix",
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
            "GET  /api/v1/nodes                     — see which hosts are up right now",
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
            {"rel": "nodes", "method": "GET", "href": "/api/v1/nodes",
             "summary": "Hosts running this framework against the same shared "
                        "NFS mounts, each announcing hostname/os/mem/cpu/disk "
                        "every few seconds. A host that hasn't checked in for "
                        "30s is left out, not marked stale."},
        ],
    }
    links = {
        "actions":   "/api/v1/actions",
        "tests":     "/api/v1/tests",
        "runs":      "/api/v1/runs",
        "reports":   "/api/v1/reports",
        "instances": "/api/v1/instances",
        "nodes":     "/api/v1/nodes",
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
    # Hold the lock across the reload and the read so a concurrent reload
    # (e.g. a background run's runhelper.ensure_registry) can't clear/rebuild
    # the registry out from under this iteration.
    with apphelpers.registry_lock:
        _, mod_to_slug = _slug_maps()
        # One query for every container's latest status, grouped here --
        # a query (and a fresh sqlite connection) per testlist was 300 round
        # trips to a database that lives on NFS.
        status_by_parent = {}
        for r in db.get_latest_namedteststatus():
            status_by_parent.setdefault(r["testparentname"], {})[r["types"]] = r["status"]
        result = []
        for modname, info in apphelpers.testfile_registry.items():
            latest_status = dict(status_by_parent.get(info["id"], {}))

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
    with apphelpers.registry_lock:
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
    # No rescan: the page that asks for this also fetches /tests, which does
    # the disk check. This only needs the last reload's result, and a reload
    # only if a run has emptied the registry since.
    test_runner.ensure_loaded()
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
    expanded, notes = _expanded_config(test_id)

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
        # Both forms, always: "config"/"steps" is what the file holds and what
        # a PUT writes back; "steps_expanded" is what the runner will actually
        # execute once plan references are resolved. They are the same list
        # for a testlist that uses no templates.
        "steps_expanded": expanded.get("steps", []) if expanded else [],
        "expansion_notes": notes,
        "plan":          _plan_summary(test_id),
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
        expanded, notes = _expanded_config(test_id)
        return ok({"id": test_id,
                   "steps": cfg.get("steps", []),
                   "steps_expanded": expanded.get("steps", []),
                   "expansion_notes": notes,
                   "has_references": stepplan.has_references(cfg),
                   "plan": _plan_summary(test_id)},
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

    expansion_notes = []
    if config is None:
        # Validate the on-disk CONFIG, expanded: the steps that will actually
        # run are the ones worth validating, and a reference entry has no
        # action for the step validator to check at all.
        full_path = _full_path(test_id)
        if not full_path:
            return err("not_found", f"No config file on disk for: {test_id}", 404)
        config = _extract_config(full_path)
        config, expansion_notes = stepplan.resolve_config(config, full_path, strict=False)
    elif stepplan.has_references(config):
        # A posted config (the testbuilder validating unsaved edits) can carry
        # references too -- expand against the container the test lives in.
        config, expansion_notes = stepplan.resolve_config(
            config, _full_path(test_id), strict=False)

    results    = validate_config_steps(config, TESTSRC_HELPERDIR,
                                       resolved_vars=resolved_vars)
    return ok({
        "id":         test_id,
        "results":    results,
        "has_errors": has_blocking_issues(results),
        "summary":    format_results_text(results),
        "expansion_notes": expansion_notes,
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

    # Interactive-run knobs; all optional, CONFIG supplies the defaults
    # (on_fault, suspend_timeout, suspend_timeout_action) -- see runsession.
    options = {}
    for key, allowed in (("source", ("lkg", "draft")),
                         ("mode", runsession.MODES),
                         ("on_fault", runsession.ON_FAULT),
                         ("suspend_timeout_action", runsession.TIMEOUT_ACTIONS)):
        if body.get(key) is not None:
            if body[key] not in allowed:
                return err("bad_request",
                           f"'{key}' must be one of {', '.join(allowed)}", 400)
            options[key] = body[key]
    if body.get("suspend_timeout") is not None:
        try:
            options["suspend_timeout"] = max(0.0, float(body["suspend_timeout"]))
        except (TypeError, ValueError):
            return err("bad_request", "'suspend_timeout' is minutes (0 = never)", 400)
    if options.get("source") == "draft" and not drafthelper.load(_full_path_reloading(test_id) or ""):
        return err("not_found", f"{test_id} has no draft to run", 404)

    # A/B: "build": "A"|"B" runs against the container's selected build for
    # that slot, "build_id" against a specific registered build. Resolved
    # now, so a refusal is a 400 here rather than a run that dies at step 0.
    # A build_tool runs the OTHER direction: "Build A"/"Build B" merges slot
    # A/B's own input overrides onto its CONFIG and, once it passes, drops
    # the result straight into that slot -- it doesn't consume an existing
    # build, so build_id has no meaning for it.
    if body.get("build") is not None or body.get("build_id") is not None:
        meta = _meta(modname) or {}
        if meta.get("is_tool"):
            if not meta.get("build_tool"):
                return err("bad_request",
                           "this tool has no configurable input; it isn't A/B built", 400)
            if body.get("build_id") is not None:
                return err("bad_request", "build_id doesn't apply to a build_tool run", 400)
            slot = (body.get("build") or "").strip().upper()
            if slot not in buildhelper.SLOTS:
                return err("bad_request",
                           f"'build' must be one of {', '.join(buildhelper.SLOTS)}", 400)
            options["build_input_slot"] = slot
        else:
            try:
                options["build_override"] = _build_override_for(
                    modname, slot=body.get("build"), build_id=body.get("build_id"))
            except buildhelper.BuildError as e:
                return err("bad_request", str(e), 400)

    try:
        run_id = runhelper.launch_run(modname, options)
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
    # live_results mirrors test_runner.run_tests' own `results` list, written
    # in step order as the active run completes each step -- populate_sqlite()
    # (and so /api/v1/tests/<id>/matrix's report-backed status) only lands
    # once, at the very end of the run, so this is the only per-step PASS/
    # FAIL/SKIPPED signal available while a run is still in flight.
    live_results = [
        {"index": i, "name": r[0], "status": r[1], "duration": r[5]}
        for i, r in enumerate(progress_state.live_results, start=1)
    ]
    return ok({
        "step":         progress_state.step,
        "testname":     progress_state.testname,
        "testid":       progress_state.testid,
        "testtype":     progress_state.testtype,
        "step_name":    progress_state.step_name,
        "processes":    process_registry.snapshot(),
        "busy":         runhelper.is_busy(),
        "batch":        batch_registry.current(),
        "live_results": live_results,
        "session":      _session_brief(runsession.sessions.current()),
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


# interactive run sessions

def _session_brief(session):
    if session is None:
        return None
    snap = session.snapshot()
    brief = {k: snap[k] for k in ("run_id", "module", "state", "reason", "mode",
                                   "on_fault", "cursor", "suspend_seconds_left",
                                   "allowed_commands", "follows_draft")}
    # numbered the way the report numbers it (execution order)
    brief["next_ordinal"] = len(snap["executed"]) + 1
    brief["next_title"] = (snap["next"] or {}).get("title")
    brief["total"] = len(snap["executed"]) + len(snap["pending"])
    brief["running_step"] = snap["running_step"]
    return brief


def _session_for(run_id):
    if run_id == "current":
        return runsession.sessions.current()
    return runsession.sessions.get(run_id)


def _session_links(run_id):
    base = f"/api/v1/runs/{run_id}"
    return {"self": f"{base}/session", "control": f"{base}/control",
            "run": base}


@api_v1.route("/runs/<run_id>/session")
def run_session(run_id):
    """Full state of an interactive run: what ran, what is pending (with
    stable uids), why it is suspended, what may be done next."""
    session = _session_for(run_id)
    if session is None:
        return err("not_found", f"No session for run: {run_id}", 404)
    return ok(session.snapshot(), links=_session_links(session.run_id))


@api_v1.route("/runs/<run_id>/control", methods=["POST"])
def run_control(run_id):
    """Body: {"command": step|resume|pause|run_to|retry|stop|abandon|
    continue_in_memory|set_timeout, ...args}. run_to takes "uid" or "pos";
    set_timeout takes "minutes" (0 = never) and optional "action"
    (stop|abandon). Returns the session snapshot after the command is queued.
    """
    session = _session_for(run_id)
    if session is None:
        return err("not_found", f"No session for run: {run_id}", 404)
    body = request.get_json(silent=True) or {}
    command = body.pop("command", None)
    if not command:
        return err("bad_request", "body must contain 'command'", 400)
    if command == "stop" and session is runsession.sessions.current():
        # The real stop, same as /runs/current/stop: flag the session AND
        # kill the run's processes, so a step blocked in a long wait (a 70s
        # OCR poll) ends now instead of the stop queueing behind it. The
        # session then skips what's left and runs the teardown steps.
        runhelper.request_stop()
        return ok(session.snapshot(), links=_session_links(session.run_id))
    try:
        snap = session.control(command, **body)
    except runsession.SessionError as e:
        return err("bad_state", str(e), 409)
    return ok(snap, links=_session_links(session.run_id))


@api_v1.route("/runs/<run_id>/adhoc", methods=["POST"])
def run_adhoc(run_id):
    """Run ONE step now, inside a suspended run, and return its result.

    Body: {"step": {action, param, title?}, "record": false}
    This is how the live viewer's "send keys" works for every emulator: it
    builds a real test_sendcommand and runs it through the same dispatch
    path a testlist step takes (not a host-side stand-in).
    record=true also inserts the step into the draft right after the last
    executed step, and the run counts it as executed there -- so what was
    done by hand is replayed next run, and Step does not repeat it now.
    """
    session = _session_for(run_id)
    if session is None:
        return err("not_found", f"No session for run: {run_id}", 404)
    body = request.get_json(silent=True) or {}
    step = body.get("step")
    if not isinstance(step, dict) or not step.get("action"):
        return err("bad_request", "body needs 'step' with an 'action'", 400)
    step = {"param": {}, "subaction": "", **step}
    step.pop(stepplan.ORIGIN_KEY, None)
    record = bool(body.get("record"))
    notes = []
    if record and not session.follows_draft:
        return err("conflict", "this run isn't following a draft (an inactive draft "
                               "exists) -- send without 'record as step'", 409)
    if record:
        if session.state != "suspended":
            return err("bad_state", "manual steps need a suspended run", 409)
        if session.cursor == 0:
            return err("bad_state", "nothing has run yet -- record after the emulator is up", 409)
        anchor = session.plan[session.cursor - 1][drafthelper.UID_KEY]
        step[drafthelper.UID_KEY] = drafthelper.new_uid()
        full_path = _full_path_reloading(session.module)
        doc = drafthelper.seed(full_path, session.lkg_steps, module=session.module)
        dispatch, _ = _dispatch_and_schemas()
        try:
            raw, notes = drafthelper.apply_ops(
                doc["steps"], [{"op": "insert", "after": anchor, "steps": [step]}],
                session.config, session.plan_def,
                known_actions={k for k, f in dispatch.items()
                               if getattr(f, "_is_teststep", False)})
            drafthelper.save(full_path, raw, module=session.module,
                             expected_revision=doc["revision"],
                             note=f"recorded manual step: {step.get('title') or step['action']}")
        except (drafthelper.DraftError, stepplan.PlanError, KeyError) as e:
            return _draft_err(e)
    try:
        result = session.adhoc(step, record=record,
                               timeout=float(body.get("timeout") or 900))
    except runsession.SessionError as e:
        return err("bad_state", str(e), 409)
    return ok({"result": result, "recorded": record, "notes": notes,
               "session": session.snapshot()}, links=_session_links(session.run_id))


# live-edit drafts (last-known-good = the .py file; draft = the working copy)

def _full_path_reloading(test_id):
    """_full_path, reloading the registry once on a miss: run_testfile()
    leaves only the module it ran registered, without __full_path__, so the
    first draft call after any run would otherwise 404 (same workaround
    test_matrix uses)."""
    full_path = _full_path(test_id)
    if not full_path:
        test_runner.reload_tests()
        full_path = _full_path(test_id)
    return full_path


def _draft_context(test_id):
    """(full_path, modname, lkg_steps, config, plan, session) for draft routes.

    While a run of this test is live, edits map through the config and plan
    that run STARTED with, and are checked against what it has frozen."""
    full_path = _full_path_reloading(test_id)
    if not full_path:
        return None
    modname = _resolve(test_id) or test_id
    session = runsession.sessions.for_module(modname)
    cfg = _extract_config(full_path)
    lkg = cfg.get("steps") or []
    if session is not None and session.follows_draft:
        return full_path, modname, lkg, session.config, session.plan_def, session
    plan = stepplan.load_plan(os.path.dirname(os.path.abspath(full_path)))
    return full_path, modname, lkg, {k: v for k, v in cfg.items() if k != "steps"}, plan, None


def _draft_payload(full_path, lkg, config, plan, session, doc=None, notes=None):
    doc = doc if doc is not None else drafthelper.load(full_path)
    data = {"draft": drafthelper.summary(doc, lkg)}
    if doc is not None:
        expanded, exp_notes = drafthelper.expand(doc["steps"], config, plan, strict=False)
        data["steps"] = doc["steps"]
        data["steps_expanded"] = expanded
        data["expansion_notes"] = exp_notes
    if session is not None:
        data["live_run"] = {"run_id": session.run_id, "state": session.state,
                            "frozen_uids": sorted(session.frozen_uids()),
                            "edit_locked_uids": sorted(session.edit_locked_uids()),
                            "retry_uid": session.retry_uid(),
                            "executed_uids": sorted(session.executed_uids()),
                            "boot_line": session.boot_line,
                            "cursor": session.cursor}
    if notes:
        data["notes"] = notes
    return data


def _draft_err(e):
    if isinstance(e, (drafthelper.DraftConflict, drafthelper.DraftLocked)):
        return err("conflict", str(e), 409)
    return err("bad_request", str(e), 400)


@api_v1.route("/tests/<path:test_id>/draft", methods=["GET", "PUT", "DELETE"])
def test_draft(test_id):
    """GET: the draft (steps, expanded steps with uids, whether the LKG moved
    under it, live-run freeze info). PUT {steps, expected_revision?, note?}:
    save a new revision -- always saved, even if it touches frozen steps (the
    live run ignores those and reports it). DELETE: discard the draft."""
    ctx = _draft_context(test_id)
    if ctx is None:
        return err("not_found", f"No config file on disk for: {test_id}", 404)
    full_path, modname, lkg, config, plan, session = ctx
    links = {**_test_links(test_id), "draft": f"/api/v1/tests/{test_id}/draft"}

    if request.method == "GET":
        return ok(_draft_payload(full_path, lkg, config, plan, session), links=links)

    if request.method == "DELETE":
        return ok({"discarded": drafthelper.discard(full_path)}, links=links)

    body = request.get_json(silent=True) or {}
    steps = body.get("steps")
    if not isinstance(steps, list):
        return err("bad_request", "PUT body must contain 'steps' (a list)", 400)
    base = session.lkg_steps if session is not None else lkg
    try:
        doc = drafthelper.save(full_path, steps, module=modname, base_steps=base,
                               expected_revision=body.get("expected_revision"),
                               note=body.get("note") or "edited")
    except drafthelper.DraftError as e:
        return _draft_err(e)
    data = _draft_payload(full_path, lkg, config, plan, session, doc=doc)
    try:
        expanded_cfg = {**config, "steps": data.get("steps_expanded") or []}
        results = validate_config_steps(expanded_cfg, TESTSRC_HELPERDIR)
        data["validation"] = {"has_errors": has_blocking_issues(results),
                              "summary": format_results_text(results)}
    except Exception as e:
        data["validation"] = {"has_errors": None, "summary": f"validator failed: {e}"}
    return ok(data, links=links)


@api_v1.route("/tests/<path:test_id>/draft/ops", methods=["POST"])
def test_draft_ops(test_id):
    """Edit the draft by EXPANDED-step uid -- the ids the session and the
    matrix show. Body: {"ops": [...], "expected_revision"?, "note"?}; see
    drafthelper.apply_ops for the op list. Creates the draft from the
    last-known-good (or from the steps the live run started with) if none
    exists. An op on a step the live run has frozen is refused (409)."""
    ctx = _draft_context(test_id)
    if ctx is None:
        return err("not_found", f"No config file on disk for: {test_id}", 404)
    full_path, modname, lkg, config, plan, session = ctx
    live = runsession.sessions.for_module(modname)
    if live is not None and not live.follows_draft:
        return err("conflict",
                   f"run {live.run_id} is running the .py while an inactive draft "
                   f"exists -- a live edit would go to a draft this run doesn't "
                   f"use. Make the draft active (or discard it) and re-run.", 409)
    body = request.get_json(silent=True) or {}
    ops = body.get("ops")
    if not isinstance(ops, list) or not ops:
        return err("bad_request", "body must contain a non-empty 'ops' list", 400)

    base = session.lkg_steps if session is not None else lkg
    doc = drafthelper.seed(full_path, base, module=modname)
    dispatch, _schemas = _dispatch_and_schemas()
    try:
        raw, notes = drafthelper.apply_ops(
            doc["steps"], ops, config, plan,
            locked=session.edit_locked_uids() if session is not None else (),
            pause_locked=(session.executed_uids() - {session.retry_uid()}
                          if session is not None else ()),
            known_actions={k for k, f in dispatch.items()
                           if getattr(f, "_is_teststep", False)})
        doc = drafthelper.save(full_path, raw, module=modname,
                               expected_revision=body.get("expected_revision", doc["revision"]),
                               note=body.get("note") or "; ".join(
                                   o.get("op", "?") for o in ops))
    except (drafthelper.DraftError, stepplan.PlanError, KeyError) as e:
        return _draft_err(e)
    return ok(_draft_payload(full_path, lkg, config, plan, session, doc=doc, notes=notes),
              links={**_test_links(test_id), "draft": f"/api/v1/tests/{test_id}/draft"})


@api_v1.route("/tests/<path:test_id>/draft/revisions")
def test_draft_revisions(test_id):
    full_path = _full_path_reloading(test_id)
    if not full_path:
        return err("not_found", f"No config file on disk for: {test_id}", 404)
    doc = drafthelper.load(full_path)
    if doc is None:
        return err("not_found", f"{test_id} has no draft", 404)
    full = request.args.get("full") in ("1", "true", "yes")
    revs = [{"revision": h["revision"], "at": h.get("at"), "note": h.get("note", ""),
             "step_count": len(h.get("steps") or []),
             **({"steps": h.get("steps")} if full else {})}
            for h in doc.get("history", [])]
    revs.append({"revision": doc["revision"], "at": doc.get("updated_at"),
                 "note": doc.get("note", ""), "step_count": len(doc["steps"]),
                 "current": True, **({"steps": doc["steps"]} if full else {})})
    return ok({"id": test_id, "revisions": revs})


@api_v1.route("/tests/<path:test_id>/draft/revert", methods=["POST"])
def test_draft_revert(test_id):
    """Body: {revision, expected_revision?}. Saves that revision's steps as a
    NEW revision -- nothing in the history is lost by reverting."""
    ctx = _draft_context(test_id)
    if ctx is None:
        return err("not_found", f"No config file on disk for: {test_id}", 404)
    full_path, modname, lkg, config, plan, session = ctx
    body = request.get_json(silent=True) or {}
    if body.get("revision") is None:
        return err("bad_request", "body must contain 'revision'", 400)
    try:
        doc = drafthelper.revert(full_path, body["revision"],
                                 expected_revision=body.get("expected_revision"))
    except drafthelper.DraftError as e:
        return _draft_err(e)
    return ok(_draft_payload(full_path, lkg, config, plan, session, doc=doc))


def _test_is_running(modname):
    return (runsession.sessions.for_module(modname) is not None
            or progress_state.testname == modname)


@api_v1.route("/tests/<path:test_id>/draft/activate", methods=["POST"])
def test_draft_activate(test_id):
    """Body: {"active": true|false}. Which version a plain Run of this test
    uses and the offline editor edits: the draft (true) or the .py (false).
    Refused while this test is running -- the run already chose."""
    full_path = _full_path_reloading(test_id)
    if not full_path:
        return err("not_found", f"No config file on disk for: {test_id}", 404)
    modname = _resolve(test_id) or test_id
    if _test_is_running(modname):
        return err("conflict", f"{test_id} is running -- switch versions after it ends", 409)
    body = request.get_json(silent=True) or {}
    try:
        doc = drafthelper.set_active(full_path, bool(body.get("active", True)))
    except drafthelper.DraftError as e:
        return _draft_err(e)
    cfg = _extract_config(full_path)
    return ok({"id": test_id, "draft": drafthelper.summary(doc, cfg.get("steps") or [])},
              links=_test_links(test_id))


@api_v1.route("/tests/<path:test_id>/draft/promote", methods=["POST"])
def test_draft_promote(test_id):
    """Write the draft's steps into the .py file (the new last-known-good) and
    drop the draft. Body: {expected_revision?}. Refused while a run of this
    test is live -- the run is still reading the draft."""
    ctx = _draft_context(test_id)
    if ctx is None:
        return err("not_found", f"No config file on disk for: {test_id}", 404)
    full_path, modname, lkg, config, plan, session = ctx
    if session is not None:
        return err("conflict", f"run {session.run_id} of this test is still live", 409)
    body = request.get_json(silent=True) or {}
    try:
        doc = drafthelper.promote(full_path, expected_revision=body.get("expected_revision"))
    except drafthelper.DraftError as e:
        return _draft_err(e)
    test_runner.reload_tests()
    return ok({"id": test_id, "promoted_revision": doc["revision"],
               "written_to": full_path}, links=_test_links(test_id))


@api_v1.route("/tests/<path:test_id>/ocr-preview", methods=["POST"])
def test_ocr_preview(test_id):
    """Preview an OCR step against the live instance: one pass of the step's
    own OCR code (dispatch_functions.ocr_preview), no waiting.

    Body: {"param": {...}} -- the step's params as the editor holds them,
    {tokens} and all (unsaved edits welcome); or {"uid": U} to preview a
    step of the live run / draft as stored. Tokens resolve against the
    CONFIG the live run started with.

    Only while no step is executing: a run of this test that is SUSPENDED,
    or instances this test left up after its run ended. While a step runs
    the instance belongs to it -- a concurrent screen grab would race it.
    """
    modname = _resolve(test_id) or test_id
    body = request.get_json(silent=True) or {}
    session = runsession.sessions.for_module(modname)
    if session is not None:
        if session.state != "suspended":
            return err("bad_state",
                       f"run {session.run_id} is executing a step -- preview "
                       f"needs it suspended (Pause, or a breakpoint)", 409)
        context, config = session.context, session.config
    else:
        if runhelper.is_busy():
            return err("bad_state", "another run is in progress", 409)
        import appstate
        if appstate.live_context.module() != modname:
            return err("not_found",
                       f"no live instances from {test_id}: run it (with a "
                       f"breakpoint) first", 404)
        context = appstate.live_context.context()
        full_path = _full_path_reloading(test_id)
        cfg = _extract_config(full_path) if full_path else {}
        config = {k: v for k, v in cfg.items() if k != "steps"}

    param = body.get("param")
    if param is None and body.get("uid"):
        step = None
        if session is not None:
            step = next((s for s in session.plan if s.get(drafthelper.UID_KEY) == body["uid"]), None)
        if step is None:
            return err("not_found", f"no step {body['uid']!r} in the live run", 404)
        param = step.get("param") or {}
    if not isinstance(param, dict):
        return err("bad_request", "body needs 'param' (a dict) or 'uid'", 400)

    dispatch, _ = _dispatch_and_schemas()
    step_fn = dispatch.get("test_ocrwordsearch")
    preview = getattr(getattr(step_fn, "__wrapped__", None), "__globals__", {}).get("ocr_preview")
    if preview is None:
        return err("not_supported", "dispatch_functions has no ocr_preview()", 501)

    okflag, res = preview(context, config, **param)
    if not okflag:
        return err("preview_failed", res, 409)

    import base64
    for r in res:
        shot = r.pop("screenshot", None)
        r["image"] = None
        if shot and os.path.exists(shot):
            try:
                from PIL import Image
                with Image.open(shot) as im:
                    r["image_size"] = list(im.size)
            except Exception:
                pass
            with open(shot, "rb") as f:
                r["image"] = "data:image/png;base64," + base64.b64encode(f.read()).decode("ascii")
        # crop coords in the step are NATIVE pixels; the image is native*scale
        r["crop"] = {k: param.get(k) for k in ("startx", "starty", "stopx", "stopy")}
    return ok({"id": test_id, "run_id": session.run_id if session else None,
               "results": res})


def _failed_container_children(children):
    """module names whose latest run had a FAIL/ERROR status for
    at least one of their test types 
    """
    with apphelpers.registry_lock:
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


def _container_dir(container):
    """(children, directory) for a container id, or (None, None) if unknown.

    Every child of a container lives in the same directory -- that is what
    makes them children -- so the first one's location is the container's.
    """
    with apphelpers.registry_lock:
        children = testid.resolve_container(container, apphelpers.testfile_registry)
        if not children:
            return None, None
        full_path = apphelpers.testfile_registry[children[0]].get("__full_path__")
    if not full_path:
        return children, None
    return children, os.path.dirname(os.path.abspath(full_path))


@api_v1.route("/containers/<path:container>/parent", methods=["GET", "PUT"])
def container_parent(container):
    """Read or edit the optional keys of a container's __testparent__.py.

    Only the optional keys are writable here. name/archtype/platform are the
    container's identity: every child repeats the name in CONFIG['parent']
    and fails to load if it stops matching, and the pair (name, archtype)
    IS the container id this URL and all of its status history are keyed by.
    Renaming a container is a multi-file edit, not a form field.
    """
    _ensure_registry()
    children, dirpath = _container_dir(container)
    if children is None:
        return err("not_found", f"Unknown container: {container}", 404)
    if not dirpath:
        return err("not_found", f"{container} has no directory on disk", 404)

    if request.method == "PUT":
        body = request.get_json(silent=True) or {}
        unknown = sorted(set(body) - set(apphelpers.OPTIONAL_PARENT_KEYS))
        if unknown:
            return err("bad_request",
                       "not editable here: %s (writable: %s)"
                       % (", ".join(unknown),
                          ", ".join(apphelpers.OPTIONAL_PARENT_KEYS)), 400)

        updates = {}
        if "description" in body:
            desc = body["description"]
            if desc is not None and not isinstance(desc, str):
                return err("bad_request", "description must be a string", 400)
            # An emptied textarea means "drop the description", not a
            # container whose blurb is the empty string.
            updates["description"] = desc or None
        for key in ("batch_run_all", "batch_run_failed", "batch_run_tools",
                    "build_tracker", "ab_testing"):
            if key in body:
                updates[key] = bool(body[key])
        if "view" in body:
            view = str(body["view"] or "").strip().lower()
            if view not in apphelpers.CONTAINER_VIEWS:
                return err("bad_request",
                           "view must be one of: %s"
                           % ", ".join(apphelpers.CONTAINER_VIEWS), 400)
            updates["view"] = view

        if updates:
            try:
                update_parent_in_file(dirpath, updates)
            except ValueError as e:
                return err("bad_request", str(e), 400)
            test_runner.reload_tests()

    try:
        parent = apphelpers.load_parent(dirpath) or {}
    except apphelpers.ParentError as e:
        return err("bad_request", str(e), 400)
    literal, computed = parent_literal_keys(dirpath)

    return ok({
        "container": container,
        "dir": os.path.relpath(dirpath, apphelpers.TESTSRC_BASE),
        "stub": os.path.join(os.path.relpath(dirpath, apphelpers.TESTSRC_BASE),
                             apphelpers.TESTPARENT_FILE),
        "children": children,
        "parent": {k: parent.get(k) for k in
                   apphelpers.REQUIRED_PARENT_KEYS + apphelpers.OPTIONAL_PARENT_KEYS},
        "view": apphelpers.parent_view(parent),
        "views": list(apphelpers.CONTAINER_VIEWS),
        "editable_keys": list(apphelpers.OPTIONAL_PARENT_KEYS),
        # Keys a stub computes in Python rather than writing as a literal --
        # the form shows them read-only instead of freezing the expression.
        "computed_keys": sorted(computed),
    }, links={"container": f"/test/{container}",
              "batch-runs": f"/api/v1/containers/{container}/batch-runs"})


@api_v1.route("/containers/<path:container>/plan", methods=["GET", "PUT"])
def container_plan(container):
    """Read or write a container's __testplan__.py -- its shared step plan.

    GET returns the whole PLAN plus, for each template and matrix, which
    testlists in the container currently reference it. That back-reference is
    the thing to look at before editing a template: changing a boot sequence
    here changes it for every test that boots.
    """
    _ensure_registry()
    children, dirpath = _container_dir(container)
    if children is None:
        return err("not_found", f"Unknown container: {container}", 404)
    if not dirpath:
        return err("not_found", f"{container} has no directory on disk", 404)

    if request.method == "PUT":
        body = request.get_json(silent=True) or {}
        plan = body.get("plan")
        if not isinstance(plan, dict):
            return err("bad_request", "PUT body must contain a 'plan' object", 400)
        try:
            ok_msg = _write_plan(dirpath, plan)
        except (stepplan.PlanError, ValueError) as e:
            return err("bad_request", str(e), 400)
        test_runner.reload_tests()
        if ok_msg:
            pass

    try:
        plan = stepplan.load_plan(dirpath)
    except stepplan.PlanError as e:
        return err("bad_request", str(e), 400)

    return ok({
        "container": container,
        "dir":  os.path.relpath(dirpath, apphelpers.TESTSRC_BASE),
        "file": os.path.relpath(stepplan.plan_path(dirpath), apphelpers.TESTSRC_BASE),
        "exists": os.path.exists(stepplan.plan_path(dirpath)),
        "plan": plan or {},
        "used_by": _plan_usage(children),
    }, links={"container": f"/test/{container}"})


def _plan_usage(children):
    """Which testlists reference each template / matrix, and how many times."""
    usage = {"templates": {}, "matrices": {}}
    for modname in children:
        full_path = None
        with apphelpers.registry_lock:
            info = apphelpers.testfile_registry.get(modname) or {}
            full_path = info.get("__full_path__")
        if not full_path or not os.path.exists(full_path):
            continue
        try:
            cfg = _extract_config(full_path)
        except Exception:
            continue
        for entry in (cfg.get("steps") or []):
            kind = stepplan.entry_kind(entry)
            if kind == "template":
                usage["templates"].setdefault(entry["use"], []).append(modname)
            elif kind == "matrix" and entry.get("matrix"):
                usage["matrices"].setdefault(entry["matrix"], []).append(modname)
    return usage


def _write_plan(dirpath, plan):
    """Write a PLAN dict into the container's __testplan__.py.

    Validated by loading it back before it is trusted: a plan that does not
    parse would break every testlist in the container at once, and finding
    that out at run time is far too late.
    """
    stepplan._validate_plan(plan, stepplan.plan_path(dirpath))
    path = stepplan.plan_path(dirpath)
    if not os.path.exists(path):
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(_NEW_PLAN_STUB)
    # Same AST-span rewriter the testlists use, pointed at PLAN instead of
    # CONFIG, so a hand-written docstring or helper above PLAN survives.
    update_config_in_file(path, dict(plan), dict_name="PLAN")
    stepplan.clear_plan_cache()
    stepplan.load_plan(dirpath)          # raises if what we wrote is broken
    return True


_NEW_PLAN_STUB = '''"""Shared step plan for this container.

Step-group templates and step matrices the testlists here reference instead
of repeating. stepplan.expand_steps() resolves them into ordinary steps
before anything runs, so the runner, the editor and the report all see one
flat numbered list.
"""

PLAN = {
    "machines": {},
    "templates": {},
    "matrices": {},
}
'''


@api_v1.route("/tests/<path:test_id>/promote", methods=["POST"])
def promote_steps(test_id):
    """Lift a run of steps out of a testlist into a container template.

    Body: {"from": 1, "to": 12, "template_id": "boot_dos_pair", "label": ...,
           "overridable": {"<step_id>": [...]}, "dry_run": false}

    `from`/`to` are 1-based positions in the testlist's ON-DISK steps (what
    the editor shows as its unexpanded list), inclusive. The steps move to the
    container's plan and are replaced by a single {"use": ...} entry, so the
    expansion is the same list in the same order -- promoting is a refactor,
    not a change to what the test does. That is checked here, not assumed:
    the expansion is compared against the original steps before anything is
    written, and a mismatch is refused.
    """
    meta = _meta(test_id)
    if not meta:
        return err("not_found", f"Unknown test: {test_id}", 404)
    full_path = _full_path(test_id)
    if not full_path:
        return err("not_found", f"No config file on disk for: {test_id}", 404)

    body  = request.get_json(silent=True) or {}
    tid   = (body.get("template_id") or "").strip()
    start = body.get("from")
    end   = body.get("to")
    if not tid:
        return err("bad_request", "template_id is required", 400)
    if not isinstance(start, int) or not isinstance(end, int):
        return err("bad_request", "'from' and 'to' must be 1-based integers", 400)

    cfg = _extract_config(full_path)
    steps = list(cfg.get("steps") or [])
    if not 1 <= start <= end <= len(steps):
        return err("bad_request",
                   f"range {start}-{end} is outside this testlist's "
                   f"{len(steps)} entries", 400)

    chunk = steps[start - 1:end]
    bad = [i for i, s in enumerate(chunk, start) if stepplan.entry_kind(s) != "step"]
    if bad:
        return err("bad_request",
                   f"entries {bad} are already template/matrix references; "
                   f"flatten them first if you want them in a template", 400)

    dirpath = os.path.dirname(os.path.abspath(full_path))
    try:
        plan = stepplan.load_plan(dirpath) or {}
    except stepplan.PlanError as e:
        return err("bad_request", str(e), 400)
    plan = copy.deepcopy(plan)
    plan.setdefault("templates", {})
    if tid in plan["templates"] and not body.get("replace"):
        return err("conflict",
                   f"template {tid!r} already exists in this container's plan; "
                   f"pass replace=true to overwrite it", 409)

    overridable = body.get("overridable") or {}
    tpl_steps = []
    for i, step in enumerate(chunk, 1):
        tstep = copy.deepcopy(step)
        for key in ("group", "subgroup", "phase", stepplan.ORIGIN_KEY):
            tstep.pop(key, None)
        sid = (body.get("step_ids") or {}).get(str(i)) or f"s{i}"
        tstep["id"] = sid
        if overridable.get(sid):
            tstep["overridable"] = sorted(overridable[sid])
        tpl_steps.append(tstep)

    plan["templates"][tid] = {
        "label": body.get("label") or tid,
        "description": body.get("description") or "",
        "steps": tpl_steps,
    }

    use_entry = {"use": tid}
    for key in ("group", "phase"):
        seen = {s.get(key) for s in chunk if s.get(key)}
        if len(seen) == 1:
            use_entry[key] = seen.pop()
    new_steps = steps[:start - 1] + [use_entry] + steps[end:]

    # Prove the refactor before writing it: the new list must expand to the
    # same steps the old one did, ignoring only the group/phase labels the
    # reference now supplies. Both sides are EXPANDED -- the testlist may
    # already hold other references, so the on-disk entries are not the steps
    # and comparing against them would be comparing 46 entries with 173 steps.
    try:
        old_plan = stepplan.load_plan(dirpath)
        before, _ = stepplan.expand_steps(cfg, old_plan, strict=True)
    except stepplan.PlanError as e:
        return err("bad_request", f"this testlist does not expand cleanly today: {e}", 400)
    expanded, notes = stepplan.expand_steps({**cfg, "steps": new_steps}, plan, strict=False)
    for s in before + expanded:
        s.pop(stepplan.ORIGIN_KEY, None)
    if not _same_steps(expanded, before):
        return err("conflict",
                   "promoting these steps would change what the test runs, so "
                   "nothing was written. This is a bug in the promotion, not "
                   "in your selection -- report the step range.", 409)

    if body.get("dry_run"):
        return ok({"id": test_id, "template_id": tid, "dry_run": True,
                   "template": plan["templates"][tid], "steps": new_steps,
                   "expansion_notes": notes})

    try:
        _write_plan(dirpath, plan)
    except (stepplan.PlanError, ValueError) as e:
        return err("bad_request", str(e), 400)
    update_config_in_file(full_path, {"steps": new_steps})
    test_runner.reload_tests()
    return ok({"id": test_id, "template_id": tid, "promoted": len(chunk),
               "steps": new_steps, "expansion_notes": notes},
              links=_test_links(test_id))


def _same_steps(a, b):
    """Equal ignoring group/subgroup/phase, which are display labels only."""
    meta = ("group", "subgroup", "phase")
    strip = lambda s: {k: v for k, v in s.items() if k not in meta}
    return [strip(s) for s in a] == [strip(s) for s in b]


@api_v1.route("/tests/<path:test_id>/flatten", methods=["POST"])
def flatten_steps(test_id):
    """Break a template/matrix reference out into plain steps in this testlist.

    Body: {"position": 3, "dry_run": false} -- 1-based into the on-disk steps.

    The inverse of promote, and the escape hatch that makes templates safe to
    adopt: a testlist that needs to diverge from its template takes its own
    copy and stops tracking it, without touching the template or any other
    testlist using it.
    """
    meta = _meta(test_id)
    if not meta:
        return err("not_found", f"Unknown test: {test_id}", 404)
    full_path = _full_path(test_id)
    if not full_path:
        return err("not_found", f"No config file on disk for: {test_id}", 404)

    body = request.get_json(silent=True) or {}
    position = body.get("position")
    if not isinstance(position, int):
        return err("bad_request", "'position' must be a 1-based integer", 400)

    cfg = _extract_config(full_path)
    dirpath = os.path.dirname(os.path.abspath(full_path))
    try:
        plan = stepplan.load_plan(dirpath)
        before, _ = stepplan.expand_steps(cfg, plan, strict=True)
        new_steps = stepplan.flatten_reference(cfg, plan, position)
        after, _  = stepplan.expand_steps({**cfg, "steps": new_steps}, plan, strict=True)
    except stepplan.PlanError as e:
        return err("bad_request", str(e), 400)

    for s in before + after:
        s.pop(stepplan.ORIGIN_KEY, None)
    if before != after:
        return err("conflict",
                   "flattening would change what the test runs, so nothing "
                   "was written", 409)

    if body.get("dry_run"):
        return ok({"id": test_id, "position": position, "dry_run": True,
                   "steps": new_steps, "step_count": len(new_steps)})

    update_config_in_file(full_path, {"steps": new_steps})
    test_runner.reload_tests()
    return ok({"id": test_id, "position": position, "steps": new_steps,
               "step_count": len(new_steps)}, links=_test_links(test_id))


@api_v1.route("/containers/<path:container>/batch-runs", methods=["POST"])
def container_batch_runs(container):
    _ensure_registry()

    body = request.get_json(silent=True) or {}
    mode = body.get("mode", "all")
    if mode not in ("all", "failed", "tools", "ab", "build_ab"):
        return err("bad_request",
                   "mode must be 'all', 'failed', 'tools', 'ab' or 'build_ab'", 400)
    ab_order = body.get("ab_order", "interleave")
    if mode in ("ab", "build_ab") and ab_order not in buildhelper.AB_ORDERS:
        return err("bad_request",
                   "ab_order must be one of %s" % ", ".join(buildhelper.AB_ORDERS), 400)

    with apphelpers.registry_lock:
        children = testid.resolve_container(container, apphelpers.testfile_registry)
        if not children:
            return err("not_found", f"Unknown container: {container}", 404)

        if mode in ("all", "ab"):
            # "Run Tests": every child except opt-outs (batch_exclude) and
            # the infra tools (is_tool) -- a routine test pass must not
            # silently re-trigger a build or a deploy.
            items = [m for m in children
                     if not apphelpers.testfile_registry[m].get("batch_exclude")
                     and not apphelpers.testfile_registry[m].get("is_tool")]
        elif mode == "tools":
            items = [m for m in children
                     if apphelpers.testfile_registry[m].get("is_tool")]
        elif mode == "build_ab":
            # "Build A & B": only the build_tool subset of the tools -- a
            # host_tool (deploy step, no configurable input) has no slot to
            # build into and stays mono, run individually or via "Run Tools".
            items = [m for m in children
                     if apphelpers.testfile_registry[m].get("is_tool")
                     and apphelpers.testfile_registry[m].get("build_tool")]
        else:
            items = _failed_container_children(children)
    if not items:
        return err("bad_request",
                   "No tools in this container" if mode == "tools" else
                   "No build tools in this container" if mode == "build_ab" else
                   "No failed tests to re-run" if mode == "failed" else
                   "No tests to run for this batch", 400)

    if mode == "ab":
        # Every variant against slot A and/or B. Each (variant, slot) pair is
        # checked up front: one variant that can't take the build would
        # otherwise stop the batch halfway with half the comparison missing.
        slots = {"a": ["A"], "b": ["B"]}.get(ab_order, ["A", "B"])
        pairs, problems = {}, []
        for m in items:
            for slot in slots:
                try:
                    pairs[(m, slot)] = _build_override_for(m, slot=slot)
                except buildhelper.BuildError as e:
                    problems.append(f"{_slug(m)} [{slot}]: {e}")
        if problems:
            return err("bad_request", "A/B batch refused:\n" + "\n".join(problems), 400)
        if ab_order == "grouped":
            order = [(m, s) for s in slots for m in items]
        else:
            order = [(m, s) for m in items for s in slots]
        items = [(m, {"build_override": pairs[(m, s)]}, f"{m} [{s}]") for m, s in order]
    elif mode == "build_ab":
        # Each build_tool compiled once per slot with that slot's input
        # overrides, then registered straight into it -- no BuildError check
        # needed up front, unlike "ab": a build_tool has nothing to refuse,
        # an unconfigured slot just builds with CONFIG as written.
        slots = {"a": ["A"], "b": ["B"]}.get(ab_order, ["A", "B"])
        if ab_order == "grouped":
            order = [(m, s) for s in slots for m in items]
        else:
            order = [(m, s) for m in items for s in slots]
        items = [(m, {"build_input_slot": s}, f"{m} [{s}]") for m, s in order]

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



# build objects & A/B -- see buildhelper.py

def _container_of(modname):
    with apphelpers.registry_lock:
        meta = apphelpers.testfile_registry.get(modname) or {}
    return testid.container_id(meta.get("id"), meta.get("system")), meta


def _build_override_for(modname, slot=None, build_id=None):
    """run option for a variant against slot A/B (or one build id).

    Raises BuildError when the variant can't honour it -- a tool, or a
    testlist with no CONFIG key for the build to replace.
    """
    modname = _resolve(modname) or modname
    container, meta = _container_of(modname)
    if meta.get("is_tool"):
        raise buildhelper.BuildError("tools build the objects; they aren't A/B run")
    if isinstance(slot, str):
        slot = slot.strip().upper()
    override = buildhelper.override_for(container, slot=slot, build_id=build_id)
    full_path = meta.get("__full_path__")
    cfg = _extract_config(full_path) if full_path else None
    if not cfg:
        cfg = test_runner.load_config_from_path(full_path)
    buildhelper.check_variant_accepts(cfg or {}, override)
    return override


def _container_or_404(container):
    _ensure_registry()
    children, dirpath = _container_dir(container)
    if children is None:
        return None, err("not_found", f"Unknown container: {container}", 404)
    return children, None


def _ab_brief(container):
    return {s: (b["id"] if b else None) for s, b in buildhelper.get_ab(container).items()}


@api_v1.route("/containers/<path:container>/builds", methods=["GET", "POST"])
def container_builds(container):
    """GET: the container's registered builds, newest first, plus the A/B
    selection as this host resolves it. POST: register a build made outside
    the runner by hand ({path, output_key, repo?, label?, platform?, slot?};
    platform is detected from the file when omitted)."""
    children, e = _container_or_404(container)
    if e:
        return e
    if request.method == "POST":
        body = request.get_json(silent=True) or {}
        try:
            bid = buildhelper.register_manual(
                container, body.get("path"), (body.get("output_key") or "").strip(),
                repo=(body.get("repo") or "").strip() or None,
                label=(body.get("label") or "").strip() or None,
                platform=(body.get("platform") or "").strip() or None,
                slot=(body.get("slot") or "").strip().upper() or None)
        except (buildhelper.BuildError, OSError) as ex:
            return err("bad_request", str(ex), 400)
        return ok({"build": buildhelper.get_build(bid)}, status=201,
                  links={"self": f"/api/v1/containers/{container}/builds/{bid}"})
    try:
        limit = max(1, min(500, int(request.args.get("limit", 25))))
    except ValueError:
        limit = 25
    return ok({
        "container":   container,
        "builds":      buildhelper.list_builds(
                           container, limit,
                           include_hidden=request.args.get("hidden") in ("1", "true")),
        "ab":          _ab_brief(container),
        "platform":    buildhelper.host_platform(),
        "output_keys": buildhelper.output_keys_for(container),
    }, links={"ab": f"/api/v1/containers/{container}/ab",
              "runs": f"/api/v1/containers/{container}/build-runs"})


@api_v1.route("/containers/<path:container>/builds/<int:build_id>", methods=["GET", "PATCH"])
def container_build(container, build_id):
    children, e = _container_or_404(container)
    if e:
        return e
    if request.method == "PATCH":
        body = request.get_json(silent=True) or {}
        label = body.get("label")
        if label is not None and not isinstance(label, str):
            return err("bad_request", "label must be a string", 400)
        # "slot": "A"/"B" files the build into that list, null takes it out.
        kw = {}
        if "slot" in body:
            kw["slot"] = (body["slot"] or "").strip().upper() or None
        if body.get("platform"):
            kw["platform"] = body["platform"]
        existing = buildhelper.get_build(build_id)
        if not existing or existing["container"] != container:
            return err("not_found", f"build {build_id} is not in {container}", 404)
        try:
            build = buildhelper.update_build(
                build_id, container, label=label,
                hidden=(bool(body["hidden"]) if "hidden" in body else None), **kw)
        except buildhelper.BuildError as ex:
            return err("bad_request", str(ex), 400)
        return ok({"build": build, "ab": _ab_brief(container)})
    build = buildhelper.get_build(build_id)
    if not build or build["container"] != container:
        return err("not_found", f"build {build_id} is not in {container}", 404)
    return ok({"build": build})


@api_v1.route("/containers/<path:container>/ab", methods=["GET", "PUT"])
def container_ab(container):
    """Which build slot A and slot B resolve to on a platform (?platform=,
    default this host): the pinned build, else the newest build in that
    slot's list that runs there.

    PUT {"A": id|null, "B": id|null, "platform"?}: an id pins that build
    (the user override); null unpins, back to newest-for-platform."""
    children, e = _container_or_404(container)
    if e:
        return e
    platform = request.args.get("platform")
    if request.method == "PUT":
        body = dict(request.get_json(silent=True) or {})
        platform = body.pop("platform", None) or platform
        unknown = sorted(set(body) - set(buildhelper.SLOTS))
        if unknown:
            return err("bad_request", f"unknown slot(s): {', '.join(unknown)}", 400)
        try:
            for slot, bid in body.items():
                buildhelper.set_ab(container, slot, bid, platform=platform)
        except (buildhelper.BuildError, ValueError) as ex:
            return err("bad_request", str(ex), 400)
    platform = platform or buildhelper.host_platform()
    state = buildhelper.slot_state(container, platform)
    return ok({"platform": platform,
               "ab": {s: st["build"] for s, st in state.items()},
               "pinned": {s: st["pinned"] for s, st in state.items()}})


@api_v1.route("/containers/<path:container>/build-input", methods=["GET", "PUT"])
def container_build_input(container):
    """Per-slot CONFIG overrides a build_tool run merges in when launched as
    "Build A"/"Build B" (e.g. {"target_branch": "some-other-branch"}).
    GET: {"build_input": {"A": {...}, "B": {...}}}.
    PUT: {"A": {key: value, ...}?, "B": {...}?} -- either or both slots;
    an empty object clears that slot's overrides."""
    children, e = _container_or_404(container)
    if e:
        return e
    if request.method == "PUT":
        body = request.get_json(silent=True) or {}
        unknown = sorted(set(body) - set(buildhelper.SLOTS))
        if unknown:
            return err("bad_request", f"unknown slot(s): {', '.join(unknown)}", 400)
        for slot, overrides in body.items():
            try:
                buildhelper.set_build_input(container, slot, overrides or {})
            except buildhelper.BuildError as ex:
                return err("bad_request", str(ex), 400)
    return ok({"build_input": buildhelper.get_build_inputs(container)})


@api_v1.route("/containers/<path:container>/build-runs")
def container_build_runs(container):
    """Reports of this container keyed by the build they ran:
    ?group_by=commit (git sha, dirty trees kept apart) or ?group_by=md5
    (the exact bytes). Omitted: a flat newest-first list."""
    children, e = _container_or_404(container)
    if e:
        return e
    labels = _variant_labels(children)
    group_by = request.args.get("group_by")
    if group_by:
        try:
            groups = buildhelper.grouped_runs(container, group_by, labels)
        except buildhelper.BuildError as ex:
            return err("bad_request", str(ex), 400)
        return ok({"container": container, "group_by": group_by, "groups": groups})
    runs = buildhelper.container_runs(container)
    for r in runs:
        r["variant"] = labels.get(r["test_id"], r["test_id"])
    return ok({"container": container, "runs": runs})


def _variant_labels(children):
    """test_id -> the variant's button label (its CONFIG "function")."""
    out = {}
    with apphelpers.registry_lock:
        for m in children:
            types = (apphelpers.testfile_registry.get(m) or {}).get("types") or {}
            out[m] = next(iter(types), m)
    return out








# routes for reports access

@api_v1.route("/tests/<path:test_id>/matrix")
def test_matrix(test_id):
    """Step-matrix view: CONFIG["steps"] enumerated 1..N (same numbering
    test_runner.run_tests uses) joined against the latest report's per-step
    status, for whatever optional grouping labels the testlist declares.

    Entirely additive/optional: a testlist with no top-level
    CONFIG["step_matrix"] and no step-level "group"/"subgroup"/"phase"
    still returns a valid response (step_matrix: null, those fields null
    per step) -- the step-matrix viewer falls back to a flat table for it.
    Nothing here is read by test_runner or any existing route.

    Lazy reload_tests(): without ANY reload this 404s with a misleading
    "No config file on disk" for a test whose registry entry hasn't been
    (re)populated yet by some OTHER route in this process's lifetime (e.g.
    this is the first request hit after a /reload) -- but this endpoint is
    polled every ~2s by the step-matrix viewer while a run is watched, and
    reload_tests() clears + re-imports the WHOLE 258-testlist registry, so
    calling it unconditionally on every poll turned a rare cold-start gap
    into constant registry churn -- which is what surfaced a separate,
    pre-existing unguarded-read race in app.py's test_details() (a KeyError
    when its own reload_tests() call there raced one from here). Only
    reload here if the lookup actually misses, and only once.
    """
    meta = _meta(test_id)
    if not meta:
        test_runner.reload_tests()
        meta = _meta(test_id)
        if not meta:
            return err("not_found", f"Unknown test: {test_id}", 404)

    cfg = _raw_config(test_id)
    if cfg is None:
        test_runner.reload_tests()
        cfg = _raw_config(test_id)
    if cfg is None:
        return err("not_found", f"No config file on disk for: {test_id}", 404)

    # Enumerated 1..N to line up with the report's step numbers, so this has
    # to be the EXPANDED list -- the numbering the runner used. A testlist
    # built from plan templates holds a handful of reference entries on disk,
    # and numbering those would put step 3 of 35 against a report that has
    # 173 steps.
    expanded, _notes = stepplan.resolve_config(cfg, _full_path(test_id), strict=False)
    raw_steps = expanded.get("steps", [])
    modname = _resolve(test_id) or test_id

    report_summary = None
    status_by_index = {}
    reports = db.get_reports_by_test_id(modname)
    if reports:
        report_id = db.get_report_id_for_path(reports[0]["filepath"])
        report = db.get_report_json(report_id) if report_id else None
        if report:
            report_summary = {
                "report_id": report["report_id"],
                "timestamp": report["timestamp"],
                "status":    report["status"],
            }
            for r in report["results"]:
                status_by_index[r["index"]] = r

    steps = []
    for i, step in enumerate(raw_steps, start=1):
        live = status_by_index.get(i, {})
        steps.append({
            "index":        i,
            "title":        step.get("title") or step.get("description") or step.get("action"),
            "action":       step.get("action"),
            "phase":        step.get("phase"),        # "build" | "run" | "postprocess" | None
            "group":        step.get("group"),
            "subgroup":     step.get("subgroup"),
            "rerun_anchor": step.get("rerun_anchor"),  # reserved, unused today
            "status":       live.get("status"),
            "duration":     live.get("duration"),
            # The expanded list is the only place a per-cell pause is visible:
            # on disk it is one key inside one override inside one entry, and
            # the testbuilder's banded view collapses all 165 of them into a
            # single row. Carried here with the provenance the pause endpoint
            # needs to write it back.
            "pause_on":     bool(test_runner._pause_flag(step.get("pause_on", False))),
            "origin":       step.get(stepplan.ORIGIN_KEY) or {},
        })

    return ok({
        "id":          test_id,
        "module":      modname,  # resolved dot-path -- matches progress_state.testname,
                                  # for the viewer to tell whether a live run IS this test
        "step_matrix": cfg.get("step_matrix"),
        "steps":       steps,
        "report":      report_summary,
    }, links=_test_links(test_id))


def _prune_override(container, key):
    """Drop an override that has nothing left in it, and the block with it."""
    if container.get(key) == {}:
        container.pop(key)


# Moved to stepplan so the draft store can write through it too.
_pause_target = stepplan.step_target


@api_v1.route("/tests/<path:test_id>/steps/<int:index>/pause", methods=["POST"])
def set_step_pause(test_id, index):
    """Set or clear pause_on for ONE step of the EXPANDED list.

    Body: {"pause_on": true|false}. `index` is the 1-based number the runner
    and the report use -- the number a person actually has in hand ("pause it
    at step 220"), which for a plan-driven testlist is not an index into
    anything on disk. Mapping it back to the one key in the one entry that
    controls it is the whole job here; see _pause_target.

    The write is verified by re-expanding and reading the flag back off step
    `index` before this returns success: an override that landed on the wrong
    cell would otherwise show up as a run that sails past the step you asked
    it to stop at, an hour later.
    """
    meta = _meta(test_id)
    if not meta:
        return err("not_found", f"Unknown test: {test_id}", 404)
    full_path = _full_path(test_id)
    if not full_path:
        return err("not_found", f"No config file on disk for: {test_id}", 404)

    want = bool((request.get_json(silent=True) or {}).get("pause_on", True))
    cfg = _extract_config(full_path)
    expanded, _notes = stepplan.resolve_config(cfg, full_path, strict=False)
    steps = expanded.get("steps", [])
    if not 1 <= index <= len(steps):
        return err("bad_request",
                   f"step {index} is out of range: this test expands to "
                   f"{len(steps)} steps", 400)

    origin = steps[index - 1].get(stepplan.ORIGIN_KEY) or {}
    pos = origin.get("entry")
    entries = cfg.get("steps") or []
    if not pos or not 1 <= pos <= len(entries):
        return err("bad_request",
                   f"step {index} carries no usable provenance "
                   f"({origin.get('error') or 'unexpanded reference'})", 400)

    try:
        target = _pause_target(entries[pos - 1], origin)
    except KeyError as e:
        return err("bad_request", str(e), 400)

    if want:
        target["pause_on"] = True
    else:
        target.pop("pause_on", None)
        # Leave no empty {} behind in the file when the last flag comes off.
        entry = entries[pos - 1]
        for block in ("override", "cell_override"):
            for key, val in list((entry.get(block) or {}).items()):
                if isinstance(val, dict):
                    for k2 in list(val):
                        if val[k2] == {}:
                            val.pop(k2)
                if val == {}:
                    entry[block].pop(key)
            _prune_override(entry, block)

    update_config_in_file(full_path, {"steps": entries})
    stepplan.clear_plan_cache()
    test_runner.reload_tests()

    # Read it back off the expansion rather than trusting the write.
    check = _extract_config(full_path)
    rechecked, _n = stepplan.resolve_config(check, full_path, strict=False)
    got = (rechecked.get("steps") or [])[index - 1] if index <= len(
        rechecked.get("steps") or []) else {}
    effective = bool(test_runner._pause_flag(got.get("pause_on", False)))
    if effective != want:
        return err("conflict",
                   f"step {index} still reads pause_on={effective} after "
                   f"writing {want} -- the override did not land where it "
                   f"was aimed; check {os.path.basename(full_path)} by hand",
                   409)

    return ok({"id": test_id, "index": index, "pause_on": effective,
               "origin": origin, "written_to": full_path},
              links=_test_links(test_id))


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


# helpers for the "self & friends" host section -- see & friends
# up above are per-instance emulator processes; this is the machines running
# the testrunner itself, for when several NFS-sharing instances are up.

def _load_nodestatus():
    helper_file = os.path.join(TESTSRC_HELPERDIR, "nodestatus.py")
    if not os.path.isfile(helper_file):
        return None, "this project has no pyhelpers/nodestatus.py, so it cannot report hosts"
    import importlib.util
    spec = importlib.util.spec_from_file_location("project_nodestatus", helper_file)
    mod = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(mod)
    except Exception as e:
        return None, f"failed to load nodestatus.py: {type(e).__name__}: {e}"
    return mod, None


@api_v1.route("/nodes")
def list_nodes():
    """every host running this framework against the shared NFS mounts that has
    checked in recently -- see nodestatus.py for the write side"""
    mod, e = _load_nodestatus()
    if e:
        return err("not_supported", e, 501)

    nodes = mod.list_nodes()
    return ok({
        "nodes": nodes,
        "count": len(nodes),
        "stale_after_seconds": mod.STALE_AFTER,
    }, links={"self": "/api/v1/nodes"})
