"""Исполнение одной версии задания с публикацией файлов перед фиксацией в БД."""

from collections import defaultdict
from functools import lru_cache
from pathlib import Path
from tempfile import TemporaryDirectory
from zipfile import ZipFile, ZIP_DEFLATED
import fcntl
import json
import logging
import os
import shutil

import cv2
import pandas as pd
import pymupdf
from psycopg.types.json import Jsonb

from . import core
from .common import FAILED_TTL, RESULT_TTL, database, digest, job_dir, job_file, relative
from .maintenance import LOCK_KEY


@lru_cache(maxsize=1)
def reader():
    import easyocr
    import torch

    torch.set_num_threads(max(1, int(os.getenv("OMP_NUM_THREADS", 2))))
    folder = Path(os.getenv("EASYOCR_MODULE_PATH", "/models"))
    folder.mkdir(parents=True, exist_ok=True)
    # Несколько воркеров могут одновременно впервые загрузить одни и те же веса.
    with (folder / "download.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        return easyocr.Reader(
            ["ru"], gpu=False, model_storage_directory=str(folder / "model"), verbose=False
        )


def read_json(path):
    return json.loads(path.read_text(encoding="utf-8"))


def first_pass(job, source, scratch, ocr, progress):
    result = core.run_pipeline(
        source,
        job["protocol_start"],
        job["protocol_end"],
        job["expiry_date"],
        job["qualification"],
        output_root=scratch,
        fio_column=job["fio_column"],
        reader=ocr,
        progress=progress,
    )
    run = Path(result["run_dir"])
    recognised = {r["Строка протокола"]: r for r in result["protocol_df"].to_dict("records")}
    rows = []
    for i, raw in enumerate(read_json(run / "protocol/ocr_rows.json")):
        entry = recognised.get(i, {})
        rows.append(
            (
                i,
                raw["page"],
                raw["kind"],
                Jsonb(raw),
                entry.get("ФИО"),
                raw["kind"] == "review" or entry.get("Проверить строку") == "Да",
            )
        )
    return run, result["cards"], rows, result["kernel_diagnostics"], result["warnings"]


def revised_pass(db, job, source, scratch, ocr, progress):
    key = job["id"]
    cards = db.execute(
        """SELECT card_no,filename,page_number AS page,bbox AS bbox_crop_xyxy,
                          detected_xywh AS bbox_detected_xywh,warnings,manual_row_no
                          FROM cards WHERE job_id=%s ORDER BY card_no""",
        (key,),
    ).fetchall()
    records = db.execute(
        """SELECT fio AS "ФИО", row_no AS "Строка протокола",
              CASE WHEN needs_review THEN 'Да' ELSE 'Нет' END AS "Проверить строку"
              FROM protocol_rows WHERE job_id=%s AND fio IS NOT NULL ORDER BY row_no""",
        (key,),
    ).fetchall()
    frame = pd.DataFrame(records, columns=["ФИО", "Строка протокола", "Проверить строку"])
    run = scratch / "revised"
    (run / "protocol").mkdir(parents=True)
    progress(35, "Восстановление вырезок по координатам")
    last_page = None
    with pymupdf.open(source) as pdf:
        artifact = db.execute(
            "SELECT relative_path FROM artifacts WHERE job_id=%s AND revision=%s AND kind='protocol'",
            (key, job["published_revision"]),
        ).fetchone()
        shutil.copyfile(job_file(key, artifact["relative_path"]), run / "protocol/protocol_pages.pdf")
        for card in cards:
            if card["page"] != last_page:
                last_page = card["page"]
                image = core.pdf_page_to_bgr(pdf[last_page - 1], 150)
            x1, y1, x2, y2 = card["bbox_crop_xyxy"]
            path = run / "cards_original" / card["filename"]
            core.save_image(path, image[y1:y2, x1:x2])
            card["file_path"] = str(path)
    overrides = {c["filename"]: c["manual_row_no"] for c in cards if c["manual_row_no"] is not None}
    progress(65, "Пересчёт сопоставлений и отчёта")
    core.ResultAssembler(ocr).assemble(
        cards, frame, run, job["expiry_date"], job["qualification"], confirmed_rows=overrides
    )
    return run, cards, [], [], job["warnings"]


def write_webp(path, image, quality):
    ok, encoded = cv2.imencode(".webp", image, [cv2.IMWRITE_WEBP_QUALITY, quality])
    if not ok:
        raise OSError("Ошибка записи WebP")
    path.write_bytes(encoded.tobytes())


def publish_files(job, source, run, cards, diagnostics):
    # Только владелец advisory lock пишет эту ревизию. При повторе после сбоя
    # её незавершённые файлы ещё не видны через API.
    dest = job_dir(job["id"]) / "revisions" / str(job["revision"])
    work, preview, output = (dest / name for name in ("work", "preview", "output"))
    for directory in (work, preview, output):
        directory.mkdir(parents=True, exist_ok=True)
    by_page = defaultdict(list)
    for number, card in enumerate(cards, 1):
        card["card_no"] = number
        crop = work / f"{number}.webp"
        write_webp(crop, core.load_image(card["file_path"]), 101)  # OpenCV lossless WebP
        card["crop_path"] = relative(crop)
        by_page[card["page"]].append(card)
    kernels = {k["page"]: k for k in diagnostics}
    pages = []
    with pymupdf.open(source) as pdf:
        for number, page in enumerate(pdf, 1):
            dpi = 200 if job["protocol_start"] <= number <= job["protocol_end"] else 150
            rect = (page.rect * pymupdf.Matrix(dpi / 72, dpi / 72)).irect
            width, height = rect.width, rect.height
            preview_path = None
            if dpi == 150:
                image = core.pdf_page_to_bgr(page, dpi)
                height, width = image.shape[:2]
                for card in by_page[number]:
                    x1, y1, x2, y2 = card["bbox_crop_xyxy"]
                    cv2.rectangle(image, (x1, y1), (x2 - 1, y2 - 1), (40, 120, 230), 3)
                    cv2.putText(
                        image,
                        str(card["card_no"]),
                        (x1 + 4, y1 + 25),
                        cv2.FONT_HERSHEY_SIMPLEX,
                        0.9,
                        (30, 60, 180),
                        2,
                    )
                if width > 1200:
                    image = cv2.resize(
                        image, (1200, max(1, round(height * 1200 / width))), interpolation=cv2.INTER_AREA
                    )
                target = preview / f"{number}.webp"
                write_webp(target, image, 78)
                preview_path = relative(target)
            pages.append(
                (number, width, height, dpi, page.rotation, Jsonb(kernels.get(number)), preview_path)
            )
    protocol, excel, archive = (output / n for n in ("protocol_pages.pdf", "report.xlsx", "result.zip"))
    shutil.copyfile(run / "protocol/protocol_pages.pdf", protocol)
    shutil.copyfile(run / "report.xlsx", excel)
    partial = archive.with_suffix(".part")
    with ZipFile(partial, "w", compression=ZIP_DEFLATED) as zipped:
        for file in sorted((run / "ready").glob("*.png")):
            zipped.write(file, "cards/" + file.name)
        for file in (protocol, excel):
            zipped.write(file, file.name)
    partial.replace(archive)
    artifacts = [
        (kind, relative(path), mime, digest(path))
        for kind, path, mime in (
            ("zip", archive, "application/zip"),
            ("protocol", protocol, "application/pdf"),
            ("report", excel, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"),
        )
    ]
    return pages, artifacts


def save_result(db, job, cards, rows, pages, artifacts, matches, warnings):
    key, rev = job["id"], job["revision"]
    with db.transaction(), db.cursor() as cur:
        if not job["published_revision"]:
            cur.executemany(
                "INSERT INTO protocol_rows(job_id,row_no,page_number,kind,raw,fio,needs_review) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s)",
                [(key, *r) for r in rows],
            )
        cur.executemany(
            """INSERT INTO pages VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
                        ON CONFLICT(job_id,page_number) DO UPDATE SET preview_relative_path=excluded.preview_relative_path""",
            [(key, *p) for p in pages],
        )
        cur.executemany(
            """INSERT INTO cards(job_id,card_no,page_number,filename,bbox,detected_xywh,warnings,crop_relative_path)
                           VALUES (%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT(job_id,card_no)
                           DO UPDATE SET crop_relative_path=excluded.crop_relative_path""",
            [
                (
                    key,
                    c["card_no"],
                    c["page"],
                    c["filename"],
                    Jsonb(c["bbox_crop_xyxy"]),
                    Jsonb(c["bbox_detected_xywh"]),
                    Jsonb(c["warnings"]),
                    c["crop_path"],
                )
                for c in cards
            ],
        )
        cur.executemany(
            """INSERT INTO annotations(job_id,card_no,geometry,source,status,version)
                           VALUES (%s,%s,%s,'auto','draft',1) ON CONFLICT DO NOTHING""",
            [(key, c["card_no"], Jsonb(c["bbox_crop_xyxy"])) for c in cards],
        )
        cur.execute("DELETE FROM matches WHERE job_id=%s", (key,))
        cur.executemany(
            "INSERT INTO matches VALUES (%s,%s,%s,%s,%s,%s,%s)",
            [
                (
                    key,
                    i,
                    int(m["protocol_row_id"]) if m["protocol_row_id"] is not None else None,
                    m["fio"],
                    m["score"],
                    m["needs_review"],
                    m["output_file"],
                )
                for i, m in enumerate(matches, 1)
            ],
        )
        cur.executemany(
            "INSERT INTO artifacts(job_id,revision,kind,relative_path,media_type,sha256) VALUES (%s,%s,%s,%s,%s,%s)",
            [(key, rev, *a) for a in artifacts],
        )
        cur.execute(
            """UPDATE jobs SET status='COMPLETED',stage='Готово',progress=100,published_revision=%s,
                    warnings=%s,completed_at=now(),expires_at=now()+%s*interval '1 second' WHERE id=%s""",
            (rev, Jsonb(warnings), RESULT_TTL, key),
        )


def process_job(job_id, revision):
    with database(autocommit=True) as db:
        db.execute(f"SELECT pg_advisory_lock({LOCK_KEY})", (job_id,))
        job = db.execute(
            "SELECT j.*,d.source_relative_path FROM jobs j JOIN documents d ON j.id=d.job_id "
            "WHERE j.id=%s",
            (job_id,),
        ).fetchone()
        if not job or job["revision"] != revision or job["status"] not in ("QUEUED", "RUNNING"):
            return

        def progress(percent, stage):
            db.execute(
                "UPDATE jobs SET status='RUNNING',progress=%s,stage=%s WHERE id=%s",
                (percent, stage, job_id),
            )

        try:
            progress(5, "Подготовка модели EasyOCR")
            ocr = reader()
            source = job_file(job_id, job["source_relative_path"])
            with TemporaryDirectory(prefix=".work-", dir=job_dir(job_id)) as tmp:
                scratch = Path(tmp)
                if job["published_revision"]:
                    result = revised_pass(db, job, source, scratch, ocr, progress)
                else:
                    result = first_pass(job, source, scratch, ocr, progress)
                run, cards, rows, diagnostics, warnings = result
                progress(88, "Подготовка архива")
                pages, artifacts = publish_files(job, source, run, cards, diagnostics)
                save_result(
                    db, job, cards, rows, pages, artifacts, read_json(run / "matches.json"), warnings
                )
        except Exception as exc:
            logging.exception("Ошибка при обработке задания %s", job_id)
            db.execute(
                """UPDATE jobs SET status='FAILED',stage='Ошибка обработки',error=%s,completed_at=now(),
                       expires_at=now()+%s*interval '1 second' WHERE id=%s""",
                (f"{type(exc).__name__}: {exc}"[:600], FAILED_TTL, job_id),
            )
            raise
