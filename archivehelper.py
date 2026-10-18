"""Connector for archive.db, the archived twin of report.sqlite.

Archiving a report is copy-on-write: the report's directory and its DB rows
are copied to the archive first and read back to confirm the copy matches,
and only then are the originals (the reports/<dir> tree and the rows in
report.sqlite) removed. Any failure along the way leaves the active db and
reports/ untouched.
"""
import os
import shutil
import sqlite3

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
ARCHIVE_DB_PATH = os.path.join(BASE_DIR, "archive.db")
ARCHIVE_REPORT_DIR = os.path.join(BASE_DIR, "reports_archive")
REPORT_DIR = os.path.join(BASE_DIR, "reports")


class ArchiveDB:
    """Same report/test_result/build_artifact shape as ReportDB (dbhelper.py),
    plus archived_at, kept as its own file so it can be read and verified
    independently of the active db.
    """

    def __init__(self, db_path=ARCHIVE_DB_PATH):
        self.db_path = db_path
        db_dir = os.path.dirname(db_path)
        if db_dir and not os.path.exists(db_dir):
            os.makedirs(db_dir, exist_ok=True)
        self._init_db()

    def _connect(self):
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self):
        conn = self._connect()
        cur = conn.cursor()
        cur.execute("""
            CREATE TABLE IF NOT EXISTS report (
                id INTEGER PRIMARY KEY,
                path TEXT,
                total_duration REAL,
                archived_at REAL
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS test_result (
                id INTEGER PRIMARY KEY,
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
                id INTEGER PRIMARY KEY,
                report_id INTEGER,
                test_id TEXT,
                artifact_key TEXT,
                artifact_path TEXT,
                artifact_filename TEXT,
                exists_on_disk INTEGER DEFAULT 0,
                file_size INTEGER,
                md5_hash TEXT
            )
        """)
        cur.execute("CREATE INDEX IF NOT EXISTS ix_archive_test_result_report ON test_result (report_id)")
        cur.execute("CREATE INDEX IF NOT EXISTS ix_archive_build_artifact_report ON build_artifact (report_id)")
        conn.commit()
        conn.close()

    def write_bundle(self, bundle):
        """Copy step: insert one report's rows, ids preserved as-is so the
        archive can be cross-referenced by the same report_id the active db
        used. INSERT OR REPLACE so a retry after a failed verify overwrites
        cleanly instead of erroring on a duplicate id.
        """
        report = bundle["report"]
        conn = self._connect()
        try:
            cur = conn.cursor()
            cur.execute(
                "INSERT OR REPLACE INTO report (id, path, total_duration, archived_at) "
                "VALUES (?, ?, ?, strftime('%s','now'))",
                (report["id"], report["path"], report.get("total_duration")),
            )

            cur.execute("DELETE FROM test_result WHERE report_id = ?", (report["id"],))
            for r in bundle["test_results"]:
                cur.execute(
                    """INSERT INTO test_result
                       (id, report_id, test_index, test_id, testparentname, test_types,
                        name, status, color, output, stdout, duration, start_time,
                        stop_time, screenshot)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (r["id"], r["report_id"], r["test_index"], r["test_id"],
                     r["testparentname"], r["test_types"], r["name"], r["status"],
                     r["color"], r["output"], r["stdout"], r["duration"],
                     r["start_time"], r["stop_time"], r["screenshot"]),
                )

            cur.execute("DELETE FROM build_artifact WHERE report_id = ?", (report["id"],))
            for a in bundle["build_artifacts"]:
                cur.execute(
                    """INSERT INTO build_artifact
                       (id, report_id, test_id, artifact_key, artifact_path,
                        artifact_filename, exists_on_disk, file_size, md5_hash)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (a["id"], a["report_id"], a["test_id"], a["artifact_key"],
                     a["artifact_path"], a["artifact_filename"], a["exists_on_disk"],
                     a["file_size"], a["md5_hash"]),
                )

            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def read_bundle(self, report_id):
        """Read step: what write_bundle actually persisted, used to verify
        the copy before anything active is deleted. Returns None if absent.
        """
        conn = self._connect()
        cur = conn.cursor()

        cur.execute("SELECT * FROM report WHERE id = ?", (report_id,))
        report_row = cur.fetchone()
        if report_row is None:
            conn.close()
            return None

        cur.execute(
            "SELECT * FROM test_result WHERE report_id = ? ORDER BY test_index",
            (report_id,),
        )
        test_results = [dict(r) for r in cur.fetchall()]

        cur.execute("SELECT * FROM build_artifact WHERE report_id = ?", (report_id,))
        build_artifacts = [dict(r) for r in cur.fetchall()]

        conn.close()
        return {
            "report": dict(report_row),
            "test_results": test_results,
            "build_artifacts": build_artifacts,
        }

    def list_reports(self, limit=200):
        """For verifying/browsing what's already archived."""
        conn = self._connect()
        cur = conn.cursor()
        cur.execute(
            "SELECT id, path, total_duration, archived_at FROM report "
            "ORDER BY archived_at DESC, id DESC LIMIT ?",
            (limit,),
        )
        rows = [dict(r) for r in cur.fetchall()]
        conn.close()
        return rows


archive_db = ArchiveDB()


def _bundle_rows_match(source_rows, archived_rows):
    if len(source_rows) != len(archived_rows):
        return False
    by_id = {r["id"]: r for r in archived_rows}
    for src in source_rows:
        arc = by_id.get(src["id"])
        if arc is None:
            return False
        for key, value in src.items():
            if arc.get(key) != value:
                return False
    return True


def _bundles_match(source, archived):
    if archived is None:
        return False
    src_report = source["report"]
    arc_report = archived["report"]
    for key in ("id", "path", "total_duration"):
        if src_report.get(key) != arc_report.get(key):
            return False
    if not _bundle_rows_match(source["test_results"], archived["test_results"]):
        return False
    if not _bundle_rows_match(source["build_artifacts"], archived["build_artifacts"]):
        return False
    return True


def _relative_files(root):
    out = {}
    for dirpath, _dirs, filenames in os.walk(root):
        for fname in filenames:
            full = os.path.join(dirpath, fname)
            rel = os.path.relpath(full, root)
            out[rel] = os.path.getsize(full)
    return out


def _dirs_match(src_dir, dst_dir):
    return _relative_files(src_dir) == _relative_files(dst_dir)


def archive_reports(report_ids, report_db, archive_db=archive_db,
                     report_dir=REPORT_DIR, archive_dir=ARCHIVE_REPORT_DIR):
    """Archive each report_id: copy its reports/ subdir and its report.sqlite
    rows to the archive, verify both copies, then remove the originals.

    Nothing is removed for a given report_id unless its archive.db copy reads
    back identical to what was in report.sqlite. Returns
    {"archived": [ids...], "failed": [{"report_id", "reason"}...]}.
    """
    os.makedirs(archive_dir, exist_ok=True)
    archived, failed = [], []

    for report_id in report_ids:
        dst_dir = None
        # Once the archive copy is verified we stop treating dst_dir as
        # scratch: a failure removing the originals after this point must
        # not roll back the one confirmed-good copy we have.
        verified = False
        try:
            bundle = report_db.get_report_bundle(report_id)
            if bundle is None:
                failed.append({"report_id": report_id, "reason": "report not found"})
                continue

            rel_dir = os.path.dirname(bundle["report"]["path"] or "") or "."
            src_dir = os.path.normpath(os.path.join(report_dir, rel_dir))
            dst_dir = os.path.normpath(os.path.join(archive_dir, rel_dir))

            dir_copied = False
            if os.path.isdir(src_dir):
                if os.path.exists(dst_dir):
                    shutil.rmtree(dst_dir)
                shutil.copytree(src_dir, dst_dir)
                if not _dirs_match(src_dir, dst_dir):
                    shutil.rmtree(dst_dir, ignore_errors=True)
                    failed.append({"report_id": report_id, "reason": "report directory copy did not verify"})
                    continue
                dir_copied = True

            archive_db.write_bundle(bundle)
            readback = archive_db.read_bundle(report_id)
            if not _bundles_match(bundle, readback):
                if dir_copied:
                    shutil.rmtree(dst_dir, ignore_errors=True)
                failed.append({"report_id": report_id, "reason": "archive.db verify failed"})
                continue

            # Confirmed written and verified in archive.db (and the report dir,
            # if it had one) -- only now is it safe to remove the originals.
            verified = True
            if dir_copied and os.path.isdir(src_dir):
                shutil.rmtree(src_dir)
            report_db.delete_report(report_id)
            archived.append(report_id)
        except Exception as e:
            if not verified and dst_dir and os.path.isdir(dst_dir):
                shutil.rmtree(dst_dir, ignore_errors=True)
            failed.append({"report_id": report_id, "reason": str(e)})

    return {"archived": archived, "failed": failed}
