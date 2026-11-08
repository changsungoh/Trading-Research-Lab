"""Rebuild the research CSV from committed canonical SQLite rows."""
from database import init_db, export_signal_research

if __name__ == "__main__":
    init_db()
    export_signal_research()
    print("Research CSV rebuilt from SQLite.")
