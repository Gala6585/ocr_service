"""Сгенерировать контрольный PDF с таблицей и двумя вымышленными удостоверениями."""

from pathlib import Path
import sys
import pymupdf


def make_pdf(target):
    font = pymupdf.Font("helv")
    names = ["Иванов Иван Иванович", "Петров Пётр Петрович"]
    with pymupdf.open() as doc:
        page = doc.new_page(width=595, height=842)

        def text_at(x, y, text, size=16):
            page.insert_text((x, y), text, fontname="Demo", fontsize=size)

        page.insert_font(fontname="Demo", fontbuffer=font.buffer)
        text_at(40, 70, "Учебный протокол", 23)
        xs, ys = (35, 100, 440, 560), (120, 170, 220, 270)
        for x in xs:
            page.draw_line((x, ys[0]), (x, ys[-1]), width=1.8)
        for y in ys:
            page.draw_line((xs[0], y), (xs[-1], y), width=1.8)
        for r, values in enumerate([("№", "ФИО", "Группа"), ("1", names[0], "А"), ("2", names[1], "Б")]):
            for c, value in enumerate(values):
                text_at(xs[c] + 9, ys[r] + 32, value)
        page = doc.new_page(width=595, height=842)
        page.insert_font(fontname="Demo", fontbuffer=font.buffer)
        for y, name in zip((105, 430), names):
            page.draw_rect(pymupdf.Rect(45, y, 550, y + 195), width=2.4)
            text_at(75, y + 48, "УДОСТОВЕРЕНИЕ", 20)
            text_at(75, y + 110, name, 23)
            text_at(75, y + 160, "Учебный образец", 14)
        target = Path(target)
        target.parent.mkdir(parents=True, exist_ok=True)
        doc.subset_fonts()
        doc.save(target, deflate=True)


if __name__ == "__main__":
    make_pdf(sys.argv[1])
