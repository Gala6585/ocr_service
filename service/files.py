from time import monotonic
from flask import Blueprint, Response, abort, g, send_file

from .common import DOWNLOAD_TTL, database, job_file
from .http import owned_job

files = Blueprint("files", __name__)


@files.get("/api/jobs/<uuid:job_id>/pages/<int:number>/preview")
def preview(job_id, number):
    with database() as db:
        owned_job(db, job_id, ready=True, lock=True)
        row = db.execute(
            "SELECT preview_relative_path FROM pages WHERE job_id=%s AND page_number=%s",
            (job_id, number),
        ).fetchone()
        if not row or not row["preview_relative_path"]:
            abort(404, "Превью отсутствует")
        response = send_file(
            job_file(job_id, row["preview_relative_path"]), mimetype="image/webp", max_age=0
        )
    response.cache_control.no_store = True
    return response


@files.post("/api/jobs/<uuid:job_id>/downloads")
def download(job_id):
    with database() as db:
        job = owned_job(db, job_id, ready=True, lock=True)
        item = db.execute(
            "SELECT id,relative_path FROM artifacts WHERE job_id=%s AND revision=%s AND kind='zip'",
            (job_id, job["published_revision"]),
        ).fetchone()
        path = job_file(job_id, item["relative_path"])
        event = db.execute(
            """INSERT INTO download_events(job_id,user_id,artifact_id,download_lease_until)
                     VALUES (%s,%s,%s,now()+interval '5 minutes') RETURNING id""",
            (job_id, g.user["id"], item["id"]),
        ).fetchone()["id"]
        db.execute(
            "UPDATE jobs SET last_download_requested_at=now(),expires_at=now()+%s*interval '1 second' WHERE id=%s",
            (DOWNLOAD_TTL, job_id),
        )
        file = path.open("rb")

    def chunks():
        renewed = monotonic()
        while chunk := file.read(256 * 1024):
            if monotonic() - renewed >= 60:
                with database() as db:
                    db.execute(
                        "UPDATE download_events SET download_lease_until=now()+interval '5 minutes' WHERE id=%s",
                        (event,),
                    )
                renewed = monotonic()
            yield chunk

    response = Response(chunks(), mimetype="application/zip")
    response.content_length = path.stat().st_size
    response.headers["Content-Disposition"] = f'attachment; filename="result-{job_id}.zip"'
    response.cache_control.no_store = True

    @response.call_on_close
    def release():
        file.close()
        with database() as db:
            db.execute("UPDATE download_events SET download_lease_until=NULL WHERE id=%s", (event,))

    return response
