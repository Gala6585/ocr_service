from datetime import date, datetime
from uuid import UUID

from flask import abort, g, request
from flask.json.provider import DefaultJSONProvider


class JSONProvider(DefaultJSONProvider):
    ensure_ascii = False

    @staticmethod
    def default(value):
        if isinstance(value, (date, datetime)):
            return value.isoformat()
        if isinstance(value, UUID):
            return str(value)
        return DefaultJSONProvider.default(value)


def owned_job(db, job_id, *, lock=False, ready=False):
    query = """SELECT j.*, d.filename,d.page_count,d.source_relative_path,d.sha256,
               (j.expires_at<=now()) AS expired
               FROM jobs j JOIN documents d ON j.id=d.job_id WHERE j.id=%s AND j.owner_id=%s"""
    if lock:
        query += " FOR UPDATE OF j"
    job = db.execute(query, (job_id, g.user["id"])).fetchone()
    if not job:
        abort(404, "Задание не найдено")
    if ready:
        if job["status"] in ("DELETING", "EXPIRED") or job["expired"]:
            abort(410, "Срок хранения результата истёк")
        if job["status"] != "COMPLETED":
            abort(409, "Результат ещё недоступен")
    return job


def revision_input(job):
    body = request.get_json()
    if not isinstance(body, dict):
        abort(400, "Ожидается объект JSON")
    if type(body.get("revision")) is not int or body["revision"] != job["revision"]:
        abort(409, "Версия задания изменилась. Обновите результаты")
    return body
