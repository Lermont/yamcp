"""Decode uploaded bytes before any asset write; never transform the image.

Geometry: https://yandex.ru/dev/direct/doc/ru/objects/adimage (2026-09-17).
Animation work is additionally bounded locally to 200 frames / 100M pixels.
"""
from __future__ import annotations

import warnings
from io import BytesIO

from PIL import Image, UnidentifiedImageError

MAX_BYTES = 10 * 1024 * 1024
MAX_FRAMES = 200
MAX_DECODED_PIXELS = 100_000_000
FIXED_SIZES = {
    (240, 400), (300, 250), (300, 500), (300, 600), (320, 50), (320, 100),
    (320, 480), (336, 280), (480, 320), (480, 800), (600, 500), (600, 1000),
    (600, 1200), (640, 100), (640, 200), (640, 960), (672, 560), (720, 1200),
    (728, 90), (900, 750), (900, 1500), (900, 1800), (960, 150), (960, 300),
    (960, 640), (960, 1440), (960, 1600), (970, 250), (1008, 840),
    (1200, 1000), (1200, 2000), (1200, 2400), (1280, 200), (1280, 400),
    (1280, 1920), (1344, 1120), (1440, 960), (1456, 180), (1920, 1280),
    (1940, 500), (2184, 270), (2910, 750), (2912, 360), (3880, 1000),
}


def geometry(width: int, height: int, requested: str, size: int) -> str:
    regular = (450 <= width <= 5000 and 450 <= height <= 5000
               and 3 * height <= 4 * width and 3 * width <= 4 * height)
    # Nearest integer pixel: the documented extremes are 1080x607 and 5000x2812.
    wide = (1080 <= width <= 5000 and 607 <= height <= 2812
            and abs(16 * height - 9 * width) <= 8)
    fixed = (width, height) in FIXED_SIZES and size <= 512 * 1024
    matches = {"REGULAR": regular, "WIDE": wide, "FIXED_IMAGE": fixed}
    if requested == "AUTO":
        for kind, valid in matches.items():
            if valid:
                return kind
    elif matches.get(requested):
        return requested
    raise ValueError(f"Изображение {width}x{height}, {size} байт не соответствует {requested}")


def inspect(data: bytes, requested: str) -> dict:
    if not data or len(data) > MAX_BYTES:
        raise ValueError("Изображение должно занимать от 1 байта до 10 МБ")
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(BytesIO(data)) as picture:
                fmt = picture.format
                if fmt not in {"PNG", "JPEG", "GIF"}:
                    raise ValueError("Поддерживаются только PNG, JPG и GIF")
                width, height = picture.size
                matched = geometry(width, height, requested, len(data))
                picture.verify()
            # verify() does not decode pixel data; reopen and load every frame.
            frames, pixels = 0, 0
            with Image.open(BytesIO(data)) as picture:
                while True:
                    if frames >= MAX_FRAMES:
                        raise ValueError("Изображение содержит более 200 кадров")
                    if picture.size != (width, height):
                        raise ValueError("Кадры изображения меняют размер холста")
                    pixels += picture.width * picture.height
                    if pixels > MAX_DECODED_PIXELS:
                        raise ValueError("Превышен лимит 100 млн декодируемых пикселей")
                    picture.load()
                    frames += 1
                    try:
                        picture.seek(frames)
                    except EOFError:
                        break
    except (OSError, SyntaxError, UnidentifiedImageError,
            Image.DecompressionBombError, Image.DecompressionBombWarning) as exc:
        raise ValueError("Изображение повреждено или небезопасно для декодирования") from exc
    return {"format": fmt, "width": width, "height": height,
            "matched_type": matched, "frames": frames, "decoded": True}
