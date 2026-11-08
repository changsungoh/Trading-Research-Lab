import sqlite3
import json
import csv
import os
import tempfile
import hashlib
import shutil
from contextlib import contextmanager
from pathlib import Path


DB_FILE = "trading_engine.db"


def get_connection():
    return sqlite3.connect(DB_FILE)


@contextmanager
def research_connection():
    conn = get_connection()
    try:
        with conn:
            yield conn
    finally:
        conn.close()


def init_db():
    conn = get_connection()
    cursor = conn.cursor()

    cursor.execute("""
    CREATE TABLE IF NOT EXISTS signals (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        timestamp TEXT,
        stock TEXT,
        code TEXT,
        price REAL,
        change_rate REAL,
        strength REAL,
        trading_value TEXT,
        setup_quality TEXT,
        rule_version TEXT
    )
    """)

    cursor.execute("""
    CREATE TABLE IF NOT EXISTS signal_research (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        entry_time TEXT,
        exit_time TEXT,
        code TEXT,
        name TEXT,
        entry_price REAL,
        last_price REAL,
        setup_quality TEXT,
        change_rate REAL,
        strength REAL,
        trading_value REAL,
        change_rate_bucket TEXT,
        strength_bucket TEXT,
        trading_value_bucket TEXT,
        momentum_efficiency REAL,
        theme_score REAL,
        matched_themes TEXT,
        rule_version TEXT,
        ret_5m REAL,
        ret_10m REAL,
        ret_30m REAL,
        max_return_30m REAL,
        min_return_30m REAL,
        first_touch TEXT,
        first_touch_time TEXT,
        first_touch_return REAL
    )
    """)
    cursor.execute("PRAGMA table_info(signal_research)")
    existing_columns = {row[1] for row in cursor.fetchall()}

    new_columns = {
        "trading_value": "REAL",
        "change_rate_bucket": "TEXT",
        "strength_bucket": "TEXT",
        "trading_value_bucket": "TEXT",
        "momentum_efficiency": "REAL",
        "candidate_id": "TEXT",
        **{f"{field}_{m}m": "REAL" if field == "price" else "TEXT"
           for m in (5, 10, 30) for field in ("target_timestamp", "observed_timestamp", "price", "status")},
    }

    for column_name, column_type in new_columns.items():
        if column_name not in existing_columns:
            cursor.execute(
                f"ALTER TABLE signal_research ADD COLUMN {column_name} {column_type}"
            )

    cursor.execute("""
    CREATE TABLE IF NOT EXISTS trades (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        timestamp TEXT,
        action TEXT,
        stock TEXT,
        code TEXT,
        price REAL,
        quantity INTEGER,
        return_pct REAL,
        pnl REAL,
        reason TEXT
    )
    """)

    cursor.execute("CREATE UNIQUE INDEX IF NOT EXISTS research_candidate_id ON signal_research(candidate_id)")
    cursor.execute("CREATE INDEX IF NOT EXISTS research_identity_lookup ON signal_research(code, entry_time, COALESCE(rule_version, ''))")
    cursor.execute("""CREATE TRIGGER IF NOT EXISTS research_no_duplicate
        BEFORE INSERT ON signal_research
        WHEN EXISTS (SELECT 1 FROM signal_research
            WHERE code = NEW.code AND entry_time = NEW.entry_time
            AND COALESCE(rule_version, '') = COALESCE(NEW.rule_version, ''))
        BEGIN SELECT RAISE(IGNORE); END""")
    cursor.execute("CREATE TABLE IF NOT EXISTS research_active (code TEXT PRIMARY KEY, payload TEXT NOT NULL)")
    cursor.execute("""CREATE VIEW IF NOT EXISTS canonical_signal_research AS
        SELECT * FROM signal_research WHERE id IN
        (SELECT MIN(id) FROM signal_research GROUP BY code, entry_time, COALESCE(rule_version, ''))""")
    conn.commit()
    conn.close()


def insert_signal_research(signal):
    with research_connection() as conn:
        cursor = conn.cursor()
        conn.execute("BEGIN IMMEDIATE")
        identity = (str(signal.get("code") or ""), signal.get("entry_time"), signal.get("rule_version") or "")
        if not identity[0] or not identity[1]:
            raise ValueError("Research requires code and entry_time")
        if conn.execute("SELECT 1 FROM signal_research WHERE code=? AND entry_time=? AND COALESCE(rule_version, '')=?", identity).fetchone():
            return False

        matched_themes = signal.get("matched_themes", "")

        if isinstance(matched_themes, list):
            matched_themes = json.dumps(matched_themes, ensure_ascii=False)

        columns = [
            "entry_time",
            "exit_time",
            "code",
            "name",
            "entry_price",
            "last_price",
            "setup_quality",
            "change_rate",
            "strength",
            "trading_value",
            "change_rate_bucket",
            "strength_bucket",
            "trading_value_bucket",
            "momentum_efficiency",
            "theme_score",
            "matched_themes",
            "rule_version",
            "ret_5m",
            "ret_10m",
            "ret_30m",
            "max_return_30m",
            "min_return_30m",
            "first_touch",
            "first_touch_time",
            "first_touch_return",
        ]

        columns += ["candidate_id"] + [f"{field}_{m}m" for m in (5, 10, 30)
            for field in ("target_timestamp", "observed_timestamp", "price", "status")]
        signal = dict(signal)
        signal["candidate_id"] = hashlib.sha256(json.dumps(identity).encode()).hexdigest()
        values = []

        for column in columns:
            if column == "matched_themes":
                values.append(matched_themes)
            else:
                values.append(signal.get(column))

        placeholders = ", ".join(["?"] * len(columns))
        column_names = ", ".join(columns)

        cursor.execute(
            f"""
            INSERT INTO signal_research (
                {column_names}
            )
            VALUES ({placeholders})
            """,
            values,
        )

        conn.commit()
        return True


def count_rows(table_name):
    conn = get_connection()
    cursor = conn.cursor()

    cursor.execute(f"SELECT COUNT(*) FROM {table_name}")
    count = cursor.fetchone()[0]

    conn.close()
    return count


def preview_signal_research(limit=5):
    conn = get_connection()
    cursor = conn.cursor()

    cursor.execute("""
    SELECT
        id,
        entry_time,
        code,
        name,
        setup_quality,
        ret_5m,
        ret_10m,
        ret_30m,
        first_touch
    FROM signal_research
    ORDER BY id DESC
    LIMIT ?
    """, (limit,))

    rows = cursor.fetchall()
    conn.close()

    return rows


if __name__ == "__main__":
    init_db()
    print("Database initialized successfully.")
    print(f"DB file: {Path(DB_FILE).resolve()}")

def load_research_active():
    with research_connection() as conn:
        return {code: json.loads(payload) for code, payload in conn.execute("SELECT code, payload FROM research_active")}


def save_research_active(signals):
    with research_connection() as conn:
        conn.execute("DELETE FROM research_active")
        conn.executemany("INSERT INTO research_active VALUES (?, ?)",
                         [(code, json.dumps(signal, ensure_ascii=False)) for code, signal in signals.items()])


def export_signal_research(path="signal_research.csv"):
    # Rebuild from committed rows; failed replacement leaves the previous CSV intact.
    with research_connection() as conn:
        cursor = conn.execute("SELECT * FROM canonical_signal_research ORDER BY id")
        columns = [item[0] for item in cursor.description]
        rows = cursor.fetchall()
    destination = Path(path).resolve()
    if destination.exists():
        with destination.open(encoding="utf-8-sig", newline="") as existing:
            header = next(csv.reader(existing), [])
        if "candidate_id" not in header:
            backup = destination.with_name(destination.name + ".pre_p0")
            # Exclusive creation protects the original CSV-only history.
            if not backup.exists():
                with destination.open("rb") as source, backup.open("xb") as target:
                    shutil.copyfileobj(source, target)
    fd, temporary = tempfile.mkstemp(dir=destination.parent, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8-sig", newline="") as stream:
            writer = csv.writer(stream)
            writer.writerow(columns)
            writer.writerows(rows)
        os.replace(temporary, destination)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
