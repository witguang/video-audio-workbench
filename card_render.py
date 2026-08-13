# -*- coding: utf-8 -*-
"""
卡片模式渲染模块（设计版 v2）

负责三件事：
1. 用 Pillow 预渲染「静态层」透明 PNG —— 封面卡（圆角/软阴影/描边/玻璃反光）、
   歌名/歌手排版、装饰元素。ffmpeg 只负责把这张 PNG 叠加到模糊背景上。
2. 把 LRC 歌词转换成 ASS 字幕 —— 当前行高亮放大、上一行压暗、淡入淡出。
3. 组装最终 ffmpeg 合成命令（1920x1080 内渲染，保证清晰度）。

三种主题：minimal(极简高级) / player(播放器分栏) / glass(玻璃拟态)。
"""

import os
import re
from typing import Optional

try:
    from PIL import Image, ImageDraw, ImageFont, ImageFilter
    PIL_AVAILABLE = True
except ImportError:  # pragma: no cover
    PIL_AVAILABLE = False

CANVAS_W, CANVAS_H = 1920, 1080

FONT_REGULAR = r"C:/Windows/Fonts/msyh.ttc"
FONT_BOLD = r"C:/Windows/Fonts/msyhbd.ttc"

TEMP_CARD_PNG = "temp_card.png"
TEMP_LYRICS_ASS = "temp_lyrics.ass"


# ==================== 主题配置 ====================
# 坐标均为 1920x1080 画布；accent 为 0xRRGGBB；ass_accent 为 &HBBGGRR
THEMES = {
    "minimal": {
        "label": "极简高级",
        "bg_blur": 34,
        "bg_saturation": 1.08,
        "bg_brightness": -0.05,
        "card_size": 540,
        "card_xy": (690, 84),
        "card_radius": 40,
        "card_border": (255, 255, 255, 46),
        "card_shadow": (0, 0, 0, 150),
        "card_sheen": True,
        "eyebrow": ("NOW PLAYING", 32, (185, 198, 218, 255), (960, 664), 12),
        "title": {"size": 68, "color": (255, 255, 255, 255), "y": 738, "max_w": 1100},
        "divider": {"y": 694, "w": 96, "h": 3, "color": (240, 201, 135, 220)},
        "artist": {"size": 40, "color": (205, 213, 228, 255), "y": 800, "max_w": 1100},
        "accent": (240, 201, 135),
        "lyrics_size": 54,
        "lyrics_tracking": 2,
        "lyrics_y": (916, 986, 1056),
    },
    "player": {
        "label": "播放器分栏",
        "bg_blur": 38,
        "bg_saturation": 1.1,
        "bg_brightness": -0.06,
        "card_size": 660,
        "card_xy": (150, 170),
        "card_radius": 44,
        "card_border": (255, 255, 255, 50),
        "card_shadow": (0, 0, 0, 160),
        "card_sheen": True,
        "eyebrow": ("NOW PLAYING", 34, (170, 188, 224, 255), (940, 296), 14),
        "title": {"size": 78, "color": (255, 255, 255, 255), "y": 350, "max_w": 860},
        "divider": {"y": 478, "w": 420, "h": 2, "color": (255, 255, 255, 60)},
        "artist": {"size": 44, "color": (211, 219, 232, 255), "y": 522, "max_w": 860},
        "meta": ("— 卡片歌词模式 —", 30, (150, 164, 186, 255), (940, 580), 6),
        "accent": (122, 162, 255),
        "lyrics_size": 54,
        "lyrics_tracking": 2,
        "lyrics_y": (868, 950, 1032),
    },
    "glass": {
        "label": "玻璃拟态",
        "bg_blur": 50,
        "bg_saturation": 1.3,
        "bg_brightness": -0.09,
        "panel_rect": (200, 110, 1720, 970),
        "panel_radius": 48,
        "cover_size": 540,
        "cover_xy": (302, 275),
        "eyebrow": ("NOW PLAYING", 32, (200, 228, 244, 255), (944, 340), 12),
        "title": {"size": 66, "color": (255, 255, 255, 255), "y": 396, "max_w": 660},
        "divider": {"y": 486, "w": 300, "h": 2, "color": (159, 227, 255, 200)},
        "artist": {"size": 40, "color": (219, 230, 240, 255), "y": 524, "max_w": 660},
        "accent": (159, 227, 255),
        "lyrics_size": 56,
        "lyrics_tracking": 2,
        "lyrics_y": (868, 950, 1032),
    },
}

THEME_LABELS = {key: cfg["label"] for key, cfg in THEMES.items()}


# ==================== 基础工具 ====================

def _font(bold: bool, size: int) -> ImageFont.FreeTypeFont:
    path = FONT_BOLD if bold else FONT_REGULAR
    try:
        return ImageFont.truetype(path, size)
    except Exception:
        return ImageFont.truetype(FONT_REGULAR, size)


def _split_title_artist(filename: str):
    """从文件名拆出 歌名 / 歌手（兼容“歌手 - 歌名”或“歌名 - 歌手”两种写法）。"""
    base = os.path.splitext(os.path.basename(filename))[0]
    clean = re.sub(r"[\(\[][^\]\)]*[\)\]]", "", base).strip()
    parts = [p.strip() for p in clean.split("-") if p.strip()]
    if len(parts) >= 2:
        return parts[1], parts[0]
    return base, ""


def _draw_tracked(draw, cx, cy, text, font, fill, tracking=0, anchor_x="m",
                  shadow=None, shadow_offset=(0, 4), shadow_blur=4):
    """按字宽手动排版，支持字距 tracking 与柔和文字阴影。"""
    if not text:
        return
    asc, desc = font.getmetrics()
    widths = [draw.textlength(ch, font=font) for ch in text]
    total = sum(widths) + tracking * max(len(text) - 1, 0)
    x = cx - total / 2 if anchor_x == "m" else (cx if anchor_x == "l" else cx - total)
    y = cy - (asc + desc) / 2

    if shadow:
        layer = Image.new("RGBA", draw._image.size, (0, 0, 0, 0))
        sd = ImageDraw.Draw(layer)
        sx, sy = x + shadow_offset[0], y + shadow_offset[1]
        for ch, w in zip(text, widths):
            sd.text((sx, sy), ch, font=font, fill=shadow)
            sx += w + tracking
        layer = layer.filter(ImageFilter.GaussianBlur(shadow_blur))
        draw._image.alpha_composite(layer)

    for ch, w in zip(text, widths):
        draw.text((x, y), ch, font=font, fill=fill)
        x += w + tracking


def _fit_font(draw, text, font_size: int, max_w: int, bold: bool) -> ImageFont.FreeTypeFont:
    """标题过长时自动缩小字号直到能放进最大宽度。"""
    size = font_size
    while size > 26:
        f = _font(bold, size)
        if draw.textlength(text, font=f) <= max_w:
            return f
        size -= 2
    return _font(bold, 26)


def _render_cover_card(cover: Image.Image, size: int, radius: int,
                       border_rgba, shadow_rgba, sheen: bool) -> Image.Image:
    """生成一张带软阴影 + 圆角 + 内描边 + 可选玻璃反光的封面卡透明图。"""
    margin = max(84, int(size * 0.16))
    full = size + 2 * margin
    canvas = Image.new("RGBA", (full, full), (0, 0, 0, 0))

    # 1) 多层软投影（略向下偏移 + 高斯模糊）
    sh = Image.new("RGBA", (full, full), (0, 0, 0, 0))
    sd = ImageDraw.Draw(sh)
    sd.rounded_rectangle(
        [margin, margin + 12, margin + size, margin + size + 12],
        radius=radius, fill=shadow_rgba)
    sh = sh.filter(ImageFilter.GaussianBlur(22))
    canvas.alpha_composite(sh)

    # 2) 圆角封面
    mask = Image.new("L", (size, size), 0)
    ImageDraw.Draw(mask).rounded_rectangle([0, 0, size - 1, size - 1], radius=radius, fill=255)
    canvas.paste(cover.resize((size, size), Image.LANCZOS), (margin, margin), mask)

    # 3) 内描边高光
    ImageDraw.Draw(canvas).rounded_rectangle(
        [margin, margin, margin + size - 1, margin + size - 1],
        radius=radius, outline=border_rgba, width=3)

    # 4) 顶部玻璃反光条（斜向渐变，模拟玻璃反射）
    if sheen:
        w, h = size + 60, size + 60
        grad = Image.new("L", (w, h), 0)
        px = grad.load()
        for y in range(h):
            for x in range(w):
                v = int(52 * max(0.0, 1 - abs(x - y) / max(w, 1)))
                px[x, y] = v
        sheen_img = Image.new("RGBA", (w, h), (255, 255, 255, 255))
        sheen_img.putalpha(grad)
        smask = Image.new("L", (w, h), 0)
        ImageDraw.Draw(smask).rounded_rectangle([0, 0, w - 1, h - 1], radius=radius, fill=255)
        canvas.paste(sheen_img, (margin - 30, margin - 30), smask)

    return canvas


# ==================== 静态层渲染 ====================

def render_static_layer(image_path: str, filename: str, theme_key: str) -> str:
    """渲染整张透明静态层（封面卡 + 排版 + 装饰），返回保存路径。"""
    if not PIL_AVAILABLE:
        raise RuntimeError("卡片模式需要 Pillow，请运行: pip install pillow")
    cfg = THEMES[theme_key]
    canvas = Image.new("RGBA", (CANVAS_W, CANVAS_H), (0, 0, 0, 0))
    draw = ImageDraw.Draw(canvas)
    cover = Image.open(image_path).convert("RGB")
    title, artist = _split_title_artist(filename)

    if theme_key == "glass":
        _render_glass(canvas, draw, cover, title, artist, cfg)
    else:
        _render_card_layout(canvas, draw, cover, title, artist, cfg, theme_key)

    path = os.path.join(os.getcwd(), TEMP_CARD_PNG)
    canvas.save(path, "PNG")
    return path


def _render_card_layout(canvas, draw, cover, title, artist, cfg, theme_key):
    """minimal / player：封面卡 + 右侧或居中排版。"""
    size = cfg["card_size"]
    cx, cy = cfg["card_xy"]  # 封面卡左上角坐标
    card = _render_cover_card(cover, size, cfg["card_radius"], cfg["card_border"],
                              cfg["card_shadow"], cfg["card_sheen"])
    # _render_cover_card 内部有 margin 留白（放阴影），换算成画布坐标
    margin = (card.width - size) // 2
    canvas.alpha_composite(card, (cx - margin, cy - margin))

    # 排版（文字基线按主题配置）
    eb_text, eb_size, eb_color, eb_xy, eb_track = cfg["eyebrow"]
    draw2 = ImageDraw.Draw(canvas)
    _draw_tracked(draw2, eb_xy[0], eb_xy[1], eb_text, _font(False, eb_size),
                  eb_color, tracking=eb_track, anchor_x="l" if theme_key == "player" else "m")

    # 装饰分割线
    div = cfg["divider"]
    lx = eb_xy[0] - div["w"] / 2 if theme_key == "minimal" else eb_xy[0]
    draw2.rounded_rectangle([lx, div["y"], lx + div["w"], div["y"] + div["h"]],
                            radius=div["h"] / 2, fill=div["color"])

    # 标题
    tfont = _fit_font(draw2, title, cfg["title"]["size"], cfg["title"]["max_w"], True)
    _draw_tracked(draw2, eb_xy[0], cfg["title"]["y"], title, tfont,
                  cfg["title"]["color"], tracking=1,
                  shadow=(0, 0, 0, 190), anchor_x="l" if theme_key == "player" else "m")

    # 歌手
    if artist:
        afont = _fit_font(draw2, artist, cfg["artist"]["size"], cfg["artist"]["max_w"], False)
        _draw_tracked(draw2, eb_xy[0], cfg["artist"]["y"], artist, afont,
                      cfg["artist"]["color"], tracking=1,
                      anchor_x="l" if theme_key == "player" else "m")

    # player 主题的底部小注记
    if "meta" in cfg:
        m_text, m_size, m_color, m_xy, m_track = cfg["meta"]
        _draw_tracked(draw2, m_xy[0], m_xy[1], m_text, _font(False, m_size),
                      m_color, tracking=m_track, anchor_x="l")


def _render_glass(canvas, draw, cover, title, artist, cfg):
    """glass：整块磨砂玻璃面板 + 内部封面与排版。"""
    x0, y0, x1, y1 = cfg["panel_rect"]
    radius = cfg["panel_radius"]

    # 面板主体（半透明白 + 顶部高光描边 + 底部内阴影）
    draw.rounded_rectangle([x0, y0, x1, y1], radius=radius, fill=(255, 255, 255, 20))
    # 顶部 1.5px 高光
    draw.rounded_rectangle([x0, y0, x1, y1], radius=radius, outline=(255, 255, 255, 96), width=2)
    # 底部细内影
    draw.rounded_rectangle([x0 + 2, y0 + 2, x1 - 2, y1 - 2], radius=radius,
                           outline=(0, 0, 0, 70), width=1)

    # 封面
    csize = cfg["cover_size"]
    ccx, ccy = cfg["cover_xy"]
    cover_img = _render_cover_card(cover, csize, 40, (255, 255, 255, 60),
                                   (0, 0, 0, 120), False)
    margin = cover_img.width // 2 - csize // 2
    canvas.alpha_composite(cover_img, (ccx - margin, ccy - margin))

    # 排版
    eb_text, eb_size, eb_color, eb_xy, eb_track = cfg["eyebrow"]
    _draw_tracked(draw, eb_xy[0], eb_xy[1], eb_text, _font(False, eb_size),
                  eb_color, tracking=eb_track, anchor_x="l")

    div = cfg["divider"]
    draw.rounded_rectangle([eb_xy[0], div["y"], eb_xy[0] + div["w"], div["y"] + div["h"]],
                           radius=div["h"] / 2, fill=div["color"])

    tfont = _fit_font(draw, title, cfg["title"]["size"], cfg["title"]["max_w"], True)
    _draw_tracked(draw, eb_xy[0], cfg["title"]["y"], title, tfont,
                  cfg["title"]["color"], tracking=1,
                  shadow=(0, 0, 0, 150), anchor_x="l")

    if artist:
        afont = _fit_font(draw, artist, cfg["artist"]["size"], cfg["artist"]["max_w"], False)
        _draw_tracked(draw, eb_xy[0], cfg["artist"]["y"], artist, afont,
                      cfg["artist"]["color"], tracking=1, anchor_x="l")


# ==================== LRC -> ASS（L2 高亮歌词） ====================

def _parse_lrc(lrc_path: str) -> list:
    pattern = re.compile(r"\[(\d+):(\d+)(?:\.(\d+))?\](.*)")
    try:
        with open(lrc_path, "r", encoding="utf-8") as f:
            content = f.read()
    except UnicodeDecodeError:
        try:
            with open(lrc_path, "r", encoding="gbk") as f:
                content = f.read()
        except Exception:
            return []

    lines = []
    for line in content.splitlines():
        m = pattern.match(line.strip())
        if not m:
            continue
        minutes, seconds = int(m.group(1)), int(m.group(2))
        frac = m.group(3) or "0"
        ms_val = int(frac) * 100 if len(frac) == 1 else int(frac) * 10 if len(frac) == 2 else int(frac[:3])
        text = m.group(4).strip()
        if text:
            lines.append((minutes * 60000 + seconds * 1000 + ms_val, text))
    lines.sort()
    return lines


def _fmt_ass(ms: int) -> str:
    h, ms = divmod(ms, 3600000)
    m, ms = divmod(ms, 60000)
    s, c = divmod(ms, 1000)
    return f"{h}:{m:02d}:{s:02d}.{c // 10:02d}"


def _ass_escape(text: str) -> str:
    """转义 ASS 事件文本中的花括号，防止被 libass 当作样式覆盖标签。"""
    return text.replace("\\", "\\\\").replace("{", "\\{").replace("}", "\\}")


def convert_lrc_to_ass(lrc_path: str, theme_key: str) -> str:
    """LRC -> ASS（Spotify 配色逻辑）。

    同一时刻展示三行：上一行 / 当前行 / 下一行。
    当前行纯白高亮并略放大，上一行与下一行 45% 半透明白（灰感），带淡入淡出。
    各主题仅在字号上略有差异，配色逻辑统一，与 Spotify 歌词一致。
    """
    cfg = THEMES[theme_key]
    lines = _parse_lrc(lrc_path)
    if not lines:
        return ""

    active_size = cfg.get("lyrics_size", 54)
    dim_size = max(active_size - 14, 28)
    tracking = cfg.get("lyrics_tracking", 2)
    cx = CANVAS_W // 2
    prev_y, active_y, next_y = cfg.get("lyrics_y", (868, 950, 1032))

    style_active = (f"Active,Microsoft YaHei,{active_size},&H00FFFFFF,&H00FFFFFF,"
                    f"&H0C0C0C,&H64000000,1,0,0,0,100,100,{tracking},"
                    f"0,1,1.5,3,2,120,120,120,1")
    style_dim = (f"Dim,Microsoft YaHei,{dim_size},&H73FFFFFF,&H00FFFFFF,"
                 f"&H0C0C0C,&H00000000,0,0,0,0,100,100,{tracking},"
                 f"0,1,0,0,2,120,120,120,1")

    head = [
        "[Script Info]",
        "ScriptType: v4.00+",
        f"PlayResX: {CANVAS_W}",
        f"PlayResY: {CANVAS_H}",
        "ScaledBorderAndShadow: yes",
        "WrapStyle: 2",
        "",
        "[V4+ Styles]",
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding",
        "Style: " + style_active,
        "Style: " + style_dim,
        "",
        "[Events]",
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text",
    ]

    events = []
    n = len(lines)
    for i in range(n):
        ms, text = lines[i]
        end_ms = lines[i + 1][0] if i + 1 < n else ms + 4000
        start_t, end_t = _fmt_ass(ms), _fmt_ass(end_ms)
        safe = _ass_escape(text)
        # 当前行：纯白高亮
        events.append(
            f"Dialogue: 0,{start_t},{end_t},Active,,0,0,0,,"
            f"{{\\an5\\pos({cx},{active_y})\\fad(280,260)}}{safe}")
        # 上一行：灰暗
        if i > 0:
            prev = _ass_escape(lines[i - 1][1])
            events.append(
                f"Dialogue: 0,{start_t},{end_t},Dim,,0,0,0,,"
                f"{{\\an5\\pos({cx},{prev_y})\\fad(380,360)}}{prev}")
        # 下一行：灰暗
        if i + 1 < n:
            nxt = _ass_escape(lines[i + 1][1])
            events.append(
                f"Dialogue: 0,{start_t},{end_t},Dim,,0,0,0,,"
                f"{{\\an5\\pos({cx},{next_y})\\fad(380,360)}}{nxt}")

    path = os.path.join(os.getcwd(), TEMP_LYRICS_ASS)
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(head + events))
    return TEMP_LYRICS_ASS


# ==================== ffmpeg 合成命令 ====================

def build_card_command(audio_path: str, image_path: str, static_png: str,
                       ass_file: str, theme_key: str, video_out: str) -> list:
    """组装卡片模式的 ffmpeg 命令：模糊背景 + 渐变/暗角 + 静态层 + ASS 歌词。"""
    cfg = THEMES[theme_key]
    W, H = CANVAS_W, CANVAS_H
    blur, sat, bri = cfg["bg_blur"], cfg["bg_saturation"], cfg["bg_brightness"]

    sy = int(H * 0.52)
    ty = 300
    filters = [
        # 背景：模糊 + 轻微色彩
        f"[1:v]scale={W}:{H}:force_original_aspect_ratio=increase:flags=lanczos,crop={W}:{H},"
        f"gblur=sigma={blur},eq=saturation={sat}:brightness={bri},setsar=1[bg]",
        # 暗角，压出前景
        "[bg]vignette=PI/6[bgv]",
        # 底部渐隐遮罩（保证歌词可读）
        f"color=s={W}x{H}:c=black:r=25,setsar=1,format=rgba,"
        f"geq=lum=0:cb=128:cr=128:a='if(lt(Y,{sy}),0,min(255,(Y-{sy})/{H - sy}*215))'[scrim_b]",
        "[bgv][scrim_b]overlay=0:0[bg1]",
        # 顶部渐隐遮罩（保证标题可读）
        f"color=s={W}x{H}:c=black:r=25,setsar=1,format=rgba,"
        f"geq=lum=0:cb=128:cr=128:a='if(gt(Y,{ty}),0,min(255,({ty}-Y)/{ty}*125))'[scrim_t]",
        "[bg1][scrim_t]overlay=0:0[bg2]",
        # 静态层（透明 PNG，含封面卡与排版）
        "[2:v]format=rgba[card]",
        "[bg2][card]overlay=0:0[bg3]",
    ]

    if ass_file:
        filters.append(f"[bg3]ass={TEMP_LYRICS_ASS}[outv]")
    else:
        filters.append("[bg3]setsar=1[outv]")

    return [
        "-y",
        "-i", audio_path,                       # input 0：完整基准音频
        "-loop", "1", "-framerate", "25", "-i", image_path,   # input 1：封面（背景+原始图）
        "-loop", "1", "-framerate", "25", "-i", static_png,   # input 2：静态层
        "-filter_complex", ",".join(filters),
        "-map", "[outv]",
        "-map", "0:a",
        "-c:v", "libx264",
        "-preset", "medium",
        "-crf", "17",
        "-pix_fmt", "yuv420p",
        "-c:a", "aac",
        "-b:a", "192k",
        "-aspect", "16:9",
        "-shortest",
        video_out,
    ]
