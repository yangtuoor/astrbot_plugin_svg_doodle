"""SVG 生成与渲染：调用 LLM 写 SVG，转 PNG，加签名。

渲染后端按可用性自动降级，跨 Windows / macOS / Linux：

1. cairosvg        —— Linux / macOS 首选，最省资源，无需外部程序
2. 无头浏览器截图  —— Windows 上没有 libcairo DLL 时的主力方案，
                      自动探测 Edge / Chrome / Chromium
3. rsvg-convert / inkscape / magick / convert —— 系统装了就用
4. 都没有则返回失败，由上层如实回报，不伪造一张空白图

为什么要有降级链：Windows 上 `pip install cairosvg` 能装上 Python 包，
但运行时找不到 libcairo-2.dll 会直接 ImportError。这不是插件的问题，
是平台缺库。让插件自己找可用后端，比让用户去装 GTK 运行时现实得多。
"""

import base64
import io
import glob
import os
import re
import shutil
import subprocess
import sys
import tempfile

from PIL import Image, ImageDraw, ImageFont

SVG_RE = re.compile(r"<svg[\s\S]*?</svg>", re.IGNORECASE)
CODE_FENCE_RE = re.compile(r"```(?:svg|xml)?\s*([\s\S]*?)```", re.IGNORECASE)

# cairosvg 是可选依赖：装不上不该让整个插件 import 失败
try:
    import cairosvg  # noqa: F401

    _HAS_CAIROSVG = True
except Exception:  # noqa: BLE001  (ImportError / OSError: DLL load failed)
    cairosvg = None
    _HAS_CAIROSVG = False


# ---------------------------------------------------------------------------
# SVG 文本处理
# ---------------------------------------------------------------------------
def extract_svg(text: str) -> str | None:
    """从模型输出里提取 SVG 代码。"""
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
    low = text.lower()
    if "<svg" in low:
        start = low.find("<svg")
        end = low.find("</svg>")
        if end > start:
            return text[start : end + 6]
    return None


def normalize_svg(svg: str, size: int = 768) -> str:
    """补全 xmlns 和 viewBox。"""
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


# ---------------------------------------------------------------------------
# 后端探测
# ---------------------------------------------------------------------------
def _browser_candidates() -> list[str]:
    """返回本机可用的 Chromium 系浏览器可执行文件路径。"""
    names = (
        "msedge",
        "microsoft-edge",
        "chrome",
        "google-chrome",
        "google-chrome-stable",
        "chromium",
        "chromium-browser",
        "brave-browser",
        "msedge.exe",
        "chrome.exe",
        "brave.exe",
    )
    found: list[str] = []
    for n in names:
        p = shutil.which(n)
        if p and p not in found:
            found.append(p)

    if sys.platform.startswith("win"):
        roots = [
            os.environ.get("PROGRAMFILES", r"C:\Program Files"),
            os.environ.get("PROGRAMFILES(X86)", r"C:\Program Files (x86)"),
            os.environ.get("LOCALAPPDATA", ""),
        ]
        rels = [
            r"Microsoft\Edge\Application\msedge.exe",
            r"Google\Chrome\Application\chrome.exe",
            r"BraveSoftware\Brave-Browser\Application\brave.exe",
        ]
        for root in roots:
            if not root:
                continue
            for rel in rels:
                p = os.path.join(root, rel)
                if os.path.isfile(p) and p not in found:
                    found.append(p)
    elif sys.platform == "darwin":
        for p in (
            "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
            "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
            "/Applications/Chromium.app/Contents/MacOS/Chromium",
            "/Applications/Brave Browser.app/Contents/MacOS/Brave Browser",
        ):
            if os.path.isfile(p) and p not in found:
                found.append(p)
    else:
        for pat in (
            "/opt/microsoft/msedge/msedge",
            "/usr/lib/chromium/chromium",
            "/snap/bin/chromium",
        ):
            for p in glob.glob(pat):
                if p not in found:
                    found.append(p)
    return found


def available_backends() -> list[str]:
    """按优先级列出当前可用的渲染后端名，用于日志与自检。"""
    out: list[str] = []
    if _HAS_CAIROSVG:
        out.append("cairosvg")
    if _browser_candidates():
        out.append("browser")
    for tool in ("rsvg-convert", "inkscape", "magick", "convert"):
        if shutil.which(tool):
            out.append(tool)
    return out


# ---------------------------------------------------------------------------
# 各后端实现
# ---------------------------------------------------------------------------
def _svg_with_size(svg: str, size: int) -> str:
    """把根 <svg> 的 width/height 钉成目标尺寸（浏览器后端靠这个控制输出大小）。"""
    svg = normalize_svg(svg, size)
    svg = re.sub(
        r'(<svg\b[^>]*?)\swidth="[^"]*"',
        r"\1",
        svg,
        count=1,
    )
    svg = re.sub(
        r'(<svg\b[^>]*?)\sheight="[^"]*"',
        r"\1",
        svg,
        count=1,
    )
    svg = re.sub(
        r"<svg\b",
        f'<svg width="{size}" height="{size}"',
        svg,
        count=1,
    )
    return svg


def _render_cairosvg(svg: str, out_path: str, size: int) -> bool:
    if not _HAS_CAIROSVG:
        return False
    try:
        cairosvg.svg2png(
            bytestring=svg.encode("utf-8"),
            write_to=out_path,
            output_width=size,
            output_height=size,
            background_color="white",
        )
        return os.path.isfile(out_path) and os.path.getsize(out_path) > 0
    except Exception:  # noqa: BLE001
        return False


def _render_browser(svg: str, out_path: str, size: int) -> bool:
    """用无头 Chromium 系浏览器把 SVG 截成 PNG。"""
    exe = None
    for cand in _browser_candidates():
        exe = cand
        break
    if not exe:
        return False

    tmpdir = tempfile.mkdtemp(prefix="svgdoodle_")
    html_path = os.path.join(tmpdir, "page.html")
    html = (
        "<!doctype html><html><head><meta charset='utf-8'>"
        "<style>html,body{margin:0;padding:0;background:#fff;}"
        "svg{display:block;}</style></head><body>"
        f"{_svg_with_size(svg, size)}"
        "</body></html>"
    )
    try:
        with open(html_path, "w", encoding="utf-8") as f:
            f.write(html)
        url = "file:///" + html_path.replace("\\", "/")
        args = [
            exe,
            "--headless=new",
            "--disable-gpu",
            "--hide-scrollbars",
            "--no-sandbox",
            "--no-first-run",
            "--force-device-scale-factor=1",
            f"--window-size={size},{size}",
            f"--screenshot={os.path.abspath(out_path)}",
            url,
        ]
        proc = subprocess.run(
            args,
            capture_output=True,
            timeout=60,
            # Windows 上从 GUI 进程起子进程不该弹黑框
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        ok = (
            os.path.isfile(out_path)
            and os.path.getsize(out_path) > 0
            and proc.returncode == 0
        )
        if not ok:
            # 老版本 Edge/Chrome 不认 --headless=new，退回旧写法重试一次
            args[1] = "--headless"
            subprocess.run(
                args,
                capture_output=True,
                timeout=60,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            ok = os.path.isfile(out_path) and os.path.getsize(out_path) > 0
        if ok:
            # 截图画布可能比目标略大，裁成正方形
            try:
                img = Image.open(out_path).convert("RGB")
                if img.size != (size, size):
                    img = img.crop((0, 0, min(img.size[0], size), min(img.size[1], size)))
                    if img.size != (size, size):
                        canvas = Image.new("RGB", (size, size), "white")
                        canvas.paste(img, (0, 0))
                        img = canvas
                    img.save(out_path)
            except Exception:  # noqa: BLE001
                pass
        return ok
    except Exception:  # noqa: BLE001
        return False
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def _render_cli(svg: str, out_path: str, size: int) -> bool:
    """rsvg-convert / inkscape / ImageMagick 兜底。"""
    tmpdir = tempfile.mkdtemp(prefix="svgdoodle_")
    svg_path = os.path.join(tmpdir, "a.svg")
    try:
        with open(svg_path, "w", encoding="utf-8") as f:
            f.write(normalize_svg(svg, size))

        rsvg = shutil.which("rsvg-convert")
        if rsvg:
            subprocess.run(
                [rsvg, "-w", str(size), "-h", str(size), "-o", out_path, svg_path],
                capture_output=True,
                timeout=60,
            )
            if os.path.isfile(out_path) and os.path.getsize(out_path) > 0:
                return True

        ink = shutil.which("inkscape")
        if ink:
            subprocess.run(
                [
                    ink,
                    svg_path,
                    "--export-type=png",
                    f"--export-filename={out_path}",
                    f"--export-width={size}",
                    f"--export-height={size}",
                ],
                capture_output=True,
                timeout=60,
            )
            if os.path.isfile(out_path) and os.path.getsize(out_path) > 0:
                return True

        for tool in ("magick", "convert"):
            exe = shutil.which(tool)
            if not exe:
                continue
            subprocess.run(
                [
                    exe,
                    "-background",
                    "white",
                    "-density",
                    "144",
                    svg_path,
                    "-resize",
                    f"{size}x{size}",
                    out_path,
                ],
                capture_output=True,
                timeout=60,
            )
            if os.path.isfile(out_path) and os.path.getsize(out_path) > 0:
                return True
        return False
    except Exception:  # noqa: BLE001
        return False
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


# ---------------------------------------------------------------------------
# 对外接口
# ---------------------------------------------------------------------------
def render_svg_to_png(svg: str, out_path: str, size: int = 768) -> bool:
    """渲染 SVG 到 PNG，按可用后端自动降级，返回是否成功。"""
    if not svg:
        return False
    os.makedirs(os.path.dirname(os.path.abspath(out_path)) or ".", exist_ok=True)

    # 按顺序试，任一个成功就返回
    for fn in (_render_cairosvg, _render_browser, _render_cli):
        try:
            if fn(svg, out_path, size):
                return True
        except Exception:  # noqa: BLE001
            continue

    # 清掉可能写了一半的残file，避免上层误判
    try:
        if os.path.isfile(out_path) and os.path.getsize(out_path) == 0:
            os.remove(out_path)
    except OSError:
        pass
    return False


# 常见中日韩字体候选，按平台排列。Windows 用微软雅黑/黑体，macOS 用苹方，
# Linux 用 Noto CJK / Droid。text 里只要有中文就必须命中一个，否则会出豆腐块。
_FONT_CANDIDATES = (
    # Windows
    r"C:\Windows\Fonts\msyh.ttc",
    r"C:\Windows\Fonts\msyhbd.ttc",
    r"C:\Windows\Fonts\simhei.ttf",
    r"C:\Windows\Fonts\simsun.ttc",
    r"C:\Windows\Fonts\Deng.ttf",
    # macOS
    "/System/Library/Fonts/PingFang.ttc",
    "/System/Library/Fonts/Hiragino Sans GB.ttc",
    "/Library/Fonts/Arial Unicode.ttf",
    # Linux
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/truetype/droid/DroidSansFallbackFull.ttf",
    "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
    "/usr/share/fonts/truetype/wqy/wqy-microhei.ttc",
    "/usr/share/fonts/truetype/arphic/uming.ttc",
)


def _load_font(font_size: int, font_path: str = ""):
    """按平台顺序找可用的中文字体，最后退到 Pillow 内置字体。"""
    cands = []
    if font_path:
        cands.append(font_path)
    cands.extend(_FONT_CANDIDATES)
    for cand in cands:
        if not cand:
            continue
        try:
            if os.path.isfile(cand):
                return ImageFont.truetype(cand, font_size)
        except Exception:  # noqa: BLE001
            continue
    return ImageFont.load_default()


def add_signature(
    png_path: str,
    signature: str = "",
    show_date: bool = True,
    font_path: str = "",
    date_text: str = "",
    size: int = 768,
) -> bool:
    """在右下角加签名和日期。跨平台字体自动探测。"""
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
        font = _load_font(font_size, font_path)
        try:
            bbox = draw.textbbox((0, 0), text, font=font)
        except Exception:  # noqa: BLE001
            bbox = (0, 0, size // 3, font_size)
        tw = bbox[2] - bbox[0]
        th = bbox[3] - bbox[1]
        x = max(2, size - tw - size // 20)
        y = max(2, size - th - size // 20)
        draw.text((x, y), text, fill=(110, 110, 110), font=font)
        img.save(png_path, quality=95)
        return True
    except Exception:  # noqa: BLE001
        return False


def svg_to_data_uri(svg: str) -> str:
    b = base64.b64encode(svg.encode("utf-8")).decode()
    return f"data:image/svg+xml;base64,{b}"


def clean_svg_for_preview(svg: str) -> str:
    """给 WebUI 预览用的干净 SVG（补 xmlns）"""
    from_core = normalize_svg(svg)
    return from_core
