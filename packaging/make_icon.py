"""生成托盘 / exe 用的 ``voicehub.ico``。

设计口径：深墨绿圆角方块 + 五根米白波形，中间一根青釉色。16px 下仍能看出
“声音”这件事，不依赖细线和小字。构建时用 4 倍超采样再缩，边缘不打锯齿。
"""

from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw

TARGET = Path(__file__).with_name("voicehub.ico")
SIZE = 1024
SCALE = 4

_INK_TOP = (36, 49, 44, 255)
_INK_BOTTOM = (19, 28, 25, 255)
_PAPER = (244, 239, 230, 255)
_CELADON = (143, 195, 176, 255)


def _background(size: int) -> Image.Image:
    gradient = Image.new("RGBA", (1, size))
    pixels = gradient.load()
    for y in range(size):
        ratio = y / max(1, size - 1)
        pixels[0, y] = tuple(
            round(top + (bottom - top) * ratio)
            for top, bottom in zip(_INK_TOP, _INK_BOTTOM, strict=True)
        )
    return gradient.resize((size, size))


def generate(force: bool = False) -> Path:
    if TARGET.is_file() and not force:
        return TARGET

    canvas = SIZE * SCALE
    image = _background(canvas)
    mask = Image.new("L", (canvas, canvas), 0)
    ImageDraw.Draw(mask).rounded_rectangle(
        (canvas * 0.045, canvas * 0.045, canvas * 0.955, canvas * 0.955),
        radius=canvas * 0.21,
        fill=255,
    )
    image.putalpha(mask)
    draw = ImageDraw.Draw(image)

    outline = canvas * 0.012
    draw.rounded_rectangle(
        (canvas * 0.045, canvas * 0.045, canvas * 0.955, canvas * 0.955),
        radius=canvas * 0.21,
        outline=(255, 255, 255, 24),
        width=int(outline),
    )

    heights = (0.28, 0.47, 0.70, 0.47, 0.28)
    bar_width = canvas * 0.088
    gap = canvas * 0.050
    total = len(heights) * bar_width + (len(heights) - 1) * gap
    left = (canvas - total) / 2
    center_y = canvas * 0.5
    for index, ratio in enumerate(heights):
        x = left + index * (bar_width + gap)
        half = canvas * ratio / 2
        draw.rounded_rectangle(
            (x, center_y - half, x + bar_width, center_y + half),
            radius=bar_width / 2,
            fill=_CELADON if index == len(heights) // 2 else _PAPER,
        )

    image = image.resize((SIZE, SIZE), Image.Resampling.LANCZOS)
    image.save(
        TARGET,
        format="ICO",
        sizes=[(16, 16), (20, 20), (24, 24), (32, 32), (40, 40), (48, 48), (64, 64), (128, 128), (256, 256)],
    )
    return TARGET


if __name__ == "__main__":
    print(generate(force=True))
