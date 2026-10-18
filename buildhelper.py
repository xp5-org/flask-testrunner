"""
buildhelper.py
--------------
Build objects: what a tool (is_tool) testlist produced, which git commit it
came from, and which of those builds every later test run actually ran.

Three tables, all in report.sqlite next to the reports they describe:

  build_object   one row per registered build output. Written when a tool
                 run passes and its CONFIG names what it produced
                 ("build_output") and where the source lives ("build_repo"),
                 or by hand from the container page's build tracker. The
                 output file is copied into BUILD_STORE/<md5>/ at
                 registration, because every build of a container usually
                 writes the SAME path (build/regular/src/86Box) -- without a
                 snapshot, "build A" is overwritten the moment build B lands
                 and A/B testing is impossible.

                 Each row also carries the platform the file runs on (sniffed
                 from its magic bytes: ELF/PE/Mach-O, else "any" for disk
                 images, ISOs and the like) and the slot list (A or B) it
                 belongs to -- a "Build A"/"Build B" run files its output
                 into that list, and the tracker can move a build between
                 lists by hand.

  container_ab_pin
                 a user override: this exact build for slot A/B on one
                 platform. Without a pin a slot resolves to the NEWEST build
                 in its list that runs on the host doing the run, so a
                 Windows runner never picks up a Linux ELF (and vice versa)
                 just because it was registered last. Pins are per platform
                 so a Linux runner and a Windows runner sharing this DB
                 don't fight over what "A" means. Server-side so the
                 per-variant RUN A / RUN B buttons, the batch runner and
                 anyone else hitting the API all agree.

  run_build      one row per report: the build the run used, how it was
                 chosen (slot A/B, or NULL for a plain run), and the md5 of
                 the file it actually pointed at. A plain run is still
                 attributed: the file at the variant's CONFIG key is hashed
                 and matched against registered builds, so history groups by
                 commit whether or not A/B was involved.

CONFIG keys (tool testlists):
  build_output  config key (or list of keys) whose resolved value is the
                produced file, e.g. "box_binary".
  build_repo    path of the git working tree it was built from, tokens ok,
                e.g. "{boxsrc_dir}".

A variant consumes a build through the SAME config key: an A/B run
overrides CONFIG[<build_output key>] with the snapshot path for that run
only. A variant without that key cannot be A/B run and is refused, rather
than silently running whatever binary its CONFIG already names.
"""

import hashlib
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import threading
import time

from dbhelper import db

BASE_DIR    = os.path.dirname(os.path.abspath(__file__))
BUILD_STORE = os.path.join(BASE_DIR, "build_store")
SLOTS       = ("A", "B")

# Batch orderings offered next to the A & B selectors.
#   interleave: v1/A, v1/B, v2/A, v2/B ...  (each variant doubled up)
#   grouped:    every variant on A, then every variant on B
#   a / b:      one slot only
AB_ORDERS = ("interleave", "grouped", "a", "b")

# What a build runs on. "any" = not a native executable (disk image, ISO,
# ROM...), usable by every host.
PLATFORMS = ("linux", "windows", "macos", "any")

_TOKEN_RE = re.compile(r"\{[^}]+\}")
_schema_lock = threading.Lock()
_schema_ready = False

# (path, size, mtime_ns) -> md5. Plain runs hash the variant's binary every
# time; an 86Box build is tens of MB and rarely changes between runs.
_md5_cache = {}


class BuildError(Exception):
    """A build can't be registered, selected, or used for a run."""


# ── schema ───────────────────────────────────────────────────────────────────

def _connect():
    conn = sqlite3.connect(db.DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    return conn


def ensure_schema():
    global _schema_ready
    if _schema_ready:
        return
    with _schema_lock:
        if _schema_ready:
            return
        conn = _connect()
        cur = conn.cursor()
        cur.execute("""
            CREATE TABLE IF NOT EXISTS build_object (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                container     TEXT NOT NULL,
                test_id       TEXT,
                variant       TEXT,
                report_id     INTEGER,
                created_at    REAL,
                source        TEXT,
                output_key    TEXT,
                orig_path     TEXT,
                store_path    TEXT,
                filename      TEXT,
                file_size     INTEGER,
                md5           TEXT,
                sha256        TEXT,
                repo_path     TEXT,
                git_sha       TEXT,
                git_branch    TEXT,
                git_dirty     INTEGER,
                git_subject   TEXT,
                label         TEXT,
                hidden        INTEGER DEFAULT 0
            )
        """)
        cur.execute("""CREATE INDEX IF NOT EXISTS ix_build_object_container
                       ON build_object (container, id)""")
        cur.execute("""CREATE INDEX IF NOT EXISTS ix_build_object_md5
                       ON build_object (md5)""")
        cur.execute("""
            CREATE TABLE IF NOT EXISTS container_ab (
                container  TEXT NOT NULL,
                slot       TEXT NOT NULL,
                build_id   INTEGER,
                PRIMARY KEY (container, slot)
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS container_ab_pin (
                container  TEXT NOT NULL,
                slot       TEXT NOT NULL,
                platform   TEXT NOT NULL,
                build_id   INTEGER NOT NULL,
                PRIMARY KEY (container, slot, platform)
            )
        """)
        for col in ("platform TEXT", "slot TEXT"):
            try:
                cur.execute(f"ALTER TABLE build_object ADD COLUMN {col}")
            except sqlite3.OperationalError:
                pass
        # Builds registered before platform existed: sniff their snapshot.
        # One that can't be read (snapshot missing, or on a path only another
        # host can see) stays NULL -- never auto-picked -- and is retried on
        # the next start.
        for r in cur.execute("SELECT id, store_path, orig_path FROM build_object "
                             "WHERE platform IS NULL").fetchall():
            plat = detect_platform(r[1]) or detect_platform(r[2])
            if plat:
                cur.execute("UPDATE build_object SET platform = ? WHERE id = ?",
                            (plat, r[0]))
        # container_ab used to hold THE build for each slot. Those builds
        # become members of that slot's list rather than pins -- a pin to one
        # build regardless of platform is what sent a Linux ELF to a Windows
        # runner.
        for r in cur.execute("SELECT slot, build_id FROM container_ab").fetchall():
            cur.execute("UPDATE build_object SET slot = ? WHERE id = ? AND slot IS NULL",
                        (r[0], r[1]))
        cur.execute("DELETE FROM container_ab")
        cur.execute("""
            CREATE TABLE IF NOT EXISTS container_build_input (
                container  TEXT NOT NULL,
                slot       TEXT NOT NULL,
                overrides  TEXT,
                PRIMARY KEY (container, slot)
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS run_build (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                report_id   INTEGER NOT NULL,
                container   TEXT,
                test_id     TEXT,
                slot        TEXT,
                output_key  TEXT,
                path_used   TEXT,
                md5         TEXT,
                build_id    INTEGER
            )
        """)
        cur.execute("""CREATE INDEX IF NOT EXISTS ix_run_build_container
                       ON run_build (container, report_id)""")
        cur.execute("""CREATE INDEX IF NOT EXISTS ix_run_build_report
                       ON run_build (report_id)""")
        # populate_sqlite() adds this lazily on first insert; container_runs()
        # selects it, so a DB that has never had a report must have it too.
        try:
            cur.execute("ALTER TABLE report ADD COLUMN total_duration REAL")
        except sqlite3.OperationalError:
            pass
        conn.commit()
        conn.close()
        _schema_ready = True


# ── file / git helpers ───────────────────────────────────────────────────────

def resolve_value(value, config):
    """A CONFIG value with its {tokens} resolved against CONFIG itself.
    Returns None if anything is left unresolved."""
    if value is None:
        return None
    resolved = db._resolve_config(str(value), config)
    return None if _TOKEN_RE.search(resolved) else resolved


def file_hashes(path, want_sha256=False):
    """(md5, sha256-or-None) of a file, md5 cached by size+mtime."""
    st = os.stat(path)
    key = (path, st.st_size, st.st_mtime_ns)
    if not want_sha256 and key in _md5_cache:
        return _md5_cache[key], None
    md5 = hashlib.md5()
    sha = hashlib.sha256() if want_sha256 else None
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            md5.update(chunk)
            if sha:
                sha.update(chunk)
    _md5_cache[key] = md5.hexdigest()
    return md5.hexdigest(), (sha.hexdigest() if sha else None)


def _git(repo, *args):
    # safe.directory=*: runs can be a different uid than whoever cloned it,
    # and a refusal here would just read as "not a git repo".
    r = subprocess.run(["git", "-c", "safe.directory=*", "-C", repo, *args],
                       capture_output=True, text=True, timeout=30)
    return r.stdout.strip() if r.returncode == 0 else None


def git_info(repo):
    """sha / branch / dirty / subject of a working tree, or {} if it isn't one.

    dirty matters: the local-tree build compiles uncommitted edits, so its
    HEAD sha alone would claim a commit the binary doesn't match.
    """
    if not repo or not os.path.isdir(repo):
        return {}
    sha = _git(repo, "rev-parse", "HEAD")
    if not sha:
        return {}
    status = _git(repo, "status", "--porcelain", "--untracked-files=no")
    return {
        "git_sha":     sha,
        "git_branch":  _git(repo, "rev-parse", "--abbrev-ref", "HEAD"),
        "git_dirty":   1 if status else 0,
        "git_subject": _git(repo, "log", "-1", "--format=%s"),
    }


def host_platform():
    """The platform this runner process is on, in PLATFORMS' vocabulary."""
    if sys.platform.startswith("win"):
        return "windows"
    if sys.platform == "darwin":
        return "macos"
    return "linux"


def detect_platform(path):
    """What a build output runs on, from its first bytes. None if unreadable.

    Only native executables are tied to a platform; anything else a tool
    registers (disk image, ISO, ROM, .bin) is "any".
    """
    try:
        with open(path, "rb") as f:
            head = f.read(4)
    except (OSError, TypeError):
        return None
    if head == b"\x7fELF":
        return "linux"
    if head[:2] == b"MZ":
        return "windows"
    if head in (b"\xcf\xfa\xed\xfe", b"\xce\xfa\xed\xfe",
                b"\xfe\xed\xfa\xcf", b"\xfe\xed\xfa\xce", b"\xca\xfe\xba\xbe"):
        return "macos"
    return "any"


def runs_on(build, platform):
    return (build or {}).get("platform") in (platform, "any")


def _snapshot(path, md5):
    """Copy a build output into the content-addressed store, once per md5."""
    dest_dir = os.path.join(BUILD_STORE, md5)
    dest = os.path.join(dest_dir, os.path.basename(path))
    if not os.path.exists(dest):
        os.makedirs(dest_dir, exist_ok=True)
        tmp = dest + ".partial"
        shutil.copy2(path, tmp)
        os.replace(tmp, dest)
    return dest


def _output_keys(config):
    keys = config.get("build_output")
    if not keys:
        return []
    return [keys] if isinstance(keys, str) else [k for k in keys if k]


# ── registration ─────────────────────────────────────────────────────────────

def _check_slot(slot):
    if slot is not None and slot not in SLOTS:
        raise BuildError(f"slot must be one of {', '.join(SLOTS)}")


def _insert_build(container, *, path, output_key, source, repo=None,
                  test_id=None, variant=None, report_id=None, label=None,
                  platform=None, slot=None):
    if not path or not os.path.isfile(path):
        raise BuildError(f"build output not found: {path}")
    if platform is not None and platform not in PLATFORMS:
        raise BuildError(f"platform must be one of {', '.join(PLATFORMS)}")
    _check_slot(slot)
    platform = platform or detect_platform(path)
    md5, sha256 = file_hashes(path, want_sha256=True)
    store_path = _snapshot(path, md5)
    info = git_info(repo)
    ensure_schema()
    conn = _connect()
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO build_object
            (container, test_id, variant, report_id, created_at, source,
             output_key, orig_path, store_path, filename, file_size, md5,
             sha256, repo_path, git_sha, git_branch, git_dirty, git_subject,
             label, platform, slot)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
    """, (container, test_id, variant, report_id, time.time(), source,
          output_key, path, store_path, os.path.basename(path),
          os.path.getsize(path), md5, sha256, repo,
          info.get("git_sha"), info.get("git_branch"), info.get("git_dirty"),
          info.get("git_subject"), label, platform, slot))
    build_id = cur.lastrowid
    conn.commit()
    conn.close()
    return build_id


def register_from_run(container, test_id, variant, report_id, config, slot=None):
    """Register what a finished tool run produced. Returns new build ids.

    Only called for a passing run: a failed build usually leaves the
    PREVIOUS binary at the output path, and registering that under this
    run's commit would be exactly the mislabel this table exists to prevent.

    slot: for a build_tool run launched as "Build A"/"Build B" (see
    get_build_inputs/set_build_input below) -- the freshly produced build
    joins that slot's list, and being the newest there, is what the slot
    resolves to on its platform unless someone has pinned another build.
    """
    repo = resolve_value(config.get("build_repo"), config)
    ids = []
    for key in _output_keys(config):
        path = resolve_value(config.get(key), config)
        if not path:
            print(f"[buildhelper] {test_id}: build_output {key!r} does not resolve")
            continue
        try:
            ids.append(_insert_build(container, path=path, output_key=key,
                                     source="run", repo=repo, test_id=test_id,
                                     variant=variant, report_id=report_id,
                                     slot=slot or None))
        except (BuildError, OSError) as e:
            print(f"[buildhelper] {test_id}: could not register {key}: {e}")
    return ids


# ── build-tool inputs (Build A / Build B) ───────────────────────────────────

def get_build_inputs(container):
    """{"A": {CONFIG key: value, ...}, "B": {...}} -- overrides merged onto a
    build_tool's own CONFIG before it runs as "Build A"/"Build B" (e.g.
    target_branch). Slots with nothing configured come back as {}: the tool
    just builds with its CONFIG as written, still tagged into that slot."""
    ensure_schema()
    conn = _connect()
    rows = conn.execute(
        "SELECT slot, overrides FROM container_build_input WHERE container = ?",
        (container,)).fetchall()
    conn.close()
    picked = {r["slot"]: (json.loads(r["overrides"]) if r["overrides"] else {}) for r in rows}
    return {s: picked.get(s, {}) for s in SLOTS}


def set_build_input(container, slot, overrides):
    if slot not in SLOTS:
        raise BuildError(f"slot must be one of {', '.join(SLOTS)}")
    if not isinstance(overrides, dict):
        raise BuildError("overrides must be a CONFIG key/value object")
    ensure_schema()
    conn = _connect()
    conn.execute("""INSERT OR REPLACE INTO container_build_input (container, slot, overrides)
                    VALUES (?,?,?)""",
                 (container, slot, json.dumps(overrides)))
    conn.commit()
    conn.close()
    return get_build_inputs(container)


def register_manual(container, path, output_key, repo=None, label=None,
                    platform=None, slot=None):
    """A build made outside the runner, added from the tracker by hand.
    platform: None = detect from the file."""
    if not output_key:
        raise BuildError("output_key is required: it is the CONFIG key variants read the build from")
    path = os.path.abspath(os.path.expanduser(path or ""))
    repo = os.path.abspath(os.path.expanduser(repo)) if repo else None
    return _insert_build(container, path=path, output_key=output_key,
                         source="manual", repo=repo, label=label or None,
                         platform=platform or None, slot=slot or None)


# ── queries / edits ──────────────────────────────────────────────────────────

def _row(r):
    d = dict(r)
    d["store_exists"] = bool(d.get("store_path") and os.path.exists(d["store_path"]))
    return d


def get_build(build_id):
    ensure_schema()
    conn = _connect()
    r = conn.execute("SELECT * FROM build_object WHERE id = ?", (build_id,)).fetchone()
    conn.close()
    return _row(r) if r else None


def list_builds(container, limit=25, include_hidden=False):
    ensure_schema()
    conn = _connect()
    rows = conn.execute(
        "SELECT * FROM build_object WHERE container = ? "
        + ("" if include_hidden else "AND COALESCE(hidden,0) = 0 ")
        + "ORDER BY id DESC LIMIT ?", (container, int(limit))).fetchall()
    conn.close()
    return [_row(r) for r in rows]


def output_keys_for(container):
    ensure_schema()
    conn = _connect()
    rows = conn.execute("SELECT DISTINCT output_key FROM build_object "
                        "WHERE container = ? AND output_key IS NOT NULL",
                        (container,)).fetchall()
    conn.close()
    return [r[0] for r in rows]


_UNSET = object()


def update_build(build_id, container, *, label=None, hidden=None,
                 slot=_UNSET, platform=None):
    """slot: "A"/"B" moves the build into that list, None takes it out of
    both. platform: corrects a wrong detection."""
    ensure_schema()
    b = get_build(build_id)
    if not b or b["container"] != container:
        raise BuildError(f"build {build_id} is not in {container}")
    sets, args = [], []
    if label is not None:
        sets.append("label = ?")
        args.append(label.strip() or None)
    if hidden is not None:
        sets.append("hidden = ?")
        args.append(1 if hidden else 0)
    if slot is not _UNSET:
        _check_slot(slot)
        sets.append("slot = ?")
        args.append(slot)
    if platform is not None:
        if platform not in PLATFORMS:
            raise BuildError(f"platform must be one of {', '.join(PLATFORMS)}")
        sets.append("platform = ?")
        args.append(platform)
    if sets:
        conn = _connect()
        conn.execute(f"UPDATE build_object SET {', '.join(sets)} WHERE id = ?",
                     (*args, build_id))
        if hidden or platform is not None:
            # A pin must not outlive the build being hidden or turning out
            # to be for another platform. Moving it between lists leaves
            # pins alone: a pin names a build, not a list member.
            conn.execute("DELETE FROM container_ab_pin WHERE container = ? AND build_id = ?",
                         (container, build_id))
        conn.commit()
        conn.close()
    return get_build(build_id)


def slot_candidates(container, slot, platform):
    """The slot's list as seen by one platform, newest first."""
    ensure_schema()
    conn = _connect()
    rows = conn.execute(
        "SELECT * FROM build_object WHERE container = ? AND slot = ? "
        "AND platform IN (?, 'any') AND COALESCE(hidden,0) = 0 "
        "ORDER BY id DESC", (container, slot, platform)).fetchall()
    conn.close()
    return [_row(r) for r in rows]


def slot_state(container, platform=None):
    """How each slot resolves on one platform (default: this host).

    {"A": {"build": build-or-None, "pinned": bool, "newest": build-or-None},
     "B": {...}} -- "newest" is what the slot falls back to without a pin.
    """
    platform = platform or host_platform()
    ensure_schema()
    conn = _connect()
    pins = {r["slot"]: r["build_id"] for r in conn.execute(
        "SELECT slot, build_id FROM container_ab_pin WHERE container = ? AND platform = ?",
        (container, platform)).fetchall()}
    conn.close()
    out = {}
    for s in SLOTS:
        newest = next((b for b in slot_candidates(container, s, platform)
                       if b["store_exists"]), None)
        pinned = get_build(pins[s]) if pins.get(s) else None
        out[s] = {"build": pinned or newest, "pinned": bool(pinned), "newest": newest}
    return out


def get_ab(container, platform=None):
    """{"A": build-or-None, "B": build-or-None} as resolved on `platform`."""
    return {s: st["build"] for s, st in slot_state(container, platform).items()}


def set_ab(container, slot, build_id, platform=None):
    """Pin a build to a slot (the user override), or with build_id None,
    unpin so the slot goes back to the newest build in its list.

    A pin is independent of the lists -- any registered build can be
    pinned to either slot, the same one to both. The pin is stored
    under the BUILD's platform, so pinning a Linux build from any page
    doesn't change what a Windows runner gets; an "any" build is pinned for
    the platform asking (default: this host).
    """
    _check_slot(slot)
    if slot is None:
        raise BuildError(f"slot must be one of {', '.join(SLOTS)}")
    ensure_schema()
    conn = _connect()
    if build_id in (None, "", 0):
        conn.execute("DELETE FROM container_ab_pin WHERE container = ? AND slot = ? "
                     "AND platform = ?", (container, slot, platform or host_platform()))
    else:
        b = get_build(int(build_id))
        if not b or b["container"] != container:
            conn.close()
            raise BuildError(f"build {build_id} is not in {container}")
        if not b.get("platform"):
            conn.close()
            raise BuildError(f"build #{b['id']} has no known platform (snapshot unreadable); "
                             f"set its platform before pinning it")
        pin_platform = ((platform or host_platform()) if b["platform"] == "any"
                        else b["platform"])
        conn.execute("INSERT OR REPLACE INTO container_ab_pin "
                     "(container, slot, platform, build_id) VALUES (?,?,?,?)",
                     (container, slot, pin_platform, b["id"]))
    conn.commit()
    conn.close()
    return get_ab(container, platform)


# ── runs ─────────────────────────────────────────────────────────────────────

def override_for(container, slot=None, build_id=None):
    """The run option for "use build X": {"build_id", "slot"}.

    Resolved at launch so a batch keeps the builds it started with even if
    someone re-points slot A halfway through.
    """
    platform = host_platform()
    if build_id is None:
        if slot not in SLOTS:
            raise BuildError(f"build slot must be one of {', '.join(SLOTS)}")
        build = get_ab(container, platform).get(slot)
        if not build:
            raise BuildError(f"no {platform} build in slot {slot}'s list for {container} "
                             f"-- build one into slot {slot} on a {platform} host, or add "
                             f"a registered {platform} build to the list")
    else:
        build = get_build(int(build_id))
        if not build or build["container"] != container:
            raise BuildError(f"build {build_id} is not in {container}")
    _check_runs_here(build)
    if not build["store_exists"]:
        raise BuildError(f"build #{build['id']} snapshot is missing: {build['store_path']}")
    return {"build_id": build["id"], "slot": slot}


def _check_runs_here(build):
    """Refuse a build this host can't execute -- a Linux ELF handed to a
    Windows runner fails at step 0 with nothing pointing at the cause."""
    platform = host_platform()
    if not runs_on(build, platform):
        raise BuildError(f"build #{build['id']} is a {build.get('platform') or 'unknown-platform'} "
                         f"build; this runner is {platform}")


def check_variant_accepts(config, override):
    """Refuse an A/B run the variant can't honour. Returns the build."""
    build = get_build(override["build_id"])
    if not build:
        raise BuildError(f"build {override['build_id']} no longer exists")
    key = build["output_key"]
    if key not in (config or {}):
        raise BuildError(
            f"variant has no CONFIG[{key!r}] to point at build #{build['id']}; "
            f"it can't be A/B run with this build")
    return build


def apply_override(config, override):
    """CONFIG copy with the build's key pointed at its snapshot."""
    build = check_variant_accepts(config, override)
    _check_runs_here(build)
    if not build["store_exists"]:
        raise BuildError(f"build #{build['id']} snapshot is missing: {build['store_path']}")
    new = dict(config)
    new[build["output_key"]] = build["store_path"]
    return new, build


def check_referenced(config, build, plan_steps):
    """Refuse a variant that has the key but never references {key} in the
    steps it will actually run (templates expanded). Overriding it would
    change nothing, and the run would pass on the variant's own binary while
    reporting build A/B.
    """
    key = build["output_key"]
    haystack = json.dumps({"steps": plan_steps,
                           "config": {k: v for k, v in config.items()
                                      if k not in (key, "steps")}},
                          default=str)
    if "{" + key + "}" not in haystack:
        raise BuildError(
            f"no step references {{{key}}}; overriding it for build "
            f"#{build['id']} would not change what runs")


def attribute(container, config, override=None):
    """What build a run is about to use: dict for record_run(), or None."""
    if override:
        build = get_build(override["build_id"])
        return {"slot": override.get("slot"), "output_key": build["output_key"],
                "path_used": build["store_path"], "md5": build["md5"],
                "build_id": build["id"]}
    for key in output_keys_for(container):
        if key not in config:
            continue
        path = resolve_value(config.get(key), config)
        if not path or not os.path.isfile(path):
            continue
        try:
            md5, _ = file_hashes(path)
        except OSError:
            continue
        ensure_schema()
        conn = _connect()
        r = conn.execute("SELECT id FROM build_object WHERE md5 = ? "
                         "ORDER BY (container = ?) DESC, id DESC LIMIT 1",
                         (md5, container)).fetchone()
        conn.close()
        return {"slot": None, "output_key": key, "path_used": path,
                "md5": md5, "build_id": r["id"] if r else None}
    return None


def record_run(report_id, container, test_id, attr):
    if not attr or not report_id:
        return
    ensure_schema()
    conn = _connect()
    conn.execute("""INSERT INTO run_build
                      (report_id, container, test_id, slot, output_key,
                       path_used, md5, build_id)
                    VALUES (?,?,?,?,?,?,?,?)""",
                 (report_id, container, test_id, attr.get("slot"),
                  attr.get("output_key"), attr.get("path_used"),
                  attr.get("md5"), attr.get("build_id")))
    conn.commit()
    conn.close()


def report_banner_html(attr):
    """One-line build header for the HTML report, or ''."""
    if not attr:
        return ""
    from markupsafe import escape
    b = get_build(attr["build_id"]) if attr.get("build_id") else None
    bits = []
    if attr.get("slot"):
        bits.append(f"<strong>slot {escape(attr['slot'])}</strong>")
    if b:
        bits.append(f"build #{b['id']}")
        if b.get("git_sha"):
            bits.append(f"{escape(b.get('git_branch') or '?')} @ "
                        f"<code>{escape(b['git_sha'][:12])}</code>"
                        + (" <em>(dirty tree)</em>" if b.get("git_dirty") else ""))
        if b.get("label"):
            bits.append(f"&ldquo;{escape(b['label'])}&rdquo;")
    else:
        bits.append("<em>unregistered build</em>")
    bits.append(f"md5 <code>{escape(attr.get('md5') or '?')}</code>")
    bits.append(f"<code>{escape(attr.get('path_used') or '')}</code>")
    return ('<p class="build-banner" style="background:#eef4fb;border:1px solid '
            '#c3dcf5;padding:6px 10px;">Build: ' + " &middot; ".join(bits) + "</p>")


# ── reporting ────────────────────────────────────────────────────────────────

_STATUS_SQL = """
    CASE
      WHEN SUM(tr.status IN ('FAIL','ERROR')) > 0 THEN 'FAIL'
      WHEN SUM(tr.status = 'WARN') > 0 THEN 'WARN'
      WHEN SUM(tr.status IN ('SKIP','SKIPPED')) > 0 THEN 'SKIP'
      ELSE 'PASS'
    END
"""


def container_runs(container, limit=500):
    """Every attributed report of a container, newest first, with its build."""
    ensure_schema()
    conn = _connect()
    rows = conn.execute(f"""
        SELECT rb.report_id, rb.test_id, rb.slot, rb.md5, rb.path_used,
               rb.build_id, r.path AS report_path, r.total_duration AS duration,
               {_STATUS_SQL} AS status,
               MIN(tr.start_time) AS started,
               bo.git_sha, bo.git_branch, bo.git_dirty, bo.label, bo.filename
        FROM run_build rb
        JOIN report r        ON r.id = rb.report_id
        JOIN test_result tr  ON tr.report_id = rb.report_id
        LEFT JOIN build_object bo ON bo.id = rb.build_id
        WHERE rb.container = ?
        GROUP BY rb.id
        ORDER BY rb.report_id DESC
        LIMIT ?
    """, (container, int(limit))).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def grouped_runs(container, group_by, labels=None):
    """Runs bucketed by git commit or by build md5, newest bucket first.

    labels maps test_id -> variant name for display.
    """
    if group_by not in ("commit", "md5"):
        raise BuildError("group_by must be 'commit' or 'md5'")
    labels = labels or {}
    groups = {}
    order = []
    for run in container_runs(container):
        run["variant"] = labels.get(run["test_id"], run["test_id"].rsplit(".", 1)[-1])
        run["timestamp"] = os.path.dirname(run["report_path"] or "")
        if group_by == "commit":
            key = (run["git_sha"] or "") + ("+dirty" if run["git_dirty"] else "")
            key = key or "unregistered"
        else:
            key = run["md5"] or "unknown"
        g = groups.get(key)
        if g is None:
            g = groups[key] = {
                "key": key, "git_sha": run["git_sha"], "git_branch": run["git_branch"],
                "git_dirty": run["git_dirty"], "md5s": [], "labels": [],
                "runs": [], "counts": {},
            }
            order.append(key)
        if run["md5"] and run["md5"] not in g["md5s"]:
            g["md5s"].append(run["md5"])
        if run["label"] and run["label"] not in g["labels"]:
            g["labels"].append(run["label"])
        g["runs"].append(run)
        g["counts"][run["status"]] = g["counts"].get(run["status"], 0) + 1
    return [groups[k] for k in order]


def latest_by_slot(container):
    """{test_id: {"A": status, "B": status}} from each slot's newest run."""
    ensure_schema()
    conn = _connect()
    rows = conn.execute(f"""
        SELECT rb.test_id, rb.slot, {_STATUS_SQL} AS status
        FROM run_build rb
        JOIN test_result tr ON tr.report_id = rb.report_id
        WHERE rb.container = ? AND rb.slot IS NOT NULL
          AND rb.report_id = (SELECT MAX(x.report_id) FROM run_build x
                              WHERE x.container = rb.container
                                AND x.test_id = rb.test_id AND x.slot = rb.slot)
        GROUP BY rb.id
    """, (container,)).fetchall()
    conn.close()
    out = {}
    for r in rows:
        out.setdefault(r["test_id"], {})[r["slot"]] = r["status"]
    return out
