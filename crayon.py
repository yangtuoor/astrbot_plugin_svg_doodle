"""蜡笔纹理后处理：把干净的矢量渲染图变成手绘蜡笔画质感"""

import random

import numpy as np
from PIL import Image, ImageDraw, ImageFilter


def _make_crayon_texture(size, angle=-35, density=0.55, seed=11):
    """生成蜡笔笔触纹理。返回 L 模式灰度图，255 = 重笔触。"""
    random.seed(seed)
    np.random.seed(seed)

    tex = Image.new("L", (size * 2, size * 2), 0)
    draw = ImageDraw.Draw(tex)

    total = int(size * size * density / 6)
    for _ in range(total):
        x = random.uniform(-size, size * 2)
        y = random.uniform(-size, size * 2)
        length = random.uniform(4, 26)
        width = random.uniform(0.6, 2.4)
        a = np.radians(angle + random.uniform(-14, 14))
        x2 = x + np.cos(a) * length
        y2 = y + np.sin(a) * length
        value = int(random.uniform(110, 255))
        draw.line([(x, y), (x2, y2)], fill=value, width=int(round(width)) or 1)

    left = (tex.width - size) // 2
    top = (tex.height - size) // 2
    tex = tex.crop((left, top, left + size, top + size))
    return tex.filter(ImageFilter.GaussianBlur(0.6))


def apply_crayon(
    src_path,
    dst_path,
    size=768,
    texture_strength=0.18,
    edge_jitter=1.8,
    grain_strength=14.0,
    stroke_rough=1.0,
    seed=7,
):
    """把 src_path 的干净图片处理成蜡笔风格，写到 dst_path。"""
    random.seed(seed)
    np.random.seed(seed)

    img = Image.open(src_path).convert("RGBA")
    if img.size != (size, size):
        img = img.resize((size, size), Image.LANCZOS)

    arr = np.array(img).astype(np.int16)
    h, w = arr.shape[:2]

    if edge_jitter > 0:
        dx = np.clip(np.random.randn(h, w) * edge_jitter, -4, 4).astype(np.int16)
        dy = np.clip(np.random.randn(h, w) * edge_jitter, -4, 4).astype(np.int16)
        yy, xx = np.mgrid[0:h, 0:w]
        sy = np.clip(yy + dy, 0, h - 1)
        sx = np.clip(xx + dx, 0, w - 1)
        arr = arr[sy, sx]

    rgb = arr[:, :, :3].astype(np.float32)
    non_white = (rgb.sum(axis=2) < 720).astype(np.float32)

    # 涂色出界：彩色区域外扩 1px，边缘略微变暗
    padded = np.pad(non_white, 1, mode="edge")
    dilated = np.zeros_like(non_white)
    for i in range(3):
        for j in range(3):
            dilated += padded[i : i + h, j : j + w]
    leak = (dilated > 0) & (non_white == 0)
    if leak.any():
        rgb[leak] = np.clip(rgb[leak] - 14, 0, 255)

    # 蜡笔笔触：只在着色区域显现
    if texture_strength > 0:
        tex = np.array(_make_crayon_texture(size, seed=seed)).astype(np.float32) / 255.0
        tex = np.clip(tex**0.7, 0, 1)
        delta = (tex - 0.5) * 2 * 255 * texture_strength * non_white
        rgb = np.clip(rgb + delta[:, :, None], 0, 255)

    if stroke_rough > 0:
        noise = np.random.randn(h, w) * stroke_rough * 8
        rgb = np.clip(rgb + noise[:, :, None], 0, 255)

    if grain_strength > 0:
        grain = (np.random.rand(h, w) - 0.5) * grain_strength
        rgb = np.clip(rgb + grain[:, :, None], 0, 255)

    painted = Image.fromarray(rgb.astype(np.uint8), "RGB")
    painted = painted.filter(ImageFilter.GaussianBlur(0.4))

    # 米白纸底
    base = np.full((size, size, 3), [253, 251, 245], dtype=np.float32)
    paper_tex = np.random.randn(size, size) * 3.5
    base = np.clip(base + paper_tex[:, :, None], 0, 255)
    paper = Image.fromarray(base.astype(np.uint8), "RGB")

    mask = Image.fromarray((non_white * 255).astype(np.uint8), "L").filter(
        ImageFilter.GaussianBlur(0.5)
    )
    result = Image.composite(painted, paper, mask)
    result.save(dst_path, quality=95)
    return dst_path
