"""Обработка удостоверений и протоколов: CPU-only ядро, независимое от интерфейса."""
from __future__ import annotations

from collections import Counter
from datetime import date, datetime, time
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any
import json
import re
import shutil
import uuid

import cv2
import numpy as np
import pandas as pd
import pymupdf
from scipy.optimize import linear_sum_assignment


def save_image(path: str | Path, image: np.ndarray) -> None:
    """Сохраняет PNG/JPG в путь, включая кириллицу."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    ok, encoded = cv2.imencode(path.suffix, image)
    if not ok:
        raise IOError(f"Не удалось кодировать изображение: {path}")
    path.write_bytes(encoded.tobytes())


def load_image(path: str | Path) -> np.ndarray:
    image = cv2.imdecode(np.frombuffer(Path(path).read_bytes(), dtype=np.uint8), cv2.IMREAD_COLOR)
    if image is None:
        raise ValueError(f"Невозможно прочитать изображение: {path}")
    return image


def pdf_page_to_bgr(page, dpi: int = 150) -> np.ndarray:
    pix = page.get_pixmap(dpi=dpi, alpha=False)
    image = np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.height, pix.width, pix.n)
    if pix.n == 1:
        return cv2.cvtColor(image, cv2.COLOR_GRAY2BGR)
    if pix.n == 4:
        return cv2.cvtColor(image, cv2.COLOR_RGBA2BGR)
    return cv2.cvtColor(image, cv2.COLOR_RGB2BGR)


def dump_json(path: str | Path, obj: Any) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=2, default=str), encoding="utf-8")


def parse_manual_date(value: str | date | datetime | None) -> datetime | None:
    """Только ручной ввод: ДД.ММ.ГГГГ / ГГГГ-ММ-ДД / date / datetime."""
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    if isinstance(value, datetime):
        return datetime.combine(value.date(), time.min)
    if isinstance(value, date):
        return datetime.combine(value, time.min)
    if isinstance(value, str):
        for fmt in ("%d.%m.%Y", "%Y-%m-%d"):
            try:
                return datetime.strptime(value.strip(), fmt)
            except ValueError:
                pass
    raise ValueError("Дата окончания: используйте 'ДД.ММ.ГГГГ' или 'ГГГГ-ММ-ДД'")


def safe_name(value: str, max_length: int = 85) -> str:
    text = re.sub(r'[\\/*?:"<>|;\n\r\t]', " ", str(value))
    return re.sub(r"\s+", " ", text).strip(" .")[:max_length].rstrip(" .")


def trim_stem_bytes(stem: str, max_bytes: int = 220) -> str:
    """Ограничивает длину будущего имени с учётом UTF-8 и суффиксов."""
    while len(stem.encode("utf-8")) > max_bytes:
        stem = stem[:-1]
    return stem.rstrip(" ._-") or "card"


def get_card_pages(total_pages: int, protocol_pages: list[int]) -> list[int]:
    selected = set(protocol_pages)
    if not selected or min(selected) < 1 or max(selected) > total_pages:
        raise ValueError("Страницы протокола должны быть указаны в пределах PDF (нумерация с 1)")
    return [i for i in range(1, total_pages + 1) if i not in selected]


class IDCardExtractor:
    """OpenCV нарезка карточек с подбором ядра и записью авторазметки страниц."""

    def __init__(self, dpi=150, margin_x=15, pad_y=5,
                 kernel_w_ratio=0.15, kernel_h_ratio=0.02,
                 auto_kernel=True, crop_pad_ratio=0.02,
                 edge_density_threshold=0.025):
        self.dpi = dpi
        self.margin_x = margin_x
        self.pad_y = pad_y
        self.kernel_w_ratio = kernel_w_ratio
        self.kernel_h_ratio = kernel_h_ratio
        self.auto_kernel = auto_kernel
        self.crop_pad_ratio = crop_pad_ratio
        self.edge_density_threshold = edge_density_threshold
        self.kernel_diagnostics: list[dict] = []
        self.crop_diagnostics: list[dict] = []

    @staticmethod
    def _detect_boxes(binary, width, height, kw_ratio, kh_ratio):
        kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (
            max(1, round(width * kw_ratio)), max(1, round(height * kh_ratio))))
        dilated = cv2.dilate(binary, kernel, iterations=1)
        contours, _ = cv2.findContours(dilated, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        boxes = []
        for cnt in contours:
            x, y, w, h = cv2.boundingRect(cnt)
            if w >= 0.30 * width and 0.08 * height <= h <= 0.90 * height:
                boxes.append((x, y, w, h))
        return sorted(boxes, key=lambda b: (b[1], b[0]))

    def _regions(self, boxes, width, height):
        pad = max(self.pad_y, round(height * self.crop_pad_ratio))
        regions = []
        x1, x2 = max(0, self.margin_x), min(width, width - self.margin_x)
        for idx, (x, y, w, h) in enumerate(boxes):
            y1, y2 = max(0, y - pad), min(height, y + h + pad)
            overlap = False
            if idx:
                prev_end = boxes[idx - 1][1] + boxes[idx - 1][3]
                if prev_end <= y:
                    y1 = max(y1, (prev_end + y) // 2)
                else:
                    overlap = True
            if idx + 1 < len(boxes):
                next_start = boxes[idx + 1][1]
                if y + h <= next_start:
                    y2 = min(y2, (y + h + next_start) // 2)
                else:
                    overlap = True
            if y1 < y2 and x1 < x2:
                regions.append({"bbox_detected": (x, y, w, h),
                                "crop_xyxy": (x1, y1, x2, y2), "overlap": overlap})
        return regions

    @staticmethod
    def _edge_density(binary, crop, width):
        x1, y1, x2, y2 = crop
        inner = max(3, round(width * 0.03))
        a, b = min(x2, x1 + inner), max(x1, x2 - inner)
        if a >= b:
            a, b = x1, x2
        band = max(3, min(12, round((y2 - y1) * 0.015)))
        zones = (binary[y1:min(y2, y1 + band), a:b],
                 binary[max(y1, y2 - band):y2, a:b])
        return [float(np.count_nonzero(z) / z.size) if z.size else 0.0 for z in zones]

    def _choose_kernel(self, binary, width, height):
        variants = [(float(self.kernel_w_ratio), float(self.kernel_h_ratio))]
        if self.auto_kernel:
            variants += [(w, h) for w in (0.10, 0.15, 0.20)
                         for h in (0.008, 0.012, 0.016, 0.020, 0.024)
                         if (w, h) not in variants]
        records = []
        for kw, kh in variants:
            boxes = self._detect_boxes(binary, width, height, kw, kh)
            regions = self._regions(boxes, width, height)
            edge_penalty = sum(max(self._edge_density(binary, r["crop_xyxy"], width))
                               for r in regions)
            heights = [b[3] for b in boxes]
            variation = float(np.std(heights) / np.mean(heights)) if heights and np.mean(heights) else 1.0
            records.append(dict(kernel_w=kw, kernel_h=kh, boxes=boxes, regions=regions,
                                count=len(regions), edge_penalty=edge_penalty,
                                overlap_count=sum(r["overlap"] for r in regions),
                                variation=variation))
        if not self.auto_kernel:
            return records[0], records
        counts = Counter(r["count"] for r in records if r["count"] > 0)
        baseline_count = records[0]["count"]
        likely = min(counts, key=lambda k: (-counts[k], abs(k - baseline_count))) if counts else 0
        best = min(records, key=lambda r: (
            abs(r["count"] - likely), r["overlap_count"],
            round(r["edge_penalty"], 3), round(r["variation"], 3),
            abs(r["kernel_w"] - self.kernel_w_ratio) + abs(r["kernel_h"] - self.kernel_h_ratio)))
        return best, records

    def extract(self, pdf_path: str | Path, page_numbers: list[int], run_dir: str | Path) -> list[dict]:
        run_dir = Path(run_dir)
        image_dir = run_dir / "dataset" / "images"
        labels_dir = run_dir / "dataset" / "labels_auto"
        crops_dir = run_dir / "cards_original"
        preview_dir = run_dir / "previews"
        for folder in (image_dir, labels_dir, crops_dir, preview_dir):
            folder.mkdir(parents=True, exist_ok=True)
        self.crop_diagnostics = []
        self.kernel_diagnostics = []
        cards = []
        with pymupdf.open(str(pdf_path)) as doc:
            for page_num in page_numbers:
                image = pdf_page_to_bgr(doc[page_num - 1], self.dpi)
                height, width = image.shape[:2]
                image_id = f"card_page_{page_num:03d}"
                save_image(image_dir / f"{image_id}.png", image)
                gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
                _, binary = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)
                binary = cv2.morphologyEx(binary, cv2.MORPH_OPEN, np.ones((3, 3), np.uint8))
                best, candidates = self._choose_kernel(binary, width, height)
                count_variants = sorted({x["count"] for x in candidates})
                self.kernel_diagnostics.append({"page": page_num, "kernel_w_ratio": best["kernel_w"],
                                                "kernel_h_ratio": best["kernel_h"],
                                                "cards_found": best["count"], "candidate_counts": count_variants})
                preview = image.copy()
                auto_lines = []
                heights = [r["crop_xyxy"][3] - r["crop_xyxy"][1] for r in best["regions"]]
                median_height = float(np.median(heights)) if heights else 0
                for card_idx, region in enumerate(best["regions"], 1):
                    x, y, w, h = region["bbox_detected"]
                    x1, y1, x2, y2 = region["crop_xyxy"]
                    d_top, d_bottom = self._edge_density(binary, region["crop_xyxy"], width)
                    warnings = []
                    if len(heights) >= 3 and median_height:
                        if y2 - y1 < 0.7 * median_height:
                            warnings.append("Возможна обрезка: маленькая высота")
                        if y2 - y1 > 1.45 * median_height:
                            warnings.append("Возможна склейка: большая высота")
                    if len(count_variants) > 1:
                        warnings.append("Число карточек зависит от kernel")
                    if region["overlap"]:
                        warnings.append("Перекрывающиеся контуры")
                    if d_top > self.edge_density_threshold:
                        warnings.append("Возможна обрезка сверху")
                    if d_bottom > self.edge_density_threshold:
                        warnings.append("Возможна обрезка снизу")
                    if y1 == 0 or y2 == height:
                        warnings.append("Вырезка касается края страницы")
                    filename = f"page_{page_num:03d}_card_{card_idx:02d}.png"
                    source_path = crops_dir / filename
                    save_image(source_path, image[y1:y2, x1:x2])
                    record = {
                        "filename": filename, "file_path": str(source_path),
                        "page": page_num, "image_file": f"dataset/images/{image_id}.png",
                        "bbox_detected_xywh": [int(z) for z in (x, y, w, h)],
                        "bbox_crop_xyxy": [int(z) for z in (x1, y1, x2, y2)],
                        "image_width": width, "image_height": height,
                        "warnings": warnings, "verified": False,
                        "annotation_status": "auto_proposal",
                    }
                    cards.append(record)
                    self.crop_diagnostics.append(record)
                    # YOLO: класс 0 / bbox по контуру. НЕ эталонная разметка!
                    auto_lines.append(f"0 {(x + w / 2) / width:.6f} {(y + h / 2) / height:.6f} "
                                      f"{w / width:.6f} {h / height:.6f}")
                    cv2.rectangle(preview, (x1, y1), (x2 - 1, y2 - 1),
                                  (0, 140, 255) if warnings else (0, 160, 0), 2)
                    cv2.rectangle(preview, (x, y), (x + w, y + h), (255, 0, 0), 2)
                    cv2.putText(preview, str(card_idx), (x1 + 5, max(20, y1 + 20)),
                                cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 0, 220), 2)
                (labels_dir / f"{image_id}.txt").write_text("\n".join(auto_lines) + ("\n" if auto_lines else ""),
                                                              encoding="utf-8")
                save_image(preview_dir / f"{image_id}_preview.jpg", preview)
        dump_json(run_dir / "dataset" / "annotations_auto.json", cards)
        dump_json(run_dir / "dataset" / "kernel_diagnostics.json", self.kernel_diagnostics)
        (run_dir / "dataset" / "labels_verified").mkdir(parents=True, exist_ok=True)
        (run_dir / "dataset" / "README.txt").write_text(
            "images/ — исходные страницы с удостоверениями.\n"
            "labels_auto/ — YOLO class=0, автоматически предложенные bounding boxes по морфологическим контурам.\n"
            "annotations_auto.json — те же обнаружения с координатами, исходными вырезками и предупреждениями.\n"
            "labels_verified/ — сюда сохраняйте размеченные вручную и проверенные YOLO labels.\n"
            "ВАЖНО: авторазметка неточная, может включать часть карточки или соседние элементы. "
            "НЕ обучать на labels_auto как на проверенном ground truth.\n"
            "В данных могут содержаться персональные данные: ограничьте доступ и сроки хранения.\n",
            encoding="utf-8")
        return cards


class ProtocolParser:
    """Распознаёт таблицы протокола; сохраняет оригинал и все отклонённые OCR-строки."""

    HEADER_WORDS = ("фамил", "фио", "ф.и.о", "столбц", "столбец", "должност", "наименование",
                    "группа безопасности", "оценка", "присвоена", "порядков")

    def __init__(self, reader, dpi=200, row_tolerance=18):
        self.reader = reader
        self.dpi = dpi
        self.row_tolerance = row_tolerance
        self.raw_rows: list[dict] = []
        self.warnings: list[str] = []

    @staticmethod
    def _find_table_cells(image):
        height, width = image.shape[:2]
        gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
        binary = cv2.adaptiveThreshold(gray, 255, cv2.ADAPTIVE_THRESH_MEAN_C,
                                      cv2.THRESH_BINARY_INV, 15, 10)
        horiz = cv2.morphologyEx(binary, cv2.MORPH_OPEN, cv2.getStructuringElement(cv2.MORPH_RECT, (40, 1)), iterations=2)
        vert = cv2.morphologyEx(binary, cv2.MORPH_OPEN, cv2.getStructuringElement(cv2.MORPH_RECT, (1, 40)), iterations=2)
        grid = cv2.addWeighted(horiz, 0.5, vert, 0.5, 0.0)
        _, grid = cv2.threshold(grid, 50, 255, cv2.THRESH_BINARY)
        grid = cv2.dilate(grid, np.ones((3, 3), np.uint8))
        contours, _ = cv2.findContours(grid, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        possible = []
        for cnt in contours:
            x, y, w, h = cv2.boundingRect(cnt)
            if w > width * 0.50 and h > height * 0.08:
                possible.append((x, y, w, h))
        if not possible:
            return None, []
        tx, ty, tw, th = max(possible, key=lambda b: b[2] * b[3])
        all_contours, _ = cv2.findContours(grid, cv2.RETR_TREE, cv2.CHAIN_APPROX_SIMPLE)
        raw = []
        for cnt in all_contours:
            x, y, w, h = cv2.boundingRect(cnt)
            if (x >= tx - 5 and y >= ty - 5 and x + w <= tx + tw + 5
                    and 25 < w < tw * 0.95 and 15 < h < th * 0.90):
                raw.append((x, y, w, h))
        cells = []
        for candidate in sorted(raw, key=lambda b: (b[1], b[0])):
            if not any(abs(candidate[0] - c[0]) < 12 and abs(candidate[1] - c[1]) < 12 for c in cells):
                cells.append(candidate)
        return (tx, ty, tw, th), cells

    def _read_rows(self, image, cells, page_num):
        # Колонки могут слегка различаться по Y: группируем по началу строки.
        groups = []
        for cell in sorted(cells, key=lambda c: (c[1], c[0])):
            found = next((row for row in groups if abs(row["y"] - cell[1]) < self.row_tolerance), None)
            if found is None:
                found = {"y": cell[1], "cells": []}
                groups.append(found)
            found["cells"].append(cell)
        rows = []
        for group in groups:
            cells_in_row = sorted(group["cells"], key=lambda c: c[0])
            values = []
            for x, y, w, h in cells_in_row:
                crop = image[y + 2:y + h - 2, x + 2:x + w - 2]
                if crop.size == 0:
                    values.append("")
                    continue
                padded = cv2.copyMakeBorder(crop, 10, 10, 10, 10, cv2.BORDER_CONSTANT,
                                            value=(255, 255, 255))
                result = self.reader.readtext(padded, detail=0, paragraph=True)
                values.append(" ".join(map(str, result)).strip())
            rows.append({"page": page_num, "values": values,
                         "cell_boxes_xywh": [list(c) for c in cells_in_row], "row_y": group["y"]})
        return rows

    @staticmethod
    def _number_only(values):
        """Нумерация даже если OCR увидел 1–2 числа вместо полного ряда.

        Удаляем только строки, где все непустые значения имеют вид малых чисел
        или их типичных OCR-замен. Реальные строки с ФИО не трогаем.
        """
        nonempty = [re.sub(r"\s+", "", str(v)).strip() for v in values if str(v).strip()]
        if not nonempty:
            return False
        # I, l, | — типичные распознавания '1'; O/o — '0'.
        return all(re.fullmatch(r"[0-9Il|Oo]{1,3}[.)]?", v) is not None for v in nonempty)

    @classmethod
    def _header_like(cls, values):
        joined = " ".join(values).lower().replace(" ", "")
        return any(w.replace(" ", "") in joined for w in cls.HEADER_WORDS)

    @staticmethod
    def _fio_candidate(text):
        """Для автоматического подсчёта требуем минимум 2 словесных компонента ФИО."""
        words = re.findall(r"[А-Яа-яЁё]{2,}(?:-[А-Яа-яЁё]{2,})?", str(text))
        # Несколько частей в одном столбце; единичная цифра — не ФИО.
        return len(words) >= 2 and not any(w.lower() in ("колонка", "столбец", "присвоена", "нумерация") for w in words)

    @staticmethod
    def _find_fio_col(headers):
        for idx, title in enumerate(headers):
            normal = re.sub(r"[\s.,]", "", title.lower()).replace("0", "о")
            if ("фио" in normal or "фамил" in normal or "слушател" in normal or "обучаем" in normal):
                return idx
        return None

    def _protocol_rotation(self, pdf, page_numbers):
        for number in page_numbers:
            image = pdf_page_to_bgr(pdf[number - 1], dpi=self.dpi)
            table, _ = self._find_table_cells(image)
            if not table or table[3] <= table[2]:
                continue
            for angle, degrees in ((cv2.ROTATE_90_CLOCKWISE, 90),
                                   (cv2.ROTATE_90_COUNTERCLOCKWISE, 270)):
                rotated = cv2.rotate(image, angle)
                heading = rotated[:rotated.shape[0] // 3]
                scale = min(1, 1400 / heading.shape[1])
                heading = cv2.resize(heading, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA)
                words = self.reader.readtext(heading, detail=0)
                if re.search(r"протокол", " ".join(words), re.IGNORECASE):
                    return degrees
        return 0

    def parse(self, pdf_path, protocol_pages, run_dir, fio_column=None) -> pd.DataFrame:
        run_dir = Path(run_dir)
        protocol_dir = run_dir / "protocol"
        protocol_dir.mkdir(parents=True, exist_ok=True)
        self.raw_rows, self.warnings = [], []
        if isinstance(fio_column, int) and fio_column < 0:
            raise ValueError("fio_column должен быть индексом столбца, начиная с 0")
        headers: list[str] = []
        records: list[dict] = []
        page_numbers = sorted(protocol_pages)
        with pymupdf.open(str(pdf_path)) as doc, pymupdf.open() as subset:
            rotation = self._protocol_rotation(doc, page_numbers)
            for page_num in page_numbers:
                subset.insert_pdf(doc, from_page=page_num - 1, to_page=page_num - 1)
                page = subset[-1]
                page.set_rotation((page.rotation + rotation) % 360)
                image = pdf_page_to_bgr(page, dpi=self.dpi)
                _, cells = self._find_table_cells(image)
                save_image(protocol_dir / f"protocol_page_{page_num:03d}.png", image)
                if not cells:
                    self.warnings.append(f"Протокол, стр. {page_num}: таблица не найдена")
                    continue
                page_rows = self._read_rows(image, cells, page_num)
                for entry in page_rows:
                    values = entry["values"]
                    if not any(v.strip() for v in values):
                        entry["kind"] = "empty"
                    elif self._number_only(values):
                        # В отличие от прежнего кода: не требуется >= 3 прочитанных номеров.
                        entry["kind"] = "column_numbering"
                    elif self._header_like(values):
                        entry["kind"] = "header"
                        if not headers:
                            headers = list(values)
                    else:
                        # Не считаем цифровые и непонятные строки людьми.
                        explicit_idx = fio_column if isinstance(fio_column, int) else None
                        current_fio_idx = (explicit_idx if explicit_idx is not None
                                           else self._find_fio_col(headers))
                        if current_fio_idx is None:
                            candidates = [(i, v) for i, v in enumerate(values) if self._fio_candidate(v)]
                            # Выбор без шапки ненадёжен — такие записи требуют проверки.
                            chosen = max(candidates, key=lambda x: len(x[1]), default=None)
                            candidate_idx = chosen[0] if chosen else None
                        else:
                            candidate_idx = current_fio_idx
                        fio = values[candidate_idx].strip() if candidate_idx is not None and candidate_idx < len(values) else ""
                        if self._fio_candidate(fio):
                            entry["kind"] = "data"
                            records.append({"ФИО": fio, "Страница протокола": page_num,
                                            "Строка протокола": len(self.raw_rows),
                                            "Проверить строку": "Да" if current_fio_idx is None else "Нет"})
                        else:
                            entry["kind"] = "review"
                            self.warnings.append(f"Протокол, стр. {page_num}, Y={entry['row_y']}: "
                                                 "сомнительная строка не включена в число ФИО")
                    self.raw_rows.append(entry)
            subset_path = protocol_dir / "protocol_pages.pdf"
            subset.save(str(subset_path))
        dump_json(protocol_dir / "ocr_rows.json", self.raw_rows)
        dump_json(protocol_dir / "warnings.json", self.warnings)
        df = pd.DataFrame(records, columns=["ФИО", "Страница протокола", "Строка протокола", "Проверить строку"])
        df.to_csv(protocol_dir / "protocol_records.csv", index=False, encoding="utf-8-sig")
        # Сырые строки не удаляются: можно исправить вручную и пересчитать сопоставление.
        return df


class ResultAssembler:
    """Один OCR на карточку, глобальное назначение ФИО, экспорт без потери оригиналов."""

    def __init__(self, reader, min_score=0.65, fuzzy_threshold=0.82, normalizer=None):
        self.reader = reader
        self.min_score = min_score
        self.fuzzy_threshold = fuzzy_threshold
        if normalizer is not None:
            self._normalize = normalizer
        else:
            # Лениво, чтобы сам импорт класса не загружал словари pymorphy3.
            import pymorphy3
            morph = pymorphy3.MorphAnalyzer()
            def normalize_word(word):
                word = re.sub(r"[^а-яА-ЯёЁ\-]", "", word).lower().replace("ё", "е")
                if len(word) < 2:
                    return word
                return morph.parse(word)[0].normal_form.replace("ё", "е")
            self._normalize = normalize_word

    def _match(self, card_words, fio_parts):
        hits = 0
        for part in fio_parts:
            if part in card_words or any(
                len(word) >= 3 and SequenceMatcher(None, part, word).ratio() >= self.fuzzy_threshold
                for word in card_words):
                hits += 1
        return hits / len(fio_parts) if fio_parts else 0.0, hits

    def assemble(self, cards, protocol_df, run_dir, expiry_date=None, qualification="",
                 replace_ready=True, confirmed_rows=None) -> pd.DataFrame:
        if "ФИО" not in protocol_df.columns:
            raise ValueError("В protocol_df требуется колонка 'ФИО'")
        run_dir = Path(run_dir)
        output_dir = run_dir / "ready"
        output_dir.mkdir(parents=True, exist_ok=True)
        if replace_ready:
            # Только производные PNG в выделенной папке; исходные crops не меняем.
            for old_png in output_dir.glob("*.png"):
                old_png.unlink()
        manual_date = parse_manual_date(expiry_date)
        records = []
        for _, row in protocol_df.iterrows():
            fio = str(row.get("ФИО", "")).strip()
            if not fio or fio.lower() in ("none", "nan"):
                continue
            parts = [self._normalize(w) for w in fio.split() if len(w) >= 2]
            if len(parts) < 2:
                continue
            records.append({"fio": fio, "parts": parts,
                            "review": row.get("Проверить строку", "Нет"),
                            "row_id": row.get("Строка протокола", None)})
        card_words_list = []
        for card in cards:
            image = load_image(card["file_path"])
            ocr_words = self.reader.readtext(image, detail=0, paragraph=True)
            raw = re.findall(r"[а-яА-ЯёЁ\-]{2,}", " ".join(map(str, ocr_words)))
            card_words_list.append(set(self._normalize(w) for w in raw))
        n, m = len(cards), len(records)
        scores = np.zeros((n, m), dtype=np.float32)
        counts = np.zeros((n, m), dtype=np.int32)
        allowed = np.zeros((n, m), dtype=bool)
        for i, words in enumerate(card_words_list):
            for j, rec in enumerate(records):
                score, hits = self._match(words, rec["parts"])
                scores[i, j], counts[i, j] = score, hits
                allowed[i, j] = hits >= 2 and score >= self.min_score
        # Подтверждённые человеком назначения резервируют карточку и строку.
        # Остальные пары по-прежнему выбирает глобальный алгоритм ноутбука.
        confirmed_rows = confirmed_rows or {}
        row_indices = {rec["row_id"]: j for j, rec in enumerate(records)}
        confirmed = {i: row_indices[confirmed_rows[card["filename"]]]
                     for i, card in enumerate(cards) if card["filename"] in confirmed_rows}
        eligible = allowed.copy()
        for i, j in confirmed.items():
            eligible[i, :] = False
            eligible[:, j] = False
        assignments = {}
        if n:
            solver_scores = np.concatenate((np.where(eligible, scores, -1.0),
                                            np.zeros((n, n), dtype=np.float32)), axis=1)
            rows, cols = linear_sum_assignment(solver_scores, maximize=True)
            assignments = {int(i): int(j) for i, j in zip(rows, cols) if j < m and eligible[i, j]}
        assignments.update(confirmed)
        summary = []
        match_metadata = []
        for i, card in enumerate(cards):
            assigned = assignments.get(i)
            match = records[assigned] if assigned is not None else None
            score = float(scores[i, assigned]) if match else 0.0
            others = [float(scores[i, j]) for j in range(m) if j != assigned and allowed[i, j]]
            alt_score = max(others) if others else None
            needs_review = (match is None or score < 0.85 or
                            (alt_score is not None and alt_score >= score - 0.05) or
                            (match is not None and match["review"] == "Да") or
                            bool(card["warnings"]))
            if match:
                name_parts = [safe_name(match["fio"], 65)]
                if qualification:
                    name_parts.append(safe_name(qualification, 55))
                if manual_date:
                    name_parts.append("до_" + manual_date.strftime("%d.%m.%Y"))
                stem = "_".join(filter(None, name_parts)) or Path(card["filename"]).stem
            else:
                stem = Path(card["filename"]).stem + "_без_ФИО"
            stem = trim_stem_bytes(stem)
            target = output_dir / f"{stem}.png"
            suffix = 2
            while target.exists():
                target = output_dir / f"{stem}_{suffix}.png"
                suffix += 1
            shutil.copy2(card["file_path"], target)
            summary.append({
                "Было": card["filename"], "Стало": target.name,
                "ФИО из протокола": match["fio"] if match else None,
                "Квалификация": qualification or None,
                "Действительно до": manual_date if manual_date else pd.NaT,
                "Источник даты": "Введена вручную" if manual_date else "Не указана",
                "Оценка ФИО": round(score, 3),
                "Оценка альтернативы": round(alt_score, 3) if alt_score is not None else None,
                "Проверить ФИО/нарезку": "Да" if needs_review else "Нет",
                "Предупреждения нарезки": "; ".join(card["warnings"]),
                "Статус": "Сопоставлен" if match else "ФИО не найдено",
            })
            match_metadata.append({"source_file": card["filename"], "output_file": target.name,
                                   "fio": match["fio"] if match else None,
                                   "score": score, "needs_review": bool(needs_review),
                                   "protocol_row_id": match["row_id"] if match else None})
        report = pd.DataFrame(summary, columns=[
            "Было", "Стало", "ФИО из протокола", "Квалификация", "Действительно до",
            "Источник даты", "Оценка ФИО", "Оценка альтернативы",
            "Проверить ФИО/нарезку", "Предупреждения нарезки", "Статус"])
        with pd.ExcelWriter(run_dir / "report.xlsx", engine="openpyxl") as writer:
            report.to_excel(writer, index=False)
            for row in writer.sheets["Sheet1"].iter_rows():
                for cell in row:
                    if cell.data_type == "f":
                        cell.data_type = "s"
        report.to_csv(run_dir / "report.csv", index=False, encoding="utf-8-sig", date_format="%d.%m.%Y")
        dump_json(run_dir / "matches.json", match_metadata)
        return report


def save_verified_annotations(run_dir: str | Path, page_number: int,
                              boxes_xyxy: list[tuple[int, int, int, int]],
                              reviewer: str = "manual") -> Path:
    """Сохраняет ИСПРАВЛЕННЫЕ человеком bbox для обучения YOLO.

    На вход — прямоугольники (x1, y1, x2, y2) в пикселях исходной страницы.
    Вызывайте после визуальной проверки исходной страницы, а не по авторамке.
    """
    run_dir = Path(run_dir)
    image_id = f"card_page_{page_number:03d}"
    image_path = run_dir / "dataset" / "images" / f"{image_id}.png"
    if not image_path.exists():
        raise FileNotFoundError(image_path)
    image = load_image(image_path)
    height, width = image.shape[:2]
    labels = []
    validated = []
    for xyxy in boxes_xyxy:
        if len(xyxy) != 4:
            raise ValueError("Каждая рамка должна содержать (x1, y1, x2, y2)")
        x1, y1, x2, y2 = map(int, xyxy)
        if not (0 <= x1 < x2 <= width and 0 <= y1 < y2 <= height):
            raise ValueError(f"Рамка за пределами страницы: {xyxy}")
        labels.append(f"0 {(x1 + x2) / (2 * width):.6f} "
                      f"{(y1 + y2) / (2 * height):.6f} "
                      f"{(x2 - x1) / width:.6f} {(y2 - y1) / height:.6f}")
        validated.append([x1, y1, x2, y2])
    out = run_dir / "dataset" / "labels_verified" / f"{image_id}.txt"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(labels) + ("\n" if labels else ""), encoding="utf-8")
    meta_path = run_dir / "dataset" / "annotations_verified.json"
    meta = json.loads(meta_path.read_text(encoding="utf-8")) if meta_path.exists() else {}
    meta[image_id] = {"page": page_number, "boxes_xyxy": validated,
                      "reviewer": reviewer, "verified": True,
                      "image_file": f"dataset/images/{image_id}.png"}
    dump_json(meta_path, meta)
    return out


def run_pipeline(pdf_path: str | Path, protocol_start: int, protocol_end: int | None = None,
                 expiry_date: str | date | datetime | None = None, qualification: str = "",
                 output_root: str | Path = "results", auto_kernel: bool = True,
                 reader=None, fio_column=None, normalizer=None, progress=None) -> dict:
    """Единая точка входа для ноутбука, Flask, Streamlit или CLI.

    expiry_date вводится ТОЛЬКО пользователем. OCR даты не выполняется.
    reader инжектируется из сервиса для однократной загрузки модели EasyOCR.
    """
    pdf_path = Path(pdf_path)
    if not pdf_path.is_file():
        raise FileNotFoundError(str(pdf_path))
    manual_dt = parse_manual_date(expiry_date)
    with pymupdf.open(str(pdf_path)) as doc:
        total_pages = len(doc)
    last = protocol_start if protocol_end is None else protocol_end
    if last < protocol_start:
        raise ValueError("Конечная страница протокола не может быть раньше начальной")
    protocol_pages = list(range(protocol_start, last + 1))
    card_pages = get_card_pages(total_pages, protocol_pages)
    run_dir = Path(output_root) / (safe_name(pdf_path.stem, 50) + "_" + uuid.uuid4().hex[:8])
    run_dir.mkdir(parents=True, exist_ok=False)
    if reader is None:
        import easyocr
        reader = easyocr.Reader(["ru"], gpu=False, verbose=False)
    if progress:
        progress(15, "Чтение таблицы протокола")
    parser = ProtocolParser(reader=reader)
    protocol_df = parser.parse(pdf_path, protocol_pages, run_dir, fio_column=fio_column)
    if progress:
        progress(45, "Нарезка удостоверений")
    extractor = IDCardExtractor(auto_kernel=auto_kernel)
    cards = extractor.extract(pdf_path, card_pages, run_dir)
    if progress:
        progress(65, "Распознавание и сопоставление ФИО")
    assembler = ResultAssembler(reader=reader, normalizer=normalizer)
    report = assembler.assemble(cards, protocol_df, run_dir,
                                expiry_date=manual_dt, qualification=qualification)
    warnings = list(parser.warnings)
    if len(cards) != len(protocol_df):
        warnings.append(f"Количество карточек ({len(cards)}) не равно числу распознанных ФИО ({len(protocol_df)}). "
                        "Проверьте protocol/ocr_rows.json и previews/")
    warnings.extend(f"Карточка {c['filename']}: {', '.join(c['warnings'])}"
                    for c in cards if c["warnings"])
    dump_json(run_dir / "manifest.json", {
        "source_pdf": str(pdf_path), "protocol_pages": protocol_pages,
        "card_pages": card_pages, "manual_expiry_date": manual_dt.strftime("%d.%m.%Y") if manual_dt else None,
        "qualification": qualification, "cards_found": len(cards),
        "protocol_fio_count": len(protocol_df), "warnings": warnings,
        "auto_annotations_verified": False,
    })
    return {"run_dir": str(run_dir), "report": report,
            "protocol_df": protocol_df, "cards": cards, "warnings": warnings,
            "kernel_diagnostics": extractor.kernel_diagnostics}
