"""Container-level test plans: step-group templates and step matrices.

A testlist's CONFIG["steps"] is a flat list, and the whole framework is built
on that: run_tests numbers them 1..N, and a report's identity for a step is
"{i}_{action}: {title}". Nothing here changes that. What it adds is a way to
write a step list *without* repeating it -- a testlist may contain, alongside
ordinary literal steps, two kinds of reference entry:

    {"use": "boot_to_dos", "with": {...}, "override": {...}}
    {"matrix": "stereo_ring", "cells": [...]}

Both resolve to ordinary steps here, before anything else sees them, so the
runner, the editor, the validator and the report all work on one expanded
list and cannot disagree about what a test does.

WHERE THE DEFINITIONS LIVE
    Beside the container's __testparent__.py, in a __testplan__.py holding a
    PLAN dict. The stub stays a small identity card; templates of twenty-odd
    steps and matrix axes get a file that is allowed to grow.

WHY CONFIG STAYS A LITERAL DICT
    A testlist could compute its steps in Python -- the runner execs the
    module and would run whatever it produced. The editor could not SAVE it:
    newprojecthelper.update_config_in_file rewrites CONFIG by AST span and
    needs a literal to write into. Keeping the references as literal dicts
    means a testlist built out of templates is still fully editable in the
    testbuilder, which is the whole point of putting them there.

TWO SUBSTITUTION PASSES, ONE SYNTAX
    Plan knobs and matrix axis values are substituted HERE, at expansion
    time. CONFIG {tokens} are substituted LATER, per step, by
    dispatch_functions.build_vars/resolve_path at call time. Both spell it
    "{name}". That is deliberate -- a template reads like the testlist it
    replaces -- and it is safe because this pass only ever substitutes names
    it was explicitly given (a template's declared params, a matrix's axes)
    and leaves every other {token} untouched for the runtime resolver. A knob
    that shadows a CONFIG key would make which pass wins ambiguous, so that
    is an error rather than a silent precedence rule.
"""

import copy
import importlib.util
import os
import re
import sys

PLAN_FILE = "__testplan__.py"

# Reserved keys on an expanded step. Read by the editor and the API; the
# runner ignores unknown step keys, so carrying provenance costs nothing.
ORIGIN_KEY = "_origin"

# Override value meaning "this step must not carry this param at all".
# Needed because a param's absence is not the same as its being empty: the
# echo-box matrix's first cell passes delay="0" to test_sendcommand and the
# rest pass no delay, which is a different call, not a different value.
REMOVE = "__remove__"

# Step-level keys an override may set, as opposed to entries in "param".
# title/description/pause_on are always allowed -- every cell of a matrix
# needs its own title, and a template that forbade that would be useless.
# action/subaction change what the step DOES, so they obey `overridable`
# like any param: one cell of a sweep may legitimately start differently,
# but only if the template says that seat is adjustable.
FREE_STEP_KEYS = ("title", "description", "pause_on")
GATED_STEP_KEYS = ("action", "subaction")

_plan_cache = {}


class PlanError(Exception):
    """A __testplan__.py is missing, malformed, or used incorrectly."""


# ── loading ──────────────────────────────────────────────────────────────────

def plan_path(dirpath):
    return os.path.join(dirpath, PLAN_FILE)


def load_plan(dirpath):
    """Read a directory's __testplan__.py and return its PLAN dict.

    Returns None when the directory has no plan file at all -- which is the
    normal case for every container that has not adopted templates, and is
    why every caller treats a missing plan as "nothing to expand" rather than
    an error.
    """
    if not dirpath:
        return None
    path = plan_path(dirpath)
    if path in _plan_cache:
        return _plan_cache[path]

    plan = None
    if os.path.exists(path):
        modname = "_testplan_%s" % abs(hash(path))
        try:
            spec = importlib.util.spec_from_file_location(modname, path)
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            plan = getattr(mod, "PLAN", None)
        except Exception as e:
            raise PlanError(f"{path} failed to load: {e}")
        finally:
            sys.modules.pop(modname, None)

        if not isinstance(plan, dict):
            raise PlanError(f"{path} defines no PLAN dict")
        _validate_plan(plan, path)

    _plan_cache[path] = plan
    return plan


def clear_plan_cache():
    """Drop cached plans so an edited __testplan__.py is re-read.

    Called from apphelpers.clear_registries() alongside the parent-stub cache,
    for the same reason: a reload must see what is on disk now.
    """
    _plan_cache.clear()


def _validate_plan(plan, path):
    for section in ("templates", "matrices", "machines"):
        block = plan.get(section)
        if block is not None and not isinstance(block, dict):
            raise PlanError(f"{path}: PLAN[{section!r}] must be a dict")

    for tid, tpl in (plan.get("templates") or {}).items():
        if not isinstance(tpl, dict):
            raise PlanError(f"{path}: template {tid!r} must be a dict")
        if not isinstance(tpl.get("steps"), list) or not tpl["steps"]:
            raise PlanError(f"{path}: template {tid!r} has no steps list")
        seen = set()
        for i, step in enumerate(tpl["steps"], 1):
            if not isinstance(step, dict) or not step.get("action"):
                raise PlanError(f"{path}: template {tid!r} step {i} has no action")
            sid = step.get("id")
            if sid:
                if sid in seen:
                    raise PlanError(
                        f"{path}: template {tid!r} reuses step id {sid!r}; ids "
                        f"are how a testlist addresses a step to override it")
                seen.add(sid)

    for mid, mtx in (plan.get("matrices") or {}).items():
        if not isinstance(mtx, dict):
            raise PlanError(f"{path}: matrix {mid!r} must be a dict")
        if not isinstance(mtx.get("steps"), list) or not mtx["steps"]:
            raise PlanError(f"{path}: matrix {mid!r} has no cell step template")
        # A plan matrix may legitimately define only the SHAPE -- the cell
        # step template and which of its params are overridable -- and leave
        # the cells to each use site. That is the normal case when two
        # testlists sweep the same axis with slightly different values, which
        # is exactly why the cells are not forced to live here. A use site
        # that supplies none either is caught at expansion, where the error
        # can name the testlist.


# ── reference detection ──────────────────────────────────────────────────────

def entry_kind(entry):
    """What a CONFIG["steps"] entry is: 'step', 'template', 'matrix'."""
    if not isinstance(entry, dict):
        return "step"
    if entry.get("use"):
        return "template"
    if entry.get("matrix") or (entry.get("axes") and entry.get("steps")):
        return "matrix"
    return "step"


def has_references(config):
    """True if this testlist's steps need expanding at all."""
    return any(entry_kind(e) != "step" for e in (config or {}).get("steps") or [])


# ── substitution ─────────────────────────────────────────────────────────────

_TOKEN_RE = re.compile(r"\{([A-Za-z_][A-Za-z0-9_]*)\}")


def _subst(value, vars_map):
    """Substitute only the names in vars_map, recursively, leaving the rest.

    An unknown {token} survives untouched so dispatch_functions.resolve_path
    can expand it against CONFIG at call time -- expansion must not consume
    runtime tokens.
    """
    if isinstance(value, str):
        return _TOKEN_RE.sub(
            lambda m: str(vars_map[m.group(1)]) if m.group(1) in vars_map else m.group(0),
            value)
    if isinstance(value, list):
        return [_subst(v, vars_map) for v in value]
    if isinstance(value, dict):
        return {k: _subst(v, vars_map) for k, v in value.items()}
    return value


def _check_shadowing(vars_map, config, what):
    clashes = sorted(set(vars_map) & set(k for k in (config or {}) if k != "steps"))
    if clashes:
        raise PlanError(
            f"{what} defines {', '.join(clashes)}, which CONFIG also defines. "
            f"Expansion and the runtime token resolver would both claim "
            f"{{{clashes[0]}}} and which one wins would depend on ordering. "
            f"Rename the knob.")


# ── expansion ────────────────────────────────────────────────────────────────

def expand_steps(config, plan=None, strict=True):
    """Resolve every reference entry in config["steps"] into real steps.

    Returns (steps, notes). `steps` is always a plain list of step dicts ready
    for the runner. `notes` collects human-readable problems; with strict=True
    a bad reference raises PlanError instead, which is what the run path
    wants -- running two thirds of a test because a template id was misspelt
    is worse than not starting.
    """
    steps_in = (config or {}).get("steps") or []
    out, notes = [], []

    for pos, entry in enumerate(steps_in, 1):
        kind = entry_kind(entry)
        try:
            if kind == "template":
                produced = _expand_template(entry, config, plan)
            elif kind == "matrix":
                produced = _expand_matrix(entry, config, plan)
            else:
                produced = [copy.deepcopy(entry)]
        except PlanError as e:
            if strict:
                raise PlanError(f"steps[{pos}]: {e}")
            notes.append(f"steps[{pos}]: {e}")
            # Keep the reference visible as a failing step rather than
            # dropping it: a silently shorter test looks like it passed.
            produced = [{
                "action": "__unresolved_reference__",
                "param": {},
                "subaction": "",
                "title": f"unresolved: {e}",
                ORIGIN_KEY: {"kind": kind, "error": str(e)},
            }]

        # Which on-disk entry produced this step. Anything that wants to write
        # back to a step it found in the EXPANDED list -- "pause at step 220"
        # from the matrix viewer, say -- has to get from step 220 to the entry
        # that made it. Recovering that by walking the expansion and matching
        # template/matrix names is guesswork as soon as one testlist uses the
        # same template twice, so it is recorded here instead, at the one place
        # that actually knows.
        for step in produced:
            step.setdefault(ORIGIN_KEY, {"kind": "step"})["entry"] = pos
        out.extend(produced)

    return out, notes


def _require_plan(plan, what):
    if not plan:
        raise PlanError(
            f"{what}, but this container has no {PLAN_FILE} beside its "
            f"__testparent__.py")
    return plan


def _machine_params(entry_with, plan, what):
    """Start-step params contributed by a named machine, if one is named."""
    machine = (entry_with or {}).get("machine")
    if not machine:
        return {}
    machines = (plan or {}).get("machines") or {}
    if machine not in machines:
        known = ", ".join(sorted(machines)) or "none defined"
        raise PlanError(f"{what} names machine {machine!r}; known: {known}")
    return dict((machines[machine] or {}).get("params") or {})


def _expand_template(entry, config, plan):
    tid = entry["use"]
    what = f"template {tid!r}"
    _require_plan(plan, f"{what} is used")
    templates = plan.get("templates") or {}
    if tid not in templates:
        known = ", ".join(sorted(templates)) or "none defined"
        raise PlanError(f"unknown template {tid!r}; known: {known}")
    tpl = templates[tid]

    # Knob values: template defaults, then the machine's, then this use's.
    vars_map = dict(tpl.get("params") or {})
    vars_map.update(_machine_params(entry.get("with"), plan, what))
    vars_map.update(entry.get("with") or {})
    vars_map.pop("machine", None)
    missing = sorted(k for k, v in vars_map.items() if v is None)
    if missing:
        raise PlanError(
            f"{what} needs {', '.join(missing)} — pass them in \"with\"")
    _check_shadowing(vars_map, config, what)

    overrides = entry.get("override") or {}
    _check_override_targets(overrides, tpl["steps"], what)
    skip = entry.get("skip") or []
    _check_override_targets({k: None for k in skip}, tpl["steps"], f"{what} skip")

    label = entry.get("as") or tid
    out = []
    for idx, tstep in enumerate(tpl["steps"], 1):
        if _skipped(skip, tstep.get("id"), idx):
            continue
        step = copy.deepcopy(tstep)
        sid = step.pop("id", None)
        overridable = step.pop("overridable", None)
        step = _subst(step, vars_map)

        over = _step_override(overrides, sid, idx)
        if over:
            _apply_override(step, over, overridable, f"{what} step {sid or idx}")

        for key in ("group", "subgroup", "phase"):
            if entry.get(key) and not step.get(key):
                step[key] = entry[key]

        step[ORIGIN_KEY] = {
            "kind": "template",
            "source": tid,
            "instance": label,
            "step_id": sid,
            "index": idx,
            "overridable": overridable,
            "overridden": sorted(over) if over else [],
        }
        out.append(step)
    return out


def _check_override_targets(overrides, tsteps, what):
    ids = {s.get("id") for s in tsteps if s.get("id")}
    for key in overrides:
        if isinstance(key, int):
            if not 1 <= key <= len(tsteps):
                raise PlanError(
                    f"{what} override targets step {key}, which has only "
                    f"{len(tsteps)} steps")
        elif key not in ids:
            known = ", ".join(sorted(ids)) or "none -- the template's steps have no ids"
            raise PlanError(
                f"{what} override targets step id {key!r}; known ids: {known}")


def _skipped(skip, sid, idx):
    """Whether a template/cell step is dropped at this use site.

    Real matrices are not perfectly rectangular: the echo-box rate sweep ends
    each cell with a "settle before next probe" wait that the LAST cell has no
    use for. Dropping one step is far better than forking the cell template in
    two over it.
    """
    if not skip:
        return False
    return (sid is not None and sid in skip) or idx in skip


def _step_override(overrides, sid, idx):
    """An override keyed by step id wins over one keyed by position."""
    if sid and sid in overrides:
        return overrides[sid]
    return overrides.get(idx)


def _merge_cell_overrides(base, extra):
    """A use site's cell_override layered onto the plan cell's own override.

    Merged one level down, per target step, so a use site pinning pause_on on
    one step does not wipe out the title or seconds the plan already set on
    another -- or on the same one.
    """
    if not extra:
        return base
    merged = {k: (dict(v) if isinstance(v, dict) else v) for k, v in base.items()}
    for key, over in extra.items():
        if isinstance(merged.get(key), dict) and isinstance(over, dict):
            merged[key].update(over)
        else:
            merged[key] = dict(over) if isinstance(over, dict) else over
    return merged


def _apply_override(step, over, overridable, what):
    """Merge one override onto an expanded step.

    A template step may declare `overridable` -- the params a testlist is
    allowed to change. That list is the template's contract with its callers
    and what the editor renders as editable; overriding anything else is
    refused rather than silently applied, because a template whose every
    param can be rewritten is not a template, it is a copy.
    """
    for key, value in (over or {}).items():
        if key in FREE_STEP_KEYS:
            step[key] = value
            continue
        if key in GATED_STEP_KEYS:
            _check_allowed([key], overridable, what)
            step[key] = value
            continue
        if key == "param":
            _merge_params(step, value, overridable, what)
            continue
        # Bare keys are a shorthand for param entries -- the common case.
        _merge_params(step, {key: value}, overridable, what)


def _check_allowed(keys, overridable, what):
    if overridable is None:
        return
    blocked = sorted(k for k in keys if k not in overridable)
    if blocked:
        raise PlanError(
            f"{what}: {', '.join(blocked)} is not overridable "
            f"(this step allows: {', '.join(overridable) or 'nothing'})")


def _merge_params(step, params, overridable, what):
    _check_allowed(params, overridable, what)
    step.setdefault("param", {})
    for key, value in params.items():
        if value == REMOVE:
            step["param"].pop(key, None)
        else:
            step["param"][key] = copy.deepcopy(value)


def matrix_cells(block, plan=None):
    """The ordered list of cells a matrix block expands to.

    A cell is a dict of axis values plus whatever per-cell overrides were
    written for it. Cells come either from the cartesian product of `axes` or
    from an explicit `cells` list -- explicit wins, because the moment a
    matrix needs one odd cell the product stops being the source of truth.
    This is also what the editor renders as its spreadsheet: one row per cell,
    one column per axis and per overridable param.
    """
    spec = _matrix_spec(block, plan)
    explicit = spec.get("cells")
    if explicit:
        return [dict(c) for c in explicit]

    axes = spec.get("axes") or {}
    cells = [{}]
    for name, values in axes.items():
        cells = [dict(c, **{name: v}) for c in cells for v in values]
    return cells


def _matrix_spec(block, plan):
    """The matrix definition: named in the plan, or inline in the testlist."""
    mid = block.get("matrix")
    if mid and not block.get("steps"):
        _require_plan(plan, f"matrix {mid!r} is used")
        matrices = (plan or {}).get("matrices") or {}
        if mid not in matrices:
            known = ", ".join(sorted(matrices)) or "none defined"
            raise PlanError(f"unknown matrix {mid!r}; known: {known}")
        spec = dict(matrices[mid])
        # A use site may add or replace cells without touching the plan.
        if block.get("cells"):
            spec["cells"] = block["cells"]
        if block.get("axes"):
            spec["axes"] = block["axes"]
        return spec
    return dict(block)


def _expand_matrix(entry, config, plan):
    mid = entry.get("matrix") or "inline"
    what = f"matrix {mid!r}"
    spec = _matrix_spec(entry, plan)
    cell_steps = spec.get("steps")
    if not cell_steps:
        raise PlanError(f"{what} has no cell step template")

    cells = matrix_cells(entry, plan)
    if not cells:
        raise PlanError(f"{what} expands to no cells")

    # Per-cell overrides written at the USE SITE, keyed by cell id:
    #
    #     {"matrix": "ring_lr_stereo", "cell_override": {
    #         "stereo_16_22050": {"recwav": {"pause_on": True}}}}
    #
    # The already-existing way to change one cell for one testlist is to give
    # the use site its own "cells" -- but that REPLACES the plan's list rather
    # than merging with it (_matrix_spec), so pausing a single step would
    # silently copy all 15 cells into the testlist and detach it from the
    # plan: a cell added to the matrix later would never reach that test, and
    # nothing on screen would say so. A pause is a debugging act on ONE step
    # of ONE test and must not cost that, hence this narrow merge.
    use_over = entry.get("cell_override") or {}
    # Same narrow-merge reasoning, for dropping a step: a live edit deleting
    # one step of one cell must not copy the plan's cells into the testlist.
    use_skip = entry.get("cell_skip") or {}
    cell_ids = [c.get("id") or f"cell{i}" for i, c in enumerate(cells, 1)]
    for block in ("cell_override", "cell_skip"):
        unknown = sorted(set(entry.get(block) or {}) - set(cell_ids))
        if unknown:
            raise PlanError(
                f"{what}: {block} names unknown cell(s) "
                f"{', '.join(unknown)}; known: {', '.join(cell_ids)}")

    out = []
    for cell_no, cell in enumerate(cells, 1):
        # Everything on a cell except the two machinery keys is a value the
        # cell template may reference. That includes "id" and "label": a cell's
        # human name is the most useful token it has -- it is what the report
        # title, the chart caption and the guest's own VERDICT line all need --
        # and excluding it just forced every cell to repeat the same string as
        # an override instead.
        cell_vars = {k: v for k, v in cell.items()
                     if k not in ("override", "skip")}
        # Position tokens. A cell often needs to know which number it is --
        # the echo-box matrix counts "PLAYSTEREO call N" across the whole run
        # -- and deriving that from the axis values is impossible.
        cell_vars.setdefault("cell_no", cell_no)
        cell_vars.setdefault("cell_index", cell_no - 1)
        _check_shadowing(cell_vars, config, f"{what} cell {cell_no}")

        cell_id = cell_ids[cell_no - 1]
        overrides = _merge_cell_overrides(cell.get("override") or {},
                                          use_over.get(cell_id) or {})
        _check_override_targets(overrides, cell_steps, f"{what} cell {cell_no}")
        skip = list(cell.get("skip") or []) + list(use_skip.get(cell_id) or [])
        _check_override_targets({k: None for k in skip}, cell_steps,
                                f"{what} cell {cell_no} skip")

        for idx, cstep in enumerate(cell_steps, 1):
            if _skipped(skip, cstep.get("id"), idx):
                continue
            step = copy.deepcopy(cstep)
            sid = step.pop("id", None)
            overridable = step.pop("overridable", None)
            step = _subst(step, cell_vars)

            over = _step_override(overrides, sid, idx)
            if over:
                _apply_override(step, over, overridable,
                                f"{what} cell {cell_no} step {sid or idx}")

            if entry.get("group") and not step.get("group"):
                step["group"] = entry["group"]
            if not step.get("subgroup"):
                step["subgroup"] = cell_id
            if entry.get("phase") and not step.get("phase"):
                step["phase"] = entry["phase"]

            step[ORIGIN_KEY] = {
                "kind": "matrix",
                "source": mid,
                "cell": cell_id,
                "cell_no": cell_no,
                "cell_values": cell_vars,
                "step_id": sid,
                "index": idx,
                "overridable": overridable,
                "overridden": sorted(over) if over else [],
            }
            out.append(step)
    return out


# ── writing back to the entry that produced an expanded step ─────────────────

def step_target(entry, origin):
    """The dict step-level keys (pause_on) for this expanded step live in, on disk.

    Given the on-disk entry that produced the step and the step's own
    provenance. The three cases are the three shapes an entry can have, and
    they differ in what "this one step" even means: a plain step IS the step;
    a template use expands each template step exactly once, so the step id
    addresses it; a matrix expands each cell step once PER CELL, so it takes
    the cell id as well.
    """
    kind = origin.get("kind")
    if kind in (None, "step"):
        return entry
    # Prefer the step's id over its position: an override keyed by id survives
    # a step being inserted above it in the template, one keyed by position
    # silently slides onto the wrong step (_step_override).
    key = origin.get("step_id") or origin.get("index")
    if kind == "template":
        return entry.setdefault("override", {}).setdefault(key, {})
    if kind == "matrix":
        cell_id = origin.get("cell")
        local = entry.get("cells")
        if local:
            # This use site already owns its cells outright -- write into the
            # one it owns rather than layering a cell_override on top of a
            # list it is no longer sharing with anyone.
            return _own_cell(local, cell_id).setdefault("override", {}).setdefault(key, {})
        return (entry.setdefault("cell_override", {})
                     .setdefault(cell_id, {})
                     .setdefault(key, {}))
    raise KeyError(f"cannot address a step of kind {kind!r}")


def skip_step(entry, origin):
    """Drop one expanded step at its use site without detaching the entry.

    A template use gets the step added to its "skip"; a matrix use gets it
    added to that cell's "cell_skip" (or the cell's own "skip" when the use
    site owns its cells). A plain step has no use site -- it is deleted by
    removing the entry itself, so that raises KeyError.
    """
    kind = origin.get("kind")
    key = origin.get("step_id") or origin.get("index")
    if kind == "template":
        skip = entry.setdefault("skip", [])
    elif kind == "matrix":
        local = entry.get("cells")
        if local:
            skip = _own_cell(local, origin.get("cell")).setdefault("skip", [])
        else:
            skip = entry.setdefault("cell_skip", {}).setdefault(origin.get("cell"), [])
    else:
        raise KeyError(f"cannot skip a step of kind {kind!r}")
    if key not in skip:
        skip.append(key)


def _own_cell(cells, cell_id):
    for i, cell in enumerate(cells, 1):
        if (cell.get("id") or f"cell{i}") == cell_id:
            return cell
    raise KeyError(f"cell {cell_id!r} is not in this entry's own cells")


# ── flattening (breaking a template back out into plain steps) ───────────────

def flatten_reference(config, plan, position):
    """Replace ONE reference entry with the literal steps it expands to.

    position is 1-based into the UNEXPANDED config["steps"] list. Returns the
    new steps list. The expansion is identical to what the runner would have
    produced -- overrides already applied, provenance stripped -- so a
    flattened testlist runs exactly as it did before. That property is the
    point: breaking a template out is an editing convenience, never a change
    in behaviour.
    """
    steps_in = list((config or {}).get("steps") or [])
    if not 1 <= position <= len(steps_in):
        raise PlanError(f"no step at position {position}")
    entry = steps_in[position - 1]
    kind = entry_kind(entry)
    if kind == "step":
        raise PlanError(f"step {position} is a plain step, not a reference")

    expanded, _ = expand_steps({**(config or {}), "steps": [entry]}, plan, strict=True)
    for step in expanded:
        step.pop(ORIGIN_KEY, None)
    return steps_in[:position - 1] + expanded + steps_in[position:]


# ── deriving a template/matrix from steps that already repeat ────────────────

def derive_matrix(blocks, *, base_id=None):
    """Turn repeated blocks of steps into (cell_steps, cells).

    `blocks` is an ordered mapping cell-id -> list of steps, e.g. the
    subgroups of an existing hand-written sweep. The first block (or the one
    named by base_id) becomes the cell template; every difference in any other
    block becomes a per-cell override. Nothing is generalised into an axis --
    that is a judgement call about intent that belongs to whoever is reading
    the test, not to a diff.

    The result is exact by construction: expanding it reproduces the input
    steps byte for byte, which is what makes converting a 173-step testlist to
    a matrix a safe, checkable edit rather than a rewrite. The `overridable`
    list on each cell step is the set of params that actually varied, which is
    also exactly the column set the editor's matrix grid should offer.
    """
    ids = list(blocks)
    if not ids:
        raise PlanError("no blocks to derive a matrix from")
    base_key = base_id or ids[0]
    if base_key not in blocks:
        raise PlanError(f"base block {base_key!r} is not one of: {', '.join(ids)}")
    base = blocks[base_key]

    cell_steps = []
    for i, step in enumerate(base, 1):
        tstep = copy.deepcopy(step)
        for key in ("group", "subgroup", ORIGIN_KEY):
            tstep.pop(key, None)
        tstep["id"] = f"s{i}"
        cell_steps.append(tstep)

    varying = set()
    for cid in ids:
        for i, step in enumerate(blocks[cid]):
            if i >= len(base):
                continue
            for key in _step_diff(step, base[i]):
                if key not in FREE_STEP_KEYS:
                    varying.add((i, key))
    for i, key in varying:
        cell_steps[i].setdefault("overridable", [])
        if key not in cell_steps[i]["overridable"]:
            cell_steps[i]["overridable"].append(key)
    for tstep in cell_steps:
        if "overridable" in tstep:
            tstep["overridable"].sort()

    cells = []
    for cid in ids:
        block = blocks[cid]
        cell = {"id": cid}
        if len(block) < len(base):
            # A short block drops trailing steps -- the last cell of a sweep
            # has no "settle before the next one" to do.
            cell["skip"] = [f"s{i}" for i in range(len(block) + 1, len(base) + 1)]
        elif len(block) > len(base):
            raise PlanError(
                f"block {cid!r} has {len(block)} steps but the template block "
                f"{base_key!r} has {len(base)}; a cell can drop steps but not "
                f"add them -- pick the longest block as the base")
        override = {}
        for i, step in enumerate(block):
            diff = _step_diff(step, base[i])
            if diff:
                override[f"s{i+1}"] = diff
        if override:
            cell["override"] = override
        cells.append(cell)
    return cell_steps, cells


def _step_diff(step, base):
    """What `step` overrides relative to `base`, in override syntax."""
    diff = {}
    sp, bp = step.get("param") or {}, base.get("param") or {}
    for key in set(sp) | set(bp):
        if sp.get(key) != bp.get(key):
            diff[key] = sp[key] if key in sp else REMOVE
    for key in FREE_STEP_KEYS + GATED_STEP_KEYS:
        if step.get(key) != base.get(key):
            diff[key] = step.get(key)
    return diff


def group_blocks(steps, group):
    """Steps of one group, split into blocks by their subgroup, in order."""
    blocks = {}
    order = []
    for step in steps:
        if step.get("group") != group:
            continue
        key = step.get("subgroup") or group
        if key not in blocks:
            blocks[key] = []
            order.append(key)
        blocks[key].append(step)
    return {k: blocks[k] for k in order}


# ── the one call every reader uses ───────────────────────────────────────────

def resolve_config(config, source_path, strict=False):
    """A testlist's CONFIG with its step references expanded.

    `source_path` is the testlist file (or its directory) -- the plan is the
    __testplan__.py beside it, because a plan belongs to the container the
    testlist lives in.

    Every consumer of a step list goes through this: the runner, the editor,
    the validator, the step-matrix viewer and the pause/discard badges. That
    is the point -- an expansion that only the runner performed would mean the
    editor showed a test that does not exist.

    Returns (config, notes). The returned dict is a shallow copy with "steps"
    replaced; the caller's dict is never mutated. A testlist with no
    references is returned unchanged, so this is cheap to call everywhere.
    """
    if not has_references(config):
        return config, []
    dirpath = source_path
    if dirpath and os.path.isfile(dirpath):
        dirpath = os.path.dirname(os.path.abspath(dirpath))
    plan = load_plan(dirpath)
    steps, notes = expand_steps(config, plan, strict=strict)
    return {**config, "steps": steps}, notes
