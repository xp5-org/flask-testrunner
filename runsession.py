"""
runsession.py
-------------
The step engine for one CONFIG-driven run: a cursor walking a step plan that
may change while the run is live, with the ability to SUSPEND between steps.

WHY NOT A FIXED LIST
    test_runner used to turn the expanded steps into callables once, up front,
    and loop over them. That made three things impossible: stopping between
    steps and waiting for a human (single-step), picking up edits to the steps
    not run yet (live edit), and parking on a failure instead of skipping the
    rest (suspend-on-fault). Here the plan is data, re-read from the test's
    draft at every step boundary, and each step's callable is looked up just
    before it runs.

SUSPENDING
    Only ever BETWEEN steps. A step is one blocking dispatch call (an OCR wait
    can sit 150s) and there is no safe point inside it; the only way into a
    running step is the existing context["abort"] cancel, which Stop still
    uses. Reasons a run suspends before a step:
        breakpoint  the step has pause_on (THE breakpoint -- there is no other)
        step        single-step mode, waiting for the next Step
        fault       the previous step failed and on_fault is suspend/step
        run_to      reached the step a run_to command named
        user        a Pause was requested while running
        draft_invalid  the draft no longer expands; nothing is run from it
    While suspended the emulators keep running and the user can drive them
    through /api/v1/instances. A suspend has a deadline (suspend_timeout,
    minutes, 0 = never) after which suspend_timeout_action (stop|abandon) is
    applied, and if every emulator that was up at suspend time exits, the run
    stops -- the old pause_on "close the window to continue" behaviour.

THE FROZEN REGION
    plan[:max(cursor, boot_line)] is frozen: steps already run, and everything
    up to and including the last emulator-start step. Edits to it in the
    draft are kept in the draft (the work is not lost) but ignored by this run
    and reported in `warnings` -- the run keeps the values it started with.
    Top-level CONFIG vars and the container's __testplan__.py are frozen the
    same way: both are captured at run start. The one exception is pause_on
    on a frozen step that has not run yet (suspended before boot): a
    breakpoint is not a boot parameter, so that is picked up.

NUMBERING
    Reports, screenshots (progress_state.step) and live_results all number
    steps by EXECUTION ORDINAL -- 1..N in the order they ran, retries and
    skipped steps included -- which is what report rows have always been
    (populate_sqlite enumerates results). Stable identity across edits is the
    step "_uid" (see drafthelper).
"""

import collections
import copy
import threading
import time

import drafthelper
import stepplan
from appstate import progress_state

MODES = ("auto", "step")
ON_FAULT = ("abort", "suspend", "step")
TIMEOUT_ACTIONS = ("stop", "abandon")
DEFAULT_SUSPEND_TIMEOUT_MIN = 60

# Emulator/instance launchers: the boot line is the last of these in the plan.
BOOT_PREFIX = "test_start"
NOT_BOOT = ("test_start_gif_capture",)

UID = drafthelper.UID_KEY


class SessionError(Exception):
    """A control command that does not apply in the session's current state."""


def is_boot_step(step):
    a = step.get("action") or ""
    return a.startswith(BOOT_PREFIX) and a not in NOT_BOOT


def _pause_flag(v):
    if isinstance(v, bool):
        return v
    return str(v).strip().lower() in ("1", "true", "yes", "on")


def _live_instances(context):
    live = []
    for key, val in (context or {}).items():
        proc = getattr(val, "process", None)
        try:
            if proc is not None and proc.poll() is None:
                live.append((key, val))
        except Exception:
            pass
    return live


class RunSession:
    def __init__(self, *, run_id, module, full_path, config, raw_steps, plan,
                 dispatch, context, interactive=True, source="lkg",
                 mode=None, on_fault=None, suspend_timeout=None,
                 suspend_timeout_action=None):
        self.run_id = run_id
        self.module = module
        self.full_path = full_path
        self.dispatch = dispatch
        self.context = context
        self.interactive = bool(interactive)

        # Frozen at run start: CONFIG vars and the container plan.
        self.config = copy.deepcopy({k: v for k, v in config.items() if k != "steps"})
        self.plan_def = copy.deepcopy(plan)
        self.continue_on_failure = bool(config.get("continue_on_failure", False))

        self.mode = mode or config.get("step_mode") or "auto"
        self.on_fault = on_fault or config.get("on_fault") or "abort"
        if suspend_timeout is None:
            suspend_timeout = config.get("suspend_timeout", DEFAULT_SUSPEND_TIMEOUT_MIN)
        self.suspend_timeout = float(suspend_timeout or 0)
        self.suspend_timeout_action = (suspend_timeout_action
                                       or config.get("suspend_timeout_action") or "stop")
        for val, allowed, what in ((self.mode, MODES, "mode"),
                                   (self.on_fault, ON_FAULT, "on_fault"),
                                   (self.suspend_timeout_action, TIMEOUT_ACTIONS,
                                    "suspend_timeout_action")):
            if val not in allowed:
                raise ValueError(f"{what} must be one of {', '.join(allowed)}; got {val!r}")
        if not self.interactive:
            # Batch runs are unattended: nothing may park them.
            self.mode, self.on_fault = "auto", "abort"

        # The plan: expanded steps, each with a _uid.
        self.source = source
        self.draft_rev = None
        existing = drafthelper.load(full_path) if full_path else None
        doc = existing if source == "draft" else None
        if doc is not None:
            raw = doc["steps"]
            self.draft_rev = doc["revision"]
        else:
            self.source = "lkg"
            raw = drafthelper.stamp(copy.deepcopy(raw_steps), seed=True)
        # Live edits land in the draft, so the run can only follow the draft
        # if the draft IS what it runs: it started from the draft, or there
        # was none yet (the first live edit creates it from these steps). A
        # run of the .py while an unrelated inactive draft exists must not
        # adopt that draft at its first boundary.
        self.follows_draft = bool(self.interactive and (doc is not None or existing is None))
        self.lkg_steps = copy.deepcopy(raw_steps)
        self.plan, _notes = drafthelper.expand(raw, self.config, self.plan_def, strict=True)
        self.boot_line = max((i for i, s in enumerate(self.plan, 1) if is_boot_step(s)),
                             default=0)

        self.cursor = 0              # plan steps consumed
        self.results = []            # report tuples, execution order
        self.records = []            # per-executed-step metadata, same order
        self.warnings = []
        self.edit_log = []
        self.draft_error = None
        self._ignore_rev = None
        self.retry_edited = False    # the retry candidate differs in the draft

        self.state = "running"       # running | suspended | finished
        self.reason = None
        self.detail = ""
        self.suspended_at = None
        self._timeout_base = None
        self.suspended_total = 0.0
        self.started_at = time.time()
        self.finished_at = None

        self.running_step = None     # the step executing right now, if any
        self.stop_kind = None        # None | "stop" | "abandon"
        self.stop_why = ""
        self.aborted = False         # fault with on_fault=abort
        self._grant = False          # run the next step without suspending
        self._retry_of = None
        self._fault_pending = False
        self._pause_requested = False
        self._run_to = None

        self.lock = threading.RLock()
        self.cv = threading.Condition(self.lock)
        self._commands = collections.deque()

    # ── plan bookkeeping ─────────────────────────────────────────────────────

    def frozen_len(self):
        return max(self.cursor, self.boot_line)

    def frozen_uids(self):
        return {s[UID] for s in self.plan[:self.frozen_len()]}

    def executed_uids(self):
        return {s[UID] for s in self.plan[:self.cursor]}

    def retry_uid(self):
        """The step a retry would re-run, if retry is possible right now."""
        with self.lock:
            if self.state == "suspended" and self.records and self.cursor > self.boot_line:
                return self.plan[self.cursor - 1][UID]
            return None

    def edit_locked_uids(self):
        """frozen_uids, less the retry candidate: fixing the step that just
        failed and then retrying it is the whole point of a fault suspend.
        If the user steps on instead, the edit stays in the draft and this
        run ignores it (reported in warnings), like any other frozen edit."""
        return self.frozen_uids() - {self.retry_uid()}

    def reconcile(self, force=False):
        """Pick up a new draft revision: keep the frozen region, take the rest."""
        if not self.follows_draft or not self.full_path:
            return
        doc = drafthelper.load(self.full_path)
        if doc is None:
            return
        rev = doc["revision"]
        with self.lock:
            if not force and rev == self.draft_rev:
                return
            if rev == self._ignore_rev:
                return
            try:
                new_plan, _ = drafthelper.expand(doc["steps"], self.config,
                                                 self.plan_def, strict=True)
            except Exception as e:
                self.draft_error = f"draft r{rev}: {type(e).__name__}: {e}"
                return

            fn = self.frozen_len()
            frozen = self.plan[:fn]
            frozen_uids = {s[UID] for s in frozen}
            index_of = {s[UID]: i for i, s in enumerate(new_plan)}

            ignored = []
            retry_uid = self.retry_uid()
            self.retry_edited = False
            for i, s in enumerate(frozen):
                j = index_of.get(s[UID])
                if j is None:
                    ignored.append(f"{s[UID]} removed")
                    continue
                ns = new_plan[j]
                if drafthelper.fingerprint(ns) != drafthelper.fingerprint(s):
                    if s[UID] == retry_uid:
                        # applies if the user retries (retry re-reads the
                        # draft); only ignored if they step on instead
                        self.retry_edited = True
                    else:
                        ignored.append(f"{s[UID]} changed")
                if i >= self.cursor:
                    # not run yet: a breakpoint change still applies
                    if _pause_flag(ns.get("pause_on", False)):
                        s["pause_on"] = True
                    else:
                        s.pop("pause_on", None)

            anchor = -1
            for s in reversed(frozen):
                if s[UID] in index_of:
                    anchor = index_of[s[UID]]
                    break
            early = [s[UID] for s in new_plan[:anchor + 1] if s[UID] not in frozen_uids]
            if early:
                ignored.append("inserted before the frozen point: " + ", ".join(early))
            tail = [s for s in new_plan[anchor + 1:] if s[UID] not in frozen_uids]

            before = [s[UID] for s in self.plan[fn:]]
            self.plan = frozen + tail
            self.draft_rev = rev
            self.draft_error = None
            self.source = "draft"
            if ignored:
                self.warnings.append({"at": time.time(), "revision": rev,
                                      "ignored": ignored,
                                      "message": "frozen for this run (already ran, or at/before "
                                                 "the emulator start) -- kept in the draft, not applied"})
            if before != [s[UID] for s in tail] or ignored:
                self.edit_log.append({"at": time.time(), "revision": rev,
                                      "after_ordinal": len(self.results),
                                      "note": doc.get("note", ""),
                                      "pending_before": len(before),
                                      "pending_after": len(tail)})

    # ── control (called from API threads) ────────────────────────────────────

    def control(self, command, **args):
        with self.lock:
            if self.state == "finished":
                raise SessionError("run has finished")
            if command == "pause":
                if self.state == "running":
                    self._pause_requested = True
                return self.snapshot()
            if command == "set_timeout":
                minutes = args.get("minutes")
                if minutes is None:
                    raise SessionError("set_timeout needs 'minutes' (0 = never)")
                self.suspend_timeout = max(0.0, float(minutes))
                if args.get("action"):
                    if args["action"] not in TIMEOUT_ACTIONS:
                        raise SessionError(f"action must be one of {', '.join(TIMEOUT_ACTIONS)}")
                    self.suspend_timeout_action = args["action"]
                if self.state == "suspended" and args.get("from_now", True):
                    self._timeout_base = time.time()
                self.cv.notify_all()
                return self.snapshot()
            if command in ("stop", "abandon"):
                if self.state == "running":
                    self.stop_kind = command
                    self.stop_why = f"{command} requested while step was running"
                    # cancel the running step through the one hook steps honour
                    self.context["abort"] = True
                    return self.snapshot()
            elif command in ("step", "resume", "run_to", "retry", "continue_in_memory"):
                if self.state != "suspended":
                    raise SessionError(f"'{command}' needs a suspended run (run is {self.state})")
                if command == "retry":
                    self._check_retry()
                if command == "run_to":
                    self._resolve_run_to(args)
            else:
                raise SessionError(f"unknown command {command!r}")
            self._commands.append(dict(args, command=command))
            self.cv.notify_all()
            return self.snapshot()

    def _check_retry(self):
        if not self.records:
            raise SessionError("nothing has run yet")
        if self.cursor <= self.boot_line:
            raise SessionError("the last step is at/before the emulator start; "
                               "retrying it would re-run boot -- start a new run")

    def _resolve_run_to(self, args):
        uid = args.get("uid")
        if not uid and args.get("pos"):
            pos = int(args["pos"])
            if not self.cursor < pos <= len(self.plan):
                raise SessionError(f"run_to pos {pos} is not a pending step")
            uid = self.plan[pos - 1][UID]
        pending = {s[UID] for s in self.plan[self.cursor:]}
        if uid not in pending:
            raise SessionError(f"run_to target {uid!r} is not a pending step")
        args["uid"] = uid

    # ── the engine ───────────────────────────────────────────────────────────

    def run(self):
        """Execute the plan. Returns the results list (report tuples)."""
        progress_state.live_results = []
        try:
            while True:
                if self.stop_kind or self.aborted:
                    break
                self.reconcile()
                if self.cursor >= len(self.plan):
                    break
                step = self.plan[self.cursor]

                if self._grant:
                    self._grant = False
                else:
                    reason = self._suspend_reason(step)
                    if reason:
                        self._handle(self._suspend(reason, step))
                        continue

                self._execute(step)

            self._wind_down()
        finally:
            with self.lock:
                self.state = "finished"
                self.finished_at = time.time()
                self.cv.notify_all()
            progress_state.step = "Idle"
            progress_state.testname = ""
            progress_state.step_name = ""
            progress_state.testtype = ""
            progress_state.testid = ""
        return self.results

    def _suspend_reason(self, step):
        if not self.interactive:
            return None
        if self.draft_error:
            return "draft_invalid"
        if self._fault_pending:
            return "fault"
        if self._pause_requested:
            return "user"
        if self._run_to and step[UID] == self._run_to:
            self._run_to = None
            return "run_to"
        if _pause_flag(step.get("pause_on", False)):
            return "breakpoint"
        if self.mode == "step":
            return "step"
        return None

    def _suspend(self, reason, step):
        with self.lock:
            self.state = "suspended"
            self.reason = reason
            self.detail = (self.draft_error if reason == "draft_invalid"
                           else self._fault_detail() if reason == "fault" else "")
            self.suspended_at = time.time()
            self._timeout_base = self.suspended_at
            self._pause_requested = False
            watched = [k for k, _ in _live_instances(self.context)]
        # A GIF recording in flight keeps one frame of the parked screen and
        # then stops capturing until the run moves on.
        held = []
        for rec in (self.context.get("_gif_recorders") or []):
            try:
                if getattr(rec, "running", False) and hasattr(rec, "hold") and not rec.held:
                    rec.hold()
                    held.append(rec)
            except Exception:
                pass
        pos = self.cursor + 1
        progress_state.step_name = (f"SUSPENDED ({reason}) before step {pos}: "
                                    f"{step.get('title') or step.get('action')}")
        print(f"[session] {progress_state.step_name}")

        while True:
            with self.lock:
                cmd = self._wait_command(reason, watched)
            if cmd.get("command") != "adhoc":
                break
            # A manual step runs here, inside the suspend: the run does not
            # move on, only this one step executes, then it parks again.
            for rec in held:
                try: rec.release()
                except Exception: pass
            try:
                self._run_adhoc(cmd)
            finally:
                for rec in held:
                    try: rec.hold()
                    except Exception: pass
                with self.lock:
                    self._timeout_base = time.time()   # user activity
                    nxt = self.plan[self.cursor] if self.cursor < len(self.plan) else {}
                    progress_state.step_name = (f"SUSPENDED ({reason}) before step "
                                                f"{self.cursor + 1}: "
                                                f"{nxt.get('title') or nxt.get('action') or 'end'}")
                cmd["done"].set()
        with self.lock:
            self.suspended_total += time.time() - self.suspended_at
            for rec in held:
                try:
                    rec.release()
                except Exception:
                    pass
            self.state = "running"
            self.reason = None
            self.detail = ""
            self.suspended_at = None
        return cmd

    def _wait_command(self, reason, watched):
        """Wait (lock held; cv.wait releases it) for the next thing that ends
        or interrupts this suspend: a command, the timeout, or every watched
        instance closing. Returns it as a command dict."""
        last_draft_check = 0.0
        while True:
            if self._commands:
                return self._commands.popleft()
            now = time.time()
            if self.suspend_timeout > 0 and now - self._timeout_base > self.suspend_timeout * 60:
                return {"command": self.suspend_timeout_action,
                        "why": f"suspended longer than {self.suspend_timeout:g} min"}
            if watched and not _live_instances(self.context):
                return {"command": "stop",
                        "why": f"every instance closed while suspended ({', '.join(watched)})"}
            if now - last_draft_check > 1.0:
                # Keep the pending list current while parked, so what a
                # viewer sees is what Step will run.
                last_draft_check = now
                self.reconcile()
                if reason == "draft_invalid" and not self.draft_error:
                    return {"command": "_draft_fixed"}
                if self.draft_error and reason != "draft_invalid":
                    self.detail = self.draft_error
            self.cv.wait(0.5)

    def _run_adhoc(self, cmd):
        step = cmd["step"]
        # A fault leaves context["abort"] set until Step/Resume clears it; a
        # manual step run before that would read it as "cancelled" and FAIL.
        self.context["abort"] = False
        try:
            if cmd.get("record"):
                with self.lock:
                    uid = step[UID]
                    # the in-suspend reconcile may already have put it at the
                    # cursor (it was written to the draft just before)
                    if not (self.cursor < len(self.plan) and self.plan[self.cursor][UID] == uid):
                        self.plan.insert(self.cursor, step)
                    target = self.plan[self.cursor]
                self._execute(target, adhoc=True)
            else:
                self._execute(step, manual=True)
            r = self.results[-1]
            cmd["result"] = {"record": dict(self.records[-1]), "output": r[3], "stdout": r[4]}
        except Exception as e:
            cmd["result"] = {"error": f"{type(e).__name__}: {e}"}

    def adhoc(self, step, record=False, timeout=900):
        """Run one step now, while suspended, and wait for its result.

        record=False: a one-off (in the report as "(manual)", not in the
        plan). record=True: the caller has already inserted this exact step
        (same _uid) into the draft right after the last executed step; it
        runs and counts as executed there, so it is not run again on Step.
        Emulator-start steps are refused: boot is frozen for a live run.
        """
        action = step.get("action")
        func = self.dispatch.get(action)
        if func is None or not getattr(func, "_is_teststep", False):
            raise SessionError(f"unknown step action {action!r}")
        if is_boot_step(step):
            raise SessionError("emulator-start steps can't run inside a live run")
        if record and not step.get(UID):
            raise SessionError("record=True needs the step's draft _uid")
        cmd = {"command": "adhoc", "step": copy.deepcopy(step), "record": bool(record),
               "done": threading.Event(), "result": None}
        if not record:
            cmd["step"].setdefault(UID, "manual")
        with self.lock:
            if self.state != "suspended":
                raise SessionError(f"manual steps need a suspended run (run is {self.state})")
            self._commands.append(cmd)
            self.cv.notify_all()
        if not cmd["done"].wait(timeout):
            raise SessionError(f"manual step still running after {timeout}s")
        return cmd["result"]

    def _fault_detail(self):
        if not self.records:
            return ""
        r = self.results[-1]
        return f"step {len(self.results)} {r[0]} -> {r[1]}: {(r[3] or '')[-2000:]}"

    def _handle(self, cmd):
        c = cmd.get("command")
        with self.lock:
            if c in ("stop", "abandon"):
                self.stop_kind = c
                self.stop_why = cmd.get("why") or f"{c} requested while suspended"
                return
            if c == "_draft_fixed":
                return
            if c == "continue_in_memory":
                doc = drafthelper.load(self.full_path) if self.full_path else None
                self._ignore_rev = doc["revision"] if doc else None
                self.warnings.append({"at": time.time(), "revision": self._ignore_rev,
                                      "ignored": [self.draft_error or "draft"],
                                      "message": "continuing with the in-memory plan; "
                                                 "this draft revision is skipped"})
                self.draft_error = None
                return
            # Leaving a fault: the failed step's abort must not cancel the
            # next step, and the fault is dealt with.
            self._fault_pending = False
            self.context["abort"] = False
            self.context["abort_sticky"] = False
            if c == "step":
                self.mode = "step"
                self._grant = True
            elif c == "resume":
                self.mode = "auto"
                self._grant = True
            elif c == "run_to":
                self.mode = "auto"
                self._run_to = cmd.get("uid")
                self._grant = True
            elif c == "retry":
                self._retry_of = next((r["ordinal"] for r in reversed(self.records)
                                       if not r.get("manual")), None)
                self.cursor -= 1
                self.draft_rev = None          # re-read: the retried step is editable now
                self._grant = True

    def _execute(self, step, manual=False, adhoc=False):
        """Run one step and record it. manual: a one-off step run while
        suspended that is NOT part of the plan (cursor stays put). adhoc: a
        step run while suspended that IS in the plan (recorded into the draft
        as it ran). Neither parks the run as a fault or aborts it -- the user
        is already driving; they see the result and decide."""
        ordinal = len(self.results) + 1
        action = step.get("action", "unknown")
        title = step.get("title")
        name = f"{ordinal}_{action}" + (f": {title}" if title else "")
        retry_of = None
        if not (manual or adhoc):
            retry_of = self._retry_of
            self._retry_of = None
        if retry_of:
            name += f" (retry of {retry_of})"
        if manual or adhoc:
            name += " (manual)"
        info = step.get("description") or ""
        remaining = len(self.plan) - self.cursor - 1
        progress_state.step = f"{ordinal}/{ordinal + remaining}"
        progress_state.step_name = name

        started = time.time()
        with self.lock:
            self.running_step = {"ordinal": ordinal, "uid": step.get(UID), "action": action,
                                 "title": title or action, "started_at": started,
                                 "manual": bool(manual or adhoc)}
        try:
            status, color, log_output, stdout_output = self._call(step, name)
        finally:
            with self.lock:
                self.running_step = None
        stopped = time.time()

        result = (name, status, color, log_output, stdout_output, stopped - started, info)
        with self.lock:
            if not manual:
                self.cursor += 1
            self.results.append(result)
            progress_state.live_results.append(result)
            self.records.append({
                "ordinal": ordinal,
                "uid": step.get(UID) if not manual else f"manual:{ordinal}",
                "plan_pos": self.cursor if not manual else None,
                "name": name, "title": title, "action": action, "status": status,
                "duration": stopped - started, "start": started, "stop": stopped,
                "retry_of": retry_of, "superseded_by": None,
                "teardown": drafthelper.is_teardown(step),
                "manual": bool(manual),        # one-off, not in the plan
                "recorded": bool(adhoc),       # run by hand AND saved into the draft
                "output": (log_output or "")[-4000:],
                "param": copy.deepcopy(step.get("param") or {}),
                "description": step.get("description") or "",
                "pause_on": _pause_flag(step.get("pause_on", False)),
                "group": step.get("group"), "subgroup": step.get("subgroup"),
                "phase": step.get("phase"),
            })
            if retry_of:
                self.records[retry_of - 1]["superseded_by"] = ordinal

            if manual or adhoc:
                # a failed manual step must not cancel the next real step
                self.context["abort"] = False
                return
            if self.stop_kind:
                return
            failed = status in ("FAIL", "ERROR")
            # Single-stepping means a person is driving: a failure parks the
            # run for them whatever on_fault says -- aborting would throw away
            # the very state they are stepping through. on_fault=abort still
            # governs a run going on its own (auto mode).
            if failed and self.interactive and (self.on_fault in ("suspend", "step")
                                                or self.mode == "step"):
                self._fault_pending = True
                if self.on_fault == "step":
                    self.mode = "step"
                return
            if self.context.get("abort"):
                if self.continue_on_failure and not self.context.get("abort_sticky"):
                    self.context["abort"] = False
                else:
                    self.aborted = True

    def _call(self, step, name):
        import test_runner
        func = self.dispatch.get(step.get("action"))
        if func is None:
            return "FAIL", "red", f"Function {step.get('action')} not found in dispatch", ""
        kwargs = copy.deepcopy(step.get("param") or {})
        kwargs["context"] = self.context
        kwargs["config"] = {**self.config, "steps": self.plan}
        print(f"Running {name}")
        try:
            try:
                result = func(**kwargs)
            except Exception as e:
                result = (False, f"Testrunner.py: PYTHON CRASH in {getattr(func, '__name__', func)}:\n"
                                 f"kwargs={kwargs}\n{type(e).__name__}: {e}")
            stdout_output = ""
            if isinstance(result, tuple):
                success, log_output = result[0], result[1]
                stdout_output = result[2] if len(result) == 3 else ""
            else:
                success, log_output = result, ""
            if self.context.get("abort") is True or not success:
                return "FAIL", "red", log_output, stdout_output
            if test_runner._has_warn_marker(log_output):
                return "WARN", "warn", log_output, stdout_output
            return "PASS", "green", log_output, stdout_output
        except Exception as e:
            return "ERROR", "gray", str(e), ""

    def _wind_down(self):
        """Everything not run: SKIPPED, except teardown steps, which run unless
        the user abandoned the run (leave everything up) -- same rule the old
        engine applied to test_terminate_all after an automatic abort."""
        if self.cursor >= len(self.plan):
            return
        run_teardown = self.stop_kind != "abandon"
        why = (self.stop_why or ("Aborted due to previous failure" if self.aborted else "Skipped"))
        for step in list(self.plan[self.cursor:]):
            if run_teardown and drafthelper.is_teardown(step):
                self.context["abort"] = False
                self._execute(step)
                self.context["abort"] = True
                continue
            ordinal = len(self.results) + 1
            action = step.get("action", "unknown")
            title = step.get("title")
            name = f"{ordinal}_{action}" + (f": {title}" if title else "")
            result = (name, "SKIPPED", "gray", why, "", 0.0, step.get("description") or "")
            now = time.time()
            with self.lock:
                self.cursor += 1
                self.results.append(result)
                progress_state.live_results.append(result)
                self.records.append({
                    "ordinal": ordinal, "uid": step[UID], "plan_pos": self.cursor,
                    "name": name, "title": title, "action": action, "status": "SKIPPED",
                    "duration": 0.0, "start": now, "stop": now, "retry_of": None,
                    "superseded_by": None, "teardown": drafthelper.is_teardown(step),
                })

    # ── reads ────────────────────────────────────────────────────────────────

    def effective_failed(self):
        """A FAIL/ERROR that a later successful retry did not supersede."""
        return any(r["status"] in ("FAIL", "ERROR") and not r["superseded_by"]
                   and not r.get("manual") for r in self.records)

    def get_start(self, name):
        return next((r["start"] for r in self.records if r["name"] == name), None)

    def get_stop(self, name):
        return next((r["stop"] for r in self.records if r["name"] == name), None)

    def allowed_commands(self):
        if self.state == "finished":
            return []
        if self.state == "running":
            return ["pause", "stop", "abandon", "set_timeout"]
        cmds = ["step", "resume", "run_to", "stop", "abandon", "set_timeout"]
        if self.records and self.cursor > self.boot_line:
            cmds.append("retry")
        if self.reason == "draft_invalid":
            cmds.append("continue_in_memory")
        return cmds

    def _step_view(self, pos, step):
        origin = step.get(stepplan.ORIGIN_KEY) or {}
        return {
            "pos": pos, "uid": step[UID],
            "title": step.get("title") or step.get("description") or step.get("action"),
            "action": step.get("action"),
            "instance": (step.get("param") or {}).get("name"),
            "group": step.get("group"), "subgroup": step.get("subgroup"),
            "phase": step.get("phase"),
            "param": step.get("param") or {},
            "description": step.get("description") or "",
            "pause_on": _pause_flag(step.get("pause_on", False)),
            "teardown": drafthelper.is_teardown(step),
            "boot": is_boot_step(step),
            "frozen": pos <= self.frozen_len(),
            "origin": {k: origin.get(k) for k in ("kind", "entry", "source", "cell", "step_id")
                       if origin.get(k) is not None},
        }

    def snapshot(self):
        with self.lock:
            now = time.time()
            left = None
            if self.state == "suspended" and self.suspend_timeout > 0:
                left = max(0.0, self.suspend_timeout * 60 - (now - self._timeout_base))
            return {
                "run_id": self.run_id, "module": self.module,
                "state": self.state, "reason": self.reason, "detail": self.detail,
                "interactive": self.interactive, "mode": self.mode, "on_fault": self.on_fault,
                "source": self.source, "draft_revision": self.draft_rev,
                "follows_draft": self.follows_draft,
                "draft_error": self.draft_error,
                "suspend_timeout_min": self.suspend_timeout,
                "suspend_timeout_action": self.suspend_timeout_action,
                "suspended_at": self.suspended_at, "suspend_seconds_left": left,
                "suspended_total": self.suspended_total
                                   + ((now - self.suspended_at) if self.suspended_at else 0),
                "started_at": self.started_at, "finished_at": self.finished_at,
                "cursor": self.cursor, "boot_line": self.boot_line,
                "retry_uid": self.retry_uid(), "retry_edited": self.retry_edited,
                "running_step": (dict(self.running_step, elapsed=now - self.running_step["started_at"])
                                 if self.running_step else None),
                "frozen_len": self.frozen_len(),
                "stop_kind": self.stop_kind, "stop_why": self.stop_why,
                "aborted": self.aborted,
                "next": (self._step_view(self.cursor + 1, self.plan[self.cursor])
                         if self.cursor < len(self.plan) else None),
                "executed": [dict(r) for r in self.records],
                "pending": [self._step_view(p, s)
                            for p, s in enumerate(self.plan[self.cursor:], self.cursor + 1)],
                "warnings": list(self.warnings), "edit_log": list(self.edit_log),
                "allowed_commands": self.allowed_commands(),
                "manual_steps_ok": self.state == "suspended",
                "instances": [k for k, _ in _live_instances(self.context)],
            }


class SessionRegistry:
    """Live and recent sessions by run_id (in memory, like RunRegistry)."""
    KEEP = 20

    def __init__(self):
        self._by_id = collections.OrderedDict()
        self._lock = threading.RLock()

    def add(self, session):
        with self._lock:
            self._by_id[session.run_id] = session
            while len(self._by_id) > self.KEEP:
                self._by_id.popitem(last=False)

    def get(self, run_id):
        with self._lock:
            return self._by_id.get(run_id)

    def current(self):
        with self._lock:
            live = [s for s in self._by_id.values() if s.state != "finished"]
            return live[-1] if live else None

    def for_module(self, module):
        with self._lock:
            live = [s for s in self._by_id.values()
                    if s.state != "finished" and s.module == module]
            return live[-1] if live else None


sessions = SessionRegistry()
