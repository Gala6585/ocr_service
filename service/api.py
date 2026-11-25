from datetime import datetime
from pathlib import Path
from uuid import uuid4
import shutil

from flask import Flask, abort, g, request
import pymupdf
from werkzeug.exceptions import HTTPException
from werkzeug.security import check_password_hash

from .common import database, digest, job_dir, relative
from .files import files
from .http import JSONProvider, owned_job
from .maintenance import notify_worker
from .review import review

app = Flask(__name__)
app.json = JSONProvider(app)
app.config["MAX_CONTENT_LENGTH"] = 101 * 1024 * 1024
app.register_blueprint(files)
app.register_blueprint(review)


@app.errorhandler(HTTPException)
def http_error(error):
    return {"error": error.description}, error.code


@app.before_request
def authenticate():
    if request.path == "/health":
        return
    auth = request.authorization
    if auth and auth.type == "basic" and auth.username and auth.password:
        with database() as db:
            user = db.execute(
                "SELECT id,username,role,password_hash FROM users WHERE username=%s", (auth.username,)
            ).fetchone()
        if user and check_password_hash(user.pop("password_hash"), auth.password):
            g.user = user
            return
    return {"error": "Требуются логин и пароль сервиса"}, 401, {"WWW-Authenticate": 'Basic realm="OCR"'}


@app.get("/health")
def health():
    with database() as db:
        db.execute("SELECT 1")
    return {"status": "ok"}


@app.get("/api/me")
def whoami():
    return g.user


def integer_field(name, default=None):
    value = request.form.get(name)
    if value in (None, ""):
        return default
    try:
        return int(value)
    except ValueError:
        abort(400, f"Параметр {name} должен быть целым числом")


@app.post("/api/jobs")
def add_job():
    upload = request.files.get("pdf")
    if not upload or not upload.filename:
        abort(400, "Требуется файл в поле pdf")
    text_date = request.form.get("expiry_date", "").strip()
    expiry_date = None
    for fmt in ("%d.%m.%Y", "%Y-%m-%d"):
        try:
            expiry_date = datetime.strptime(text_date, fmt).date()
            break
        except ValueError:
            pass
    if text_date and not expiry_date:
        abort(400, "Укажите дату окончания в формате ДД.ММ.ГГГГ или ГГГГ-ММ-ДД")
    start = integer_field("protocol_start", 1)
    end = integer_field("protocol_end", start)
    column = integer_field("fio_column")
    qual = request.form.get("qualification", "").strip()
    if len(qual) > 200 or (column is not None and not 0 <= column <= 100):
        abort(400, "Проверьте квалификацию и номер столбца ФИО")
    key = uuid4()
    folder = job_dir(key)
    source = folder / "input" / "source.pdf"
    source.parent.mkdir(parents=True)
    try:
        upload.save(source)
        if source.stat().st_size > 100 * 1024**2:
            abort(413, "Максимальный размер PDF — 100 МБ")
        try:
            with pymupdf.open(source) as pdf:
                if not pdf.is_pdf or pdf.needs_pass or not 1 <= len(pdf) <= 200:
                    abort(400, "Требуется PDF без пароля, от 1 до 200 страниц")
                count = len(pdf)
                if not 1 <= start <= end <= count:
                    abort(400, "Страницы протокола должны входить в диапазон PDF, начиная с 1")
                if any(page.rect.width * page.rect.height * (200 / 72) ** 2 > 32_000_000 for page in pdf):
                    abort(400, "Слишком большой размер страницы для распознавания")
        except (pymupdf.FileDataError, RuntimeError, ValueError):
            abort(400, "Не удалось прочитать PDF")
        filename = Path(upload.filename.replace(chr(92), "/")).name[:250] or "source.pdf"
        with database() as db:
            db.execute(
                """INSERT INTO jobs(id,owner_id,status,protocol_start,protocol_end,
                       fio_column,expiry_date,qualification) VALUES (%s,%s,'QUEUED',%s,%s,%s,%s,%s)""",
                (key, g.user["id"], start, end, column, expiry_date, qual),
            )
            db.execute(
                "INSERT INTO documents VALUES (%s,%s,%s,%s,%s)",
                (key, filename, relative(source), digest(source), count),
            )
    except Exception:
        shutil.rmtree(folder)
        raise
    notify_worker(key)
    return {"job_id": key, "status": "QUEUED", "revision": 1}, 202


@app.get("/api/jobs")
def list_jobs():
    with database() as db:
        rows = db.execute(
            """SELECT j.id,j.status,j.progress,j.stage,j.created_at,j.expires_at,d.filename
                          FROM jobs j JOIN documents d ON d.job_id=j.id WHERE j.owner_id=%s
                          ORDER BY created_at DESC""",
            (g.user["id"],),
        ).fetchall()
    return {"jobs": rows}


@app.get("/api/jobs/<uuid:job_id>")
def get_job(job_id):
    with database() as db:
        job = owned_job(db, job_id)
    return {
        k: job[k]
        for k in (
            "id",
            "filename",
            "status",
            "stage",
            "progress",
            "revision",
            "published_revision",
            "expiry_date",
            "qualification",
            "page_count",
            "created_at",
            "completed_at",
            "expires_at",
            "last_download_requested_at",
            "cleaned_at",
            "warnings",
            "error",
        )
    }


@app.get("/api/jobs/<uuid:job_id>/results")
def results(job_id):
    with database() as db:
        job = owned_job(db, job_id, ready=True, lock=True)
        cards = db.execute(
            """SELECT c.card_no,c.page_number,c.bbox,c.detected_xywh,c.warnings,m.row_no,
                   m.fio,m.score,m.needs_review,m.output_file,a.status AS annotation_status,
                   p.width,p.height,p.dpi FROM cards c JOIN pages p USING(job_id,page_number)
                   JOIN matches m USING(job_id,card_no) JOIN annotations a USING(job_id,card_no)
                   WHERE c.job_id=%s ORDER BY c.card_no""",
            (job_id,),
        ).fetchall()
        rows = db.execute(
            "SELECT row_no,page_number,kind,raw,fio,needs_review FROM protocol_rows "
            "WHERE job_id=%s ORDER BY row_no",
            (job_id,),
        ).fetchall()
        pages = db.execute(
            "SELECT page_number,width,height,dpi,rotation FROM pages WHERE job_id=%s "
            "AND preview_relative_path IS NOT NULL ORDER BY page_number",
            (job_id,),
        ).fetchall()
    return {"revision": job["revision"], "cards": cards, "pages": pages, "protocol_rows": rows}
