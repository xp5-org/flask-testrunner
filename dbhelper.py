import os
import sqlite3
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



    def get_latest_report_summary(self):
            conn = self._connect()
            cur = conn.cursor()
            cur.execute("SELECT id FROM report ORDER BY id DESC LIMIT 1")
            row = cur.fetchone()
            if not row:
                conn.close()
                return []

            latest_report_id = row[0]
            rows = self.fetch_results_for_report(latest_report_id)
            conn.close()

            summary = []
            for r in rows:
                # 0:id, 1:report_id, 2:test_index, 3:test_id, 4:name, 5:status, 9:duration
                step_name = r[4] or "Unnamed Step"
                status_val = r[5].upper() if r[5] else "OTHER"
                duration = f"{r[9]:.2f}" if r[9] is not None else "0.00"

                if "PASS" in status_val:
                    status = "PASS"
                elif "FAIL" in status_val:
                    status = "FAIL"
                else:
                    status = "OTHER"
                summary.append((step_name, duration, status))
            return summary


    def get_failed_steps_log(self, internal_id):
            conn = self._connect()
            cur = conn.cursor()
            
            cur.execute("""
                SELECT MAX(report_id) 
                FROM test_result 
                WHERE test_id = ?
            """, (internal_id,))
            res = cur.fetchone()
            
            if not res or res[0] is None:
                conn.close()
                return []
                
            latest_id_for_test = res[0]

            cur.execute("""
                SELECT name, output 
                FROM test_result 
                WHERE report_id = ? AND status != 'PASS'
                ORDER BY test_index ASC
            """, (latest_id_for_test,))
            
            rows = cur.fetchall()
            conn.close()
            return [{"name": r[0], "output": r[1]} for r in rows]



    def get_all_reports_summary(self):
        conn = self._connect()
        cur = conn.cursor()
        cur.execute("""
            SELECT 
                r.path,
                SUM(tr.duration) as total_duration,
                CASE WHEN MIN(tr.status) = 'FAIL' THEN 'FAIL' ELSE 'PASS' END as overall_status,
                MIN(tr.start_time) as report_start
            FROM report r
            JOIN test_result tr ON r.id = tr.report_id
            GROUP BY r.id, r.path
            ORDER BY r.id DESC
        """)
        rows = cur.fetchall()
        conn.close()
        #return [(r[0], str(r[1]), r[2].upper() if r[2] else "", r[3]) for r in rows]
        return [(r[0], f"{r[1]:.2f}" if r[1] is not None else "0.00", r[2].upper() if r[2] else "", r[3]) for r in rows]

   

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

        conn.commit()
        conn.close()

    def populate_sqlite(self, test_id, results, html_report_path, total_duration=0.00, get_start=None, get_stop=None):
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
                (report_id, test_index, test_id, name, status, color, output, stdout, duration, start_time, stop_time, screenshot)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    report_id,
                    idx,
                    test_id,
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



