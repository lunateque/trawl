import sqlite3
from importlib.resources import files
from pathlib import Path


def _migrate(conn: sqlite3.Connection) -> None:
    directory = files("trawl.db").joinpath("migrations")
    for file in sorted(directory.iterdir(), key=lambda item: item.name):
        if not file.name.endswith(".sql"):
            continue
        version = int(file.name.split("_", 1)[0])
        with conn:
            conn.execute("BEGIN IMMEDIATE")
            if version <= conn.execute("PRAGMA user_version").fetchone()[0]:
                continue
            statement = ""
            for line in file.read_text(encoding="utf-8").splitlines(keepends=True):
                statement += line
                if sqlite3.complete_statement(statement):
                    conn.execute(statement)
                    statement = ""
            if statement.strip():
                raise ValueError(f"incomplete migration: {file.name}")
            conn.execute(f"PRAGMA user_version = {version}")


def open_db(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    try:
        conn.execute("PRAGMA busy_timeout = 5000")
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA foreign_keys = ON")
        conn.row_factory = sqlite3.Row
        _migrate(conn)
    except BaseException:
        conn.close()
        raise
    return conn
