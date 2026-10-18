/* livebuilder.js -- the testbuilder's dual mode.

   OFFLINE EDIT  the editor edits whichever version of the test is ACTIVE:
                 the .py (last-known-good) or its draft. Switching the active
                 version is refused while that test is running.
   LIVE RUN      shown when a run of the selected test is live. The step list
                 comes from the run's session (what ran, what is next, the
                 pending draft queue); every edit is written to the draft and
                 the run picks it up at its next step boundary. The lower
                 right pane shows the run's instances, OCR previews and a
                 send-keys box that runs real steps inside the suspended run.

   Loaded after testbuilder.html's own script; it wraps a few of that
   script's functions (render, selectStep, saveAndSync, addStep, sendSteps,
   updateFields) rather than forking them, so offline editing keeps working
   exactly as before when no draft/live run is involved.
*/
(function () {
"use strict";

const API = "/api/v1";
const POLL_MS = 1500;
const RUN_REFRESH_MS = 3000;       // viewer refresh while a step is executing
const $ = (id) => document.getElementById(id);
const esc = (s) => String(s ?? "").replace(/&/g, "&amp;").replace(/</g, "&lt;")
                                    .replace(/>/g, "&gt;").replace(/"/g, "&quot;");

async function api(path, opts = {}) {
    const init = {headers: {"Content-Type": "application/json"}, ...opts};
    if (init.body && typeof init.body !== "string") init.body = JSON.stringify(init.body);
    const r = await fetch(API + path, init);
    let j = {};
    try { j = await r.json(); } catch (e) {}
    if (!r.ok || j.error) {
        const msg = (j.error && j.error.message) || `${r.status} ${r.statusText}`;
        const err = new Error(msg); err.status = r.status; throw err;
    }
    return j.data;
}

const Live = {
    on: false,              // showing the live view
    userOffline: null,      // run_id the user explicitly left live view for
    session: null,          // latest session snapshot for the SELECTED test
    draft: null,            // draft summary (null = no draft)
    draftDoc: null,         // full draft payload (steps) when loaded
    running: false,         // a run of the selected test is live
    selUid: null,           // selected step in the live view
    instances: [],          // run instances (liveview entries)
    instSel: null,          // selected instance id ("run:box86_1")
    keys: [],               // queued special keys
    shotKey: null,          // what the viewer last captured for (hold policy)
    lastShotAt: 0,
    ocr: null,              // last OCR preview result shown in the viewer
    cropDrag: null,
};
window.Live = Live;

const mod = () => DOM.testListSel.value;

/* ── wrapping the editor ─────────────────────────────────────────────── */

const orig = {
    render: window.render, selectStep: window.selectStep, saveAndSync: window.saveAndSync,
    addStep: window.addStep, sendSteps: window.sendSteps, updateFields: window.updateFields,
    selectAction: window.selectAction, renderConfigEditor: window.renderConfigEditor,
};

window.render = function () {
    if (Live.on && Live.session) return renderLiveTimeline();
    orig.render();
    decorateOfflineTimeline();
};
window.selectStep = function (i) {
    if (Live.on && Live.session) return;          // live rows use their own handler
    return orig.selectStep(i);
};
window.saveAndSync = function () {
    if (Live.on && Live.session) return saveLiveStep();
    return orig.saveAndSync();
};
window.addStep = function () {
    if (Live.on && Live.session) return addLiveStep();
    return orig.addStep();
};
window.sendSteps = function () {
    if (!Live.on && Live.draft && Live.draft.active) return saveDraftOffline();
    return orig.sendSteps();
};
window.updateFields = function () {
    Live.selUid = null; Live.ocr = null; Live.session = null; Live.on = false;
    $("lv-ctl").style.display = "none";
    $("lv-rightcol").classList.remove("live");
    $("lv-ocr-bar").style.display = "none";
    $("lv-faultline").style.display = "none";
    $("lv-warnline").style.display = "none";
    $("lv-btn-live").classList.remove("on");
    $("lv-btn-offline").classList.add("on");
    $("lv-edit-note").textContent = "";
    const p = orig.updateFields();
    Promise.resolve(p).then(() => loadDraft()).then(() => { poll(true); });
    return p;
};
window.selectAction = function (name, el) {
    orig.selectAction(name, el);
    // OCR preview needs the live instance, so it only exists in live mode
    $("lv-ocr-bar").style.display = (Live.on && name === "test_ocrwordsearch") ? "" : "none";
};
window.renderConfigEditor = function (config) {
    orig.renderConfigEditor(config);
    lockConfigIfDraft();
};

/* ── draft / version (offline) ───────────────────────────────────────── */

async function loadDraft() {
    Live.draft = null; Live.draftDoc = null;
    if (!mod()) return renderVersionBar();
    try {
        const d = await api(`/tests/${encodeURIComponent(mod())}/draft`);
        Live.draft = d.draft; Live.draftDoc = d.draft ? d : null;
    } catch (e) { /* no config / not found: leave null */ }
    renderVersionBar();
    if (Live.draft && Live.draft.active && !Live.on) useDraftSteps();
}

function useDraftSteps() {
    if (!Live.draftDoc) return;
    State.steps = JSON.parse(JSON.stringify(Live.draftDoc.steps || []));
    State.expanded = Live.draftDoc.steps_expanded || State.steps;
    State.hasReferences = State.steps.some(s => isPlanRef(s));
    State.selectedIndex = null;
    orig.render(); decorateOfflineTimeline();
    lockConfigIfDraft();
}

function renderVersionBar() {
    const sel = $("lv-version"), revs = $("lv-revisions"), badge = $("lv-draft-badge");
    const d = Live.draft;
    sel.innerHTML = `<option value="lkg">Last-known-good (.py)</option>`
        + (d ? `<option value="draft">Draft r${d.revision}</option>` : "");
    sel.value = (d && d.active) ? "draft" : "lkg";
    sel.disabled = !d || Live.running;
    sel.title = Live.running ? "can't switch while this test is running" : "";
    revs.innerHTML = `<option value="">Revisions</option>`
        + (d ? (d.revisions || []).slice().reverse().map(r => `<option value="${r}">revert to r${r}</option>`).join("") : "");
    revs.disabled = !d || !(d.revisions || []).length;
    $("lv-compare").disabled = !d;
    $("lv-promote").disabled = !d || Live.running;
    $("lv-discard").disabled = !d || Live.running;
    if (d) {
        const bits = [d.active ? "draft ACTIVE" : "draft inactive", `r${d.revision}`];
        if (d.matches_lkg) bits.push("same as .py");
        if (d.lkg_changed) bits.push(".py changed since draft forked!");
        badge.textContent = bits.join(" · ");
        badge.className = "lv-badge" + (d.lkg_changed ? " warn" : "");
        badge.style.display = "";
    } else badge.style.display = "none";
}

Live.chooseVersion = async function (v) {
    if (!Live.draft) return;
    try {
        await api(`/tests/${encodeURIComponent(mod())}/draft/activate`,
                  {method: "POST", body: {active: v === "draft"}});
    } catch (e) { alert(e.message); }
    await reloadTest();
};
Live.revertTo = async function (rev) {
    $("lv-revisions").value = "";
    if (!rev || !confirm(`Save revision ${rev}'s steps as a new draft revision?`)) return;
    try {
        await api(`/tests/${encodeURIComponent(mod())}/draft/revert`,
                  {method: "POST", body: {revision: Number(rev), expected_revision: Live.draft.revision}});
    } catch (e) { return alert(e.message); }
    await reloadTest();
};
Live.promote = async function () {
    if (!confirm("Write the draft's steps into the .py (the new last-known-good) and remove the draft?")) return;
    try {
        await api(`/tests/${encodeURIComponent(mod())}/draft/promote`,
                  {method: "POST", body: {expected_revision: Live.draft.revision}});
    } catch (e) { return alert(e.message); }
    await reloadTest();
};
Live.discard = async function () {
    if (!confirm("Delete the draft and all its revisions?")) return;
    try { await api(`/tests/${encodeURIComponent(mod())}/draft`, {method: "DELETE"}); }
    catch (e) { return alert(e.message); }
    await reloadTest();
};
async function reloadTest() { await window.updateFields(); }

async function saveDraftOffline() {
    try {
        const d = await api(`/tests/${encodeURIComponent(mod())}/draft`, {method: "PUT",
            body: {steps: State.steps, expected_revision: Live.draft.revision, note: "offline edit"}});
        Live.draft = d.draft; Live.draftDoc = d;
        renderVersionBar();
        const v = d.validation || {};
        alert(`Saved to draft r${d.draft.revision}` + (v.has_errors ? `\n\nValidator:\n${v.summary}` : ""));
    } catch (e) { alert("Draft save failed: " + e.message); }
}

function lockConfigIfDraft() {
    const box = DOM.configContainer;
    if (!box) return;
    const old = document.getElementById("lv-cfg-lock");
    if (old) old.remove();
    const locked = (Live.draft && Live.draft.active && !Live.on) || (Live.on && Live.session);
    box.querySelectorAll("input").forEach(i => { i.disabled = !!locked; });
    if (locked) {
        const n = document.createElement("div");
        n.id = "lv-cfg-lock"; n.className = "lv-cfg-lock";
        n.textContent = Live.on
            ? "Build configuration is frozen for the live run (it was set before the emulator started)."
            : "Build configuration lives in the .py — switch Active version to Last-known-good to edit it. The draft holds steps only.";
        box.prepend(n);
    }
}

function decorateOfflineTimeline() {
    // pause checkbox wording: it is THE breakpoint now
    DOM.stepsContainer.querySelectorAll('.step input[type=checkbox]').forEach(cb => {
        cb.title = "Breakpoint: the run suspends before this step";
    });
}

Live.compare = function () {
    if (!Live.draftDoc) return;
    fetch(`${API}/tests/${encodeURIComponent(mod())}/steps`).then(r => r.json()).then(j => {
        const lkg = (j.data && j.data.steps) || [];
        const dft = Live.draftDoc.steps || [];
        const label = (s) => s ? esc((s.use ? `use ${s.use}` : s.matrix ? `matrix ${s.matrix}` : s.action)
                           + (s.title ? ` — ${s.title}` : "") + (s.pause_on ? "  ⏸" : "")) : "";
        const key = (s) => s ? JSON.stringify({...s, _uid: undefined}) : "";
        // Align by step id, not position: a draft entry "e<N>" came from the
        // .py's entry N (drafthelper seeds them that way); "n..." ids are new.
        const used = new Set(); let rows = "", nchg = 0;
        const row = (cls, i, a, b) => { if (cls) nchg++;
            return `<tr class="${cls}"><td>${i}</td><td class="l">${label(a)}</td><td class="r">${label(b)}</td></tr>`; };
        const emitDeletedUpTo = (n) => { for (let j = 1; j <= n; j++) if (!used.has(j)) {
            used.add(j); rows += row("del", j, lkg[j - 1], null); } };
        dft.forEach(b => {
            const m = /^e(\d+)$/.exec(b._uid || "");
            const j = m ? Number(m[1]) : 0;
            if (j && j <= lkg.length && !used.has(j)) {
                emitDeletedUpTo(j - 1); used.add(j);
                rows += row(key(lkg[j - 1]) !== key(b) ? "diff" : "", j, lkg[j - 1], b);
            } else rows += row("add", "+", null, b);
        });
        emitDeletedUpTo(lkg.length);
        let m = $("lv-cmp");
        if (!m) { m = document.createElement("div"); m.id = "lv-cmp"; document.body.appendChild(m); }
        m.innerHTML = `<div id="lv-cmp-head"><b style="flex:1">Last-known-good vs draft r${Live.draft.revision}</b>
            <span style="font-size:12px;color:#777">${nchg} change(s) — yellow: changed · green: only on that side</span>
            <button onclick="document.getElementById('lv-cmp').remove()">Close</button></div>
            <div id="lv-cmp-body"><table><tr><th>#</th><th>.py (${lkg.length})</th><th>draft (${dft.length})</th></tr>${rows}</table></div>`;
    });
};

/* ── starting a run ──────────────────────────────────────────────────── */

Live.startRun = async function () {
    if (!mod()) return alert("Select a test first");
    const body = {test: mod(), mode: $("lv-run-mode").value};
    if ($("lv-run-fault").value) body.on_fault = $("lv-run-fault").value;
    if ($("lv-run-timeout").value !== "") body.suspend_timeout = Number($("lv-run-timeout").value);
    try {
        await api("/runs", {method: "POST", body});
        Live.userOffline = null;
        setTimeout(() => poll(true), 800);
    } catch (e) { alert("Run not started: " + e.message); }
};

/* ── polling the session ─────────────────────────────────────────────── */

let pollTimer = null, polling = false;
async function poll(force) {
    if (polling) return;
    polling = true;
    let snap = null;
    try {
        const cur = await api("/runs/current");
        if (cur.session && cur.session.module === mod() && cur.session.state !== "finished")
            snap = await api(`/runs/${cur.session.run_id}/session`);
    } catch (e) { snap = null; }
    polling = false;

    const mine = snap && snap.module === mod() && snap.state !== "finished" ? snap : null;
    const was = Live.session;
    Live.session = mine;
    Live.running = !!mine;
    $("lv-btn-live").disabled = !mine;
    $("lv-btn-live").title = mine ? "" : "no run of this test is live";

    if (mine && !Live.on && Live.userOffline !== mine.run_id) setMode(true, true);
    if (!mine && Live.on) {
        // the run ended: back to offline, showing whatever version is active
        setMode(false, true);
        await loadDraft();
    }
    if (Live.on && mine) {
        const changed = !was || was.cursor !== mine.cursor || was.state !== mine.state
            || was.executed.length !== mine.executed.length
            || was.draft_revision !== mine.draft_revision || force;
        renderControlBar();
        if (changed) { renderLiveTimeline(); loadInstances(); }
        viewerTick(changed);
    } else if (force) {
        loadInstances();
    }
    renderVersionBar();
    clearTimeout(pollTimer);
    pollTimer = setTimeout(poll, POLL_MS);
}

function setMode(live, auto) {
    if (live && !Live.session) return;
    Live.on = !!live;
    $("lv-rightcol").classList.toggle("live", Live.on);
    if (!Live.on) Live.ocr = null;
    if (!auto) Live.userOffline = live ? null : (Live.session && Live.session.run_id);
    $("lv-btn-live").classList.toggle("on", Live.on);
    $("lv-btn-offline").classList.toggle("on", !Live.on);
    $("lv-ctl").style.display = Live.on ? "" : "none";
    $("lv-faultline").style.display = "none";
    $("lv-warnline").style.display = "none";
    document.querySelectorAll("#plan-bar, #plan-edit-bar").forEach(e => { if (Live.on) e.style.display = "none"; });
    DOM.moveTarget.disabled = Live.on;
    document.querySelector('[data-testid="move-step-btn"]').disabled = Live.on;
    document.querySelector('[data-testid="save-sync-btn"]').textContent = Live.on ? "Save to draft" : "Save & Sync";
    Live.selUid = null;
    lockConfigIfDraft();
    if (Live.on) { renderControlBar(); renderLiveTimeline(); loadInstances(); viewerTick(true);
                   requestAnimationFrame(fitImage); }
    else {
        if (Live.draft && Live.draft.active) useDraftSteps();
        else if (!auto) window.updateFields();
        else { orig.render(); decorateOfflineTimeline(); }
        renderParamTable({}, {}); renderStepDescription("");
        $("lv-edit-note").textContent = "";
    }
}
Live.setMode = (live) => setMode(live, false);

/* ── control bar ─────────────────────────────────────────────────────── */

const REASONS = {breakpoint: "BREAKPOINT", step: "SINGLE-STEP", fault: "FAULT", run_to: "RUN-TO reached",
                 user: "PAUSED", draft_invalid: "DRAFT INVALID"};

function renderControlBar() {
    const s = Live.session; if (!s) return;
    const ctl = $("lv-ctl");
    ctl.classList.toggle("running", s.state === "running");
    // numbered the way the report will number it: after everything executed
    const next = s.next ? `step ${s.executed.length + 1}: ${s.next.title || s.next.action}` : "end of test";
    $("lv-state").textContent = s.state === "suspended"
        ? `SUSPENDED · ${REASONS[s.reason] || s.reason} · before ${next}`
        : s.running_step
          ? `RUNNING step ${s.running_step.ordinal}: ${s.running_step.title} · ${Math.floor(s.running_step.elapsed)}s`
            + ` · ${s.mode === "step" ? "single-step: suspends after this step" : "auto"}`
          : `RUNNING · ${s.mode === "step" ? "single-step" : "auto"}`;
    const allowed = new Set(s.allowed_commands || []);
    ctl.querySelectorAll("button[data-cmd]").forEach(b => {
        b.disabled = !allowed.has(b.dataset.cmd);
        if (b.dataset.cmd === "run_to") b.disabled = b.disabled || !pendingSelected();
    });
    const t = s.suspend_seconds_left;
    $("lv-timer").textContent = s.state !== "suspended" ? `timeout ${s.suspend_timeout_min ? s.suspend_timeout_min + "m" : "∞"}`
        : (t == null ? "timeout ∞" : `timeout ${Math.floor(t / 60)}:${String(Math.floor(t % 60)).padStart(2, "0")} → ${s.suspend_timeout_action}`);

    const fl = $("lv-faultline");
    if (s.state === "suspended" && s.detail) {
        // headline + the verdict line; the full log is in the step's Step log
        const lines = s.detail.split("\n").map(x => x.trim()).filter(Boolean);
        const tail = lines.slice(1).filter(x => !/^\[?'?(Crop|OCR|Total) /.test(x) && !x.startsWith("['")).slice(-2);
        fl.textContent = [lines[0], ...tail].join("\n") + "   (click the failed step for its full log)";
        fl.style.display = "";
    } else fl.style.display = "none";
    const wl = $("lv-warnline");
    const w = (s.warnings || []).slice(-1)[0];
    if (!s.follows_draft) {
        wl.textContent = "This run uses the .py while an inactive draft exists — live edits are off "
            + "(they would go to a draft this run isn't using). Make the draft active, or discard it, and re-run.";
        wl.style.display = "";
    } else if (w) { wl.textContent = `draft r${w.revision}: ${w.ignored.join(", ")} — ${w.message}`; wl.style.display = ""; }
    else if (s.retry_edited) { wl.textContent = "the step that just ran was edited in the draft — Retry runs the edited version"; wl.style.display = ""; }
    else wl.style.display = "none";
}

function pendingSelected() {
    const s = Live.session;
    return s && Live.selUid && s.pending.some(p => p.uid === Live.selUid);
}

document.addEventListener("click", async (e) => {
    const b = e.target.closest("#lv-ctl button");
    if (!b || !Live.session) return;
    const body = {};
    if (b.dataset.cmd) {
        body.command = b.dataset.cmd;
        if (body.command === "run_to") body.uid = Live.selUid;
        if (body.command === "stop" && !confirm("Stop: skip the remaining steps and run teardown steps?")) return;
        if (body.command === "abandon" && !confirm("Abandon: skip everything left, including teardown, and leave instances running?")) return;
    } else if (b.dataset.timeout !== undefined) {
        const s = Live.session;
        const add = Number(b.dataset.timeout);
        const left = s.suspend_seconds_left != null ? s.suspend_seconds_left / 60 : (s.suspend_timeout_min || 0);
        body.command = "set_timeout";
        body.minutes = add === 0 ? 0 : Math.ceil(left + add);
    } else return;
    try { Live.session = await api(`/runs/${Live.session.run_id}/control`, {method: "POST", body}); }
    catch (err) { alert(err.message); }
    renderControlBar(); setTimeout(() => poll(true), 300);
});

/* ── live timeline ───────────────────────────────────────────────────── */

function renderLiveTimeline() {
    const s = Live.session; if (!s) return;
    const box = DOM.stepsContainer;
    box.innerHTML = "";
    const retryUid = s.retry_uid;
    const planExec = s.executed;
    planExec.forEach(r => {
        const d = document.createElement("div");
        const isRetry = r.uid === retryUid && !r.superseded_by && r === lastPlanRecord(planExec);
        d.className = `step lv-done ${["FAIL", "ERROR"].includes(r.status) ? "lv-fail" : ""} ${isRetry ? "lv-retry" : ""}`
            + (Live.selUid === r.uid && Live.selRecord === r.ordinal ? " selected" : "");
        const frozen = r.manual || !isRetry || !s.follows_draft;
        d.innerHTML = `<div class="dot"></div><span class="lv-title">${r.ordinal}. ${esc(r.title || r.action)}${r.manual ? " <i>(manual)</i>" : r.recorded ? " <i>(recorded)</i>" : ""}</span>`
            + `<span class="lv-st ${esc(r.status)}">${esc(r.status)}${r.superseded_by ? "↺" : ""}</span>`
            + (frozen ? `<span class="lv-lock" title="already ran — frozen for this run">ran</span>`
                      : `<span class="lv-lock edit" title="editable: Retry re-runs it with your edits">retry</span>`);
        d.onclick = () => selectLive(r.uid, r, !frozen);
        box.appendChild(d);
    });
    const div = document.createElement("div");
    div.className = "lv-divider";
    div.textContent = s.pending.length ? `▼ next · ${s.pending.length} pending (draft queue${s.source === "draft" ? " r" + s.draft_revision : ""})` : "▼ end of test";
    box.appendChild(div);
    const lockAll = !s.follows_draft;
    s.pending.forEach((p, i) => {
        p = lockAll ? {...p, frozen: true} : p;
        const d = document.createElement("div");
        d.className = `step ${i === 0 ? "lv-next" : ""} ${p.uid.startsWith("n") ? "lv-new" : ""}`
            + (Live.selUid === p.uid ? " selected" : "");
        d.innerHTML = `<input type="checkbox" ${p.pause_on ? "checked" : ""} ${lockAll ? "disabled" : ""} title="Breakpoint: suspend before this step" style="margin-right:6px">`
            + `<div class="dot"></div><span class="lv-title">${s.executed.length + i + 1}. ${esc(p.title || p.action)}</span>`
            + (p.frozen ? `<span class="lv-lock" title="at/before the emulator start — frozen for this run">boot</span>`
                        : `<button class="btn-delete" title="delete from the draft">✖</button>`);
        const cb = d.querySelector("input");
        cb.onclick = (e) => { e.stopPropagation(); draftOps([{op: "set_pause", uid: p.uid, pause_on: cb.checked}]); };
        const del = d.querySelector("button");
        if (del) del.onclick = (e) => {
            e.stopPropagation();
            if (confirm(`Delete "${p.title || p.action}" from the draft?`)) draftOps([{op: "delete", uid: p.uid}]);
        };
        d.onclick = () => selectLive(p.uid, p, !p.frozen);
        box.appendChild(d);
    });
    const tools = document.createElement("div");
    tools.className = "lv-tools";
    tools.innerHTML = `<button id="lv-trunc" ${pendingSelected() ? "" : "disabled"} title="delete the selected step and everything after it (teardown steps kept)">Delete from selected</button>`;
    tools.querySelector("button").onclick = () => {
        const p = Live.session.pending.find(x => x.uid === Live.selUid);
        if (p && confirm(`Delete "${p.title || p.action}" and everything after it from the draft? Teardown steps are kept.`))
            draftOps([{op: "truncate", uid: p.uid, keep_teardown: true}]);
    };
    box.appendChild(tools);
    DOM.insertLabel.textContent = Live.selUid ? "Add Step inserts after the selected step"
                                              : "Add Step inserts before the next step";
}

function lastPlanRecord(recs) {
    for (let i = recs.length - 1; i >= 0; i--) if (!recs[i].manual) return recs[i];
    return null;
}

function selectLive(uid, step, editable) {
    Live.selUid = uid;
    Live.selRecord = step.ordinal;
    Live.selEditable = editable;
    Live.selStep = step;
    orig.selectAction(step.action);
    $("lv-ocr-bar").style.display = (step.action === "test_ocrwordsearch") ? "" : "none";
    renderParamTable(State.schema[step.action] || {}, step.param || {});
    renderStepDescription(step.description || "");
    const log = $("lv-steplog");
    if (step.ordinal) {
        $("lv-steplog-name").textContent = `#${step.ordinal} ${step.status}`;
        $("lv-steplog-text").textContent = step.output || "(no output)";
        log.style.display = "";
    } else log.style.display = "none";
    DOM.paramTableBody.querySelectorAll("input").forEach(i => { i.disabled = !editable; });
    $("lv-edit-note").textContent = editable
        ? (step.ordinal ? "edits apply when you Retry this step" : "edits go to the draft queue")
        : "read-only: this step is frozen for the live run";
    renderLiveTimeline(); renderControlBar();
    showCropFromParams();
}

async function draftOps(ops, note) {
    try {
        await api(`/tests/${encodeURIComponent(mod())}/draft/ops`, {method: "POST", body: {ops, note}});
    } catch (e) { alert(e.message); }
    // the session reconciles the draft within ~1s while suspended
    setTimeout(() => poll(true), 1200);
    loadDraft();
}

function saveLiveStep() {
    if (!Live.selUid || !Live.selEditable) return alert("Select a pending step (or the step that just ran, to Retry it) to save.");
    const prev = Live.selStep || {};
    const step = {action: State.currentSelectedAction || prev.action, param: getParams(),
                  description: getStepDescription()};
    if (prev.title) step.title = prev.title;
    if (prev.pause_on) step.pause_on = true;
    if (prev.teardown) step.teardown = true;
    draftOps([{op: "replace", uid: Live.selUid, step}]);
}

function addLiveStep() {
    if (!State.currentSelectedAction) return alert("Select an action first");
    const s = Live.session;
    const step = {action: State.currentSelectedAction, param: getParams(), description: getStepDescription()};
    let op;
    if (Live.selUid && (s.pending.some(p => p.uid === Live.selUid && !p.frozen) || Live.selUid === s.retry_uid)) {
        op = {op: "insert", after: Live.selUid, steps: [step]};
    } else {
        const last = lastPlanRecord(s.executed);
        if (last) op = {op: "insert", after: last.uid, steps: [step]};
        else if (s.pending.length) {
            const firstFree = s.pending.find(p => !p.frozen);
            if (!firstFree) return alert("Nothing after the emulator start yet — add steps once it has booted.");
            op = {op: "insert", before: firstFree.uid, steps: [step]};
        } else op = {op: "insert", at: "end", steps: [step]};
    }
    draftOps([op]);
}

/* ── instances viewer ────────────────────────────────────────────────── */

async function loadInstances() {
    let list = [];
    try { list = (await api("/instances")).instances || []; } catch (e) {}
    // only this run's instances (the ones in its context)
    Live.instances = list.filter(i => i.source === "run");
    if (!Live.instances.some(i => i.id === Live.instSel))
        Live.instSel = (Live.instances.find(i => (i.capabilities || []).includes("screenshot")) || Live.instances[0] || {}).id || null;
    const tabs = $("lv-tabs");
    tabs.innerHTML = Live.instances.map(i =>
        `<button class="${i.id === Live.instSel ? "on" : ""}" data-iid="${esc(i.id)}" title="${esc(i.kind)} pid ${esc(i.pid)}">${esc(i.label)}</button>`).join("");
    tabs.querySelectorAll("button").forEach(b => b.onclick = () => {
        Live.instSel = b.dataset.iid; Live.ocr = null; loadInstances(); Live.refreshShot(true);
    });
    const inst = currentInstance();
    const caps = (inst && inst.capabilities) || [];
    $("lv-noimg").style.display = inst ? "none" : "";
    $("lv-imgwrap").style.display = inst && caps.includes("screenshot") ? "" : "none";
    $("lv-tty").style.display = inst && caps.includes("tty") && !caps.includes("screenshot") ? "" : "none";
    renderKeysBox();
}

function currentInstance() { return Live.instances.find(i => i.id === Live.instSel) || null; }

function viewerTick(changed) {
    // Running: refresh on an interval. Suspended/single-step: ONE capture when
    // the run parks (or moves on), then hold until the step changes again --
    // same rule the GIF recorder follows. Manual refresh always works.
    const s = Live.session;
    if (!s) return;
    const key = `${s.run_id}:${s.cursor}:${s.executed.length}:${s.state}`;
    if (s.state === "running") {
        if (Date.now() - Live.lastShotAt > RUN_REFRESH_MS) Live.refreshShot(false);
        $("lv-view-status").textContent = "live · refreshing while the step runs";
    } else {
        if (key !== Live.shotKey) { Live.shotKey = key; Live.ocr = null; Live.refreshShot(false); }
        $("lv-view-status").textContent = "held · suspended (refreshes when the step moves)";
    }
}

Live.refreshShot = async function (manual) {
    const inst = currentInstance();
    Live.lastShotAt = Date.now();
    if (!inst) return;
    const caps = inst.capabilities || [];
    if (caps.includes("screenshot")) {
        if (manual) Live.ocr = null;
        if (Live.ocr) return;                      // an OCR preview is on screen
        const img = $("lv-img");
        const url = `${API}/instances/${encodeURIComponent(inst.id)}/screenshot?_=${Date.now()}`;
        await new Promise(res => {
            const tmp = new Image();
            tmp.onload = () => { img.src = tmp.src; fitImage(); res(); };
            tmp.onerror = () => res();
            tmp.src = url;
        });
        $("lv-ocr").style.display = "none";
        showCropFromParams();
    } else if (caps.includes("tty")) {
        try {
            const d = await api(`/instances/${encodeURIComponent(inst.id)}/tty?tail=200`);
            $("lv-tty").textContent = d.text || "";
        } catch (e) { $("lv-tty").textContent = e.message; }
    }
};

function fitImage() {
    const img = $("lv-img"), box = $("lv-screen");
    if (!img.naturalWidth) return;
    const bw = box.clientWidth - 8, bh = box.clientHeight - 8;
    const k = Math.min(bw / img.naturalWidth, bh / img.naturalHeight, 1.5);
    img.style.width = Math.max(1, Math.floor(img.naturalWidth * k)) + "px";
    img.style.height = Math.max(1, Math.floor(img.naturalHeight * k)) + "px";
    showCropFromParams();
}
window.addEventListener("resize", fitImage);

/* ── OCR preview + crop box ──────────────────────────────────────────── */

Live.previewOcr = async function () {
    const note = $("lv-ocr-bar-note");
    note.textContent = "reading…";
    const param = getParams();
    const inst = currentInstance();
    if (!param.name && inst && inst.id.startsWith("run:")) param.name = inst.id.slice(4);
    try {
        const d = await api(`/tests/${encodeURIComponent(mod())}/ocr-preview`, {method: "POST", body: {param}});
        const r = (d.results || [])[0];
        if (!r) throw new Error("no result");
        Live.ocr = r;
        $("lv-img").src = r.image; await new Promise(res => { $("lv-img").onload = res; setTimeout(res, 500); });
        $("lv-img").onload = null;
        fitImage();
        const v = $("lv-ocr-verdict");
        v.textContent = r.verdict;
        v.className = r.found ? "pass" : r.failphrase_seen ? "fail" : "wait";
        const phrases = [r.resolved.successphrase, r.resolved.failphrase, r.resolved.whilephrase].filter(Boolean);
        let html = esc(r.text);
        phrases.forEach(p => {
            const re = new RegExp(p.replace(/[.*+?^${}()|[\]\\]/g, "\\$&").replace(/&/g, "&amp;").replace(/</g, "&lt;"), "gi");
            html = html.replace(re, m => `<mark>${m}</mark>`);
        });
        $("lv-ocr-text").innerHTML = html;
        $("lv-ocr-meta").textContent = `${r.label} · scale ${r.scale}× · ${r.image_size ? r.image_size.join("×") : ""} · drag on the image to set the crop box`;
        $("lv-ocr").style.display = "";
        $("lv-view-status").textContent = "OCR preview (Refresh returns to the live screen)";
        note.textContent = "";
    } catch (e) { note.textContent = e.message; }
};

function scaleOf() {
    // crop params are NATIVE pixels; captures are native × instance scale
    return (Live.ocr && Live.ocr.scale) || 1;
}

function showCropFromParams() {
    const box = $("lv-crop"), img = $("lv-img");
    const val = (k) => { const el = document.getElementById(`p_${k}`); return el ? el.value : ""; };
    const v = ["startx", "starty", "stopx", "stopy"].map(k => parseFloat(val(k)));
    const isOcr = State.currentSelectedAction === "test_ocrwordsearch";
    if (!isOcr || v.some(isNaN) || !img.naturalWidth || !Live.ocr) { box.style.display = "none"; return; }
    const k = (img.clientWidth / img.naturalWidth) * scaleOf();
    box.style.left = (v[0] * k) + "px"; box.style.top = (v[1] * k) + "px";
    box.style.width = ((v[2] - v[0]) * k) + "px"; box.style.height = ((v[3] - v[1]) * k) + "px";
    box.style.display = "block";
}
document.addEventListener("input", (e) => { if (e.target.closest && e.target.closest("#param-table")) showCropFromParams(); });

(function setupCropDrag() {
    const wrap = $("lv-imgwrap"), img = $("lv-img"), draw = $("lv-crop-draw");
    const pt = (e) => { const r = img.getBoundingClientRect();
        return [Math.max(0, Math.min(r.width, e.clientX - r.left)), Math.max(0, Math.min(r.height, e.clientY - r.top))]; };
    wrap.addEventListener("mousedown", (e) => {
        if (!Live.ocr || State.currentSelectedAction !== "test_ocrwordsearch") return;
        e.preventDefault();
        Live.cropDrag = pt(e);
        wrap.classList.add("cropping");
    });
    window.addEventListener("mousemove", (e) => {
        if (!Live.cropDrag) return;
        const [x0, y0] = Live.cropDrag, [x1, y1] = pt(e);
        Object.assign(draw.style, {display: "block", left: Math.min(x0, x1) + "px", top: Math.min(y0, y1) + "px",
                                   width: Math.abs(x1 - x0) + "px", height: Math.abs(y1 - y0) + "px"});
    });
    window.addEventListener("mouseup", (e) => {
        if (!Live.cropDrag) return;
        const [x0, y0] = Live.cropDrag, [x1, y1] = pt(e);
        Live.cropDrag = null; wrap.classList.remove("cropping"); draw.style.display = "none";
        if (Math.abs(x1 - x0) < 4 || Math.abs(y1 - y0) < 4) return;
        const k = (img.naturalWidth / img.clientWidth) / scaleOf();   // display px -> native px
        const set = (key, v) => { const el = document.getElementById(`p_${key}`); if (el && !el.disabled) el.value = Math.round(v); };
        set("startx", Math.min(x0, x1) * k); set("starty", Math.min(y0, y1) * k);
        set("stopx", Math.max(x0, x1) * k); set("stopy", Math.max(y0, y1) * k);
        showCropFromParams();
        $("lv-ocr-bar-note").textContent = "crop set (unsaved) — Preview OCR to check it";
    });
})();

/* ── send keys (runs a real step inside the suspended run) ───────────── */

const QEMU_KEY = {Return: "ret", Esc: "esc", Tab: "tab", BackSpace: "backspace", Up: "up", Down: "down",
                  Left: "left", Right: "right", F1: "f1", F2: "f2", F3: "f3", F10: "f10", space: "spc"};

function renderKeysBox() {
    const inst = currentInstance();
    const kind = inst ? inst.kind : "";
    const ok = !!(Live.session && Live.session.manual_steps_ok && inst && ["86box", "dosbox", "qemu", "basilisk"].includes(kind));
    $("lv-keys").classList.toggle("disabled", !ok);
    $("lv-keys-send").disabled = !ok;
    $("lv-keys-kind").textContent = inst ? `→ ${inst.label}` : "";
    if (ok && /^available while|^no key step/.test($("lv-keys-status").textContent)) $("lv-keys-status").textContent = "";
    if (!ok) $("lv-keys-status").textContent = !Live.session ? "available while a run of this test is suspended"
        : Live.session.state !== "suspended" ? "available while the run is suspended (Pause / breakpoint / single-step)"
        : inst ? `no key step for ${kind} instances` : "";
    $("lv-keys-record").disabled = !(Live.session && Live.session.follows_draft);
    $("lv-keys-chips").innerHTML = Live.keys.map((k, i) => `<span data-i="${i}" title="click to remove">${esc(k)}</span>`).join("");
    $("lv-keys-chips").querySelectorAll("span").forEach(sp => sp.onclick = () => { Live.keys.splice(+sp.dataset.i, 1); renderKeysBox(); });
}
document.addEventListener("click", (e) => {
    const b = e.target.closest(".lv-kb button");
    if (!b) return;
    Live.keys.push(b.dataset.k); renderKeysBox();
});
Live.clearKeys = () => { Live.keys = []; $("lv-keys-text").value = ""; renderKeysBox(); };

function keySteps(inst, text, keys, shot) {
    const name = inst.id.startsWith("run:") ? inst.id.slice(4) : inst.label;
    if (inst.kind === "qemu" || inst.kind === "basilisk") {
        const out = [];
        if (text) out.push({action: "test_sendkeyboardinput", title: `manual: type "${text}"`,
                            param: {name, inputstring: text, takeascreenshot: shot && !keys.length}});
        if (keys.length) out.push({action: "test_sendspecialkeys", title: `manual: ${keys.join(", ")}`,
                                   param: {name, keys: keys.map(k => QEMU_KEY[k] || k.toLowerCase()), takeascreenshot: shot}});
        return out;
    }
    const param = {name, takeascreenshot: shot};
    if (text) param.cmd_text = text;
    if (keys.length) param.special_keys = keys.slice();
    return [{action: "test_sendcommand", title: `manual: ${[text && `"${text}"`, ...keys].filter(Boolean).join(" + ")}`, param}];
}

Live.sendKeys = async function () {
    const inst = currentInstance(), s = Live.session;
    if (!inst || !s) return;
    const text = $("lv-keys-text").value;
    if (!text && !Live.keys.length) return;
    const steps = keySteps(inst, text, Live.keys, $("lv-keys-shot").checked);
    const record = $("lv-keys-record").checked;
    const st = $("lv-keys-status");
    const msgs = [];
    $("lv-keys-send").disabled = true;
    try {
        for (const step of steps) {
            st.textContent = `running ${step.action}…`;
            const d = await api(`/runs/${s.run_id}/adhoc`, {method: "POST", body: {step, record}});
            const rec = (d.result && d.result.record) || {};
            msgs.push(`${rec.status || "?"} · ${step.action}${record ? " · recorded in the draft" : ""}`);
            if (rec.status !== "PASS" && rec.status !== "WARN")
                msgs.push(String((d.result && (d.result.output || d.result.error)) || "").split("\n").slice(-3).join("\n"));
            Live.session = d.session;
        }
        Live.keys = []; $("lv-keys-text").value = "";
        renderKeysBox();
        st.textContent = msgs.join("\n");
    } catch (e) { renderKeysBox(); st.textContent = e.message; }
    $("lv-keys-send").disabled = false;
    Live.ocr = null; Live.refreshShot(true);
    renderLiveTimeline(); renderControlBar();
    if (record) loadDraft();
};

/* ── splitter ────────────────────────────────────────────────────────── */

(function setupSplit() {
    const split = $("lv-split"), pane = $("lv-pane"), col = $("lv-rightcol");
    let drag = null;
    split.addEventListener("mousedown", (e) => { drag = {y: e.clientY, h: pane.getBoundingClientRect().height}; e.preventDefault(); });
    window.addEventListener("mousemove", (e) => {
        if (!drag) return;
        const total = col.getBoundingClientRect().height;
        const h = Math.max(120, Math.min(total - 120, drag.h - (e.clientY - drag.y)));
        pane.style.flex = `0 0 ${h}px`;
        fitImage();
    });
    window.addEventListener("mouseup", () => { drag = null; });
})();

document.addEventListener("DOMContentLoaded", () => {
    // the template's own init() runs on the same event, before this one
    renderKeysBox();
    poll(true);
});
})();
