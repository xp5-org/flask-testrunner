import os
import sqlite3
import hashlib
from collections import defaultdict
import re


#######################################
### config stuff #####################
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
REPORT_DIR = os.path.join(BASE_DIR, "reports")
FLASKRUNNER_HELPERDIR = "/testrunnerapp/helpers"
TESTSRC_HELPERDIR = "/testsrc/helpers"
DB_PATH = os.path.join(BASE_DIR, "report.sqlite")
#######################################

class ReportDB:
    def __init__(self):
        self.DB_PATH = DB_PATH
        db_dir = os.path.dirname(DB_PATH)
        if db_dir and not os.path.exists(db_dir):
            os.makedirs(db_dir, exist_ok=True)
        self._init_db()


    def _connect(self):
        return sqlite3.connect(self.DB_PATH)


    def _init_db(self):
        conn = self._connect()
        cur = conn.cursor()

        cur.execute("""
            CREATE TABLE IF NOT EXISTS report (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                path TEXT
            )
        """)

        cur.execute("""
            CREATE TABLE IF NOT EXISTS test_result (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                report_id INTEGER,
                test_index INTEGER,
                test_id TEXT,
                testparentname TEXT,
                test_types TEXT,
                name TEXT,
                status TEXT,
                color TEXT,
                output TEXT,
                stdout TEXT,
                duration REAL,
                start_time REAL,
                stop_time REAL,
                screenshot TEXT
            )
        """)

        cur.execute("""
            CREATE TABLE IF NOT EXISTS build_artifact (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                report_id     INTEGER NOT NULL,
                test_id       TEXT,
                artifact_key  TEXT,
                artifact_path TEXT,
                artifact_filename TEXT,
                exists_on_disk INTEGER DEFAULT 0,
                file_size     INTEGER,
                md5_hash      TEXT,
                FOREIGN KEY (report_id) REFERENCES report(id)
            )
        """)

        conn.commit()
        conn.close()


    def fetch_results_for_report(self, report_id):
        conn = self._connect()
        cur = conn.cursor()
        cur.execute(
            "SELECT * FROM test_result WHERE report_id = ? ORDER BY test_index",
            (report_id,)
        )
        rows = cur.fetchall()
        conn.close()
        return rows


    def get_latest_report_summary(self, target_id=None):
        conn = self._connect()
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()
        cur.execute("SELECT id FROM report ORDER BY id DESC LIMIT 1")
        row = cur.fetchone()
        if not row:
            conn.close()
            return []

        latest_report_id = row["id"]
        cur.execute("SELECT * FROM test_result WHERE report_id = ?", (latest_report_id,))
        rows = cur.fetchall()
        conn.close()

        summary = []
        for r in rows:
            if target_id and r["test_id"] != target_id:
                continue

            step_name = r["name"] or "Unnamed Step"
            status_raw = r["status"].upper() if r["status"] else "SKIP"
            duration_val = f"{r['duration']:.2f}" if r["duration"] is not None else "0.00"

            if "PASS" in status_raw:
                status = "PASS"
            elif "FAIL" in status_raw:
                status = "FAIL"
            else:
                status = "SKIP"

            summary.append({
                "step_name": step_name,
                "duration": duration_val,
                "status": status,
                "test_id": r["test_id"],
                "types": r["test_types"]
            })

        return summary


    def get_failed_steps_log(self, testparentname, test_types="*"):
            conn = self._connect()
            cur = conn.cursor()

            sql = """
                SELECT testparentname, test_types, name, output
                FROM test_result
                WHERE report_id = (
                    SELECT MAX(report_id)
                    FROM test_result
                    WHERE testparentname = ?
            """
            params = [testparentname]

            if test_types != "*":
                sql += " AND test_types = ?"
                params.append(test_types)

            sql += """
                )
                AND status IN ('FAIL', 'ERROR')
                ORDER BY test_index ASC
            """

            cur.execute(sql, tuple(params))
            rows = cur.fetchall()
            conn.close()

            results = [
                {
                    "testparentname": r[0],
                    "test_type": r[1],
                    "name": r[2],
                    "output": r[3]
                }
                for r in rows
            ]
            return results




    def get_max_report_id(self):
        conn = self._connect()
        cur = conn.cursor()
        cur.execute("SELECT MAX(id) FROM report")
        row = cur.fetchone()
        conn.close()
        return row[0] if row else None


    def get_all_reports_summary(self, test_parent_name=None, per_parent_limit=None):
        """Report-level summaries, newest first.

        per_parent_limit keeps only the N newest reports of each parent testlist,
        so the front page stays short without dropping whole testlists off it.
        """
        conn = self._connect()
        cur = conn.cursor()

        # match fail & error and return fail
        query = """
            SELECT
                r.id as report_id,
                r.path,
                SUM(tr.duration) as total_duration,
                CASE
                    WHEN MIN(tr.status) IN ('FAIL', 'ERROR') THEN 'FAIL'
                    ELSE 'PASS'
                END as overall_status,
                MIN(tr.start_time) as report_start,
                MIN(tr.test_id) as test_id
            FROM report r
            LEFT JOIN test_result tr ON r.id = tr.report_id
        """

        params = []
        if test_parent_name:
            query += " WHERE tr.testparentname = ?"
            params.append(test_parent_name)

        query += """
            GROUP BY r.id, r.path
        """

        if per_parent_limit is not None:
            query = """
                SELECT path, total_duration, overall_status, report_start, test_id
                FROM (
                    -- test_id  the parent name is a display
                    -- label that gets renamed ("openwatcom1" -> "OpenWatcom
                    -- Bartest"), which would split one testlist's history into
                    -- several partitions and show 3 reports for each of them.
                    SELECT *, ROW_NUMBER() OVER (
                        PARTITION BY test_id ORDER BY report_id DESC
                    ) as parent_rank
                    FROM (%s)
                )
                WHERE parent_rank <= ?
            """ % query
            params.append(per_parent_limit)
        else:
            query = """
                SELECT path, total_duration, overall_status, report_start, test_id
                FROM (%s)
            """ % query

        # report_id, not report_start: reports with no result rows have a NULL
        # start_time and SQLite sorts NULLs first on DESC, which would float
        # empty reports to the top of the page.
        query += " ORDER BY report_id DESC"

        cur.execute(query, params)
        rows = cur.fetchall()
        conn.close()

        return [(r[0], f"{r[1]:.2f}" if r[1] is not None else "0.00", r[2].upper() if r[2] else "", r[3], r[4]) for r in rows]


    def init_report_db(self):
        db_dir = os.path.dirname(DB_PATH)
        if db_dir and not os.path.exists(db_dir):
            os.makedirs(db_dir, exist_ok=True)

        conn = self._connect()
        cur = conn.cursor()

        cur.execute("""
            CREATE TABLE IF NOT EXISTS report (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                path TEXT
            )
        """)

        cur.execute("""
            CREATE TABLE IF NOT EXISTS test_result (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                report_id INTEGER,
                test_index INTEGER,
                test_id TEXT,
                testparentname TEXT,
                test_types TEXT,
                name TEXT,
                status TEXT,
                color TEXT,
                output TEXT,
                stdout TEXT,
                duration REAL,
                start_time REAL,
                stop_time REAL,
                screenshot TEXT
            )
        """)

        cur.execute("""
            CREATE TABLE IF NOT EXISTS build_artifact (
                id                INTEGER PRIMARY KEY AUTOINCREMENT,
                report_id         INTEGER NOT NULL,
                test_id           TEXT,
                artifact_key      TEXT,
                artifact_path     TEXT,
                artifact_filename TEXT,
                exists_on_disk    INTEGER DEFAULT 0,
                file_size         INTEGER,
                md5_hash          TEXT,
                FOREIGN KEY (report_id) REFERENCES report(id)
            )
        """)

        # get_latest_namedteststatus() runs a correlated MAX(report_id) subquery
        # per row; without these it full-scans test_result for every row and the
        # index page's /api/v1/tests call takes ~18s.
        cur.execute("""
            CREATE INDEX IF NOT EXISTS ix_test_result_parent_types_report
            ON test_result (testparentname, test_types, report_id)
        """)

        cur.execute("""
            CREATE INDEX IF NOT EXISTS ix_test_result_report
            ON test_result (report_id)
        """)

        conn.commit()
        conn.close()


    def populate_sqlite(self, test_id, testparentname, test_types, results, html_report_path, total_duration=0.00, get_start=None, get_stop=None, config=None):
        subdir_path = os.path.dirname(html_report_path)
        screenshot_map = defaultdict(list)
        for fname in os.listdir(subdir_path):
            if fname.endswith((".png", ".gif")):
                m = re.match(r"screenshot-[^-]+-(\d+)(?:-\d+)?\.(png|gif)$", fname)
                if m:
                    step_num = int(m.group(1))
                    screenshot_map[step_num].append(fname)

        conn = self._connect()
        cur = conn.cursor()

        try:
            cur.execute("ALTER TABLE report ADD COLUMN total_duration REAL")
        except sqlite3.OperationalError:
            pass

        rel_path = os.path.relpath(html_report_path, REPORT_DIR)
        cur.execute(
            "INSERT INTO report (path, total_duration) VALUES (?, ?)",
            (rel_path, round(total_duration, 2))
        )

        report_id = cur.lastrowid

        for idx, (name, status, color, output, stdout, duration) in enumerate(results, start=1):
            start_ts = get_start(name) if get_start else None
            stop_ts = get_stop(name) if get_stop else None
            screenshots = ",".join(screenshot_map.get(idx, []))
            cur.execute(
                """
                INSERT INTO test_result
                (report_id, test_index, test_id, testparentname, test_types, name, status, color, output, stdout, duration, start_time, stop_time, screenshot)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    report_id,
                    idx,
                    test_id,
                    testparentname,
                    test_types,
                    name,
                    status,
                    color,
                    output,
                    stdout,
                    duration,
                    start_ts,
                    stop_ts,
                    screenshots
                )
            )

        conn.commit()
        conn.close()

        # Derive and store build artifacts from CONFIG if provided
        if config:
            self.save_build_artifacts(report_id, test_id, config)


    # ── Build artifact helpers ────────────────────────────────────────────────

    # Config keys whose resolved values are build output artifacts.
    # Each entry: (config_key, human label shown in reports)
    ARTIFACT_KEYS = [
        ("prg_filepath",    "PRG file"),
        ("d64_drive8_file", "Disk image (drive 8)"),
        ("d64_drive9_file", "Disk image (drive 9)"),
    ]

    @staticmethod
    def _resolve_config(tmpl: str, config: dict, passes: int = 6) -> str:
        """Multi-pass {token} resolver — mirrors repair_config._resolve."""
        s = str(tmpl)
        for _ in range(passes):
            prev = s
            s = re.sub(
                r"\{([^}]+)\}",
                lambda m: str(config[m.group(1)]) if m.group(1) in config else m.group(0),
                s,
            )
            if s == prev:
                break
        return s

    def extract_build_artifacts(self, config: dict) -> list[dict]:
        """
        Resolve build artifact paths from a CONFIG dict.

        Returns a list of dicts:
            {artifact_key, artifact_path, artifact_filename,
             exists_on_disk, file_size, md5_hash}
        """
        artifacts = []
        token_re = re.compile(r"\{[^}]+\}")

        for key, label in self.ARTIFACT_KEYS:
            raw = config.get(key)
            if not raw or str(raw).strip() in ("", "None", "null"):
                continue

            resolved = self._resolve_config(str(raw), config)

            # Skip if still has unresolved tokens or isn't an absolute path
            if token_re.search(resolved) or not resolved.startswith("/"):
                continue

            exists    = os.path.isfile(resolved)
            file_size = None
            md5_hash  = None

            if exists:
                file_size = os.path.getsize(resolved)
                try:
                    h = hashlib.md5()
                    with open(resolved, "rb") as f:
                        for chunk in iter(lambda: f.read(65536), b""):
                            h.update(chunk)
                    md5_hash = h.hexdigest()
                except OSError:
                    pass

            artifacts.append({
                "artifact_key":      label,
                "artifact_path":     resolved,
                "artifact_filename": os.path.basename(resolved),
                "exists_on_disk":    1 if exists else 0,
                "file_size":         file_size,
                "md5_hash":          md5_hash,
            })

        return artifacts

    def save_build_artifacts(self, report_id: int, test_id: str, config: dict) -> None:
        """
        get build artifacts from `CONFIG` and write them to build_artifact.
        """
        artifacts = self.extract_build_artifacts(config)
        if not artifacts:
            return

        conn = self._connect()
        cur = conn.cursor()

        # Migrate existing DBs that predate this table
        cur.execute("""
            CREATE TABLE IF NOT EXISTS build_artifact (
                id                INTEGER PRIMARY KEY AUTOINCREMENT,
                report_id         INTEGER NOT NULL,
                test_id           TEXT,
                artifact_key      TEXT,
                artifact_path     TEXT,
                artifact_filename TEXT,
                exists_on_disk    INTEGER DEFAULT 0,
                file_size         INTEGER,
                md5_hash          TEXT,
                FOREIGN KEY (report_id) REFERENCES report(id)
            )
        """)
        # Add new columns to pre-existing tables that lack them
        for col, coldef in [
            ("artifact_filename", "TEXT"),
            ("md5_hash",          "TEXT"),
        ]:
            try:
                cur.execute(f"ALTER TABLE build_artifact ADD COLUMN {col} {coldef}")
            except sqlite3.OperationalError:
                pass

        for a in artifacts:
            cur.execute(
                """
                INSERT INTO build_artifact
                    (report_id, test_id, artifact_key, artifact_path,
                     artifact_filename, exists_on_disk, file_size, md5_hash)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (report_id, test_id,
                 a["artifact_key"], a["artifact_path"],
                 a["artifact_filename"],
                 a["exists_on_disk"], a["file_size"], a["md5_hash"])
            )

        conn.commit()
        conn.close()

    def get_build_artifacts(self, report_id: int) -> list[dict]:
        """Return all build artifacts for a given report_id."""
        conn = self._connect()
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()
        cur.execute("""
            SELECT artifact_key, artifact_path, artifact_filename,
                   exists_on_disk, file_size, md5_hash
            FROM build_artifact
            WHERE report_id = ?
            ORDER BY id ASC
        """, (report_id,))
        rows = cur.fetchall()
        conn.close()
        return [dict(r) for r in rows]

    def get_latest_build_artifacts(self, test_id: str) -> list[dict]:
        """
        Return build artifacts from the most recent report for a given test_id.
        """
        conn = self._connect()
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()
        cur.execute("""
            SELECT ba.artifact_key, ba.artifact_path, ba.artifact_filename,
                   ba.exists_on_disk, ba.file_size, ba.md5_hash
            FROM build_artifact ba
            WHERE ba.report_id = (
                SELECT MAX(report_id) FROM build_artifact WHERE test_id = ?
            )
            ORDER BY ba.id ASC
        """, (test_id,))
        rows = cur.fetchall()
        conn.close()
        return [dict(r) for r in rows]


    def get_reports_by_test_id(self, test_id):
        conn = self._connect()
        cur = conn.cursor()
        cur.execute("""
            SELECT DISTINCT r.id,
                            r.path
            FROM report r
            JOIN test_result tr ON tr.report_id = r.id
            WHERE tr.test_id = ?
            ORDER BY r.id DESC
        """, (test_id,))
        rows = cur.fetchall()
        conn.close()

        reports = []
        for r in rows:
            filepath = r[1]
            reports.append({
                "filepath": filepath,
                "filename": os.path.basename(filepath),
                "timestamp": os.path.dirname(filepath)
            })
        return reports
    

    def get_latest_namedteststatus(self, testparentname=None):
        #each testparaentname might have multiple test_types
        #so parentname + test_type = unique test key
        #each unique test run of that combo has its own reportid 
        conn = self._connect()
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()

        query = """
        SELECT t1.testparentname, t1.test_id, t1.test_types, t1.status 
        FROM test_result t1
        WHERE t1.report_id = (
            SELECT MAX(t2.report_id) 
            FROM test_result t2 
            WHERE t2.testparentname = t1.testparentname 
            AND t2.test_types = t1.test_types
        )
        """
        params = []

        if testparentname:
            query += " AND t1.testparentname = ?"
            params.append(testparentname)

        cur.execute(query, params)
        rows = cur.fetchall()
        conn.close()

        results = {}
        for r in rows:
            test_id = r["test_id"]
            types = r["test_types"]
            parent_name = r["testparentname"]
            status_raw = r["status"].upper() if r["status"] else "None"

            dict_key = (test_id, types)

            if dict_key not in results:
                results[dict_key] = {
                    "testparentname": parent_name,
                    "test_id": test_id,
                    "types": types,
                    "status": "None"
                }

            if status_raw == "FAIL" or status_raw == "ERROR":
                results[dict_key]["status"] = "FAIL"
            elif status_raw == "PASS" and results[dict_key]["status"] != "FAIL":
                results[dict_key]["status"] = "PASS"

        return list(results.values())


    # V1 api read helper

    @staticmethod
    def _derive_status(results):
        """build list of per-step status strings up to one overall PASS/FAIL."""
        for r in results:
            s = (r.get("status") or "").upper()
            if s in ("FAIL", "ERROR"):
                return "FAIL"
        return "PASS" if results else "UNKNOWN"

    def list_reports(self, limit=50):
        """list of reports with a rolled-up overall status.

        sorted most recent first 

        [{report_id, path, timestamp, total_duration, status}]
        `timestamp` is the report's subdir name (e.g. 20260704_120000)
        """
        conn = self._connect()
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()
        cur.execute("""
            SELECT r.id AS report_id,
                   r.path AS path,
                   SUM(tr.duration) AS total_duration,
                   CASE WHEN MIN(tr.status) IN ('FAIL', 'ERROR') THEN 'FAIL'
                        ELSE 'PASS' END AS status,
                   MIN(tr.start_time) AS started_at
            FROM report r
            LEFT JOIN test_result tr ON tr.report_id = r.id
            GROUP BY r.id, r.path
            ORDER BY r.id DESC
            LIMIT ?
        """, (limit,))
        rows = cur.fetchall()
        conn.close()

        out = []
        for r in rows:
            path = r["path"] or ""
            out.append({
                "report_id":      r["report_id"],
                "path":           path,
                "timestamp":      os.path.dirname(path) or path,
                "total_duration": round(r["total_duration"], 2) if r["total_duration"] is not None else 0.0,
                "status":         (r["status"] or "UNKNOWN").upper(),
                "started_at":     r["started_at"],
            })
        return out

    def get_report_json(self, report_id):
        """metadata + per-step results + build artifacts.

        Returns None if the report_id does not exist.
        """
        conn = self._connect()
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()

        cur.execute("SELECT id, path FROM report WHERE id = ?", (report_id,))
        report_row = cur.fetchone()
        if not report_row:
            conn.close()
            return None

        cur.execute(
            "SELECT * FROM test_result WHERE report_id = ? ORDER BY test_index",
            (report_id,),
        )
        result_rows = cur.fetchall()
        conn.close()

        results = []
        for r in result_rows:
            screenshots = [s for s in (r["screenshot"] or "").split(",") if s]
            results.append({
                "index":     r["test_index"],
                "test_id":   r["test_id"],
                "name":      r["name"],
                "status":    r["status"],
                "duration":  r["duration"],
                "output":    r["output"],
                "stdout":    r["stdout"],
                "start_time": r["start_time"],
                "stop_time":  r["stop_time"],
                "screenshots": screenshots,
            })

        path = report_row["path"] or ""
        return {
            "report_id":      report_row["id"],
            "path":           path,
            "timestamp":      os.path.dirname(path) or path,
            "status":         self._derive_status(results),
            "total_duration": round(sum(x["duration"] or 0.0 for x in results), 2),
            "results":        results,
            "artifacts":      self.get_build_artifacts(report_id),
        }

    def get_report_id_for_path(self, rel_path):
        """Resolve a stored relative report path (e.g. '20260704.../mod.html')
        back to its report_id. Returns None if not found."""
        conn = self._connect()
        cur = conn.cursor()
        cur.execute(
            "SELECT id FROM report WHERE path = ? ORDER BY id DESC LIMIT 1",
            (rel_path,),
        )
        row = cur.fetchone()
        conn.close()
        return row[0] if row else None


db = ReportDB()