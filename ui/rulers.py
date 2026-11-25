from io import BytesIO
from math import ceil

from PIL import Image, ImageDraw, ImageFont


def with_rulers(preview, width, height):
    """Add rulers labelled in original page pixels, even for a resized preview."""
    page = Image.open(BytesIO(preview)).convert("RGB")
    page.thumbnail((850, 1100))
    left, top = 54, 32
    canvas = Image.new("RGB", (left + page.width + 24, top + page.height + 1), "#f0f5f3")
    canvas.paste(page, (left, top))
    draw = ImageDraw.Draw(canvas)
    font = ImageFont.load_default(size=15)
    ink = "#182b29"
    draw.line((left, top - 1, left + page.width, top - 1), fill=ink)
    draw.line((left - 1, top, left - 1, top + page.height), fill=ink)
    draw.text((5, 5), "X/Y", font=font, fill=ink)

    def ticks(limit, pixels):
        step = max(50, ceil(limit * 85 / pixels / 50) * 50)
        return range(0, limit + 1, step)

    for value in ticks(width, page.width):
        x = left + round(value * page.width / width)
        draw.line((x, top - 8, x, top - 1), fill=ink)
        draw.text((x, top - 10), str(value), font=font, fill=ink, anchor="mb")
    for value in ticks(height, page.height):
        y = top + round(value * page.height / height)
        draw.line((left - 8, y, left - 1, y), fill=ink)
        draw.text((left - 11, y), str(value), font=font, fill=ink, anchor="rm")
    return canvas
