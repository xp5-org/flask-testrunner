"""
drafthelper.py
--------------
Per-testlist live-edit drafts: the "working copy" of a testlist's steps.

TWO VERSIONS OF A TEST
    The __testlist__*.py file is the LAST-KNOWN-GOOD (LKG) version. Batch runs,
    a plain Run and every other reader keep using it untouched. A draft is
    where live edits land -- written the moment the user makes them, so a
    crashed run, a closed tab or a /reload never costs the work. A draft only
    ever becomes the LKG through an explicit promote().

WHERE A DRAFT LIVES
    <project>/__drafts__/<testlist>.json -- a subdirectory, so reload_tests'
    scan of the project dir (files only, __testlist__*.py only) never sees it.
    Only "steps" is stored: top-level CONFIG vars are set before the emulator
    starts and are out of scope for live editing.

STEP UIDS
    Positions shift the moment a step is inserted or deleted, so every raw
    entry in a draft carries a "_uid". An EXPANDED step's uid is derived from
    its entry's uid plus its provenance (template step id, matrix cell), which
    keeps it stable across edits anywhere else in the list. An LKG entry with
    no _uid is "e<pos>" -- the same value seed() stamps when a draft is first
    made from it, so a run started from the LKG and a draft made mid-run agree
    on every uid. Both reserved keys are stripped on promote.
"""

import copy
import hashlib
import json
import os
import secrets
import threading
import time

import stepplan

DRAFT_DIR = "__drafts__"
UID_KEY = "_uid"
HISTORY_LIMIT = 50

TEARDOWN_ACTIONS = ("test_terminate_all",)

_lock = threading.RLock()


class DraftError(Exception):
    """A draft operation that cannot be applied as asked (maps to 400)."""


class DraftConflict(DraftError):
    """expected_revision did not match the stored draft (maps to 409)."""


class DraftLocked(DraftError):
    """An edit aimed at a step a live run has frozen (maps to 409)."""


# ── paths, hashing, uids ─────────────────────────────────────────────────────

def draft_path(full_path):
    d = os.path.join(os.path.dirname(os.path.abspath(full_path)), DRAFT_DIR)
    base = os.path.splitext(os.path.basename(full_path))[0]
    return os.path.join(d, base + ".json")


def strip(steps):
    """Steps as they would be written to the .py file: no reserved keys."""
    out = []
    for entry in steps or []:
        e = copy.deepcopy(entry)
        e.pop(UID_KEY, None)
        e.pop(stepplan.ORIGIN_KEY, None)
        out.append(e)
    return out


def steps_hash(steps):
    blob = json.dumps(strip(steps), sort_keys=True, default=str)
    return hashlib.sha1(blob.encode("utf-8")).hexdigest()


def new_uid():
    return "n" + secrets.token_hex(4)


def stamp(steps, seed=False):
    """Give every entry a unique _uid, in place. seed=True uses e<pos> for
    entries that have none (the LKG convention); otherwise fresh ones."""
    seen = set()
    for pos, entry in enumerate(steps or [], 1):
        uid = entry.get(UID_KEY)
        if not uid or uid in seen:
            uid = f"e{pos}" if (seed and not uid) else new_uid()
            while uid in seen:
                uid = new_uid()
            entry[UID_KEY] = uid
        seen.add(uid)
    return steps


def is_teardown(step):
    return bool(step.get("teardown")) or step.get("action") in TEARDOWN_ACTIONS


# ── expansion with uids ──────────────────────────────────────────────────────

def _expanded_uid(entry_uid, origin):
    kind = origin.get("kind")
    sub = origin.get("step_id") or origin.get("index")
    if kind == "template":
        return f"{entry_uid}:{sub}"
    if kind == "matrix":
        return f"{entry_uid}:{origin.get('cell')}:{sub}"
    return entry_uid


def expand(raw_steps, config, plan, strict=True):
    """Expand raw steps against a GIVEN plan and config, stamping each
    expanded step's "_uid". Returns (steps, notes).

    Takes the plan rather than loading it so a live run can keep expanding
    against the plan it started with -- edits to __testplan__.py mid-run do
    not reach it, same rule as every other container detail.
    """
    steps, notes = stepplan.expand_steps(
        {**(config or {}), "steps": raw_steps}, plan, strict=strict)
    for step in steps:
        origin = step.get(stepplan.ORIGIN_KEY) or {}
        pos = origin.get("entry")
        entry = raw_steps[pos - 1] if pos and 1 <= pos <= len(raw_steps) else {}
        entry_uid = entry.get(UID_KEY) or f"e{pos}"
        step[UID_KEY] = _expanded_uid(entry_uid, origin)
    return steps, notes


def fingerprint(step):
    """What a step DOES -- the part a frozen step must not silently change.
    title/description/pause_on are cosmetic or control-only and excluded."""
    blob = json.dumps({"action": step.get("action"),
                       "subaction": step.get("subaction"),
                       "param": step.get("param") or {}},
                      sort_keys=True, default=str)
    return hashlib.sha1(blob.encode("utf-8")).hexdigest()


# ── storage ──────────────────────────────────────────────────────────────────

def load(full_path):
    p = draft_path(full_path)
    try:
        with open(p, "r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return None


def _write(full_path, doc):
    p = draft_path(full_path)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    tmp = f"{p}.tmp.{os.getpid()}"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(doc, f, indent=2, default=str)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, p)


def save(full_path, steps, *, module="", base_steps=None,
         expected_revision=None, note=""):
    """Write a new revision of the draft, creating it if needed.

    base_steps is the LKG steps list the draft forks from; it is only read
    when the draft is first created (to record base_hash). expected_revision,
    when given, must match the stored revision -- two editors (matrix viewer
    and testbuilder) cannot silently overwrite each other.
    """
    with _lock:
        doc = load(full_path)
        if expected_revision is not None:
            have = doc["revision"] if doc else 0
            if int(expected_revision) != have:
                raise DraftConflict(
                    f"draft is at revision {have}, edit was made against "
                    f"revision {expected_revision} -- reload and reapply")
        steps = stamp(copy.deepcopy(steps))
        now = time.time()
        if doc is None:
            # active: which version a plain Run uses and the offline editor
            # edits. A new draft starts inactive -- the .py stays in charge
            # until the user switches (see set_active).
            doc = {"module": module, "testlist": os.path.abspath(full_path),
                   "revision": 0, "created_at": now, "active": False,
                   "base_hash": steps_hash(base_steps or []), "history": []}
        else:
            doc.setdefault("history", []).append({
                "revision": doc["revision"], "at": doc.get("updated_at"),
                "note": doc.get("note", ""), "steps": doc["steps"]})
            doc["history"] = doc["history"][-HISTORY_LIMIT:]
        doc["revision"] += 1
        doc["updated_at"] = now
        doc["note"] = note
        doc["steps"] = steps
        if module:
            doc["module"] = module
        _write(full_path, doc)
        return doc


def seed(full_path, lkg_steps, module=""):
    """The draft, creating it from the LKG steps (with e<pos> uids) if absent."""
    with _lock:
        doc = load(full_path)
        if doc is not None:
            return doc
        seeded = stamp(copy.deepcopy(lkg_steps), seed=True)
        return save(full_path, seeded, module=module, base_steps=lkg_steps,
                    note="created from last-known-good")


def is_active(full_path):
    doc = load(full_path) if full_path else None
    return bool(doc and doc.get("active"))


def set_active(full_path, active):
    """Choose which version is live: the draft (True) or the .py (False).
    Not a revision -- the steps don't change, only which set is used."""
    with _lock:
        doc = load(full_path)
        if doc is None:
            raise DraftError("no draft to activate")
        doc["active"] = bool(active)
        _write(full_path, doc)
        return doc


def revert(full_path, revision, expected_revision=None):
    with _lock:
        doc = load(full_path)
        if doc is None:
            raise DraftError("no draft to revert")
        old = next((h for h in doc.get("history", [])
                    if h["revision"] == int(revision)), None)
        if old is None:
            raise DraftError(f"revision {revision} is not in this draft's history")
        return save(full_path, old["steps"], expected_revision=expected_revision,
                    note=f"reverted to r{revision}")


def discard(full_path):
    with _lock:
        try:
            os.remove(draft_path(full_path))
            return True
        except FileNotFoundError:
            return False


def promote(full_path, expected_revision=None):
    """Write the draft's steps into the .py file (the new LKG), drop the draft."""
    from newprojecthelper import update_config_in_file
    with _lock:
        doc = load(full_path)
        if doc is None:
            raise DraftError("no draft to promote")
        if expected_revision is not None and int(expected_revision) != doc["revision"]:
            raise DraftConflict(
                f"draft is at revision {doc['revision']}, promote asked for "
                f"{expected_revision}")
        update_config_in_file(full_path, {"steps": strip(doc["steps"])})
        discard(full_path)
        return doc


def summary(doc, lkg_steps=None):
    """A draft's header for API responses, without its full history."""
    if doc is None:
        return None
    out = {k: doc.get(k) for k in ("module", "revision", "created_at",
                                   "updated_at", "note", "base_hash")}
    out["active"] = bool(doc.get("active"))
    out["revisions"] = [h["revision"] for h in doc.get("history", [])]
    if lkg_steps is not None:
        # The .py file was changed (the old way) after this draft forked
        # from it -- promote would overwrite that change.
        out["lkg_changed"] = steps_hash(lkg_steps) != doc.get("base_hash")
        out["matches_lkg"] = steps_hash(lkg_steps) == steps_hash(doc["steps"])
    return out


# ── edits by expanded-step uid ───────────────────────────────────────────────

def _locate(raw, config, plan, uid):
    expanded, _ = expand(raw, config, plan, strict=True)
    for i, step in enumerate(expanded):
        if step[UID_KEY] == uid:
            origin = step.get(stepplan.ORIGIN_KEY) or {}
            return expanded, i, step, origin, origin.get("entry")
    raise DraftError(f"no step with uid {uid!r} in the draft")


def _flatten(raw, config, plan, pos):
    """Replace reference entry `pos` (1-based) with plain steps, each keeping
    the uid it had while expanded, so a live run stays aligned."""
    entry = raw[pos - 1]
    produced, _ = expand([entry], config, plan, strict=True)
    base = entry.get(UID_KEY) or f"e{pos}"
    flat = []
    for step in produced:
        origin = step.pop(stepplan.ORIGIN_KEY, {}) or {}
        step[UID_KEY] = _expanded_uid(base, origin)
        flat.append(step)
    return raw[:pos - 1] + flat + raw[pos:]


def _prune_empty(entry):
    """Leave no empty {} / [] behind once the last flag comes off."""
    for block in ("override", "cell_override"):
        blk = entry.get(block)
        if not isinstance(blk, dict):
            continue
        for key, val in list(blk.items()):
            if isinstance(val, dict):
                for k2 in list(val):
                    if val[k2] == {}:
                        val.pop(k2)
            if val == {}:
                blk.pop(key)
        if blk == {}:
            entry.pop(block)


def _new_steps(steps, known_actions):
    if not isinstance(steps, list) or not steps:
        raise DraftError("insert needs a non-empty 'steps' list")
    out = []
    for s in steps:
        if not isinstance(s, dict) or not s.get("action"):
            raise DraftError("every inserted step needs an 'action'")
        if known_actions is not None and s["action"] not in known_actions:
            raise DraftError(f"unknown action {s['action']!r}")
        s = copy.deepcopy(s)
        s.pop(stepplan.ORIGIN_KEY, None)
        # keep a caller-chosen fresh uid (a manual step recorded as it ran
        # must carry the same uid in the draft as in the live plan)
        if not str(s.get(UID_KEY) or "").startswith("n"):
            s[UID_KEY] = new_uid()
        s.setdefault("param", {})
        s.setdefault("subaction", "")
        out.append(s)
    return out


def apply_ops(raw, ops, config, plan, *, locked=(), pause_locked=None, known_actions=None):
    """Apply expanded-level edit ops to a raw steps list. Returns (raw, notes).

    `locked` is the set of expanded uids a live run has frozen (already run,
    or at/before the boot line); an op aimed at one raises DraftLocked.
    `pause_locked` (default: same as locked) is the narrower set set_pause is
    refused on -- a breakpoint on a frozen step that has not run yet is fine.

    ops:
      {"op": "set_pause", "uid": U, "pause_on": bool}
      {"op": "delete",    "uid": U}
      {"op": "truncate",  "uid": U, "keep_teardown": true}   U and everything after
      {"op": "insert",    "after": U | "before": U | "at": "end", "steps": [...]}
      {"op": "replace",   "uid": U, "step": {...}}
    A reference entry (template/matrix use) is flattened only where there is
    no narrower way to express the edit: delete and pause never flatten.
    """
    raw = stamp(copy.deepcopy(raw))
    locked = set(locked or ())
    pause_locked = locked if pause_locked is None else set(pause_locked)
    notes = []

    def check(uid, what, frozen=None):
        if uid in (locked if frozen is None else frozen):
            raise DraftLocked(f"{what}: step {uid} is frozen for the live run "
                              f"(already ran, or at/before the emulator start)")

    for op in ops or []:
        kind = op.get("op")
        if kind == "set_pause":
            uid = op.get("uid")
            check(uid, "set_pause", pause_locked)
            _, _, _, origin, pos = _locate(raw, config, plan, uid)
            entry = raw[pos - 1]
            target = stepplan.step_target(entry, origin)
            if op.get("pause_on", True):
                target["pause_on"] = True
            else:
                target.pop("pause_on", None)
                _prune_empty(entry)

        elif kind == "delete":
            uid = op.get("uid")
            check(uid, "delete")
            _, _, _, origin, pos = _locate(raw, config, plan, uid)
            if origin.get("kind") in (None, "step"):
                raw.pop(pos - 1)
            else:
                stepplan.skip_step(raw[pos - 1], origin)

        elif kind == "truncate":
            uid = op.get("uid")
            check(uid, "truncate")
            keep_td = op.get("keep_teardown", True)
            expanded, i, _, _, _ = _locate(raw, config, plan, uid)
            doomed = [s for s in expanded[i:] if not (keep_td and is_teardown(s))]
            doomed_uids = {s[UID_KEY] for s in doomed}
            # Whole entries go at once; a reference entry only partly doomed
            # keeps its reference and skips the doomed steps.
            by_entry = {}
            for s in expanded:
                pos = (s.get(stepplan.ORIGIN_KEY) or {}).get("entry")
                by_entry.setdefault(pos, []).append(s)
            drop = set()
            for pos, members in by_entry.items():
                hit = [s for s in members if s[UID_KEY] in doomed_uids]
                if not hit:
                    continue
                if len(hit) == len(members):
                    drop.add(pos)
                else:
                    for s in hit:
                        stepplan.skip_step(raw[pos - 1], s[stepplan.ORIGIN_KEY])
            raw = [e for p, e in enumerate(raw, 1) if p not in drop]
            notes.append(f"truncated {len(doomed)} step(s) from {uid}"
                         + (" (teardown steps kept)" if keep_td else ""))

        elif kind == "insert":
            new = _new_steps(op.get("steps"), known_actions)
            if op.get("at") == "end":
                raw.extend(new)
            else:
                anchor = op.get("after") or op.get("before")
                if not anchor:
                    raise DraftError("insert needs 'after', 'before' or 'at': 'end'")
                after = bool(op.get("after"))
                if not after:
                    check(anchor, "insert before")
                expanded, i, _, origin, pos = _locate(raw, config, plan, anchor)
                if origin.get("kind") not in (None, "step"):
                    members = [s[UID_KEY] for s in expanded
                               if (s.get(stepplan.ORIGIN_KEY) or {}).get("entry") == pos]
                    edge = members[-1] if after else members[0]
                    if anchor != edge:
                        raw = _flatten(raw, config, plan, pos)
                        notes.append(f"flattened entry {pos} to insert inside it")
                    # re-find the anchor's raw position (flattening moves it)
                    pos = next((p for p, e in enumerate(raw, 1)
                                if e.get(UID_KEY) == anchor), pos)
                at = pos if after else pos - 1
                raw[at:at] = new
            notes.append("inserted " + ", ".join(s[UID_KEY] for s in new))

        elif kind == "replace":
            uid = op.get("uid")
            check(uid, "replace")
            new = _new_steps([op.get("step")], known_actions)[0]
            _, _, _, origin, pos = _locate(raw, config, plan, uid)
            if origin.get("kind") not in (None, "step"):
                raw = _flatten(raw, config, plan, pos)
                notes.append(f"flattened entry {pos} to replace a step inside it")
                pos = next(p for p, e in enumerate(raw, 1) if e.get(UID_KEY) == uid)
            new[UID_KEY] = uid
            raw[pos - 1] = new

        else:
            raise DraftError(f"unknown op {kind!r}")

    # Every op must leave a draft that still expands -- refuse the batch
    # otherwise, rather than save something the next boundary chokes on.
    try:
        expand(raw, config, plan, strict=True)
    except stepplan.PlanError as e:
        raise DraftError(f"edit would leave the draft unexpandable: {e}")
    return raw, notes
