# -*- coding: utf-8 -*-
"""生成应用图标 assets/icon.png + icon.ico（开发期工具，用完保留可重跑）。"""
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

OUT = Path(__file__).resolve().parents[1] / "assets"
OUT.mkdir(parents=True, exist_ok=True)
SIZE = 256


def base_image():
    img = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    # 粉色渐变圆角方块
    top, bottom = (255, 131, 165), (251, 114, 153)
    grad = Image.new("RGBA", (SIZE, SIZE))
    gd = ImageDraw.Draw(grad)
    for y in range(SIZE):
        t = y / (SIZE - 1)
        gd.line([(0, y), (SIZE, y)],
                fill=tuple(int(top[i] + (bottom[i] - top[i]) * t) for i in range(3)))
    mask = Image.new("L", (SIZE, SIZE), 0)
    md = ImageDraw.Draw(mask)
    md.rounded_rectangle([8, 8, SIZE - 8, SIZE - 8], radius=56, fill=255)
    img.paste(grad, (0, 0), mask)
    # 顶部两个"天线"（电视/弹幕元素）
    d = ImageDraw.Draw(img)
    d.line([(78, 8), (108, 46)], fill=(255, 255, 255, 230), width=14)
    d.line([(178, 8), (148, 46)], fill=(255, 255, 255, 230), width=14)
    # 白色 B
    font = None
    for cand in ("C:/Windows/Fonts/arialbd.ttf", "C:/Windows/Fonts/msyhbd.ttc",
                 "C:/Windows/Fonts/segoeuib.ttf"):
        try:
            font = ImageFont.truetype(cand, 150)
            break
        except OSError:
            continue
    if font is None:
        font = ImageFont.load_default()
    bbox = d.textbbox((0, 0), "B", font=font)
    w, h = bbox[2] - bbox[0], bbox[3] - bbox[1]
    d.text(((SIZE - w) / 2 - bbox[0], (SIZE - h) / 2 - bbox[1] + 14), "B",
           font=font, fill=(255, 255, 255, 255))
    return img


img = base_image()
img.save(OUT / "icon.png")
img.save(OUT / "icon.ico", sizes=[(16, 16), (24, 24), (32, 32), (48, 48),
                                  (64, 64), (128, 128), (256, 256)])
print("icon ->", OUT / "icon.png", "and icon.ico")
