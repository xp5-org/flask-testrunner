"""
step_validator.py
-----------------
Validates CONFIG["steps"] against the real dispatch function signatures.

Uses dispatchhelper.load_step_dispatch (already used by the testbuilder) so
there is no duplicate import logic and no stub injection needed.

Two passes per step:

  Pass 1 — Signature check:
    Every param key is checked against the function's inspect.signature().
    Catches renamed/mistyped argument names (disk_path vs disk8_path etc).

  Pass 2 — Value check:
    Every param value is checked for:
      hardcoded_srcpath  — absolute path embedding the source projdir
      unresolved_token   — {token} remains after resolution
      doubled_segment    — /seg/seg repeated component in resolved path
      bare_filename      — _path/_dir/_file param resolved to a bare filename
"""

import inspect
import re
import difflib


ABS_RE     = re.compile(r"^/[a-zA-Z]")
TOKEN_RE   = re.compile(r"\{[^}]+\}")
DOUBLED_RE = re.compile(r"(/[^/]+)\1")
PATH_PARAM = re.compile(r"(_path|_dir|_file)$")

# Keys that dispatch functions receive implicitly — not declared in signatures
ALWAYS_VALID = frozenset({"context", "config"})
META_KEYS    = frozenset({"action", "subaction"})


# ── Public API ────────────────────────────────────────────────────────────────

def validate_config_steps(config: dict, dispatch_dir: str,
                           resolved_vars: dict | None = None) -> list[dict]:
    """
    Validate every step in config["steps"].

    config        — raw CONFIG dict from the testlist file
    dispatch_dir  — directory containing dispatch_functions.py
    resolved_vars — var map for value resolution (clone identity overrides).
                    Falls back to config itself if None.

    Returns list of result dicts — one per step:
    {
        step_index, action, found, valid_params,
        issues: [{key, kind, value, resolved, suggestion, detail}]
    }

    Issue kinds:
        load_error         — dispatchhelper could not load dispatch_functions.py
        unknown_action     — action name not in dispatch
        unknown_param      — param key not in function signature
        hardcoded_srcpath  — absolute path still containing source projdir
        unresolved_token   — token remains after resolution
        doubled_segment    — doubled path component in resolved value
        bare_filename      — _path/_dir/_file resolved to a bare filename
    """
    vars_map = resolved_vars if resolved_vars is not None else dict(config)

    try:
        dispatch = _load(dispatch_dir)
    except Exception as e:
        return [_err_result(str(e))]

    results     = []
    src_projdir = config.get("projdir", "")

    for idx, step in enumerate(config.get("steps", [])):
        action = step.get("action", "")
        param  = step.get("param") or {}

        result = {
            "step_index":   idx,
            "action":       action,
            "found":        action in dispatch,
            "valid_params": [],
            "issues":       [],
        }

        if action not in dispatch:
            result["issues"].append({
                "key": action, "kind": "unknown_action",
                "value": "", "resolved": "",
                "suggestion": _suggest(action, dispatch.keys()),
                "detail": f"No dispatch function named '{action}'",
            })
            results.append(result)
            continue

        valid = _sig_params(dispatch[action])
        result["valid_params"] = sorted(valid)

        # Pass 1: signature
        for key in param:
            if key in META_KEYS or key in ALWAYS_VALID:
                continue
            if key not in valid:
                result["issues"].append({
                    "key": key, "kind": "unknown_param",
                    "value": str(param[key] or ""), "resolved": "",
                    "suggestion": _suggest(key, valid),
                    "detail": f"'{key}' is not a parameter of {action}()",
                })

        # Pass 2: values
        for key, raw_val in param.items():
            if key in META_KEYS or key in ALWAYS_VALID or raw_val is None:
                continue
            raw_str  = str(raw_val)
            resolved = _resolve(raw_str, vars_map)

            if ABS_RE.match(raw_str) and not TOKEN_RE.search(raw_str):
                if src_projdir and src_projdir in raw_str:
                    result["issues"].append({
                        "key": key, "kind": "hardcoded_srcpath",
                        "value": raw_str, "resolved": resolved,
                        "suggestion": None,
                        "detail": f"Absolute path still contains source folder '{src_projdir}'",
                    })
                    continue

            if TOKEN_RE.search(resolved):
                result["issues"].append({
                    "key": key, "kind": "unresolved_token",
                    "value": raw_str, "resolved": resolved,
                    "suggestion": None,
                    "detail": f"Token(s) remain after resolution: {resolved}",
                })
                continue

            if DOUBLED_RE.search(resolved):
                result["issues"].append({
                    "key": key, "kind": "doubled_segment",
                    "value": raw_str, "resolved": resolved,
                    "suggestion": None,
                    "detail": f"Repeated path component: {resolved}",
                })
                continue

            if (PATH_PARAM.search(key) and resolved
                    and "/" not in resolved
                    and resolved.lower() not in ("none", "null", "")):
                result["issues"].append({
                    "key": key, "kind": "bare_filename",
                    "value": raw_str, "resolved": resolved,
                    "suggestion": _suggest_full_path(resolved, vars_map),
                    "detail": f"Param '{key}' is a bare filename with no path",
                })

        results.append(result)

    return results


def has_blocking_issues(results: list[dict]) -> bool:
    """True if any issue should block the clone proceeding."""
    # bare_filename is auto-repaired by repair_config — not a blocker
    BLOCKING = {"load_error", "unknown_action", "unknown_param",
                "hardcoded_srcpath", "unresolved_token", "doubled_segment"}
    return any(
        iss["kind"] in BLOCKING
        for r in results
        for iss in r["issues"]
    )


def format_results_text(results: list[dict]) -> str:
    lines = []
    for r in results:
        if not r["issues"]:
            lines.append(f"  step {r['step_index']} {r['action']}: OK")
        else:
            for iss in r["issues"]:
                sug = f" → '{iss['suggestion']}'" if iss.get("suggestion") else ""
                lines.append(
                    f"  step {r['step_index']} {r['action']}: "
                    f"[{iss['kind']}] {iss['key']}{sug} — {iss['detail']}"
                )
    return "\n".join(lines)


# ── Internals ─────────────────────────────────────────────────────────────────

def _load(dispatch_dir: str) -> dict:
    """Load dispatch functions via dispatchhelper (shared with testbuilder)."""
    import dispatchhelper
    raw = dispatchhelper.load_step_dispatch(dispatch_dir)
    # Filter to decorated step functions only
    return {
        name: func
        for name, func in raw.items()
        if getattr(func, "_is_teststep", False)
    }


def _sig_params(func) -> set:
    sig = inspect.signature(func)
    return {
        name for name, p in sig.parameters.items()
        if p.kind not in (p.VAR_KEYWORD, p.VAR_POSITIONAL)
    }


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


def _suggest(bad: str, candidates) -> str | None:
    m = difflib.get_close_matches(bad, candidates, n=1, cutoff=0.55)
    return m[0] if m else None


def _suggest_full_path(bare_filename: str, vars_map: dict) -> str | None:
    """
    For a *_path/*_dir/*_file param that resolved to a bare filename (no
    slash), look for a sibling config/var key whose own resolved value is
    a full path ending in that exact filename — e.g. d64_path resolving to
    "main.d64" should suggest "{d64_drive8_file}" if d64_drive8_file
    resolves to ".../output/main.d64". That sibling key is what the rest
    of the config already uses to build the same absolute path, so
    referencing it as a {token} keeps the value in sync with everything
    else instead of hardcoding a fresh absolute path.

    Returns the candidate as a "{key}" template string, or None if no
    sibling key's resolved value ends with the bare filename.
    """
    candidates = []
    for k, v in vars_map.items():
        if not isinstance(v, str) or not v:
            continue
        resolved_v = _resolve(v, vars_map)
        if TOKEN_RE.search(resolved_v):
            continue  # didn't fully resolve — not a usable candidate
        if "/" not in resolved_v:
            continue  # itself bare — not a "full path" candidate
        if resolved_v.endswith("/" + bare_filename) or resolved_v == bare_filename:
            candidates.append(k)

    if not candidates:
        return None

    # Prefer the shortest resolved path (closest/most specific match) when
    # multiple keys happen to end with the same filename.
    candidates.sort(key=lambda k: len(_resolve(vars_map[k], vars_map)))
    return "{" + candidates[0] + "}"


def _err_result(detail: str) -> dict:
    return {
        "step_index": -1, "action": "", "found": False, "valid_params": [],
        "issues": [{"key": "", "kind": "load_error", "value": "", "resolved": "",
                    "suggestion": None, "detail": detail}],
    }