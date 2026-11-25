"""Применить версионированные SQL-файлы и настроить учётные записи первого запуска."""

import os
from pathlib import Path

from psycopg import sql
from werkzeug.security import generate_password_hash

from .common import database


def main():
    with database() as db:
        db.execute("SELECT pg_advisory_xact_lock(462019)")
        db.execute(
            "CREATE TABLE IF NOT EXISTS schema_migrations (name text PRIMARY KEY, applied_at timestamptz DEFAULT now())"
        )
        for file in sorted((Path(__file__).resolve().parents[1] / "migrations").glob("*.sql")):
            if db.execute("SELECT 1 FROM schema_migrations WHERE name=%s", (file.name,)).fetchone():
                continue
            db.execute(file.read_text(encoding="utf-8"))
            db.execute("INSERT INTO schema_migrations(name) VALUES (%s)", (file.name,))
        for role, secret in [("ocr_app", "APP_DB_PASSWORD"), ("ocr_readonly", "READONLY_DB_PASSWORD")]:
            exists = db.execute("SELECT 1 FROM pg_roles WHERE rolname=%s", (role,)).fetchone()
            command = sql.SQL("ALTER ROLE" if exists else "CREATE ROLE")
            db.execute(
                sql.SQL("{} {} LOGIN PASSWORD {}").format(
                    command, sql.Identifier(role), sql.Literal(os.environ[secret])
                )
            )
        db.execute("REVOKE CREATE ON SCHEMA public FROM PUBLIC")
        db.execute("GRANT USAGE ON SCHEMA public TO ocr_app, ocr_readonly")
        db.execute("GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO ocr_app")
        db.execute("GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO ocr_app")
        db.execute("REVOKE ALL ON schema_migrations FROM ocr_app")
        db.execute("GRANT SELECT ON ALL TABLES IN SCHEMA public TO ocr_readonly")
        db.execute("REVOKE SELECT ON users FROM ocr_readonly")
        db.execute("GRANT SELECT(id,username,role) ON users TO ocr_readonly")
        db.execute(
            "INSERT INTO users(username,password_hash,role) VALUES (%s,%s,'reviewer') "
            "ON CONFLICT(username) DO NOTHING",
            (
                os.environ.get("OCR_USERNAME", "operator"),
                generate_password_hash(os.environ["OCR_PASSWORD"]),
            ),
        )


if __name__ == "__main__":
    main()
