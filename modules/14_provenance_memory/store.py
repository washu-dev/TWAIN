import sqlite3

class Store:
    def __init__(self):
        with sqlite3.connect("data.db") as conn:
            cur = conn.cursor()
            create_table = """ CREATE TABLE IF NOT EXISTS sessions (
            session_id INTEGER NOT NULL PRIMARY KEY AUTOINCREMENT,
            researcher_id INTEGER NOT NULL,
            execution_plan TEXT NOT NULL,
            execution_state TEXT NOT NULL,
            provenance_log TEXT NOT NULL)
            """
