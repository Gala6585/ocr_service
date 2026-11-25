"""Доставка заданий из PostgreSQL в RQ и очистка истёкших файлов."""

import logging
import os
import shutil
import time

from rq import Queue
from .common import database, job_dir, redis_client

log = logging.getLogger(__name__)
LOCK_KEY = "hashtextextended(%s::text, 0)"


def dispatch(job_id=None):
    conn = redis_client()
    queue = Queue("ocr", connection=conn)
    with database() as db:
        where = "AND id=%s" if job_id else ""
        rows = db.execute(
            f"""SELECT id,revision FROM jobs WHERE status IN ('QUEUED','RUNNING')
                              AND next_dispatch_at<=now() {where} ORDER BY next_dispatch_at
                              LIMIT 32 FOR UPDATE SKIP LOCKED""",
            (job_id,) if job_id else (),
        ).fetchall()
        for row in rows:
            key = str(row["id"])
            rq_id = f"{key}-{row['revision']}"
            db.execute(
                "UPDATE jobs SET next_dispatch_at=now()+interval '30 seconds' WHERE id=%s", (key,)
            )
            if not db.execute(
                f"SELECT pg_try_advisory_xact_lock({LOCK_KEY}) AS free", (key,)
            ).fetchone()["free"]:
                continue
            if conn.lpos(queue.key, rq_id) is None:
                queue.enqueue(
                    "service.tasks.process_job",
                    key,
                    row["revision"],
                    job_id=rq_id,
                    job_timeout=7200,
                    result_ttl=0,
                    failure_ttl=86400,
                )


def notify_worker(job_id):
    try:
        dispatch(job_id)
    except Exception:
        # Данные уже зафиксированы. Периодическая доставка повторит попытку.
        log.exception("Не удалось сразу поставить задание %s в очередь", job_id)


def cleanup_one():
    with database() as db:
        row = db.execute(
            """SELECT id FROM jobs j
              WHERE (status='DELETING' OR (status IN ('COMPLETED','FAILED') AND expires_at<=now()))
                AND NOT EXISTS(SELECT 1 FROM download_events d WHERE d.job_id=j.id
                               AND d.download_lease_until>now())
                AND NOT EXISTS(SELECT 1 FROM datasets s WHERE s.job_id=j.id AND s.status='PENDING'
                               AND s.created_at>now()-interval '5 minutes')
              ORDER BY expires_at LIMIT 1 FOR UPDATE OF j SKIP LOCKED"""
        ).fetchone()
        if not row:
            return False
        key = row["id"]
        db.execute("UPDATE jobs SET status='DELETING' WHERE id=%s", (key,))
    # Второй процесс может подобрать DELETING после перезапуска. Блокировка
    # также сериализует такие повторы, пока удаляется каталог.
    with database() as db:
        job = db.execute("SELECT status FROM jobs WHERE id=%s FOR UPDATE", (key,)).fetchone()
        if job["status"] != "DELETING":
            return True
        folder = job_dir(key)
        if folder.exists():
            shutil.rmtree(folder)
        for table in ("corrections", "matches", "annotations", "cards", "protocol_rows", "pages"):
            db.execute(f"DELETE FROM {table} WHERE job_id=%s", (key,))
        db.execute("UPDATE artifacts SET deleted_at=now() WHERE job_id=%s", (key,))
        db.execute("UPDATE documents SET filename='source.pdf' WHERE job_id=%s", (key,))
        db.execute(
            """UPDATE jobs SET status='EXPIRED', stage='Срок хранения истёк', cleaned_at=now(),
                   warnings='[]', error=NULL, qualification='' WHERE id=%s""",
            (key,),
        )
    return True


def main():
    logging.basicConfig(level=logging.INFO)
    while True:
        try:
            dispatch()
        except Exception:
            log.exception("Сбой доставки в очередь")
        try:
            for _ in range(32):
                if not cleanup_one():
                    break
        except Exception:
            log.exception("Сбой очистки")
        time.sleep(max(1, int(os.getenv("CLEANUP_INTERVAL_SECONDS", 30))))


if __name__ == "__main__":
    main()
