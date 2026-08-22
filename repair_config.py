"""
repair_config.py
----------------
Pre-write repair pass run before a CONFIG is written to a cloned testlist.

Core rule (mirrors step_validator.py):
    Every resolved path value must start with config["projbasedir"].
    Any that don't are either fixed (top-level templates) or cleared
    (step params, so the dispatch function falls back to the config value).

Two passes:

  1. Top-level template strings:
     Any string containing {tokens} that resolves outside projbasedir has
     its literal prefix stripped, leaving just the {token} portion.
     e.g. "/bad/prefix{projbasedir}{projdir}/output"
          → "{projbasedir}{projdir}/output"

  2. Step param values:
     Any PATH_PARAM_KEY whose resolved value is outside projbasedir is
     set to None, letting the dispatch function fall back to the config.
     Token templates are never cleared — only plain strings.
     Bare filenames (no slash, no token) on path params are also cleared.
"""

import re
import copy

TOKEN_RE   = re.compile(r"\{[^}]+\}")

# A param/config key is treated as a filesystem path by its name suffix — the
# same convention step_validator uses for its value checks. This makes the
# repair pass project-agnostic: any project's step params (VICE's d64_path /
# out_dir, qemu's hdd_img_path / hdd_qcow_path / floppy1_path / sourcecode_dir,
# etc.) are detected without a hardcoded per-project list.
PATH_KEY_RE = re.compile(r"(_path|_dir|_file|_filepath)$")

# Legacy explicit key sets kept as a fallback so projects whose path keys don't
# match PATH_KEY_RE (e.g. VICE's "src", "mountpath") keep their old behaviour.
# New projects should rely on the naming convention above instead of extending
# these.
_LEGACY_PATH_PARAM_KEYS = frozenset({
    "out_dir", "src_dir", "prg_filepath", "d64_path",
    "disk8_path", "disk9_path", "autostart_path",
    "driver1_path", "driver2_path",
})

_LEGACY_PATH_CONFIG_KEYS = frozenset({
    "out_dir", "src", "prg_filepath",
    "d64_drive8_file", "d64_drive9_file",
    "cmainfile_path", "mountpath",
})


def _is_path_param_key(key: str) -> bool:
    return bool(PATH_KEY_RE.search(key)) or key in _LEGACY_PATH_PARAM_KEYS


def _is_path_config_key(key: str) -> bool:
    return bool(PATH_KEY_RE.search(key)) or key in _LEGACY_PATH_CONFIG_KEYS

GLOBAL_KEYS = frozenset({
    "viceconf", "viceconf_filepath", "rom_path", "config_path", "hatari_config",
})

# Known legacy -> current dispatch-arg renames for step params. Mirrors
# KNOWN_STEP_PARAM_FIXES in cloneproj.html so old testlists (and clones
# of them) get repaired automatically on every clone, even for requests
# that don't carry a step_param_fixes payload. `None` means "drop the
# param" rather than rename it.
#
# Add entries here (and to the matching table in cloneproj.html) as more
# "the dispatch function's args got renamed but old testlists still use
# the old names" cases turn up.
KNOWN_STEP_PARAM_FIXES = {
    "test_compiletheprogram": {
        "d64_drive8_file": "d64_path",
        "d64_file":        None,
    },
}


def repair_config(config: dict, new_vars: dict) -> tuple[dict, list[dict]]:
    """
    Return (repaired_config, repair_log).

    config   — raw CONFIG from the source testlist file
    new_vars — final identity overrides for the clone (projdir, projbasedir,
               testname, testtype, mirror/hardcoded edits, etc.)

    repair_log entries: {scope, key, kind, old, new}
    """
    out         = copy.deepcopy(config)
    log         = []
    projbasedir = new_vars.get("projbasedir", "").rstrip("/")
    if not projbasedir:
        # Can't validate without a base — return as-is
        return out, log

    # Merge new_vars into the var map used for resolution
    vars_map = {**out, **new_vars}

    # ── Pass 1: top-level template strings ───────────────────────────────────
    for k in [kk for kk in out if _is_path_config_key(kk) and kk not in GLOBAL_KEYS]:
        v = out.get(k)
        if not v or not isinstance(v, str) or not TOKEN_RE.search(v):
            continue

        resolved = _resolve(v, vars_map)

        if TOKEN_RE.search(resolved):
            # Still has unresolved tokens — strip any literal prefix before first token
            fixed_tmpl = _strip_prefix(v)
            if fixed_tmpl != v:
                out[k] = fixed_tmpl
                vars_map[k] = fixed_tmpl
                log.append({"scope": "config", "key": k, "kind": "stripped_prefix",
                            "old": v, "new": fixed_tmpl})
            continue

        if not resolved.startswith(projbasedir):
            # Resolved outside projbasedir — strip prefix from template
            fixed_tmpl = _strip_prefix(v)
            if fixed_tmpl != v:
                out[k] = fixed_tmpl
                vars_map[k] = fixed_tmpl
                log.append({"scope": "config", "key": k, "kind": "stripped_prefix",
                            "old": v, "new": fixed_tmpl})

    # ── Pass 2: step param values ─────────────────────────────────────────────
    # Runs BEFORE the d64_path default-fill pass below, so a bad/bare-filename
    # d64_path gets cleared to None first, and the default-fill pass then has
    # an accurate "is this empty?" to act on.
    for step in out.get("steps", []):
        action = step.get("action", "")
        param  = step.get("param") or {}

        for pk in list(param):
            if not _is_path_param_key(pk) or pk in GLOBAL_KEYS:
                continue
            pv = param[pk]
            if pv is None:
                continue
            pv_str = str(pv)

            # Token template — resolve and check
            if TOKEN_RE.search(pv_str):
                resolved = _resolve(pv_str, vars_map)
                still_templated = TOKEN_RE.search(resolved)
                bare = not still_templated and resolved and "/" not in resolved
                if bare:
                    param[pk] = None
                    log.append({"scope": f"step/{action}", "key": pk,
                                "kind": "cleared_bare_filename",
                                "old": pv_str, "new": None})
                elif not still_templated and not resolved.startswith(projbasedir):
                    param[pk] = None
                    log.append({"scope": f"step/{action}", "key": pk,
                                "kind": "cleared_wrong_basepath",
                                "old": pv_str, "new": None})
                # If still has tokens or resolves correctly — leave alone
                continue

            # Plain string
            resolved = _resolve(pv_str, vars_map)

            # Bare filename — no slash
            if "/" not in pv_str:
                param[pk] = None
                log.append({"scope": f"step/{action}", "key": pk,
                            "kind": "cleared_bare_filename",
                            "old": pv_str, "new": None})
                continue

            # Resolves outside projbasedir
            if not resolved.startswith(projbasedir):
                param[pk] = None
                log.append({"scope": f"step/{action}", "key": pk,
                            "kind": "cleared_wrong_basepath",
                            "old": pv_str, "new": None})
                continue

            # Under the right base but the next path segment is not new_projdir —
            # it's pointing at a different project folder (possibly an ancestor clone)
            new_projdir = new_vars.get("projdir", "").strip("/")
            if new_projdir and _wrong_subfolder(resolved, projbasedir, new_projdir):
                param[pk] = None
                log.append({"scope": f"step/{action}", "key": pk,
                            "kind": "cleared_stale_projdir",
                            "old": pv_str, "new": None})

    # ── Pass 1.5: known legacy step-param renames ─────────────────────────────
    # Fix step params that reference dispatch-function args by an old name
    # before checking values, so the renamed param's value gets the same
    # path validation/repair as Pass 2 above (note: Pass 2 already ran for
    # the OLD key name above; the renamed param will be picked up correctly
    # next time repair_config runs, e.g. on the next clone of this output).
    for step in out.get("steps", []):
        action = step.get("action", "")
        table  = KNOWN_STEP_PARAM_FIXES.get(action)
        if not table:
            continue
        param = step.get("param") or {}

        for old_key, new_key in table.items():
            if old_key not in param:
                continue
            old_val = param.pop(old_key)
            if new_key:
                # Don't clobber an existing meaningful value for new_key
                if param.get(new_key) in (None, ""):
                    param[new_key] = old_val
                log.append({"scope": f"step/{action}", "key": old_key,
                            "kind": "renamed_param", "old": old_key, "new": new_key})
            else:
                log.append({"scope": f"step/{action}", "key": old_key,
                            "kind": "removed_param", "old": old_key, "new": None})

    # ── Pass 1.6: ensure test_compiletheprogram has an explicit d64_path ─────
    # test_compiletheprogram falls back to <out_dir>/<cmainfile>.d64 when
    # d64_path is missing/None (see dispatch_functions.test_compiletheprogram).
    # Since cmainfile includes its source extension (e.g. "deleteme33.c"),
    # that default filename ("deleteme33.c.d64") never matches d64_disk8_name
    # ("deleteme33.d64") that test_emulator_start/d64_drive8_file expects.
    # So d64_path must always mirror d64_drive8_file unless something more
    # specific was already configured. Runs AFTER Pass 2 above, so a bad
    # bare-filename/wrong-basepath d64_path has already been cleared to None
    # and is correctly seen as "empty" here.
    _EMPTY_TOKENS = {"", "none", "null"}
    if "d64_drive8_file" in out:
        for step in out.get("steps", []):
            if step.get("action") != "test_compiletheprogram":
                continue
            param   = step.setdefault("param", {})
            current = param.get("d64_path")
            is_empty = current is None or str(current).strip().lower() in _EMPTY_TOKENS
            if is_empty:
                log.append({"scope": "step/test_compiletheprogram", "key": "d64_path",
                            "kind": "set_default_d64_path",
                            "old": current, "new": "{d64_drive8_file}"})
                param["d64_path"] = "{d64_drive8_file}"

    # ── Pass 1.7: auto-inject driver params from src/ disk scan ──────────────
    # When a test_compiletheprogram step has no driver1_path/driver1_label
    # (either because the testlist predates those params, or was cloned before
    # they were added), scan the resolved src_dir on disk for cc65 driver
    # binary files (.emd, .mou, .ser, .joy, .tgi). For each found, inject
    # driver1_path / driver2_path (and corresponding labels) into:
    #   a) the step param dict  — as {src}filename.ext token references
    #   b) the top-level config — as driver1_path / driver1_label keys
    # so the values resolve correctly at dispatch time and are visible in
    # the test editor. Only fires when driver slots are genuinely absent/None.
    import os as _os

    DRIVER_EXTS = (".emd", ".mou", ".ser", ".joy", ".tgi")
    DRIVER_SLOTS = [
        ("driver1_path", "driver1_label"),
        ("driver2_path", "driver2_label"),
    ]

    # Resolve src once for this config
    _raw_src = out.get("src", "")
    _res_src = _resolve(_raw_src, vars_map).rstrip("/") if _raw_src else ""

    for step in out.get("steps", []):
        if step.get("action") != "test_compiletheprogram":
            continue
        param = step.setdefault("param", {})

        # Collect which driver slots are genuinely empty
        empty_slots = []
        for path_key, label_key in DRIVER_SLOTS:
            path_val  = param.get(path_key)
            label_val = param.get(label_key)
            path_empty  = path_val  is None or str(path_val).strip().lower()  in _EMPTY_TOKENS
            label_empty = label_val is None or str(label_val).strip().lower() in _EMPTY_TOKENS
            if path_empty and label_empty:
                empty_slots.append((path_key, label_key))

        if not empty_slots:
            continue  # all slots already configured

        if not _res_src or not _os.path.isdir(_res_src):
            continue  # can't scan without a real src dir

        # Find driver files on disk, sorted for deterministic slot assignment
        found = sorted(
            f for f in _os.listdir(_res_src)
            if _os.path.splitext(f)[1].lower() in DRIVER_EXTS
        )
        if not found:
            continue

        for (path_key, label_key), fname in zip(empty_slots, found):
            stem       = _os.path.splitext(fname)[0]      # e.g. "c64-reu"
            path_token = "{src}" + fname                   # e.g. "{src}c64-reu.emd"

            # Inject into step param
            param[path_key]  = path_token
            param[label_key] = stem
            log.append({"scope": "step/test_compiletheprogram", "key": path_key,
                        "kind": "injected_driver_param",
                        "old": None, "new": path_token})
            log.append({"scope": "step/test_compiletheprogram", "key": label_key,
                        "kind": "injected_driver_param",
                        "old": None, "new": stem})

            # Also inject into top-level config if absent, so the editor
            # shows the key and it resolves correctly via vars_map
            if path_key not in out or out[path_key] in (None, ""):
                out[path_key]      = path_token
                vars_map[path_key] = path_token
                log.append({"scope": "config", "key": path_key,
                            "kind": "injected_driver_config",
                            "old": None, "new": path_token})
            if label_key not in out or out[label_key] in (None, ""):
                out[label_key]      = stem
                vars_map[label_key] = stem
                log.append({"scope": "config", "key": label_key,
                            "kind": "injected_driver_config",
                            "old": None, "new": stem})

    return out, log


def apply_step_param_fixes(config: dict, fixes: list[dict] | None) -> tuple[dict, list[dict]]:
    """
    Apply step param fixes collected from the clone UI's Step 4 — both
    fixes the user picked manually (for `unknown_param`/value/action
    issues not covered by KNOWN_STEP_PARAM_FIXES) and the tool's own
    auto-applied known-fix entries.

    Run this BEFORE repair_config(), so repair_config's value-checks see
    the corrected param names.

    fixes — list of dicts, one of:
        {"step_index": i, "type": "rename_param", "old_key": ..., "new_key": ...}
        {"step_index": i, "type": "remove_param", "old_key": ...}
        {"step_index": i, "type": "set_value",    "key": ...,     "value": ...}
        {"step_index": i, "type": "set_action",   "value": ...}

    Returns (config, log) — log entries match repair_config()'s format.
    """
    out   = copy.deepcopy(config)
    log   = []
    steps = out.get("steps", [])

    for fix in (fixes or []):
        idx = fix.get("step_index")
        if not isinstance(idx, int) or idx < 0 or idx >= len(steps):
            continue
        step   = steps[idx]
        action = step.get("action", "")
        ftype  = fix.get("type")

        if ftype == "rename_param":
            param   = step.setdefault("param", {})
            old_key = fix.get("old_key")
            new_key = fix.get("new_key")
            if old_key and new_key and old_key in param:
                param[new_key] = param.pop(old_key)
                log.append({"scope": f"step/{action}", "key": old_key,
                            "kind": "ui_renamed_param", "old": old_key, "new": new_key})

        elif ftype == "remove_param":
            param   = step.setdefault("param", {})
            old_key = fix.get("old_key")
            if old_key and old_key in param:
                param.pop(old_key)
                log.append({"scope": f"step/{action}", "key": old_key,
                            "kind": "ui_removed_param", "old": old_key, "new": None})

        elif ftype == "set_value":
            param = step.setdefault("param", {})
            key   = fix.get("key")
            val   = fix.get("value")
            if key:
                old_val = param.get(key)
                param[key] = val
                log.append({"scope": f"step/{action}", "key": key,
                            "kind": "ui_set_value", "old": old_val, "new": val})

        elif ftype == "set_action":
            new_action = fix.get("value")
            if new_action:
                log.append({"scope": f"step/{idx}", "key": "action",
                            "kind": "ui_set_action", "old": action, "new": new_action})
                step["action"] = new_action

    return out, log


# ── Internals ─────────────────────────────────────────────────────────────────

def _strip_prefix(tmpl: str) -> str:
    """Remove any literal characters before the first {token}."""
    m = TOKEN_RE.search(tmpl)
    if not m or m.start() == 0:
        return tmpl
    return tmpl[m.start():]


def _wrong_subfolder(resolved: str, projbasedir: str, expected_projdir: str) -> bool:
    """
    True if resolved is under projbasedir but the subfolder immediately
    after it is not expected_projdir.
    e.g. projbasedir=/testsrc/sourcedir/c64src, expected=c64_deleteme2
         /testsrc/sourcedir/c64src/mcmfractal/output  → True  (wrong subfolder)
         /testsrc/sourcedir/c64src/c64_deleteme2/output → False (correct)
    """
    base = projbasedir.rstrip("/") + "/"
    if not resolved.startswith(base):
        return False
    rest     = resolved[len(base):]      # e.g. "mcmfractal/output"
    next_seg = rest.split("/")[0]        # "mcmfractal"
    return bool(next_seg) and next_seg != expected_projdir.strip("/")


def _resolve(tmpl: str, vars_map: dict, passes: int = 6) -> str:
    s = tmpl
    for _ in range(passes):
        prev = s
        s = re.sub(
            r"\{([^}]+)\}",
            lambda m: str(vars_map[m.group(1)]) if m.group(1) in vars_map else m.group(0),
            s,
        )
        if s == prev:
            break
    return s