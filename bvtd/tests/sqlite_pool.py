"""Pool giả bằng SQLite (trong bộ nhớ) cho test lớp patient_db khi không có PostgreSQL.
Chỉ dùng cho test: chạy đúng schema.sql (đã dịch sang SQLite) và đúng câu SQL của patient_db."""
import os
import re
import sqlite3
from contextlib import contextmanager
from datetime import date, datetime

sqlite3.register_adapter(datetime, lambda d: d.isoformat(sep=" "))
sqlite3.register_adapter(date, lambda d: d.isoformat())

SCHEMA_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "schema.sql")


def patients_ddl_for_sqlite() -> str:
    txt = open(SCHEMA_PATH, encoding="utf-8").read()
    txt = txt[txt.index("CREATE TABLE IF NOT EXISTS patients"):]
    txt = "\n".join(l.split("--")[0] for l in txt.splitlines())
    txt = re.sub(r"BIGSERIAL\s+PRIMARY KEY", "INTEGER PRIMARY KEY AUTOINCREMENT", txt)
    return txt.replace("now()", "CURRENT_TIMESTAMP")


class Conn:
    is_sqlite = True

    def __init__(self, raw):
        self.raw = raw

    def execute(self, sql, args=()):
        return self.raw.execute(sql.replace("%s", "?"), tuple(args))


class Pool:
    def __init__(self):
        self.raw = sqlite3.connect(":memory:", check_same_thread=False)
        self.raw.row_factory = sqlite3.Row
        self.raw.executescript(patients_ddl_for_sqlite())

    @contextmanager
    def connection(self):
        try:
            yield Conn(self.raw)
            self.raw.commit()
        except Exception:
            self.raw.rollback()
            raise
