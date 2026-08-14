# -*- coding: utf-8 -*-
import json
import os
import re
import threading
import traceback
import tkinter as tk
from dataclasses import dataclass
from tkinter import filedialog, messagebox, ttk
from tkinter.scrolledtext import ScrolledText
from urllib.parse import unquote, urlparse

import card_render
import lyric_translate
import sph_publish
from mode1_ffmpeg import SOURCE_LOCAL, SOURCE_R2, DEFAULT_CARD_THEME, mode1_process

APP_DIR = os.path.dirname(os.path.abspath(__file__))
SETTINGS_FILE = os.path.join(APP_DIR, "app_settings.json")
VIDEO_EXTENSIONS = (
    ".mp4", ".mkv", ".avi", ".mov", ".flv", ".wmv", ".m4v", ".webm",
    ".ts", ".m2ts", ".mts", ".mpg", ".mpeg", ".3gp", ".ogv", ".vob",
    ".rm", ".rmvb", ".asf",
)
VIDEO_FILE_TYPES = [
    (
        "所有支持的视频",
        "*.mp4 *.mkv *.avi *.mov *.flv *.wmv *.m4v *.webm *.ts *.m2ts *.mts *.mpg *.mpeg *.3gp *.ogv *.vob *.rm *.rmvb *.asf",
    ),
    ("常规视频", "*.mp4 *.mkv *.avi *.mov *.flv *.wmv *.m4v"),
    ("Web 视频", "*.webm *.ogv"),
    ("采集/蓝光/传输流", "*.ts *.m2ts *.mts *.vob"),
    ("移动端/旧格式", "*.3gp *.rm *.rmvb *.asf *.mpg *.mpeg"),
    ("所有文件", "*.*"),
]

MODE_LOCAL_FILE = "本地电脑直处理"
MODE_LOCAL_FOLDER = "本地文件夹批量"
MODE_R2_URL = "R2 URL 下载后处理"

MODE_CONFIG = {
    MODE_LOCAL_FILE: {
        "input_label": "本地视频文件",
        "browse_text": "选择视频",
        "allow_browse": True,
        "source_mode": SOURCE_LOCAL,
        "guide": [
            "1. 选择本机视频文件。",
            "2. 选择封面图片（用于生成视频），可在此指定歌词文件（LRC/SRT/VTT/纯歌词）。",
            "3. 推荐开启极简卡片歌词模式，设置统一的处理范围即可！",
        ],
    },
    MODE_LOCAL_FOLDER: {
        "input_label": "本地视频文件夹",
        "browse_text": "选择文件夹",
        "allow_browse": True,
        "source_mode": SOURCE_LOCAL,
        "guide": [
            "1. 选择一个本地视频文件夹。",
            "2. 可选指定统一输出目录、统一封面和歌词文件。",
            "3. 设置统一的处理范围，批量合成时音频/视频/字幕自动对齐。",
        ],
    },
    MODE_R2_URL: {
        "input_label": "R2 视频 URL",
        "browse_text": "手动输入",
        "allow_browse": False,
        "source_mode": SOURCE_R2,
        "guide": [
            "1. 粘贴 R2 视频的完整下载链接。",
            "2. 设置导出音频和可选的视频输出位置，可配歌词文件。",
            "3. 设置统一的处理范围，只渲染所选片段、字幕自动对齐。",
        ],
    },
}


@dataclass
class BatchJob:
    source_path: str
    audio_out: str
    video_out: str


def sanitize_filename(value: str) -> str:
    cleaned = re.sub(r'[<>:"/\\|?*]+', "_", value)
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" .")
    return cleaned or "未命名"


def smart_parse_name(video_path: str) -> str:
    base_name = os.path.splitext(os.path.basename(video_path))[0]
    clean_name = re.sub(r"[\(\[][^\]\)]*[\)\]]", "", base_name).strip()
    raw_parts = [part.strip() for part in clean_name.split("-") if part.strip()] # 已在此处彻底修正 typo
    parts = [part for part in raw_parts if not part.isdigit()]

    if len(parts) >= 2:
        preferred_name = f"{parts[1]} - {parts[0]}"
    elif len(parts) == 1:
        preferred_name = parts[0]
    else:
        preferred_name = base_name

    return sanitize_filename(preferred_name)


def _format_output_field_from_stem(stem: str) -> str:
    """由「歌手 - 歌名」文件名（不含扩展名）生成输出字段：
    {歌名 - 歌手 #歌手去空格 #歌名去空格}；多歌手按逗号拆分、各生成一个话题标签。"""
    clean = re.sub(r"[\(\[][^\]\)]*[\)\]]", "", stem).strip()
    raw_parts = [p.strip() for p in clean.split("-") if p.strip()]
    parts = [p for p in raw_parts if not p.isdigit()]

    if len(parts) >= 2:
        artist, title = parts[0], parts[1]
    elif len(parts) == 1:
        artist, title = "", parts[0]
    else:
        artist, title = "", stem.strip()

    tags = []
    if artist:
        for a in artist.split(","):
            a = a.strip()
            if a:
                tags.append("#" + re.sub(r"\s+", "", a))
    if title:
        tags.append("#" + re.sub(r"\s+", "", title))

    head = f"{title} - {artist}" if artist else title
    return " ".join([head] + tags)


def build_output_field(video_path: str) -> str:
    stem = os.path.splitext(os.path.basename(video_path))[0]
    return _format_output_field_from_stem(stem)


class VideoWorkbenchApp:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title("视频音频处理工作台 (独立裁剪版)")
        self.set_initial_window_size()
        self.root.minsize(1080, 700)
        self.root.configure(bg="#f6f2ea")

        self._create_variables()
        self._build_style()
        self._build_ui()
        self._bind_events()
        self.root.report_callback_exception = self.report_callback_exception
        self.load_settings()
        self.update_mode_ui()
        self.update_output_field()
        self.set_status("就绪：统一处理范围已生效，音频/视频/字幕自动对齐。")
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)

    def set_initial_window_size(self):
        screen_width = self.root.winfo_screenwidth()
        screen_height = self.root.winfo_screenheight()
        width = min(1240, max(1080, screen_width - 80))
        height = min(860, max(700, screen_height - 100))
        pos_x = max((screen_width - width) // 2, 20)
        pos_y = max((screen_height - height) // 2, 20)
        self.root.geometry(f"{width}x{height}+{pos_x}+{pos_y}")

    def _create_variables(self):
        self.mode_var = tk.StringVar(value=MODE_LOCAL_FILE)
        self.video_var = tk.StringVar()
        self.image_var = tk.StringVar()
        self.lrc_var = tk.StringVar()
        # 双语中文来源：off=关闭 / file=中文翻译文件 / api=翻译 API
        self.zh_mode_var = tk.StringVar(value="off")
        self.zh_lrc_var = tk.StringVar()
        self.zh_provider_var = tk.StringVar()
        self.zh_api_key_var = tk.StringVar()
        self.zh_provider_labels = [cfg["label"] for cfg in lyric_translate.PROVIDERS.values()]
        self.vinyl_mode_var = tk.BooleanVar(value=True)
        self.audio_var = tk.StringVar()
        self.video_out_var = tk.StringVar()
        self.batch_output_var = tk.StringVar()
        
        # 统一处理范围（导出音频/卡片视频/字幕共用，视频范围为准）
        self.video_start_var = tk.StringVar(value="00:00:00")
        self.video_end_var = tk.StringVar(value="End")

        self.auto_open_var = tk.BooleanVar(value=True)
        self.card_theme_var = tk.StringVar(value=DEFAULT_CARD_THEME)
        self.fix_overlap_var = tk.BooleanVar(value=True)
        # 画面方向：默认横屏 16:9；勾选后输出竖屏 1080×1920（9:16，适配手机）
        self.portrait_var = tk.BooleanVar(value=False)
        # 歌词显示方式：spotify=三行高亮（默认） / scroll=连续滚动（参考主流播放器卡拉OK）
        self.lyric_style_labels = {"spotify": "三行高亮", "scroll": "滚动歌词"}
        self.lyric_style_var = tk.StringVar(value="spotify")
        # 输出画质预设（参考 HandBrake 内置预设，默认 Fast 1080p）
        self.quality_var = tk.StringVar(value=card_render.DEFAULT_VIDEO_PRESET)
        # 数字指纹（防盗用）：可见水印 + 不可见 metadata 指纹（唯一 ID 自动生成）
        self.watermark_enabled_var = tk.BooleanVar(value=False)
        self.watermark_text_var = tk.StringVar(value="")
        self.watermark_position_var = tk.StringVar(value="bottom_right")
        self.status_var = tk.StringVar()
        self.output_field_var = tk.StringVar()
        # 发布到视频号：semi=半自动（推荐，停在发表前由用户点击）/ auto=全自动（headless 直发，有封号风险）
        self.sph_mode_var = tk.StringVar(value="semi")
        self.sph_cookie_var = tk.StringVar(value=sph_publish.DEFAULT_COOKIE_FILE)
        self.input_label_var = tk.StringVar(value=MODE_CONFIG[MODE_LOCAL_FILE]["input_label"])

    def _build_style(self):
        style = ttk.Style()
        style.theme_use("clam")

        # —— 配色「黑胶金标 / Vinyl & Gold」：暖象牙封套 + 奶油卡 + 黄铜金 + 复古表头绿 ——
        PAPER = "#f6f2ea"   # 页面底（唱片封套）
        CARD  = "#fffdf8"   # 卡片面（奶油白）
        INK   = "#1f1a15"   # 主文字（暖近黑）
        BODY  = "#4a4238"   # 正文（暖棕灰）
        MUTED = "#82796a"   # 次要/提示文字
        BRASS = "#b5862c"   # 黄铜金（强调、主按钮、焦点）
        GREEN = "#2f5d50"   # 复古表头绿（状态/点缀）
        HAIR  = "#e7dfd0"   # 发丝线/边框

        # 供 _build_ui 等复用（如标题下的金色签名线）
        self.accent = BRASS
        self.paper = PAPER

        # 基础面：默认容器融入卡片底（覆盖未显式指定样式的内嵌 Frame）
        style.configure("TFrame", background=CARD)
        style.configure("Root.TFrame", background=PAPER)
        style.configure("Card.TFrame", background=CARD)

        # —— 文字 ——
        style.configure("Title.TLabel", background=PAPER, foreground=INK, font=("Microsoft YaHei UI", 22, "bold"))
        style.configure("CardTitle.TLabel", background=CARD, foreground=INK, font=("Microsoft YaHei UI", 12, "bold"))
        style.configure("Body.TLabel", background=CARD, foreground=BODY, font=("Microsoft YaHei UI", 10))
        style.configure("Hint.TLabel", background=CARD, foreground=MUTED, font=("Microsoft YaHei UI", 9))
        style.configure("Status.TLabel", background=CARD, foreground=GREEN, font=("Microsoft YaHei UI", 9, "bold"))

        # —— 按钮：主按钮黄铜实心、次按钮发丝描边，均扁平 + 状态反馈 ——
        style.configure("Primary.TButton", font=("Microsoft YaHei UI", 11, "bold"), padding=(18, 10),
                        background=BRASS, foreground="#ffffff",
                        relief="flat", borderwidth=0, focuscolor=BRASS)
        style.map("Primary.TButton",
                  background=[("active", "#c89a3e"), ("pressed", "#9a7022"), ("disabled", "#d9c9a8")],
                  foreground=[("disabled", "#f3ead8")],
                  focuscolor=[("focus", "#c89a3e")])
        style.configure("Secondary.TButton", font=("Microsoft YaHei UI", 10), padding=(12, 8),
                        background=CARD, foreground=BODY,
                        relief="solid", borderwidth=1, bordercolor=HAIR, focuscolor=BRASS)
        style.map("Secondary.TButton",
                  background=[("active", "#f3ece0"), ("pressed", "#ece2d0")],
                  bordercolor=[("active", BRASS)],
                  foreground=[("disabled", MUTED)])

        # —— 勾选框 / 单选：圆形点指示器（选中实心 / 未选中空心），背景对齐卡片 ——
        style.configure("TCheckbutton", background=CARD, foreground=BODY,
                        font=("Microsoft YaHei UI", 10), focuscolor=BRASS)
        style.configure("TRadiobutton", background=CARD, foreground=BODY,
                        font=("Microsoft YaHei UI", 10), focuscolor=BRASS)
        # 复用的「卡片底」变体，供已显式指定样式的勾选框/单选使用
        style.configure("Body.TCheckbutton", background=CARD, foreground=BODY, font=("Microsoft YaHei UI", 10))
        style.configure("Body.TRadiobutton", background=CARD, foreground=BODY, font=("Microsoft YaHei UI", 10))

        # 勾选框的「√」改成「圆点」：用圆形图片替换原生勾选指示器
        try:
            from PIL import Image, ImageDraw, ImageTk
            _d = 16
            _off = Image.new("RGBA", (_d, _d), (0, 0, 0, 0))
            ImageDraw.Draw(_off).ellipse([4, 4, 12, 12], outline=MUTED, width=2)
            _on = Image.new("RGBA", (_d, _d), (0, 0, 0, 0))
            ImageDraw.Draw(_on).ellipse([4, 4, 12, 12], fill=BRASS)
            self._cb_off_img = ImageTk.PhotoImage(_off)
            self._cb_on_img = ImageTk.PhotoImage(_on)
            style.element_create("CB.dot", "image", self._cb_off_img,
                                 ("selected", self._cb_on_img))
            style.layout("TCheckbutton", [
                ("Checkbutton.padding", {"sticky": "nswe", "children": [
                    ("CB.dot", {"side": "left", "sticky": ""}),
                    ("Checkbutton.focus", {"side": "left", "sticky": "w", "children": [
                        ("Checkbutton.label", {"sticky": "nswe"})
                    ]})
                ]})
            ])
        except Exception:
            pass

        # —— 输入框：白底 + 发丝边，聚焦时黄铜描边 ——
        style.configure("TEntry", fieldbackground="#ffffff", foreground=INK,
                        insertcolor=INK, padding=6,
                        bordercolor=HAIR, lightcolor=HAIR, darkcolor=HAIR)
        style.map("TEntry",
                  bordercolor=[("focus", BRASS)],
                  lightcolor=[("focus", BRASS)],
                  darkcolor=[("focus", BRASS)])
        style.configure("TCombobox", fieldbackground="#ffffff", background="#ffffff",
                        foreground=INK, arrowcolor=BRASS, padding=5,
                        bordercolor=HAIR, lightcolor=HAIR, darkcolor=HAIR)
        style.map("TCombobox",
                  bordercolor=[("focus", BRASS)],
                  arrowcolor=[("active", "#c89a3e")])

        # —— 分区卡片：发丝线边框 ——
        style.configure("Section.TLabelframe", background=CARD, borderwidth=1, relief="solid", bordercolor=HAIR)
        style.configure("Section.TLabelframe.Label", background=CARD, foreground=INK, font=("Microsoft YaHei UI", 11, "bold"))

        # —— 滚动条：细杆、与页面同色槽 ——
        style.configure("TScrollbar", background=HAIR, troughcolor=PAPER, bordercolor=PAPER, arrowcolor=MUTED, relief="flat")

    def _build_ui(self):
        shell = ttk.Frame(self.root, style="Root.TFrame", padding=(18, 16, 18, 14))
        shell.grid(row=0, column=0, sticky="nsew")
        self.root.grid_rowconfigure(0, weight=1)
        self.root.grid_columnconfigure(0, weight=1)
        shell.grid_rowconfigure(1, weight=1)
        shell.grid_columnconfigure(0, weight=1)

        header = ttk.Frame(shell, style="Root.TFrame")
        header.grid(row=0, column=0, sticky="ew", pady=(0, 14))
        header.grid_columnconfigure(0, weight=1)

        ttk.Label(header, text="视频音频处理工作台", style="Title.TLabel").grid(row=0, column=0, sticky="w")
        accent = tk.Frame(header, bg=self.accent, height=3)
        accent.grid(row=1, column=0, sticky="ew", pady=(8, 0))
        accent.grid_propagate(False)

        content = ttk.Frame(shell, style="Root.TFrame")
        content.grid(row=1, column=0, sticky="nsew")
        content.grid_columnconfigure(0, weight=3, uniform="content")
        content.grid_columnconfigure(1, weight=2, uniform="content")
        content.grid_rowconfigure(0, weight=1)

        left = ttk.Frame(content, style="Card.TFrame")
        left.grid(row=0, column=0, sticky="nsew", padx=(0, 10))
        left.grid_columnconfigure(0, weight=1)
        left.grid_rowconfigure(0, weight=1)

        self.left_canvas = tk.Canvas(left, bg="#f6f2ea", highlightthickness=0, bd=0)
        self.left_canvas.grid(row=0, column=0, sticky="nsew")
        left_scrollbar = ttk.Scrollbar(left, orient="vertical", command=self.left_canvas.yview)
        left_scrollbar.grid(row=0, column=1, sticky="ns")
        self.left_canvas.configure(yscrollcommand=left_scrollbar.set)

        left_inner = ttk.Frame(self.left_canvas, style="Root.TFrame")
        left_inner.grid_columnconfigure(0, weight=1)
        self.left_canvas_window = self.left_canvas.create_window((0, 0), window=left_inner, anchor="nw")

        right = ttk.Frame(content, style="Root.TFrame")
        right.grid(row=0, column=1, sticky="nsew")
        right.grid_columnconfigure(0, weight=1)
        right.grid_rowconfigure(0, weight=1)

        self._build_left_column(left_inner)
        self._build_right_column(right)

        self.left_canvas.bind("<Configure>", self._resize_left_canvas_window)
        left_inner.bind("<Configure>", self._update_left_scrollregion)

        # 底部操作栏
        action_bar = ttk.Frame(shell, style="Card.TFrame", padding=(14, 12))
        action_bar.grid(row=2, column=0, sticky="ew", pady=(14, 0))
        action_bar.grid_columnconfigure(0, weight=1)
        action_bar.grid_columnconfigure(1, weight=1)
        action_bar.grid_columnconfigure(2, weight=1)
        
        mode_select_frame = ttk.Frame(action_bar, style="Card.TFrame")
        mode_select_frame.grid(row=0, column=0, sticky="w")
        ttk.Checkbutton(mode_select_frame, text="完成后自动打开输出目录", variable=self.auto_open_var).grid(row=0, column=0, sticky="w", pady=(0, 4))
        ttk.Checkbutton(mode_select_frame, text="开启卡片歌词模式", variable=self.vinyl_mode_var).grid(row=1, column=0, sticky="w")

        theme_row = ttk.Frame(mode_select_frame, style="Card.TFrame")
        theme_row.grid(row=2, column=0, sticky="w", pady=(6, 0))
        ttk.Label(theme_row, text="卡片风格:", style="Body.TLabel").grid(row=0, column=0, sticky="w")
        self.theme_combo = ttk.Combobox(theme_row, state="readonly", width=10)
        self.theme_combo.grid(row=0, column=1, sticky="w", padx=(8, 0))
        self.theme_combo["values"] = [card_render.THEME_LABELS[k] for k in card_render.THEMES]
        self.theme_combo.set(card_render.THEME_LABELS.get(DEFAULT_CARD_THEME, "极简高级"))
        self.theme_combo.bind("<<ComboboxSelected>>", lambda _e: self._on_theme_selected())
        ttk.Checkbutton(theme_row, text="竖屏输出（9:16 适配手机）", variable=self.portrait_var,
                        style="Body.TCheckbutton").grid(row=0, column=2, sticky="w", padx=(16, 0))
        ttk.Label(theme_row, text="歌词显示:", style="Body.TLabel").grid(row=0, column=3, sticky="w", padx=(16, 0))
        self.lyric_style_combo = ttk.Combobox(theme_row, state="readonly", width=10)
        self.lyric_style_combo.grid(row=0, column=4, sticky="w", padx=(8, 0))
        self.lyric_style_combo["values"] = list(self.lyric_style_labels.values())
        self.lyric_style_combo.set(self.lyric_style_labels["spotify"])
        self.lyric_style_combo.bind("<<ComboboxSelected>>", lambda _e: self._on_lyric_style_selected())
        ttk.Label(theme_row, text="输出画质:", style="Body.TLabel").grid(row=1, column=0, sticky="w", pady=(6, 0))
        self.quality_combo = ttk.Combobox(theme_row, state="readonly", width=16)
        self.quality_combo.grid(row=1, column=1, sticky="w", padx=(8, 0), pady=(6, 0))
        self.quality_combo["values"] = list(card_render.VIDEO_PRESETS.keys())
        self.quality_combo.set(card_render.DEFAULT_VIDEO_PRESET)
        self.quality_combo.bind("<<ComboboxSelected>>", lambda _e: self._on_quality_selected())
        ttk.Checkbutton(theme_row, text="数字指纹（防盗用）", variable=self.watermark_enabled_var,
                        style="Body.TCheckbutton").grid(row=2, column=0, sticky="w", pady=(6, 0))
        ttk.Label(theme_row, text="水印文字:", style="Body.TLabel").grid(row=2, column=1, sticky="w", padx=(16, 0), pady=(6, 0))
        self.watermark_entry = ttk.Entry(theme_row, textvariable=self.watermark_text_var, width=22)
        self.watermark_entry.grid(row=2, column=2, sticky="w", padx=(8, 0), pady=(6, 0))
        ttk.Button(theme_row, text="验证指纹（识别盗版）", style="Secondary.TButton",
                   command=self._verify_fingerprint).grid(row=2, column=3, sticky="w", padx=(16, 0), pady=(6, 0))
        ttk.Label(theme_row, text="水印位置:", style="Body.TLabel").grid(row=3, column=0, sticky="w", pady=(6, 0))
        self.watermark_position_combo = ttk.Combobox(theme_row, state="readonly", width=10)
        self.watermark_position_combo.grid(row=3, column=1, sticky="w", padx=(8, 0), pady=(6, 0))
        self.watermark_position_combo["values"] = list(card_render.WATERMARK_POSITIONS.values())
        self.watermark_position_combo.set(card_render.WATERMARK_POSITIONS["bottom_right"])
        self.watermark_position_combo.bind("<<ComboboxSelected>>", lambda _e: self._on_watermark_position_selected())

        ttk.Button(action_bar, text="重新智能生成输出路径", style="Secondary.TButton", command=lambda: self.smart_fill_outputs(force=True, announce=True)).grid(row=0, column=1, sticky="e", padx=(8, 8))
        self.run_button = ttk.Button(action_bar, text="开始生成音频 / 视频", style="Primary.TButton", command=self.run_action)
        self.run_button.grid(row=0, column=2, sticky="e")

        status_bar = ttk.Frame(shell, style="Card.TFrame", padding=(12, 8))
        status_bar.grid(row=3, column=0, sticky="ew", pady=(14, 0))
        status_bar.grid_columnconfigure(0, weight=1)
        ttk.Label(status_bar, textvariable=self.status_var, style="Status.TLabel").grid(row=0, column=0, sticky="w")

        # 输出字段（最底部）：按当前输入生成「歌名 - 歌手 #话题标签」，右键复制
        output_field_card = ttk.Frame(shell, style="Card.TFrame", padding=(12, 10))
        output_field_card.grid(row=4, column=0, sticky="ew", pady=(10, 0))
        output_field_card.grid_columnconfigure(0, weight=1)
        ttk.Label(output_field_card, text="输出字段（右键点击复制）", style="Hint.TLabel").grid(row=0, column=0, sticky="w")
        self.output_field_label = ttk.Label(output_field_card, textvariable=self.output_field_var,
                                            style="Body.TLabel", wraplength=1000, justify="left")
        self.output_field_label.grid(row=1, column=0, sticky="ew", pady=(4, 0))
        self.output_field_label.bind("<Button-3>", self._copy_output_field)

        # 发布到视频号（底部卡片内）：半自动/全自动 + 重新登录
        publish_row = ttk.Frame(output_field_card, style="Card.TFrame")
        publish_row.grid(row=2, column=0, sticky="ew", pady=(10, 0))
        ttk.Label(publish_row, text="发布到视频号:", style="Body.TLabel").grid(row=0, column=0, sticky="w", padx=(0, 8))
        self.sph_mode_combo = ttk.Combobox(publish_row, state="readonly", width=22,
                                           values=["半自动（推荐，手动点发表）", "全自动（封号风险）"])
        self.sph_mode_combo.grid(row=0, column=1, sticky="w", padx=(0, 10))
        self.sph_mode_combo.bind("<<ComboboxSelected>>", lambda _e: self._set_sph_mode_combo())
        self.sph_button = ttk.Button(publish_row, text="发布到视频号", style="Secondary.TButton",
                                     command=self.publish_to_sph)
        self.sph_button.grid(row=0, column=2, sticky="w", padx=(0, 10))
        self.sph_login_button = ttk.Button(publish_row, text="重新登录视频号", style="Secondary.TButton",
                                           command=self.login_sph)
        self.sph_login_button.grid(row=0, column=3, sticky="w")
        ttk.Label(publish_row, text="半自动：登录/上传/填文案后停在「发表」前由你手动点击，风险最低；全自动无人值守但有封号风险。",
                  style="Hint.TLabel").grid(row=1, column=0, columnspan=4, sticky="w", pady=(4, 0))

    def _build_left_column(self, parent: ttk.Frame):
        # 第 1 步
        mode_box = ttk.LabelFrame(parent, text="第 1 步：选择处理方式", style="Section.TLabelframe", padding=14)
        mode_box.grid(row=0, column=0, sticky="ew", pady=(0, 10))
        mode_box.grid_columnconfigure(0, weight=1)
        for idx, (mode_name, meta) in enumerate(MODE_CONFIG.items()):
            holder = ttk.Frame(mode_box, style="Card.TFrame")
            holder.grid(row=idx, column=0, sticky="ew", pady=4)
            holder.grid_columnconfigure(0, weight=1)
            ttk.Radiobutton(holder, text=mode_name, value=mode_name, variable=self.mode_var).grid(row=0, column=0, sticky="w")
            ttk.Label(holder, text=meta["guide"][0], style="Hint.TLabel", wraplength=620, justify="left").grid(row=1, column=0, sticky="w", padx=(24, 0))

        # 第 2 步
        input_box = ttk.LabelFrame(parent, text="第 2 步：选择输入来源及扩展配置", style="Section.TLabelframe", padding=14)
        input_box.grid(row=2, column=0, sticky="ew", pady=(0, 10))
        input_box.grid_columnconfigure(1, weight=1)
        
        ttk.Label(input_box, textvariable=self.input_label_var, style="Body.TLabel").grid(row=0, column=0, sticky="w", pady=(0, 6))
        self.source_entry = ttk.Entry(input_box, textvariable=self.video_var)
        self.source_entry.grid(row=0, column=1, sticky="ew", padx=8, pady=(0, 6))
        self.browse_source_button = ttk.Button(input_box, text="选择视频", style="Secondary.TButton", command=self.browse_source)
        self.browse_source_button.grid(row=0, column=2, sticky="ew", pady=(0, 6))

        ttk.Label(input_box, text="封面图片（可选）", style="Body.TLabel").grid(row=1, column=0, sticky="w", pady=(0, 6))
        ttk.Entry(input_box, textvariable=self.image_var).grid(row=1, column=1, sticky="ew", padx=8, pady=(0, 6))
        ttk.Button(input_box, text="选择图片", style="Secondary.TButton", command=self.browse_image).grid(row=1, column=2, sticky="ew", pady=(0, 6))

        ttk.Label(input_box, text="同步歌词 LRC/SRT/VTT/纯歌词（可选）", style="Body.TLabel").grid(row=2, column=0, sticky="w", pady=(0, 6))
        ttk.Entry(input_box, textvariable=self.lrc_var).grid(row=2, column=1, sticky="ew", padx=8, pady=(0, 6))
        ttk.Button(input_box, text="选择歌词", style="Secondary.TButton", command=self.browse_lrc).grid(row=2, column=2, sticky="ew", pady=(0, 6))
        ttk.Checkbutton(
            input_box,
            text="消除歌词时间重叠（SRT/VTT 相邻行太近时自动截断，防止两行歌词同时高亮）",
            variable=self.fix_overlap_var,
            style="Body.TCheckbutton",
        ).grid(row=3, column=0, columnspan=3, sticky="w", pady=(0, 2))

        # 双语字幕（主英附中）：中文来源三选一（关闭 / 中文翻译文件 / API 自动翻译）
        bilingual_frame = ttk.Frame(input_box)
        bilingual_frame.grid(row=4, column=0, columnspan=3, sticky="w", pady=(0, 2))
        ttk.Label(bilingual_frame, text="双语字幕（当前行英文下附中文译文）：", style="Body.TLabel").pack(side="left")
        ttk.Radiobutton(bilingual_frame, text="关闭", variable=self.zh_mode_var, value="off",
                        command=self._toggle_bilingual_ui, style="Body.TRadiobutton").pack(side="left", padx=(6, 0))
        ttk.Radiobutton(bilingual_frame, text="中文翻译文件", variable=self.zh_mode_var, value="file",
                        command=self._toggle_bilingual_ui, style="Body.TRadiobutton").pack(side="left", padx=(6, 0))
        ttk.Radiobutton(bilingual_frame, text="API 自动翻译", variable=self.zh_mode_var, value="api",
                        command=self._toggle_bilingual_ui, style="Body.TRadiobutton").pack(side="left", padx=(6, 0))

        # 中文来源：中文翻译文件行
        self.zh_lrc_label = ttk.Label(input_box, text="中文翻译文件（与英文逐行对应）", style="Body.TLabel")
        self.zh_lrc_entry = ttk.Entry(input_box, textvariable=self.zh_lrc_var)
        self.zh_lrc_button = ttk.Button(input_box, text="选择中文", style="Secondary.TButton", command=self.browse_zh_lrc)
        self.zh_lrc_label.grid(row=5, column=0, sticky="w", pady=(0, 6))
        self.zh_lrc_entry.grid(row=5, column=1, sticky="ew", padx=8, pady=(0, 6))
        self.zh_lrc_button.grid(row=5, column=2, sticky="ew", pady=(0, 6))

        # 中文来源：API 自动翻译行（接口下拉 + API Key）
        self.api_row = ttk.Frame(input_box)
        self.api_row.grid(row=6, column=0, columnspan=3, sticky="ew", pady=(0, 6))
        ttk.Label(self.api_row, text="翻译接口", style="Body.TLabel").pack(side="left")
        self.zh_provider_combo = ttk.Combobox(self.api_row, textvariable=self.zh_provider_var,
                                              state="readonly", width=10,
                                              values=self.zh_provider_labels)
        self.zh_provider_combo.pack(side="left", padx=(4, 12))
        ttk.Label(self.api_row, text="API Key", style="Body.TLabel").pack(side="left")
        self.zh_api_key_entry = ttk.Entry(self.api_row, textvariable=self.zh_api_key_var, show="*")
        self.zh_api_key_entry.pack(side="left", fill="x", expand=True, padx=(4, 12))
        ttk.Label(self.api_row, text="仅存本机，不随视频输出", style="Body.TLabel").pack(side="left")
        ttk.Button(self.api_row, text="清缓存", style="Secondary.TButton",
                   command=self.clear_translation_cache).pack(side="left", padx=(8, 0))
        self.zh_provider_var.set(self.zh_provider_labels[0] if self.zh_provider_labels else "DeepSeek")
        self._toggle_bilingual_ui()  # 首次启动默认“关闭”：隐藏两种中文来源行

        # 第 3 步（统一处理范围：导出音频/卡片视频/字幕共用）
        clip_box = ttk.LabelFrame(parent, text="第 3 步：设置处理范围（音频/视频/字幕共用，自动对齐）", style="Section.TLabelframe", padding=14)
        clip_box.grid(row=3, column=0, sticky="ew", pady=(0, 10))
        clip_box.grid_columnconfigure(1, weight=1)

        # 统一处理范围（以视频为准，同时作用于导出音频与卡片视频）
        ttk.Label(clip_box, text="起始时间", style="Body.TLabel").grid(row=0, column=0, sticky="w", pady=4)
        ttk.Entry(clip_box, textvariable=self.video_start_var, width=15).grid(row=0, column=1, sticky="w", padx=8, pady=4)
        ttk.Label(clip_box, text="截止时间", style="Body.TLabel").grid(row=0, column=2, sticky="w", padx=(10, 0), pady=4)
        ttk.Entry(clip_box, textvariable=self.video_end_var, width=15).grid(row=0, column=3, sticky="w", padx=8, pady=4)
        ttk.Button(clip_box, text="重置", style="Secondary.TButton", command=self.reset_video_clip).grid(row=0, column=4, sticky="e", pady=4)
        ttk.Label(clip_box, text="留空或填 00:00:00 ~ End 表示整首；字幕与音频随该范围整体平移对齐。",
                  style="Hint.TLabel").grid(row=1, column=0, columnspan=5, sticky="w", pady=(2, 0))

        # 第 4 步
        output_box = ttk.LabelFrame(parent, text="第 4 步：设置输出位置", style="Section.TLabelframe", padding=14)
        output_box.grid(row=4, column=0, sticky="ew", pady=(0, 10))
        output_box.grid_columnconfigure(1, weight=1)

        self.audio_row = ttk.Frame(output_box, style="Card.TFrame")
        self.audio_row.grid(row=0, column=0, columnspan=3, sticky="ew")
        self.audio_row.grid_columnconfigure(1, weight=1)
        ttk.Label(self.audio_row, text="导出音频", style="Body.TLabel").grid(row=0, column=0, sticky="w")
        ttk.Entry(self.audio_row, textvariable=self.audio_var).grid(row=0, column=1, sticky="ew", padx=8)
        ttk.Button(self.audio_row, text="设置音频路径", style="Secondary.TButton", command=self.browse_audio_output).grid(row=0, column=2, sticky="ew")

        self.video_row = ttk.Frame(output_box, style="Card.TFrame")
        self.video_row.grid(row=1, column=0, columnspan=3, sticky="ew", pady=(8, 0))
        self.video_row.grid_columnconfigure(1, weight=1)
        ttk.Label(self.video_row, text="输出视频", style="Body.TLabel").grid(row=0, column=0, sticky="w")
        ttk.Entry(self.video_row, textvariable=self.video_out_var).grid(row=0, column=1, sticky="ew", padx=8)
        ttk.Button(self.video_row, text="设置视频路径", style="Secondary.TButton", command=self.browse_video_output).grid(row=0, column=2, sticky="ew")

        self.batch_row = ttk.Frame(output_box, style="Card.TFrame")
        self.batch_row.grid(row=2, column=0, columnspan=3, sticky="ew", pady=(8, 0))
        self.batch_row.grid_columnconfigure(1, weight=1)
        ttk.Label(self.batch_row, text="批量输出目录", style="Body.TLabel").grid(row=0, column=0, sticky="w")
        ttk.Entry(self.batch_row, textvariable=self.batch_output_var).grid(row=0, column=1, sticky="ew", padx=8)
        ttk.Button(self.batch_row, text="设置输出目录", style="Secondary.TButton", command=self.browse_batch_output).grid(row=0, column=2, sticky="ew")

        ttk.Label(
            output_box,
            text="本地单文件和 R2 模式会自动推荐输出路径；批量模式会按文件名自动生成。",
            style="Hint.TLabel",
            wraplength=640,
            justify="left",
        ).grid(row=3, column=0, columnspan=3, sticky="w", pady=(12, 0))

    def _build_right_column(self, parent: ttk.Frame):
        log_card = ttk.Frame(parent, style="Card.TFrame", padding=16)
        log_card.grid(row=0, column=0, sticky="nsew")
        log_card.grid_columnconfigure(0, weight=1)
        log_card.grid_rowconfigure(1, weight=1)
        ttk.Label(log_card, text="实时日志", style="CardTitle.TLabel").grid(row=0, column=0, sticky="w")
        self.log_text = ScrolledText(log_card, height=20, wrap="word", font=("Consolas", 10), bg="#16120e", fg="#e8dfcc", insertbackground="#d8ad55")
        self.log_text.grid(row=1, column=0, sticky="nsew", pady=(10, 0))
        self.log_text.configure(state="disabled")

    def _update_left_scrollregion(self, _event=None):
        self.left_canvas.configure(scrollregion=self.left_canvas.bbox("all"))

    def _resize_left_canvas_window(self, event):
        self.left_canvas.itemconfigure(self.left_canvas_window, width=event.width)

    def _bind_events(self):
        self.mode_var.trace_add("write", lambda *_: self._on_mode_change())
        self.video_var.trace_add("write", lambda *_: self.update_output_field())
        self.root.bind_all("<MouseWheel>", self._on_mousewheel)

    def _on_mode_change(self):
        self.update_mode_ui()
        self.update_output_field()

    def update_output_field(self):
        mode = self.mode_var.get()
        source = self.video_var.get().strip()
        blocks = []

        if mode == MODE_LOCAL_FOLDER and source and os.path.isdir(source):
            for fn in sorted(os.listdir(source)):
                fp = os.path.join(source, fn)
                if os.path.isfile(fp) and fn.lower().endswith(VIDEO_EXTENSIONS):
                    blocks.append(build_output_field(fp))
        elif source:
            if mode == MODE_R2_URL:
                stem = os.path.splitext(os.path.basename(urlparse(source).path))[0]
                stem = unquote(stem).strip()
                if stem:
                    blocks.append(_format_output_field_from_stem(stem))
            elif os.path.isfile(source):
                blocks.append(build_output_field(source))

        self.output_field_var.set(" ".join(blocks))

    def _copy_output_field(self, _event=None):
        text = self.output_field_var.get().strip()
        if not text:
            return
        self.root.clipboard_clear()
        self.root.clipboard_append(text)
        self.set_status("输出字段已复制到剪贴板。")

    # —— 发布到视频号（Playwright 自动化）——

    def _set_sph_mode_combo(self):
        """下拉选择同步到 sph_mode_var：index 0=半自动 / 1=全自动。"""
        self.sph_mode_var.set("semi" if self.sph_mode_combo.current() == 0 else "auto")

    def _sph_mode_ui(self):
        """按 sph_mode_var 复位下拉（load_settings 后调用）。"""
        idx = 0 if self.sph_mode_var.get() == "semi" else 1
        self.sph_mode_combo.current(idx)

    def _set_sph_busy(self, busy: bool):
        state = "disabled" if busy else "normal"
        self.sph_button.configure(state=state)
        self.sph_login_button.configure(state=state)

    def publish_to_sph(self):
        """发布到视频号：手动选视频 → 用输出字段作文案 → 后台线程执行。"""
        output_field = self.output_field_var.get().strip()
        if not output_field:
            messagebox.showerror("错误", "输出字段为空，请先选择输入视频生成输出字段。")
            return
        video_path = filedialog.askopenfilename(
            parent=self.root, title="选择要发布到视频号的视频", filetypes=VIDEO_FILE_TYPES)
        if not video_path:
            return
        if not os.path.isfile(video_path):
            messagebox.showerror("错误", "所选视频文件不存在。")
            return

        self.save_settings()  # 持久化 sph_mode + sph_cookie
        fields = sph_publish.build_publish_fields(output_field)
        mode = self.sph_mode_var.get()

        self.set_busy(True, f"正在发布到视频号（{'全自动' if mode == 'auto' else '半自动'}）…")
        self._set_sph_busy(True)
        self.append_log(f"=== 发布到视频号：{os.path.basename(video_path)} ===")
        self.append_log(f"标题：{fields.title}")
        self.append_log(f"文案：{fields.description}")

        threading.Thread(target=self._sph_publish_worker,
                         args=(video_path, fields, mode), daemon=True).start()

    def _sph_on_ready(self):
        """semi 模式填完表单瞬间（worker 线程内）回调：跳回 GUI 线程弹提示并恢复按钮。"""
        self.root.after(0, lambda: (
            self._set_sph_busy(False),
            self.set_busy(False, "等待你在浏览器中点击「发表」…"),
            messagebox.showinfo("发布到视频号",
                                "已完成登录/上传/填文案。\n请在弹出的浏览器窗口中手动点击「发表」完成发布，"
                                "关闭窗口后流程结束。")))

    def _sph_publish_worker(self, video_path, fields, mode):
        try:
            sph_publish.publish(
                video_path,
                fields.title,
                fields.description,
                fields.tags,
                mode=mode,
                cookie_path=self.sph_cookie_var.get(),
                log_cb=self._worker_log,
                on_ready=self._sph_on_ready if mode == "semi" else None,
            )
            self.root.after(0, self._sph_finish, True, None, mode)
        except sph_publish.CookieExpiredError as exc:
            self.root.after(0, self._sph_finish, False, str(exc), mode)
        except sph_publish.SelectorNotFoundError as exc:
            self.root.after(0, self._sph_finish, False,
                            f"页面结构与脚本预期不符（平台可能已改版）。\n{exc}\n\n"
                            f"建议先用低风险内容验证，或更新 sph_publish.py 中的 SEL_* 选择器。", mode)
        except sph_publish.PlaywrightNotInstalledError as exc:
            self.root.after(0, self._sph_finish, False, str(exc), mode)
        except Exception as exc:
            self.root.after(0, self._sph_finish, False, f"发布异常：{exc}", mode)

    def login_sph(self):
        """有头扫码重新登录视频号（Cookie 约 7-30 天失效，需定期刷新）。"""
        self.set_busy(True, "请在弹出的浏览器中扫码登录视频号…")
        self._set_sph_busy(True)
        self.append_log("=== 重新登录视频号：请扫码 ===")

        def worker():
            try:
                sph_publish.get_cookie(self.sph_cookie_var.get(), log_cb=self._worker_log)
                self.root.after(0, self._sph_finish, True, None, "semi")
            except sph_publish.PlaywrightNotInstalledError as exc:
                self.root.after(0, self._sph_finish, False, str(exc), "semi")
            except Exception as exc:
                self.root.after(0, self._sph_finish, False, f"登录失败：{exc}", "semi")

        threading.Thread(target=worker, daemon=True).start()

    def _sph_finish(self, ok: bool, err, mode):
        self._set_sph_busy(False)
        self.set_busy(False, "就绪" if ok else "发布/登录失败")
        if ok:
            if mode == "auto":
                messagebox.showinfo("发布到视频号", "已自动点击「发表」，发布完成。")
            # semi 模式成功时不重复弹窗（提示已在 _sph_on_ready 弹出）
        else:
            messagebox.showerror("发布到视频号失败", err)

    def _on_theme_selected(self):
        label = self.theme_combo.get()
        for key, lbl in card_render.THEME_LABELS.items():
            if lbl == label:
                self.card_theme_var.set(key)
                break

    def _on_lyric_style_selected(self):
        label = self.lyric_style_combo.get()
        for key, lbl in self.lyric_style_labels.items():
            if lbl == label:
                self.lyric_style_var.set(key)
                break

    def _on_quality_selected(self):
        self.quality_var.set(self.quality_combo.get())

    def _on_watermark_position_selected(self):
        label = self.watermark_position_combo.get()
        key = card_render.WATERMARK_POSITION_LABELS.get(label, "bottom_right")
        self.watermark_position_var.set(key)

    def _verify_fingerprint(self):
        path = filedialog.askopenfilename(title="选择要验证的视频", filetypes=VIDEO_FILE_TYPES)
        if not path:
            return
        self.set_busy(True, "正在计算并比对内容指纹...")

        def worker():
            try:
                import fingerprint
                result = fingerprint.verify(path)
                self.root.after(0, lambda: self._finish_verify(result))
            except Exception as exc:
                self.root.after(0, lambda: self._finish_verify(
                    {"matched": False, "reason": f"验证异常：{exc}"}))

        threading.Thread(target=worker, daemon=True).start()

    def _finish_verify(self, result):
        self.set_busy(False)
        if not result.get("matched"):
            reason = result.get("reason", "未匹配到登记条目。")
            self._worker_log(f"[指纹验证] 未命中 —— {reason}")
            for r in (result.get("results") or [])[:5]:
                self._worker_log(
                    f"  · ID:{r.get('wm_id') or '?'} 音频相似 {r['audio_score']:.2f} / 视频相似 {r['video_score']:.2f}")
            messagebox.showinfo("指纹验证结果", reason)
            return
        best = result["best"]
        self._worker_log(f"[指纹验证] ✅ 命中登记条目 ID:{best.get('wm_id')}")
        self._worker_log(f"  源视频: {best.get('source')}  时长约 {best.get('duration')}s")
        self._worker_log(
            f"  音频相似度 {best['audio_score']:.3f} / 视频相似度 {best['video_score']:.3f}")
        messagebox.showinfo(
            "指纹验证结果",
            "✅ 匹配到你的原创视频\n\n"
            f"指纹 ID: {best.get('wm_id')}\n"
            f"源视频: {best.get('source')}\n"
            f"音频相似度: {best['audio_score']:.1%}\n"
            f"视频相似度: {best['video_score']:.1%}")

    def _on_mousewheel(self, event):
        widget = self.root.winfo_containing(event.x_root, event.y_root)
        if widget and self._widget_belongs_to_left_panel(widget):
            self.left_canvas.yview_scroll(int(-1 * (event.delta / 120)), "units")

    def _widget_belongs_to_left_panel(self, widget):
        current = widget
        while current is not None:
            if current == self.left_canvas:
                return True
            current = getattr(current, "master", None)
        return False

    def reset_video_clip(self):
        self.video_start_var.set("00:00:00")
        self.video_end_var.set("End")

    def browse_source(self):
        mode = self.mode_var.get()
        if mode == MODE_LOCAL_FILE:
            path = filedialog.askopenfilename(parent=self.root, filetypes=VIDEO_FILE_TYPES)
            if path:
                self.video_var.set(path)
                self.smart_fill_outputs(force=True, announce=True)
                self.set_status(f"已选择视频：{os.path.basename(path)}")
                self.append_log(f"已选择视频：{path}")
        elif mode == MODE_LOCAL_FOLDER:
            path = filedialog.askdirectory(parent=self.root)
            if path:
                self.video_var.set(path)
                if not self.batch_output_var.get().strip():
                    self.batch_output_var.set(path)
                self.set_status(f"已选择文件夹：{path}")
                self.append_log(f"已选择批量文件夹：{path}")

    def browse_image(self):
        path = filedialog.askopenfilename(parent=self.root, filetypes=[("图片文件", "*.jpg *.jpeg *.png *.webp"), ("所有文件", "*.*")])
        if path:
            self.image_var.set(path)
            self.set_status(f"已选择封面：{os.path.basename(path)}")
            self.append_log(f"已选择封面：{path}")

    def browse_lrc(self):
        path = filedialog.askopenfilename(parent=self.root, filetypes=[
            ("歌词/字幕文件", "*.lrc *.srt *.vtt *.plain *.txt"),
            ("LRC 歌词", "*.lrc"), ("SRT 字幕", "*.srt"), ("VTT 字幕", "*.vtt"),
            ("纯歌词（自动匹配同名 SRT 时间轴）", "*.plain"), ("文本歌词", "*.txt"),
            ("所有文件", "*.*")])
        if path:
            self.lrc_var.set(path)
            self.set_status(f"已装载歌词：{os.path.basename(path)}")
            self.append_log(f"已装载同步歌词文件：{path}")

    def _toggle_bilingual_ui(self):
        mode = self.zh_mode_var.get()
        if mode == "file":
            for widget in (self.zh_lrc_label, self.zh_lrc_entry, self.zh_lrc_button):
                widget.grid()
            self.api_row.grid_remove()
        elif mode == "api":
            self.api_row.grid()
            for widget in (self.zh_lrc_label, self.zh_lrc_entry, self.zh_lrc_button):
                widget.grid_remove()
        else:
            self.api_row.grid_remove()
            for widget in (self.zh_lrc_label, self.zh_lrc_entry, self.zh_lrc_button):
                widget.grid_remove()

    def _current_provider_key(self):
        label = self.zh_provider_var.get()
        for key, cfg in lyric_translate.PROVIDERS.items():
            if cfg["label"] == label:
                return key
        return "deepseek"

    def _zh_lyrics_inputs(self):
        """根据双语中文来源返回 (中文翻译文件路径, 翻译 API 配置)，未启用则 ('', None)。"""
        mode = self.zh_mode_var.get()
        if mode == "file":
            return self.zh_lrc_var.get().strip(), None
        if mode == "api":
            api_key = self.zh_api_key_var.get().strip()
            if api_key:
                return "", {"provider": self._current_provider_key(), "api_key": api_key}
            return "", None
        return "", None

    def browse_zh_lrc(self):
        path = filedialog.askopenfilename(parent=self.root, filetypes=[
            ("中文翻译文件", "*.plain *.txt *.lrc *.srt *.vtt"),
            ("纯文本翻译", "*.plain"), ("文本翻译", "*.txt"),
            ("LRC 翻译", "*.lrc"), ("SRT 翻译", "*.srt"), ("VTT 翻译", "*.vtt"),
            ("所有文件", "*.*")])
        if path:
            self.zh_lrc_var.set(path)
            self.set_status(f"已装载中文翻译：{os.path.basename(path)}")
            self.append_log(f"已装载中文翻译文件：{path}")

    def clear_translation_cache(self):
        lyric_translate.clear_translation_cache()
        self.set_status("已清除歌词翻译缓存，下次处理将重新调用 API 翻译。")
        self.append_log("已清除歌词翻译缓存。")

    def browse_audio_output(self):
        path = filedialog.asksaveasfilename(parent=self.root, defaultextension=".mp3", filetypes=[("MP3", "*.mp3"), ("M4A", "*.m4a"), ("所有文件", "*.*")])
        if path:
            self.audio_var.set(path)
            self.set_status("音频路径已设置。")
            self.append_log(f"音频输出位置：{path}")

    def browse_video_output(self):
        path = filedialog.asksaveasfilename(parent=self.root, defaultextension=".mp4", filetypes=[("MP4", "*.mp4"), ("所有文件", "*.*")])
        if path:
            self.video_out_var.set(path)
            self.set_status("视频路径已设置。")
            self.append_log(f"视频输出位置：{path}")

    def browse_batch_output(self):
        path = filedialog.askdirectory(parent=self.root)
        if path:
            self.batch_output_var.set(path)
            self.set_status("批量输出目录已设置。")
            self.append_log(f"批量输出目录：{path}")

    def default_r2_output_base(self) -> str:
        return os.path.join(APP_DIR, "outputs")

    def smart_fill_outputs(self, force: bool = False, announce: bool = False):
        mode = self.mode_var.get()
        source = self.video_var.get().strip()

        if mode == MODE_LOCAL_FOLDER:
            if source and os.path.isdir(source) and not self.batch_output_var.get().strip():
                self.batch_output_var.set(source)
            return

        if not source:
            return

        if mode == MODE_LOCAL_FILE and os.path.isfile(source):
            base_dir = os.path.dirname(source)
            stem = smart_parse_name(source)
        elif mode == MODE_R2_URL:
            parsed = urlparse(source)
            stem = sanitize_filename(os.path.splitext(os.path.basename(parsed.path))[0] or "r2_video")
            base_dir = self.default_r2_output_base()
        else:
            return

        suggested_audio = os.path.join(base_dir, f"{stem}.mp3")
        suggested_video = os.path.join(base_dir, f"{stem}.mp4")
        changed = False

        if force or not self.audio_var.get().strip():
            self.audio_var.set(suggested_audio)
            changed = True
        if force or not self.video_out_var.get().strip():
            self.video_out_var.set(suggested_video)
            changed = True

        if announce and changed:
            self.set_status("已自动生成建议输出路径。")
            self.append_log(f"智能生成音频输出：{suggested_audio}")
            self.append_log(f"智能生成视频输出：{suggested_video}")

    def update_mode_ui(self):
        mode = self.mode_var.get()
        meta = MODE_CONFIG[mode]
        self.input_label_var.set(meta["input_label"])

        if meta["allow_browse"]:
            self.browse_source_button.configure(text=meta["browse_text"], state="normal")
        else:
            self.browse_source_button.configure(text=meta["browse_text"], state="disabled")

        if mode == MODE_LOCAL_FOLDER:
            self.audio_row.grid_remove()
            self.video_row.grid_remove()
            self.batch_row.grid()
            self.set_status("批量模式：会自动按文件名生成输出。")
        else:
            self.audio_row.grid()
            self.video_row.grid()
            self.batch_row.grid_remove()
            if mode == MODE_R2_URL:
                self.set_status("R2 模式：可以点击“智能生成输出路径”。")
            else:
                self.set_status("本地单文件模式：选择视频后将智能推荐输出路径。")
                self.smart_fill_outputs(force=False)

    def clear_log(self):
        self.log_text.configure(state="normal")
        self.log_text.delete("1.0", tk.END)
        self.log_text.configure(state="disabled")

    def append_log(self, message: str):
        self.log_text.configure(state="normal")
        self.log_text.insert(tk.END, message.rstrip() + "\n")
        self.log_text.see(tk.END)
        self.log_text.configure(state="disabled")

    def set_status(self, message: str):
        self.status_var.set(message)

    def set_busy(self, busy: bool, label: str = ""):
        self.run_button.configure(state="disabled" if busy else "normal")
        if label:
            self.set_status(label)

    def build_batch_jobs(self, folder_path: str, output_dir: str, include_video: bool):
        jobs = []
        used_names = set()
        for file_name in sorted(os.listdir(folder_path)):
            full_path = os.path.join(folder_path, file_name)
            if not os.path.isfile(full_path) or not file_name.lower().endswith(VIDEO_EXTENSIONS):
                continue

            stem = smart_parse_name(full_path)
            candidate = stem
            index = 2
            while candidate.lower() in used_names:
                candidate = f"{stem} ({index})"
                index += 1
            used_names.add(candidate.lower())

            jobs.append(
                BatchJob(
                    source_path=full_path,
                    audio_out=os.path.join(output_dir, f"{candidate}.mp3"),
                    video_out=os.path.join(output_dir, f"{candidate}.mp4") if include_video else "",
                )
            )

        return jobs

    def validate_before_run(self):
        mode = self.mode_var.get()
        source = self.video_var.get().strip()
        image = self.image_var.get().strip()
        lrc = self.lrc_var.get().strip()

        if not source:
            messagebox.showerror("错误", "请先选择视频文件、视频文件夹，或者填写 R2 链接。")
            return False

        if image and not os.path.isfile(image):
            messagebox.showerror("错误", "背景图片/封面不存在，请重新选择。")
            return False

        if lrc and not os.path.isfile(lrc):
            messagebox.showerror("错误", "指定的歌词/字幕文件不存在，请重新选择。")
            return False

        if mode == MODE_LOCAL_FILE and not os.path.isfile(source):
            messagebox.showerror("错误", "请选择有效的本地视频文件。")
            return False

        if mode == MODE_LOCAL_FOLDER and not os.path.isdir(source):
            messagebox.showerror("错误", "请选择有效的视频文件夹。")
            return False

        if mode != MODE_LOCAL_FOLDER:
            self.smart_fill_outputs(force=False)
            if not self.audio_var.get().strip():
                messagebox.showerror("错误", "请设置导出音频路径。")
                return False
            if image and not self.video_out_var.get().strip():
                messagebox.showerror("错误", "已选择背景图，请设置输出视频路径。")
                return False

        return True

    def run_action(self):
        if not self.validate_before_run():
            return

        self.save_settings()
        self.clear_log()
        mode = self.mode_var.get()
        self.append_log(f"=== 开始任务：{mode} ===")
        self.append_log("任务已启动，正在准备生成文件...")

        if mode == MODE_LOCAL_FOLDER:
            folder_path = self.video_var.get().strip()
            output_dir = self.batch_output_var.get().strip() or folder_path
            jobs = self.build_batch_jobs(folder_path, output_dir, include_video=bool(self.image_var.get().strip()))
            if not jobs:
                messagebox.showwarning("提示", "当前文件夹里没有可处理的视频文件。")
                return
            self.set_busy(True, f"批量处理中：共 {len(jobs)} 个任务。")
            threading.Thread(target=self._run_batch, args=(jobs, output_dir), daemon=True).start()
            return

        self.set_busy(True, "处理中，请查看右侧实时日志。")
        threading.Thread(target=self._run_single, daemon=True).start()

    def _worker_log(self, message: str):
        self.root.after(0, lambda msg=message: self.append_log(msg))

    def _run_single(self):
        try:
            _zh_path, _zh_api = self._zh_lyrics_inputs()
            result = mode1_process(
                self.video_var.get().strip(),
                self.image_var.get().strip(),
                self.audio_var.get().strip(),
                self.video_out_var.get().strip(),
                self.video_start_var.get().strip(),
                self.video_end_var.get().strip(),
                MODE_CONFIG[self.mode_var.get()]["source_mode"],
                logger=self._worker_log,
                use_vinyl_mode=self.vinyl_mode_var.get(),
                lrc_path=self.lrc_var.get().strip(),
                video_start_time=self.video_start_var.get().strip(),
                video_end_time=self.video_end_var.get().strip(),
                card_theme=self.card_theme_var.get(),
                fix_lyric_overlap=self.fix_overlap_var.get(),
                zh_lyrics_path=_zh_path,
                zh_api_config=_zh_api,
                orientation="portrait" if self.portrait_var.get() else "landscape",
                lyric_style=self.lyric_style_var.get(),
                quality_preset=self.quality_var.get(),
                watermark_enabled=self.watermark_enabled_var.get(),
                watermark_text=self.watermark_text_var.get(),
                watermark_position=self.watermark_position_var.get(),
            )

            output_anchor = result.video_output or result.audio_output or self.audio_var.get().strip()
            last_output_dir = os.path.dirname(os.path.abspath(output_anchor)) if output_anchor else ""
            failures = [] if result.success else [result.message]
            self.root.after(0, lambda: self.finish_run(1 if result.success else 0, 1, last_output_dir, failures))
        except Exception as exc:
            self.root.after(0, lambda: self.finish_run(0, 1, "", [f"运行异常：{exc}"]))

    def _run_batch(self, jobs, output_dir):
        try:
            success_count = 0
            failures = []
            total_count = len(jobs)

            for index, job in enumerate(jobs, start=1):
                self.root.after(0, lambda i=index, t=total_count: self.set_status(f"批量处理中：第 {i}/{t} 个任务。"))
                self._worker_log(f"--- 第 {index}/{total_count} 个任务：{os.path.basename(job.source_path)} ---")
                _zh_path, _zh_api = self._zh_lyrics_inputs()
                result = mode1_process(
                    job.source_path,
                    self.image_var.get().strip(),
                    job.audio_out,
                    job.video_out,
                    self.video_start_var.get().strip(),
                    self.video_end_var.get().strip(),
                    SOURCE_LOCAL,
                    logger=self._worker_log,
                    use_vinyl_mode=self.vinyl_mode_var.get(),
                    lrc_path=self.lrc_var.get().strip(),
                    video_start_time=self.video_start_var.get().strip(),
                    video_end_time=self.video_end_var.get().strip(),
                    card_theme=self.card_theme_var.get(),
                    fix_lyric_overlap=self.fix_overlap_var.get(),
                    zh_lyrics_path=_zh_path,
                    zh_api_config=_zh_api,
                    orientation="portrait" if self.portrait_var.get() else "landscape",
                    lyric_style=self.lyric_style_var.get(),
                    quality_preset=self.quality_var.get(),
                    watermark_enabled=self.watermark_enabled_var.get(),
                    watermark_text=self.watermark_text_var.get(),
                    watermark_position=self.watermark_position_var.get(),
                )
                if result.success:
                    success_count += 1
                else:
                    failures.append(f"{os.path.basename(job.source_path)}：{result.message}")

            self.root.after(0, lambda: self.finish_run(success_count, total_count, output_dir, failures))
        except Exception as exc:
            self.root.after(0, lambda: self.finish_run(0, len(jobs), output_dir, [f"批量运行异常：{exc}"]))

    def finish_run(self, success_count: int, total_count: int, last_output_dir: str, failures):
        self.set_busy(False, f"任务结束：成功 {success_count}/{total_count}。")

        if success_count <= 0:
            detail = "\n".join(failures[:8]) if failures else "未成功处理任何任务。"
            messagebox.showwarning("处理失败", detail)
            return

        summary = f"处理完成，成功 {success_count}/{total_count} 个任务。"
        if failures:
            summary += "\n\n失败任务：\n" + "\n".join(failures[:5])
            if len(failures) > 5:
                summary += f"\n... 另有 {len(failures) - 5} 个失败任务。"

        if self.auto_open_var.get() and last_output_dir and os.path.exists(last_output_dir):
            if messagebox.askyesno("完成", f"{summary}\n\n是否打开输出目录？"):
                os.startfile(last_output_dir)
        else:
            messagebox.showinfo("完成", summary)

    def load_settings(self):
        if not os.path.exists(SETTINGS_FILE):
            return
        try:
            with open(SETTINGS_FILE, "r", encoding="utf-8") as handle:
                data = json.load(handle)
        except (OSError, json.JSONDecodeError):
            return

        self.mode_var.set(data.get("mode", MODE_LOCAL_FILE))
        self.video_var.set(data.get("video", ""))
        self.image_var.set(data.get("image", ""))
        self.lrc_var.set(data.get("lrc", ""))                 
        self.vinyl_mode_var.set(data.get("vinyl_mode", True)) 
        self.audio_var.set(data.get("audio", ""))
        self.video_out_var.set(data.get("video_out", ""))
        self.batch_output_var.set(data.get("batch_output", ""))
        # 统一处理范围：兼容旧版仅存 audio start/end 的设置，迁移到 video 字段
        _v_start = data.get("video_start")
        _v_end = data.get("video_end")
        if not _v_start:
            _v_start = data.get("start", "00:00:00")
        if not _v_end:
            _v_end = data.get("end", "End")
        self.video_start_var.set(_v_start)
        self.video_end_var.set(_v_end)
        self.auto_open_var.set(data.get("auto_open", True))
        self.card_theme_var.set(data.get("card_theme", DEFAULT_CARD_THEME))
        self.theme_combo.set(card_render.THEME_LABELS.get(self.card_theme_var.get(), "极简高级"))
        self.fix_overlap_var.set(data.get("fix_overlap", True))
        self.portrait_var.set(data.get("portrait", False))
        _style_key = data.get("lyric_style", "spotify")
        if _style_key not in self.lyric_style_labels:
            _style_key = "spotify"
        self.lyric_style_var.set(_style_key)
        self.lyric_style_combo.set(self.lyric_style_labels[_style_key])
        _quality = data.get("quality", card_render.DEFAULT_VIDEO_PRESET)
        if _quality not in card_render.VIDEO_PRESETS:
            _quality = card_render.DEFAULT_VIDEO_PRESET
        self.quality_var.set(_quality)
        self.quality_combo.set(_quality)
        self.watermark_enabled_var.set(data.get("watermark_enabled", False))
        self.watermark_text_var.set(data.get("watermark_text", ""))
        _wm_pos = data.get("watermark_position", "bottom_right")
        if _wm_pos not in card_render.WATERMARK_POSITIONS:
            _wm_pos = "bottom_right"
        self.watermark_position_var.set(_wm_pos)
        self.watermark_position_combo.set(card_render.WATERMARK_POSITIONS[_wm_pos])
        # 双语中文来源：优先读 zh_mode，兼容旧版 bilingual 布尔键
        if "zh_mode" in data:
            zh_mode = data.get("zh_mode", "off")
        else:
            zh_mode = "file" if data.get("bilingual", False) else "off"
        self.zh_mode_var.set(zh_mode)
        self.zh_lrc_var.set(data.get("zh_lrc", ""))
        _provider = data.get("zh_provider", "deepseek")
        _label = lyric_translate.PROVIDERS.get(_provider, {}).get("label", "DeepSeek")
        self.zh_provider_var.set(_label)
        self.zh_api_key_var.set(data.get("zh_api_key", ""))
        # 发布到视频号：模式 + 登录态文件路径
        _sph_mode = data.get("sph_mode", "semi")
        if _sph_mode not in ("semi", "auto"):
            _sph_mode = "semi"
        self.sph_mode_var.set(_sph_mode)
        self.sph_cookie_var.set(data.get("sph_cookie", sph_publish.DEFAULT_COOKIE_FILE))
        self._sph_mode_ui()
        self._toggle_bilingual_ui()

    def save_settings(self):
        data = {
            "mode": self.mode_var.get(),
            "video": self.video_var.get().strip(),
            "image": self.image_var.get().strip(),
            "lrc": self.lrc_var.get().strip(),                 
            "vinyl_mode": self.vinyl_mode_var.get(),           
            "audio": self.audio_var.get().strip(),
            "video_out": self.video_out_var.get().strip(),
            "batch_output": self.batch_output_var.get().strip(),
            "video_start": self.video_start_var.get().strip(),
            "video_end": self.video_end_var.get().strip(),
            "auto_open": self.auto_open_var.get(),
            "card_theme": self.card_theme_var.get(),
            "fix_overlap": self.fix_overlap_var.get(),
            "portrait": self.portrait_var.get(),
            "lyric_style": self.lyric_style_var.get(),
            "quality": self.quality_var.get(),
            "watermark_enabled": self.watermark_enabled_var.get(),
            "watermark_text": self.watermark_text_var.get().strip(),
            "watermark_position": self.watermark_position_var.get(),
            "zh_mode": self.zh_mode_var.get(),
            "zh_lrc": self.zh_lrc_var.get().strip(),
            "zh_provider": self._current_provider_key(),
            "zh_api_key": self.zh_api_key_var.get().strip(),
            "sph_mode": self.sph_mode_var.get(),
            "sph_cookie": self.sph_cookie_var.get().strip(),
        }
        try:
            with open(SETTINGS_FILE, "w", encoding="utf-8") as handle:
                json.dump(data, handle, ensure_ascii=False, indent=2)
        except OSError:
            pass

    def on_close(self):
        self.save_settings()
        self.root.destroy()

    def report_callback_exception(self, exc, val, tb):
        details = "".join(traceback.format_exception(exc, val, tb))
        try:
            self.append_log("界面异常：\n" + details)
        except Exception:
            pass
        self.set_status("界面操作发生异常，请查看提示。")
        messagebox.showerror("界面异常", f"{val}\n\n详细信息已写入右侧日志。")


def main():
    root = tk.Tk()
    VideoWorkbenchApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()