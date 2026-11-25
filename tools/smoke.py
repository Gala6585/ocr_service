"""Проверка полного цикла на синтетическом PDF. Запускается с хоста с пакетом requests."""

from io import BytesIO
from datetime import datetime
from pathlib import Path
from time import monotonic, sleep
from zipfile import ZipFile
import argparse
import json
import xml.etree.ElementTree as ET

import requests

ROOT = Path(__file__).resolve().parents[1]


def settings():
    return dict(
        line.split("=", 1)
        for line in (ROOT / ".env").read_text(encoding="utf-8").splitlines()
        if line and not line.startswith("#") and "=" in line
    )


def wait(session, route, revision):
    last = None
    deadline = monotonic() + 720
    while monotonic() < deadline:
        response = session.get(route, timeout=10)
        response.raise_for_status()
        job = response.json()
        if (job["revision"], job["stage"]) != last:
            last = job["revision"], job["stage"]
            print(last, flush=True)
        if job["status"] == "FAILED":
            raise RuntimeError(job["error"])
        if job["status"] == "COMPLETED" and job["revision"] == revision:
            return
        sleep(2)
    raise TimeoutError("Время ожидания результата истекло")


def check_zip(blob, expected_files, corrected_fio=None):
    with ZipFile(BytesIO(blob)) as z:
        assert z.testzip() is None
        assert sorted(n for n in z.namelist() if n.startswith("cards/")) == sorted(
            "cards/" + n for n in expected_files
        )
        assert z.read("protocol_pages.pdf").startswith(b"%PDF")
        with ZipFile(BytesIO(z.read("report.xlsx"))) as report:
            sheet = ET.fromstring(report.read("xl/worksheets/sheet1.xml"))
            if corrected_fio:
                assert corrected_fio in "".join(sheet.itertext())


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("pdf", type=Path)
    parser.add_argument("--existing-job", help="Проверить задание, уже отправленное через интерфейс")
    args = parser.parse_args()
    config = settings()
    api = "http://127.0.0.1:" + config["API_PORT"]
    session = requests.Session()
    session.auth = (config["OCR_USERNAME"], config["OCR_PASSWORD"])
    assert requests.get(api + "/api/jobs", timeout=10).status_code == 401
    data = {
        "protocol_start": 1,
        "protocol_end": 1,
        "expiry_date": "2030-12-31",
        "qualification": "Учебная",
    }
    assert (
        session.post(
            api + "/api/jobs", data=data, files={"pdf": ("bad.pdf", b"broken")}, timeout=20
        ).status_code
        == 400
    )
    if args.existing_job:
        key = args.existing_job
    else:
        with args.pdf.open("rb") as source:
            response = session.post(
                api + "/api/jobs", data=data, files={"pdf": (args.pdf.name, source)}, timeout=20
            )
        assert response.status_code == 202, response.text
        key = response.json()["job_id"]
    route = api + "/api/jobs/" + key
    print("job", key, flush=True)
    wait(session, route, 1)
    response = session.get(route + "/results", timeout=20)
    response.raise_for_status()
    result = response.json()
    assert len(result["cards"]) == 2, result["cards"]
    assert len({c["row_no"] for c in result["cards"] if c["fio"]}) == 2, result["cards"]
    check_dir = ROOT / ".checks"
    check_dir.mkdir(exist_ok=True)
    (check_dir / "auto-results.json").write_text(
        json.dumps(result, ensure_ascii=False), encoding="utf-8"
    )
    (check_dir / "job-id.txt").write_text(key, encoding="ascii")
    card = result["cards"][0]
    preview = session.get(route + f"/pages/{card['page_number']}/preview", timeout=15)
    assert preview.status_code == 200 and preview.content[:4] == b"RIFF"
    original = session.post(route + "/downloads", timeout=20)
    original.raise_for_status()
    check_zip(original.content, [c["output_file"] for c in result["cards"]])
    assert (
        session.post(
            route + "/dataset-promotion",
            json={"revision": 1, "consent": True, "storage_basis": "Учебный PDF"},
            timeout=10,
        ).status_code
        == 409
    )
    box = list(card["bbox"])
    box[0] += 2
    corrected = "Тестов Пётр Сергеевич"
    change = {"revision": 1, "card_no": card["card_no"], "bbox": box, "fio": corrected}
    bad = session.patch(route + "/corrections", json={**change, "bbox": [-1, 0, 1, 1]}, timeout=10)
    assert bad.status_code == 400
    response = session.patch(route + "/corrections", json=change, timeout=15)
    assert response.status_code == 202, response.text
    wait(session, route, 2)
    revised = session.get(route + "/results", timeout=10).json()["cards"]
    assert revised[0]["bbox"] == box and revised[0]["fio"] == corrected
    new = session.post(route + "/downloads", timeout=20)
    new.raise_for_status()
    check_zip(new.content, [c["output_file"] for c in revised], corrected)
    assert session.patch(route + "/corrections", json=change, timeout=10).status_code == 409
    session.post(
        route + "/annotations/verify", json={"revision": 2, "card_no": card["card_no"]}, timeout=10
    ).raise_for_status()
    promotion = {"revision": 2, "consent": True, "storage_basis": "Синтетический контрольный PDF"}
    assert (
        session.post(
            route + "/dataset-promotion", json={**promotion, "consent": False}, timeout=10
        ).status_code
        == 400
    )
    promoted = session.post(route + "/dataset-promotion", json=promotion, timeout=15)
    promoted.raise_for_status()
    dataset = promoted.json()
    assert dataset["verified_cards"] == 1
    duplicate = session.post(route + "/dataset-promotion", json=promotion, timeout=10)
    assert duplicate.json()["dataset_id"] == dataset["dataset_id"]
    (check_dir / "dataset-id.txt").write_text(dataset["dataset_id"], encoding="ascii")
    status = session.get(route, timeout=10).json()
    issued = datetime.fromisoformat(status["last_download_requested_at"])
    expires = datetime.fromisoformat(status["expires_at"])
    assert (expires - issued).total_seconds() == int(config["DOWNLOAD_TTL_SECONDS"])
    print("OK: OCR, ZIP/PDF/PNG/XLSX, corrections, verified dataset and access checks", flush=True)


if __name__ == "__main__":
    main()
