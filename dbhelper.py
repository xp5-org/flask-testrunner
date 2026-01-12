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
            name = r[3]
            duration = str(r[8])
            status = r[4].upper() if r[4] else ""
            summary.append((name, duration, status))
        return summary


    def get_all_reports_summary(self):
        conn = self._connect()
        cur = conn.cursor()
        cur.execute("""
            SELECT 
                r.path,
                SUM(tr.duration) as total_duration,
                CASE WHEN MIN(tr.status) = 'FAIL' THEN 'FAIL' ELSE 'PASS' END as overall_status
            FROM report r
            JOIN test_result tr ON r.id = tr.report_id
            GROUP BY r.id, r.path
            ORDER BY r.id DESC
        """)
        rows = cur.fetchall()
        conn.close()
        return [(r[0], str(r[1]), r[2].upper() if r[2] else "") for r in rows]
   

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


    def populate_sqlite(self, results, html_report_path, total_duration=0.0, get_start=None, get_stop=None):
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
        cur.execute("INSERT INTO report (path, total_duration) VALUES (?, ?)", (rel_path, total_duration))
        report_id = cur.lastrowid

        for idx, (name, status, color, output, stdout, duration) in enumerate(results, start=1):
            start_ts = get_start(name) if get_start else None
            stop_ts = get_stop(name) if get_stop else None
            screenshots = ",".join(screenshot_map.get(idx, []))
            cur.execute("""
                INSERT INTO test_result
                (report_id, test_index, name, status, color, output, stdout, duration, start_time, stop_time, screenshot)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (report_id, idx, name, status, color, output, stdout, duration, start_ts, stop_ts, screenshots))

        conn.commit()
        conn.close()
