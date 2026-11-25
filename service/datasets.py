"""Завершение уже разрешённого переноса, в том числе после перезапуска Flask."""

import json
import logging
import shutil
from pathlib import Path

from .common import DATA, database, digest, inside, job_file


def publish(dataset_id):
    with database() as db:
        job_id = db.execute("SELECT job_id FROM datasets WHERE id=%s", (dataset_id,)).fetchone()[
            "job_id"
        ]
        job = db.execute(
            """SELECT j.pipeline_version,d.source_relative_path FROM jobs j JOIN documents d ON j.id=d.job_id
               WHERE j.id=%s FOR UPDATE OF j""",
            (job_id,),
        ).fetchone()
        record = db.execute("SELECT * FROM datasets WHERE id=%s FOR UPDATE", (dataset_id,)).fetchone()
        if record["status"] == "READY":
            return True
        root = DATA / "datasets"
        original = inside(root, str(Path(record["source_relative_path"]).relative_to("datasets")))
        annotation = inside(root, str(Path(record["annotations_relative_path"]).relative_to("datasets")))
        source = job_file(job_id, job["source_relative_path"])
        part = original.with_suffix(".part")
        if not original.is_file() or digest(original) != record["sha256"]:
            if not source.is_file():
                for file in (part, original, annotation, annotation.with_suffix(".part")):
                    file.unlink(missing_ok=True)
                db.execute("UPDATE datasets SET status='SOURCE_EXPIRED' WHERE id=%s", (dataset_id,))
                return False
            original.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, part)
            if digest(part) != record["sha256"]:
                raise OSError("Контрольная сумма исходного PDF изменилась")
            part.replace(original)
        annotation.parent.mkdir(parents=True, exist_ok=True)
        manifest = {
            "source_sha256": record["sha256"],
            "pipeline_version": job["pipeline_version"],
            "coordinates": "xyxy pixels, rendered page, one-based page_number",
            "verified_cards": record["annotations"],
        }
        part = annotation.with_suffix(".part")
        part.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
        part.replace(annotation)
        db.execute("UPDATE datasets SET status='READY' WHERE id=%s", (dataset_id,))
    return True


if __name__ == "__main__":
    with database() as db:
        pending = db.execute("SELECT id FROM datasets WHERE status='PENDING'").fetchall()
    for record in pending:
        try:
            publish(record["id"])
        except OSError:
            logging.exception("Не удалось завершить перенос датасета %s", record["id"])
