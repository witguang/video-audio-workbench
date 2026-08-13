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
    from PIL import Image, ImageChops, ImageDraw, ImageFont, ImageFilter
    PIL_AVAILABLE = True
except ImportError:  # pragma: no cover
    PIL_AVAILABLE = False

CANVAS_W, CANVAS_H = 1920, 1080

FONT_REGULAR = r"C:/Windows/Fonts/msyh.ttc"
FONT_BOLD = r"C:/Windows/Fonts/msyhbd.ttc"

TEMP_CARD_PNG = "temp_card.png"
TEMP_LYRICS_ASS = "temp_lyrics.ass"

# ==================== 输出配置（轻量 h264 · 1080p30 · AAC 立体声） ====================
VIDEO_FPS = 30             # 输出帧率 1080p30
VIDEO_PRESET = "veryfast"  # h264 档位：veryfast 比 medium 快 2-3 倍，文件略大
VIDEO_CRF = 23             # 画质档：23 为标准档，比 17 快且体积小很多
AUDIO_BITRATE = "160k"     # AAC 立体声音频码率



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
        "panel_rect": (200, 110, 1720, 1060),
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
        "lyrics_y": (858, 940, 1022),
    },
}

THEME_LABELS = {key: cfg["label"] for key, cfg in THEMES.items()}


# ==================== 双语歌词布局 ====================

# 开启双语（主英附中）时，当前行要在英文下方额外叠一行中文译文：
# 三行位置整体比单语上移、行距拉开，字号收紧，避免两行高的当前行与上/下一行重叠。
BILINGUAL_LAYOUT = {
    "minimal": {"lyrics_y": (886, 970, 1050), "lyrics_size": 48, "zh_size": 32},
    "player":  {"lyrics_y": (858, 945, 1028), "lyrics_size": 48, "zh_size": 32},
    "glass":   {"lyrics_y": (882, 959, 1036), "lyrics_size": 46, "zh_size": 30},
}
# 中文译文颜色：88% 白（弱于英文纯白主歌词，形成主次层次）
BILINGUAL_ZH_COLOR = "&HE0FFFFFF&"


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
        # 注意：不能用 paste(sheen_img, box, smask)——掩码 alpha 恒为 255，会把白色反光
        # 整块盖在封面上导致卡片变白。先用圆角掩码裁剪反光自身的渐变 alpha，再按自身
        # 透明度合成，封面才会保留可见。
        sheen_img.putalpha(ImageChops.multiply(sheen_img.getchannel("A"), smask))
        canvas.alpha_composite(sheen_img, (margin - 30, margin - 30))

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

    # 歌手（与标题同款柔和阴影，保证在明亮背景上也能看清）
    if artist:
        afont = _fit_font(draw2, artist, cfg["artist"]["size"], cfg["artist"]["max_w"], False)
        _draw_tracked(draw2, eb_xy[0], cfg["artist"]["y"], artist, afont,
                      cfg["artist"]["color"], tracking=1,
                      shadow=(0, 0, 0, 190),
                      anchor_x="l" if theme_key == "player" else "m")


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
                      cfg["artist"]["color"], tracking=1,
                      shadow=(0, 0, 0, 150), anchor_x="l")


# ==================== 歌词(LRC/SRT/VTT) -> ASS（Spotify 配色） ====================

def _read_text(path: str) -> str:
    """读取文本，优先 UTF-8，回退 GBK。"""
    try:
        with open(path, "r", encoding="utf-8") as f:
            return f.read()
    except UnicodeDecodeError:
        try:
            with open(path, "r", encoding="gbk") as f:
                return f.read()
        except Exception:
            return ""
    except Exception:
        return ""


def _clean_cue_text(text: str) -> str:
    """清理字幕文本：去掉 HTML 标签、换行标记，规整空白。"""
    text = re.sub(r"<[^>]+>", "", text)
    text = text.replace("\\N", " ").replace("\\n", " ")
    return " ".join(text.split())


def _to_ms(ts: str) -> int:
    """时间戳(支持 H:MM:SS.mmm / MM:SS.mmm，逗号或点号作毫秒分隔) -> 毫秒。"""
    ts = ts.replace(",", ".")
    parts = ts.split(":")
    sec_ms = parts[-1].split(".")
    total = int(sec_ms[0]) * 1000
    if len(sec_ms) > 1:
        total += int(sec_ms[1].ljust(3, "0")[:3])
    if len(parts) >= 3:
        total += int(parts[-3]) * 3600000 + int(parts[-2]) * 60000
    elif len(parts) == 2:
        total += int(parts[-2]) * 60000
    return total


def _parse_lrc(content: str) -> list:
    """LRC：同一行带一个时间点，结尾取下一行起点（无下一行则 +4s）。"""
    pattern = re.compile(r"\[(\d+):(\d+)(?:\.(\d+))?\](.*)")
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
    return [(ms, lines[i + 1][0] if i + 1 < len(lines) else ms + 4000, text)
            for i, (ms, text) in enumerate(lines)]


def _parse_srt(content: str) -> list:
    """SRT：序号 + 起止时间轴 + 多行文本（可用显式结束时间）。"""
    cues = []
    for block in re.split(r"\r?\n\r?\n+", content.strip()):
        lines = [l.strip() for l in block.splitlines() if l.strip()]
        if not lines:
            continue
        if re.match(r"^\d+$", lines[0]):  # 可选序号行
            lines = lines[1:]
        if not lines:
            continue
        m = re.match(
            r"(\d{1,2}:\d{2}:\d{2}[,.]\d{1,3})\s*-->\s*(\d{1,2}:\d{2}:\d{2}[,.]\d{1,3})",
            lines[0])
        if not m:
            continue
        text = _clean_cue_text(" ".join(lines[1:]))
        if text:
            cues.append((_to_ms(m.group(1)), _to_ms(m.group(2)), text))
    cues.sort()
    return cues


def _parse_vtt(content: str) -> list:
    """VTT：WEBVTT 头，跳过 STYLE/REGION/NOTE 块，时间行后跟多行文本。"""
    content = content.splitlines()
    cues = []
    i = 0
    while i < len(content):
        line = content[i].strip()
        if not line:
            i += 1
            continue
        low = line.lower()
        if low.startswith("webvtt") or low.startswith(("style", "region", "note")):
            while i < len(content) and content[i].strip():
                i += 1
            continue
        m = re.match(
            r"(\d{1,2}:\d{2}(?::\d{2})?[.,]\d{1,3})\s*-->\s*"
            r"(\d{1,2}:\d{2}(?::\d{2})?[.,]\d{1,3})", line)
        if not m:
            i += 1
            continue
        start, end = _to_ms(m.group(1)), _to_ms(m.group(2))
        i += 1
        text_lines = []
        while i < len(content) and content[i].strip():
            text_lines.append(content[i].strip())
            i += 1
        text = _clean_cue_text(" ".join(text_lines))
        if text:
            cues.append((start, end, text))
    cues.sort()
    return cues


def _parse_plain(content: str) -> list:
    """纯歌词：没有时间轴，一行一句。返回 [(0, 0, text), ...]，时间轴后续由同名 SRT 匹配填充。"""
    out = []
    for line in content.splitlines():
        text = _clean_cue_text(line)
        if text:
            out.append((0, 0, text))
    return out


def _resolve_overlap(lines: list) -> list:
    """消除歌词时间重叠：每行结尾不晚于下一行起点。

    SRT/VTT 的字幕时间轴常常相互重叠（词级字幕尤其严重），叠加渲染时会同时出现
    两行高亮。这里把每行结尾裁剪到「下一行起点」，杜绝重叠；被完全吞掉的行丢弃。
    """
    out = []
    n = len(lines)
    for i in range(n):
        ms, end_ms, text = lines[i]
        limit = lines[i + 1][0] if i + 1 < n else end_ms
        new_end = min(end_ms, limit)
        if new_end > ms:
            out.append((ms, new_end, text))
    return out


def _match_srt_timeline(plain_lines: list, plain_path: str, srt_lines: list = None) -> list:
    """纯歌词行 -> 用 SRT 的时间轴做逐行匹配（每行歌词 = 一个对齐单元）。

    背景：词级 SRT 字幕常带错词/空行/重叠时间，而 .plain 是干净的一行一句歌词。
    这里把 SRT 的时间轴嫁接到 plain 每行上，且严格以「一行 plain 歌词」为一个单元：
    1. 用 SRT 时间轴（未传入时自动查找同主名 .srt），把 plain 与 SRT 全文切分成
       token 流做 LCS 全局对齐（天然单调、正确处理重复段落），得到每行在 SRT 流里
       配对成功的 token 位置。
    2. 只有配对置信度足够高的行才作为锚点（配对 token 数 / 行内 token 数 >= 0.5）。
       弱匹配的行（如 SRT 里根本没出现的歌词，只靠一两个常见词碰运气）视为空档，
       在最近两个锚点之间按 token 占比线性插值，防止它抢走相邻行真正对应的 cue。
    3. 锚点起点做亚 cue 细分：取「该行第一个配对 token 在 cue 内的位置比例」对应的
       时间，而不是整条 cue 的起点，避免整行提前或偏晚、相邻行被挤得太短。
    4. 起点强制单调递增；结尾统一取下一行起点（最后一行 +3.5s），保证同一时刻
       只有一行高亮、绝不重叠。
    找不到 SRT 或完全无法匹配时，退化为均匀间隔（3.5s/行）。
    """
    if srt_lines is None:
        dirname = os.path.dirname(os.path.abspath(plain_path))
        base = os.path.splitext(os.path.basename(plain_path))[0]
        srt_path = os.path.join(dirname, base + ".srt")
        if not os.path.isfile(srt_path):
            # 没有配对 SRT：退化为均匀间隔，保证纯歌词仍能渲染
            return [(i * 3500, (i + 1) * 3500, text) for i, (_, _, text) in enumerate(plain_lines)]
        srt_lines = _parse_srt(_read_text(srt_path))
    if not srt_lines:
        return [(i * 3500, (i + 1) * 3500, text) for i, (_, _, text) in enumerate(plain_lines)]

    def toks(s: str) -> list:
        # 小写并按非字母/数字/中文切分（isalnum 已涵盖中日韩字符）
        buf, out = [], []
        for ch in s.lower():
            if ch.isalnum():
                buf.append(ch)
            elif buf:
                out.append("".join(buf))
                buf = []
        if buf:
            out.append("".join(buf))
        return out

    plain_toks = [toks(text) for _, _, text in plain_lines]
    srt_toks = [toks(text) for _, _, text in srt_lines]
    n_cues = len(srt_lines)
    # 展平为 (行号, token) 流
    P = [(li, t) for li, ts in enumerate(plain_toks) for t in ts]
    S = [(ci, t) for ci, ts in enumerate(srt_toks) for t in ts]
    p, s = len(P), len(S)
    if not P or not S:
        return plain_lines

    starts = [None] * len(plain_lines)
    if p * s <= 2_000_000:  # LCS 矩阵大小兜底，超大文本走贪心窗口
        # LCS 动态规划（长度表）
        dp = [[0] * (s + 1) for _ in range(p + 1)]
        for i in range(1, p + 1):
            ptok = P[i - 1][1]
            row_prev = dp[i - 1]
            row_cur = dp[i]
            for j in range(1, s + 1):
                if ptok == S[j - 1][1]:
                    row_cur[j] = row_prev[j - 1] + 1
                elif row_prev[j] >= row_cur[j - 1]:
                    row_cur[j] = row_prev[j]
                else:
                    row_cur[j] = row_cur[j - 1]
        # 回溯：记录每个配对的 plain token 对应的 srt token 下标
        matched_srt = {}
        i, j = p, s
        while i > 0 and j > 0:
            if P[i - 1][1] == S[j - 1][1] and dp[i][j] == dp[i - 1][j - 1] + 1:
                matched_srt[i - 1] = j - 1
                i -= 1
                j -= 1
            elif dp[i - 1][j] >= dp[i][j - 1]:
                i -= 1
            else:
                j -= 1
        # —— 每行歌词视为一个单元，由「配对 token 位置」决定锚点 ——
        # SRT 流下标 -> (cue 序号, cue 内 token 位置)
        cue_len = [len(ts) for ts in srt_toks]
        _counter = [0] * n_cues
        stream_meta = []
        for ci, _t in S:
            stream_meta.append((ci, _counter[ci]))
            _counter[ci] += 1

        # 每行在 SRT 流中配对成功的 token 下标（保持顺序）
        line_first_idx = [0]
        for ts in plain_toks:
            line_first_idx.append(line_first_idx[-1] + len(ts))
        line_matches = []
        for li, ts in enumerate(plain_toks):
            ms_ = []
            for idx in range(line_first_idx[li], line_first_idx[li + 1]):
                if idx in matched_srt:
                    ms_.append(matched_srt[idx])
            line_matches.append(ms_)

        # 锚点 = 配对置信度 >= 0.5 的行；弱匹配行留空，稍后插值
        ANCHOR_MIN = 0.5
        for li, ts in enumerate(plain_toks):
            ms_ = line_matches[li]
            if not ms_ or len(ms_) / max(len(ts), 1) < ANCHOR_MIN:
                continue
            sidx = ms_[0]
            ci, pos = stream_meta[sidx]
            cs, ce, _ = srt_lines[ci]
            # 亚 cue 细分：第一个配对 token 在 cue 内的位置比例 -> 对应时间
            starts[li] = int(cs + (ce - cs) * pos / max(cue_len[ci], 1))
    else:
        # 超大文本：贪心窗口（按 token 重合度取第一句达标 cue，极少数情况触发）
        WINDOW = 40
        MIN_OVERLAP = 0.5
        n_cues = len(srt_lines)
        pointer = 0
        for li, ts in enumerate(plain_toks):
            target_set = set(ts)
            for k in range(pointer, min(pointer + WINDOW, n_cues)):
                cue_set = set(srt_toks[k])
                if not cue_set:
                    continue
                inter = len(target_set & cue_set)
                if 2.0 * inter / (len(ts) + len(srt_toks[k])) >= MIN_OVERLAP:
                    starts[li] = srt_lines[k][0]
                    pointer = k + 1
                    break

    anchors = [i for i, st in enumerate(starts) if st is not None]
    if not anchors:
        # 完全无法匹配：退化为均匀间隔
        return [(i * 3500, (i + 1) * 3500, text) for i, (_, _, text) in enumerate(plain_lines)]

    prefix = [0]
    for ts in plain_toks:
        prefix.append(prefix[-1] + max(len(ts), 1))

    for i, st in enumerate(starts):
        if st is not None:
            continue
        prev = next((a for a in reversed(anchors) if a < i), None)
        nxt = next((a for a in anchors if a > i), None)
        if prev is not None and nxt is not None:
            t_prev, t_next = starts[prev], starts[nxt]
            span_p = max(prefix[nxt] - prefix[prev], 1)
            starts[i] = int(t_prev + (t_next - t_prev) * (prefix[i] - prefix[prev]) / span_p)
        elif prev is not None:
            starts[i] = starts[prev] + (i - prev) * 3500  # 结尾：3.5s/行外推
        else:
            starts[i] = max(0, starts[nxt] - (nxt - i) * 3500)  # 开头：3.5s/行回退

    # 插值后的原始起点快照：后续单调/最小间隔的顺延只在必要时生效，回拉时以此为下限
    interp_starts = starts[:]

    # 起点严格单调：相邻两行若撞到同一条 cue（或多行共享同一时间），把后行推到
    # 下一条更晚的 cue 起点，避免 _resolve_overlap 把整行丢弃。
    for i in range(1, len(starts)):
        if starts[i] <= starts[i - 1]:
            cand = next((srt_lines[k][0] for k in range(n_cues)
                         if srt_lines[k][0] > starts[i - 1]), None)
            starts[i] = cand if cand is not None else starts[i - 1] + 500

    # 最小可读时长：每行至少 2.2s，过短的行向后顺延（长间隔自然吸收累计位移）。
    # 若顺延到超过最后一条 SRT cue 的结束时间，则从尾部回拉，保证不超出歌曲长度。
    MIN_DUR = 2200
    for i in range(1, len(starts)):
        if starts[i] < starts[i - 1] + MIN_DUR:
            starts[i] = starts[i - 1] + MIN_DUR
    limit = srt_lines[-1][1] + 500
    if starts[-1] > limit:
        for i in range(len(starts) - 2, -1, -1):
            starts[i] = max(interp_starts[i], min(starts[i], starts[i + 1] - MIN_DUR))

    # 结尾统一为「下一行起点」，最后一行 +3.5s
    return [(starts[i], starts[i + 1] if i + 1 < len(plain_lines) else starts[i] + 3500, text)
            for i, (_, _, text) in enumerate(plain_lines)]


def _prefer_plain_sibling(path: str, subtitle_lines: list) -> list:
    """SRT/VTT 被选中时，若同目录存在同主名 .plain/.txt，以纯歌词文本和行为准。

    用户选 .srt/.vtt 常常只是想要它的时间轴，而歌词文本可能错词、行切分也乱。
    plain 才是干净正确的一行一句歌词。这里改用 plain 的行 + SRT 的时间轴，
    让「无论选 .srt 还是 .plain，结果都是 plain 文本 + SRT 时间轴」。
    """
    base = os.path.splitext(path)[0]
    for cand in (base + ".plain", base + ".txt"):
        if os.path.isfile(cand):
            plain_lines = _parse_plain(_read_text(cand))
            if plain_lines:
                return _match_srt_timeline(plain_lines, cand, srt_lines=subtitle_lines)
    return subtitle_lines


def _parse_lyrics(path: str, fix_overlap: bool = True) -> list:
    """按扩展名分发解析，统一返回 [(start_ms, end_ms, text), ...]。

    - LRC：时间轴来自 [mm:ss]，结尾用下一句起点推断。
    - SRT / VTT：用字幕自带起止时间轴；若同目录有同主名 .plain/.txt，
      自动改为 plain 文本 + SRT 时间轴（见 _prefer_plain_sibling）。
    - plain / txt：无时间轴的纯歌词行，自动寻找同目录同名 .srt 做时间轴匹配。
    fix_overlap=True 时统一消除重叠（每行结尾不晚于下一行起点）。
    """
    content = _read_text(path)
    if not content:
        return []
    ext = os.path.splitext(path)[1].lower()
    if ext == ".vtt":
        lines = _prefer_plain_sibling(path, _parse_vtt(content))
    elif ext == ".srt":
        lines = _prefer_plain_sibling(path, _parse_srt(content))
    elif ext == ".plain":
        lines = _parse_plain(content)
    else:
        lines = _parse_lrc(content) or _parse_plain(content)
    if not lines:
        return []
    if all(ms == 0 and end_ms == 0 for ms, end_ms, _ in lines):
        lines = _match_srt_timeline(lines, path)
    if fix_overlap:
        lines = _resolve_overlap(lines)
    return lines


def _parse_zh_lines(zh_path: str) -> list:
    """解析中文翻译文件，返回纯文本行列表（按行号与英文行 1:1 配对）。

    支持 plain/txt/lrc/srt/vtt；带时间轴的格式只取其文本部分，不关心其自身时间轴。
    双语对齐由英文行的时间轴决定，中文文件只提供译文文本，因此两份文件的行数应一致
    （多余/缺失的行忽略）。
    """
    if not zh_path:
        return []
    content = _read_text(zh_path)
    if not content:
        return []
    ext = os.path.splitext(zh_path)[1].lower()
    if ext == ".srt":
        return [text for _, _, text in _parse_srt(content)]
    if ext == ".vtt":
        return [text for _, _, text in _parse_vtt(content)]
    if ext == ".lrc":
        return [text for _, _, text in _parse_lrc(content)]
    return [text for _, _, text in _parse_plain(content)]


def _fmt_ass(ms: int) -> str:
    h, ms = divmod(ms, 3600000)
    m, ms = divmod(ms, 60000)
    s, c = divmod(ms, 1000)
    return f"{h}:{m:02d}:{s:02d}.{c // 10:02d}"


def _ass_escape(text: str) -> str:
    """转义 ASS 事件文本中的花括号，防止被 libass 当作样式覆盖标签。"""
    return text.replace("\\", "\\\\").replace("{", "\\{").replace("}", "\\}")


def convert_lrc_to_ass(lrc_path: str, theme_key: str, fix_overlap: bool = True,
                       zh_path: str = None, zh_lines: list = None,
                       zh_translate_fn: object = None) -> str:
    """歌词(LRC/SRT/VTT/plain) -> ASS（Spotify 配色逻辑）。

    同一时刻展示三行：上一行 / 当前行 / 下一行。
    当前行纯白高亮并略放大，上一行与下一行 45% 半透明白（灰感），带淡入淡出。
    SRT/VTT 用字幕自带的起止时间轴，LRC 用「下一句起点」推断结尾，
    plain 纯歌词自动匹配同目录同名 SRT 时间轴。
    fix_overlap=True 时裁剪每行结尾不晚于下一行起点，避免两行歌词同时高亮重叠。
    各主题仅在字号上略有差异，配色逻辑统一，与 Spotify 歌词一致。

    zh_path：可选的中文翻译文件（plain/txt/lrc/srt/vtt）。传入后开启「主英附中」双语：
    当前行在英文下方叠加一行较小的中文译文（88% 白），上一行/下一行仍只显示英文灰暗行。
    中文按行号与英文行 1:1 配对，双语对齐完全由英文行时间轴决定。
    双语模式下三行位置错开、字号收紧（BILINGUAL_LAYOUT），给中文译文留出空间。

    中文来源的三种方式，按优先级 zh_lines > zh_translate_fn > zh_path：
    - zh_lines：直接给定与英文行等长的中文行列表（如翻译 API 的返回）。
    - zh_translate_fn：回调 ``fn(英文文本行列表) -> 中文行列表``，在解析出英文
      行后调用（如接入翻译 API）。回调抛异常会以 ValueError 向上传播。
    - zh_path：翻译文件路径，见上。
    """
    cfg = THEMES[theme_key]
    lines = _parse_lyrics(lrc_path, fix_overlap=fix_overlap)
    if not lines:
        return ""

    # 中文来源：直接列表 > 翻译回调 > 翻译文件，取首个非空来源进入双语模式
    zh_lines = list(zh_lines) if zh_lines else None
    if zh_lines is None and zh_translate_fn is not None:
        try:
            zh_lines = list(zh_translate_fn([text for _, _, text in lines]) or [])
        except Exception as exc:
            raise ValueError(f"中文翻译失败: {exc}") from exc
    elif zh_lines is None:
        zh_lines = _parse_zh_lines(zh_path) if zh_path else []
    bilingual = bool(zh_lines)

    if bilingual:
        bcfg = BILINGUAL_LAYOUT.get(theme_key, BILINGUAL_LAYOUT["minimal"])
        active_size = bcfg["lyrics_size"]
        zh_size = bcfg["zh_size"]
        prev_y, active_y, next_y = bcfg["lyrics_y"]
    else:
        active_size = cfg.get("lyrics_size", 54)
        prev_y, active_y, next_y = cfg.get("lyrics_y", (868, 950, 1032))
    dim_size = max(active_size - 14, 28)
    tracking = cfg.get("lyrics_tracking", 2)
    cx = CANVAS_W // 2

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
        ms, end_ms, text = lines[i]
        start_t, end_t = _fmt_ass(ms), _fmt_ass(end_ms)
        safe = _ass_escape(text)
        # 双语「主英附中」：当前行英文下方叠一行较小的中文译文（两行块整体居中）
        if bilingual and i < len(zh_lines) and zh_lines[i]:
            zh = _ass_escape(zh_lines[i])
            active_text = f"{safe}\\N{{\\fs{zh_size}\\c{BILINGUAL_ZH_COLOR}}}{zh}"
        else:
            active_text = safe
        # 当前行：纯白高亮
        events.append(
            f"Dialogue: 0,{start_t},{end_t},Active,,0,0,0,,"
            f"{{\\an5\\pos({cx},{active_y})\\fad(280,260)}}{active_text}")
        # 上一行：灰暗
        if i > 0:
            prev = _ass_escape(lines[i - 1][2])
            events.append(
                f"Dialogue: 0,{start_t},{end_t},Dim,,0,0,0,,"
                f"{{\\an5\\pos({cx},{prev_y})\\fad(380,360)}}{prev}")
        # 下一行：灰暗
        if i + 1 < n:
            nxt = _ass_escape(lines[i + 1][2])
            events.append(
                f"Dialogue: 0,{start_t},{end_t},Dim,,0,0,0,,"
                f"{{\\an5\\pos({cx},{next_y})\\fad(380,360)}}{nxt}")

    path = os.path.join(os.getcwd(), TEMP_LYRICS_ASS)
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(head + events))
    return TEMP_LYRICS_ASS


# ==================== ffmpeg 合成命令 ====================

def build_background_command(image_path: str, static_png: str, theme_key: str,
                             bg_png: str) -> list:
    """一次性合成「静态背景」PNG：模糊 + 色彩 + 暗角 + 上下渐隐 + 封面卡片层。

    背景是静态不变的，只需渲染一帧，避免编码阶段每一帧都重跑 gblur 等重滤镜，
    这是卡片模式提速的关键。输出为一张不透明白底 PNG（temp_bg.png）。
    """
    cfg = THEMES[theme_key]
    W, H = CANVAS_W, CANVAS_H
    blur, sat, bri = cfg["bg_blur"], cfg["bg_saturation"], cfg["bg_brightness"]

    sy = int(H * 0.52)
    ty = 300
    filters = [
        # 背景：模糊 + 轻微色彩
        f"[0:v]scale={W}:{H}:force_original_aspect_ratio=increase:flags=lanczos,crop={W}:{H},"
        f"gblur=sigma={blur},eq=saturation={sat}:brightness={bri},setsar=1[bg]",
        # 暗角，压出前景
        "[bg]vignette=PI/6[bgv]",
        # 底部渐隐遮罩（保证歌词可读）
        f"color=s={W}x{H}:c=black:r=1,setsar=1,format=rgba,"
        f"geq=lum=0:cb=128:cr=128:a='if(lt(Y,{sy}),0,min(255,(Y-{sy})/{H - sy}*215))'[scrim_b]",
        "[bgv][scrim_b]overlay=0:0[bg1]",
        # 顶部渐隐遮罩（保证标题可读）
        f"color=s={W}x{H}:c=black:r=1,setsar=1,format=rgba,"
        f"geq=lum=0:cb=128:cr=128:a='if(gt(Y,{ty}),0,min(255,({ty}-Y)/{ty}*125))'[scrim_t]",
        "[bg1][scrim_t]overlay=0:0[bg2]",
        # 静态层（透明 PNG，含封面卡与排版）
        "[1:v]format=rgba[card]",
        "[bg2][card]overlay=0:0,format=rgb24[out]",
    ]
    return [
        "-y",
        "-loop", "1", "-framerate", "1", "-i", image_path,     # input 0：封面原图
        "-loop", "1", "-framerate", "1", "-i", static_png,      # input 1：静态卡片层
        "-filter_complex", ",".join(filters),
        "-map", "[out]",
        "-frames:v", "1",
        bg_png,
    ]


def build_card_command(audio_path: str, bg_png: str,
                       ass_file: str, theme_key: str, video_out: str) -> list:
    """组装最终渲染命令：静态背景循环 + ASS 歌词 + 轻量 h264/AAC 输出。

    背景已提前合成（见 build_background_command），编码阶段只有字幕叠加，
    1080p30 veryfast 档位即可接近实时输出。
    """
    filters = ([f"[1:v]ass={TEMP_LYRICS_ASS}[outv]"] if ass_file
               else ["[1:v]format=yuv420p[outv]"])
    return [
        "-y",
        "-i", audio_path,                       # input 0：完整基准音频
        "-loop", "1", "-framerate", str(VIDEO_FPS), "-i", bg_png,   # input 1：静态背景
        "-filter_complex", ",".join(filters),
        "-map", "[outv]",
        "-map", "0:a",
        "-c:v", "libx264",
        "-preset", VIDEO_PRESET,
        "-crf", str(VIDEO_CRF),
        "-r", str(VIDEO_FPS),
        "-pix_fmt", "yuv420p",
        "-c:a", "aac",
        "-b:a", AUDIO_BITRATE,
        "-ac", "2",
        "-aspect", "16:9",
        "-shortest",
        video_out,
    ]
