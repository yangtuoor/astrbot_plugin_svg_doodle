"""SVG 生成与渲染：调用 LLM 写 SVG，cairosvg 转 PNG，加签名"""

import asyncio
import base64
import io
import re

import cairosvg
from PIL import Image, ImageDraw, ImageFont

SVG_RE = re.compile(r"<svg[\s\S]*?</svg>", re.IGNORECASE)
CODE_FENCE_RE = re.compile(r"```(?:svg|xml)?\s*([\s\S]*?)```", re.IGNORECASE)


def extract_svg(text: str) -> str | None:
    """从模型输出里提取 SVG 代码"""
    if not text:
        return None
    m = SVG_RE.search(text)
    if m:
        return m.group(0)
    m = CODE_FENCE_RE.search(text)
    if m:
        inner = m.group(1)
        m2 = SVG_RE.search(inner)
        if m2:
            return m2.group(0)
        return inner.strip() if "<svg" in inner.lower() else None
    if "<svg" in text.lower():
        start = text.lower().find("<svg")
        end = text.lower().find("</svg>")
        if end > start:
            return text[start : end + 6]
    return None


def normalize_svg(svg: str, size: int = 768) -> str:
    """补全 xmlns 和尺寸"""
    if "xmlns" not in svg:
        svg = re.sub(r"<svg\b", '<svg xmlns="http://www.w3.org/2000/svg"', svg, count=1)
    if "viewBox" not in svg:
        svg = re.sub(
            r"<svg\b([^>]*)>",
            lambda m: f'<svg{m.group(1)} viewBox="0 0 {size} {size}">',
            svg,
            count=1,
        )
    return svg


def render_svg_to_png(svg: str, out_path: str, size: int = 768) -> bool:
    """渲染 SVG 到 PNG，返回是否成功"""
    try:
        cairosvg.svg2png(
            bytestring=svg.encode("utf-8"),
            write_to=out_path,
            output_width=size,
            output_height=size,
            background_color="white",
        )
        return True
    except Exception:
        return False


def add_signature(
    png_path: str,
    signature: str = "",
    show_date: bool = True,
    font_path: str = "",
    date_text: str = "",
    size: int = 768,
) -> bool:
    """在右下角加签名和日期"""
    if not signature and not show_date:
        return True
    try:
        img = Image.open(png_path).convert("RGB")
        draw = ImageDraw.Draw(img)
        text_parts = []
        if signature:
            text_parts.append(f"— {signature}")
        if show_date and date_text:
            text_parts.append(date_text)
        text = "  ".join(text_parts)
        if not text:
            return True
        font_size = max(16, size // 34)
        font = None
        for candidate in (
            font_path,
            "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
            "/usr/share/fonts/truetype/droid/DroidSansFallbackFull.ttf",
        ):
            if not candidate:
                continue
            try:
                font = ImageFont.truetype(candidate, font_size)
                break
            except Exception:
                continue
        if font is None:
            font = ImageFont.load_default()
        bbox = draw.textbbox((0, 0), text, font=font)
        tw = bbox[2] - bbox[0]
        th = bbox[3] - bbox[1]
        x = size - tw - size // 20
        y = size - th - size // 20
        draw.text((x, y), text, fill=(110, 110, 110), font=font)
        img.save(png_path, quality=95)
        return True
    except Exception:
        return False


def svg_to_data_uri(svg: str) -> str:
    b = base64.b64encode(svg.encode("utf-8")).decode()
    return f"data:image/svg+xml;base64,{b}"


def clean_svg_for_preview(svg: str) -> str:
    """给 WebUI 预览用的干净 SVG（补 xmlns）"""
    from_core = normalize_svg(svg)
    return from_core
