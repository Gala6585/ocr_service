from uuid import NAMESPACE_URL, uuid5
import re
from flask import Blueprint, abort, g
from psycopg.types.json import Jsonb

from .common import database
from .datasets import publish
from .http import owned_job, revision_input
from .maintenance import notify_worker

review = Blueprint("review", __name__)


def require_reviewer():
    if g.user["role"] != "reviewer":
        abort(403, "Требуется роль проверяющего")


def get_card(db, job_id, number):
    if type(number) is not int:
        abort(400, "Укажите номер карточки")
    card = db.execute(
        """SELECT c.*,p.width,p.height,m.row_no,m.fio FROM cards c
                      JOIN pages p USING(job_id,page_number) JOIN matches m USING(job_id,card_no)
                      WHERE c.job_id=%s AND card_no=%s""",
        (job_id, number),
    ).fetchone()
    if not card:
        abort(404, "Карточка не найдена")
    return card


def valid_bbox(box, card):
    return (
        isinstance(box, list)
        and len(box) == 4
        and all(type(n) is int for n in box)
        and 0 <= box[0] < box[2] <= card["width"]
        and 0 <= box[1] < box[3] <= card["height"]
    )


def correct_fio(db, job, card, body):
    key = job["id"]
    fio = body.get("fio")
    if (
        not isinstance(fio, str)
        or len(fio) > 160
        or not re.fullmatch(r"[А-Яа-яЁё-]{2,}(?:\s+[А-Яа-яЁё-]{2,})+", fio.strip())
    ):
        abort(400, "Введите фамилию и имя кириллицей, при наличии добавьте отчество")
    # Явный null означает новую строку, иначе по умолчанию правится текущее назначение.
    row_no = body.get("row_no", card["row_no"])
    if row_no is None:
        row_no = db.execute(
            "SELECT coalesce(max(row_no),-1)+1 AS n FROM protocol_rows WHERE job_id=%s", (key,)
        ).fetchone()["n"]
        db.execute(
            "INSERT INTO protocol_rows(job_id,row_no,page_number,kind,raw,fio) VALUES (%s,%s,%s,'manual','{}',%s)",
            (key, row_no, job["protocol_start"], fio.strip()),
        )
    else:
        if type(row_no) is not int:
            abort(400, "Укажите номер строки протокола")
        taken = db.execute(
            "SELECT 1 FROM cards WHERE job_id=%s AND manual_row_no=%s AND card_no<>%s",
            (key, row_no, card["card_no"]),
        ).fetchone()
        if taken:
            abort(409, "Эта строка уже назначена другой карточке вручную")
        changed = db.execute(
            """UPDATE protocol_rows SET fio=%s,needs_review=false WHERE job_id=%s AND row_no=%s
                             AND kind IN ('data','review','manual')""",
            (fio.strip(), key, row_no),
        )
        if not changed.rowcount:
            abort(400, "Выберите строку данных протокола")
    db.execute(
        "UPDATE cards SET manual_row_no=%s WHERE job_id=%s AND card_no=%s",
        (row_no, key, card["card_no"]),
    )
    return row_no


@review.patch("/api/jobs/<uuid:job_id>/corrections")
def correction(job_id):
    require_reviewer()
    with database() as db:
        job = owned_job(db, job_id, ready=True, lock=True)
        body = revision_input(job)
        card = get_card(db, job_id, body.get("card_no"))
        box = body.get("bbox", card["bbox"])
        if not valid_bbox(box, card):
            abort(400, "Координаты выходят за границы страницы")
        before = {"bbox": card["bbox"], "fio": card["fio"], "row_no": card["row_no"]}
        new_row = card["row_no"]
        if "fio" in body:
            new_row = correct_fio(db, job, card, body)
        if box != card["bbox"]:
            db.execute(
                "UPDATE cards SET bbox=%s WHERE job_id=%s AND card_no=%s",
                (Jsonb(box), job_id, card["card_no"]),
            )
            db.execute(
                """UPDATE annotations SET geometry=%s,source='human',status='draft',version=version+1,
                       annotator=%s,verified_at=NULL WHERE job_id=%s AND card_no=%s""",
                (Jsonb(box), g.user["id"], job_id, card["card_no"]),
            )
        elif "fio" not in body:
            abort(400, "Измените ФИО или координаты")
        rev = job["revision"] + 1
        after = {"bbox": box, "fio": body.get("fio", card["fio"]), "row_no": new_row}
        db.execute(
            """INSERT INTO corrections(job_id,card_no,revision,author_id,before_value,after_value)
                      VALUES (%s,%s,%s,%s,%s,%s)""",
            (job_id, card["card_no"], rev, g.user["id"], Jsonb(before), Jsonb(after)),
        )
        db.execute(
            """UPDATE jobs SET revision=%s,status='QUEUED',stage='Пересборка',progress=0,
                   expires_at=NULL,completed_at=NULL,error=NULL,next_dispatch_at=now() WHERE id=%s""",
            (rev, job_id),
        )
    notify_worker(job_id)
    return {"job_id": job_id, "revision": rev, "status": "QUEUED"}, 202


@review.post("/api/jobs/<uuid:job_id>/annotations/verify")
def verify(job_id):
    require_reviewer()
    with database() as db:
        job = owned_job(db, job_id, ready=True, lock=True)
        body = revision_input(job)
        card = get_card(db, job_id, body.get("card_no"))
        db.execute(
            """UPDATE annotations SET source='human',status='verified',annotator=%s,verified_at=now(),
                      version=version+1 WHERE job_id=%s AND card_no=%s AND status<>'verified'""",
            (g.user["id"], job_id, card["card_no"]),
        )
    return {"status": "verified"}


@review.post("/api/jobs/<uuid:job_id>/dataset-promotion")
def promote(job_id):
    require_reviewer()
    with database() as db:
        job = owned_job(db, job_id, ready=True, lock=True)
        body = revision_input(job)
        basis = body.get("storage_basis")
        if (
            body.get("consent") is not True
            or not isinstance(basis, str)
            or not 1 <= len(basis.strip()) <= 500
        ):
            abort(400, "Подтвердите разрешение на хранение исходного PDF и укажите основание")
        labels = db.execute(
            """SELECT c.page_number,a.card_no,a.geometry AS bbox,p.width,p.height,p.dpi,p.rotation,
                        a.version,a.verified_at,u.username AS reviewer FROM annotations a
                        JOIN users u ON a.annotator=u.id JOIN cards c USING(job_id,card_no)
                        JOIN pages p USING(job_id,page_number) WHERE a.job_id=%s AND a.status='verified'
                        ORDER BY a.card_no""",
            (job_id,),
        ).fetchall()
        if not labels:
            abort(409, "Сначала проверьте и подтвердите хотя бы одну рамку")
        for item in labels:
            item["verified_at"] = item["verified_at"].isoformat()
        versions = ",".join(f"{a['card_no']}.{a['version']}" for a in labels)
        dataset_id = uuid5(NAMESPACE_URL, f"ocr:{job_id}:{job['revision']}:{versions}")
        existing = db.execute("SELECT status FROM datasets WHERE id=%s", (dataset_id,)).fetchone()
        if existing and existing["status"] == "READY":
            return {"dataset_id": dataset_id, "verified_cards": len(labels)}
        db.execute(
            """INSERT INTO datasets(id,job_id,revision,approved_by,storage_basis,source_relative_path,
                   annotations_relative_path,sha256,annotations,status) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,'PENDING')
                   ON CONFLICT(id) DO UPDATE SET status='PENDING'""",
            (
                dataset_id,
                job_id,
                job["revision"],
                g.user["id"],
                basis.strip(),
                f"datasets/sources/{dataset_id}/source.pdf",
                f"datasets/annotations/{dataset_id}/verified.json",
                job["sha256"],
                Jsonb(labels),
            ),
        )
    if not publish(dataset_id):
        abort(410, "Срок хранения исходного PDF истёк")
    return {"dataset_id": dataset_id, "verified_cards": len(labels)}, 201
