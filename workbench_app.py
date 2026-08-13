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
from urllib.parse import urlparse

import card_render
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
            "2. 选择封面图片（用于生成视频），可在此指定歌词 LRC 文件。",
            "3. 推荐开启极简卡片歌词模式，并独立配置音/视频裁剪范围！",
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
            "3. 可统一设定音/视频裁剪范围批量进行合成裁剪。",
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
            "3. 支持独立设置音/视频裁剪范围进行二次精准裁剪。",
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


class VideoWorkbenchApp:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title("视频音频处理工作台 (独立裁剪版)")
        self.set_initial_window_size()
        self.root.minsize(1080, 700)
        self.root.configure(bg="#eef3f9")

        self._create_variables()
        self._build_style()
        self._build_ui()
        self._bind_events()
        self.root.report_callback_exception = self.report_callback_exception
        self.load_settings()
        self.update_mode_ui()
        self.update_summary()
        self.set_status("就绪：音视频范围已支持独立拆分控制，体验完美的歌词对齐吧！")
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
        self.vinyl_mode_var = tk.BooleanVar(value=True) 
        self.audio_var = tk.StringVar()
        self.video_out_var = tk.StringVar()
        self.batch_output_var = tk.StringVar()
        
        # 音频裁剪范围
        self.start_var = tk.StringVar(value="00:00:00")
        self.end_var = tk.StringVar(value="End")
        
        # 视频裁剪范围
        self.video_start_var = tk.StringVar(value="00:00:00")
        self.video_end_var = tk.StringVar(value="End")

        self.auto_open_var = tk.BooleanVar(value=True)
        self.card_theme_var = tk.StringVar(value=DEFAULT_CARD_THEME)
        self.status_var = tk.StringVar()
        self.summary_var = tk.StringVar()
        self.guide_var = tk.StringVar()
        self.input_label_var = tk.StringVar(value=MODE_CONFIG[MODE_LOCAL_FILE]["input_label"])

    def _build_style(self):
        style = ttk.Style()
        style.theme_use("clam")
        style.configure("Root.TFrame", background="#eef3f9")
        style.configure("Card.TFrame", background="#ffffff")
        style.configure("Title.TLabel", background="#eef3f9", foreground="#172033", font=("Microsoft YaHei UI", 22, "bold"))
        style.configure("Subtitle.TLabel", background="#eef3f9", foreground="#5f6b7a", font=("Microsoft YaHei UI", 11))
        style.configure("CardTitle.TLabel", background="#ffffff", foreground="#1f2a44", font=("Microsoft YaHei UI", 12, "bold"))
        style.configure("Body.TLabel", background="#ffffff", foreground="#465066", font=("Microsoft YaHei UI", 10))
        style.configure("Hint.TLabel", background="#ffffff", foreground="#6a7284", font=("Microsoft YaHei UI", 9))
        style.configure("HeroTag.TLabel", background="#dde9ff", foreground="#1d4fd7", font=("Microsoft YaHei UI", 9, "bold"), padding=(10, 4))
        style.configure("Primary.TButton", font=("Microsoft YaHei UI", 11, "bold"), padding=(12, 10))
        style.configure("Secondary.TButton", font=("Microsoft YaHei UI", 10), padding=(10, 8))
        style.configure("Section.TLabelframe", background="#ffffff", borderwidth=1)
        style.configure("Section.TLabelframe.Label", background="#ffffff", foreground="#1f2a44", font=("Microsoft YaHei UI", 11, "bold"))

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
        ttk.Label(
            header,
            text="系统已优化：音视频裁剪范围已支持独立面板控制，彻底防止滚动歌词错位！",
            style="Subtitle.TLabel",
        ).grid(row=1, column=0, sticky="w", pady=(4, 10))

        tag_bar = ttk.Frame(header, style="Root.TFrame")
        tag_bar.grid(row=2, column=0, sticky="w")
        for idx, label in enumerate(("极简卡片歌词", "批量合成", "R2 链路支持", "音视频独立裁剪")):
            ttk.Label(tag_bar, text=label, style="HeroTag.TLabel").grid(row=0, column=idx, padx=(0, 8))

        content = ttk.Frame(shell, style="Root.TFrame")
        content.grid(row=1, column=0, sticky="nsew")
        content.grid_columnconfigure(0, weight=3, uniform="content")
        content.grid_columnconfigure(1, weight=2, uniform="content")
        content.grid_rowconfigure(0, weight=1)

        left = ttk.Frame(content, style="Card.TFrame")
        left.grid(row=0, column=0, sticky="nsew", padx=(0, 10))
        left.grid_columnconfigure(0, weight=1)
        left.grid_rowconfigure(0, weight=1)

        self.left_canvas = tk.Canvas(left, bg="#eef3f9", highlightthickness=0, bd=0)
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
        right.grid_rowconfigure(2, weight=1)

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

        ttk.Button(action_bar, text="重新智能生成输出路径", style="Secondary.TButton", command=lambda: self.smart_fill_outputs(force=True, announce=True)).grid(row=0, column=1, sticky="e", padx=(8, 8))
        self.run_button = ttk.Button(action_bar, text="开始生成音频 / 视频", style="Primary.TButton", command=self.run_action)
        self.run_button.grid(row=0, column=2, sticky="e")

        status_bar = ttk.Frame(shell, style="Card.TFrame", padding=(12, 8))
        status_bar.grid(row=3, column=0, sticky="ew", pady=(14, 0))
        status_bar.grid_columnconfigure(0, weight=1)
        ttk.Label(status_bar, textvariable=self.status_var, style="Body.TLabel").grid(row=0, column=0, sticky="w")

    def _build_left_column(self, parent: ttk.Frame):
        intro = ttk.Frame(parent, style="Card.TFrame", padding=16)
        intro.grid(row=0, column=0, sticky="ew", pady=(0, 10))
        intro.grid_columnconfigure(0, weight=1)
        ttk.Label(intro, text="快速开始", style="CardTitle.TLabel").grid(row=0, column=0, sticky="w")
        ttk.Label(
            intro,
            text="现在支持独立设置音频和视频范围。若有同步歌词，视频裁剪范围会自动对成品视频进行二次切片，彻底防错位！",
            style="Body.TLabel",
            wraplength=640,
            justify="left",
        ).grid(row=1, column=0, sticky="w", pady=(6, 0))

        # 第 1 步
        mode_box = ttk.LabelFrame(parent, text="第 1 步：选择处理方式", style="Section.TLabelframe", padding=14)
        mode_box.grid(row=1, column=0, sticky="ew", pady=(0, 10))
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

        ttk.Label(input_box, text="同步歌词 LRC（可选）", style="Body.TLabel").grid(row=2, column=0, sticky="w", pady=(0, 6))
        ttk.Entry(input_box, textvariable=self.lrc_var).grid(row=2, column=1, sticky="ew", padx=8, pady=(0, 6))
        ttk.Button(input_box, text="选择歌词", style="Secondary.TButton", command=self.browse_lrc).grid(row=2, column=2, sticky="ew", pady=(0, 6))

        # 第 3 步（独立设置音视频范围）
        clip_box = ttk.LabelFrame(parent, text="第 3 步：设置处理范围（音频与视频独立控制）", style="Section.TLabelframe", padding=14)
        clip_box.grid(row=3, column=0, sticky="ew", pady=(0, 10))
        clip_box.grid_columnconfigure(1, weight=1)

        # 音频裁剪范围
        ttk.Label(clip_box, text="音频起始", style="Body.TLabel").grid(row=0, column=0, sticky="w", pady=4)
        ttk.Entry(clip_box, textvariable=self.start_var, width=15).grid(row=0, column=1, sticky="w", padx=8, pady=4)
        ttk.Label(clip_box, text="音频截止", style="Body.TLabel").grid(row=0, column=2, sticky="w", padx=(10, 0), pady=4)
        ttk.Entry(clip_box, textvariable=self.end_var, width=15).grid(row=0, column=3, sticky="w", padx=8, pady=4)
        ttk.Button(clip_box, text="重置音频", style="Secondary.TButton", command=self.reset_audio_clip).grid(row=0, column=4, sticky="e", pady=4)

        # 视频裁剪范围
        ttk.Label(clip_box, text="视频起始", style="Body.TLabel").grid(row=1, column=0, sticky="w", pady=4)
        ttk.Entry(clip_box, textvariable=self.video_start_var, width=15).grid(row=1, column=1, sticky="w", padx=8, pady=4)
        ttk.Label(clip_box, text="视频截止", style="Body.TLabel").grid(row=1, column=2, sticky="w", padx=(10, 0), pady=4)
        ttk.Entry(clip_box, textvariable=self.video_end_var, width=15).grid(row=1, column=3, sticky="w", padx=8, pady=4)
        ttk.Button(clip_box, text="重置视频", style="Secondary.TButton", command=self.reset_video_clip).grid(row=1, column=4, sticky="e", pady=4)

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
        summary_card = ttk.Frame(parent, style="Card.TFrame", padding=16)
        summary_card.grid(row=0, column=0, sticky="ew", pady=(0, 10))
        summary_card.grid_columnconfigure(0, weight=1)
        ttk.Label(summary_card, text="当前任务摘要", style="CardTitle.TLabel").grid(row=0, column=0, sticky="w")
        ttk.Label(summary_card, textvariable=self.summary_var, style="Body.TLabel", wraplength=460, justify="left").grid(row=1, column=0, sticky="w", pady=(8, 0))

        guide_card = ttk.Frame(parent, style="Card.TFrame", padding=16)
        guide_card.grid(row=1, column=0, sticky="ew", pady=(0, 10))
        guide_card.grid_columnconfigure(0, weight=1)
        ttk.Label(guide_card, text="操作引导", style="CardTitle.TLabel").grid(row=0, column=0, sticky="w")
        ttk.Label(guide_card, textvariable=self.guide_var, style="Body.TLabel", wraplength=460, justify="left").grid(row=1, column=0, sticky="w", pady=(8, 0))

        log_card = ttk.Frame(parent, style="Card.TFrame", padding=16)
        log_card.grid(row=2, column=0, sticky="nsew")
        log_card.grid_columnconfigure(0, weight=1)
        log_card.grid_rowconfigure(1, weight=1)
        ttk.Label(log_card, text="实时日志", style="CardTitle.TLabel").grid(row=0, column=0, sticky="w")
        self.log_text = ScrolledText(log_card, height=20, wrap="word", font=("Consolas", 10), bg="#0f172a", fg="#e5edf8", insertbackground="#e5edf8")
        self.log_text.grid(row=1, column=0, sticky="nsew", pady=(10, 0))
        self.log_text.configure(state="disabled")

    def _update_left_scrollregion(self, _event=None):
        self.left_canvas.configure(scrollregion=self.left_canvas.bbox("all"))

    def _resize_left_canvas_window(self, event):
        self.left_canvas.itemconfigure(self.left_canvas_window, width=event.width)

    def _bind_events(self):
        self.mode_var.trace_add("write", lambda *_: self._on_mode_change())
        for variable in (self.video_var, self.image_var, self.audio_var, self.video_out_var, self.batch_output_var, self.start_var, self.end_var, self.video_start_var, self.video_end_var, self.vinyl_mode_var):
            variable.trace_add("write", lambda *_: self.update_summary())
        self.root.bind_all("<MouseWheel>", self._on_mousewheel)

    def _on_mode_change(self):
        self.update_mode_ui()
        self.update_summary()

    def _on_theme_selected(self):
        label = self.theme_combo.get()
        for key, lbl in card_render.THEME_LABELS.items():
            if lbl == label:
                self.card_theme_var.set(key)
                break
        self.update_summary()

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

    def reset_audio_clip(self):
        self.start_var.set("00:00:00")
        self.end_var.set("End")

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
        path = filedialog.askopenfilename(parent=self.root, filetypes=[("歌词文件", "*.lrc"), ("所有文件", "*.*")])
        if path:
            self.lrc_var.set(path)
            self.set_status(f"已装载歌词：{os.path.basename(path)}")
            self.append_log(f"已装载同步歌词文件：{path}")

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

    def update_summary(self):
        mode = self.mode_var.get()
        source = self.video_var.get().strip() or "未选择"
        image = self.image_var.get().strip() or "未选择封面图，仅导出音频"
        lrc = self.lrc_var.get().strip() or "未装载歌词"
        clip_audio = f"{self.start_var.get().strip() or '00:00:00'}  至  {self.end_var.get().strip() or 'End'}"
        clip_video = f"{self.video_start_var.get().strip() or '00:00:00'}  至  {self.video_end_var.get().strip() or 'End'}"
        theme_label = card_render.THEME_LABELS.get(self.card_theme_var.get(), "极简高级")
        vinyl_mode = f"开启 ({theme_label})" if self.vinyl_mode_var.get() else "关闭 (静态图片画面)"

        if mode == MODE_LOCAL_FOLDER:
            output_info = self.batch_output_var.get().strip() or "默认输出到源文件夹"
            behavior = f"批量合成音频及视频。视频模式：{vinyl_mode}"
        else:
            output_info = f"音频：{self.audio_var.get().strip() or '未设置'}\n视频：{self.video_out_var.get().strip() or '未设置 / 不生成'}"
            behavior = f"背景图为空则只出音频。视频裁剪：{clip_video} (模式：{vinyl_mode})"

        summary = (
            f"模式：{mode}\n"
            f"输入：{source}\n"
            f"封面图：{image}\n"
            f"同步歌词：{lrc}\n"
            f"卡片模式：{vinyl_mode}\n"
            f"音频提取范围：{clip_audio}\n"
            f"视频裁剪范围：{clip_video}\n"
            f"输出位置：\n{output_info}\n"
            f"处理方式：{behavior}"
        )
        self.summary_var.set(summary)
        self.guide_var.set("\n".join(MODE_CONFIG[mode]["guide"]))

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
            messagebox.showerror("错误", "指定的歌词 LRC 文件不存在，请重新选择。")
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
            result = mode1_process(
                self.video_var.get().strip(),
                self.image_var.get().strip(),
                self.audio_var.get().strip(),
                self.video_out_var.get().strip(),
                self.start_var.get().strip(),
                self.end_var.get().strip(),
                MODE_CONFIG[self.mode_var.get()]["source_mode"],
                logger=self._worker_log,
                use_vinyl_mode=self.vinyl_mode_var.get(),
                lrc_path=self.lrc_var.get().strip(),
                video_start_time=self.video_start_var.get().strip(),
                video_end_time=self.video_end_var.get().strip(),
                card_theme=self.card_theme_var.get(),
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
                result = mode1_process(
                    job.source_path,
                    self.image_var.get().strip(),
                    job.audio_out,
                    job.video_out,
                    self.start_var.get().strip(),
                    self.end_var.get().strip(),
                    SOURCE_LOCAL,
                    logger=self._worker_log,
                    use_vinyl_mode=self.vinyl_mode_var.get(),
                    lrc_path=self.lrc_var.get().strip(),
                    video_start_time=self.video_start_var.get().strip(),
                    video_end_time=self.video_end_var.get().strip(),
                    card_theme=self.card_theme_var.get(),
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
        self.start_var.set(data.get("start", "00:00:00"))
        self.end_var.set(data.get("end", "End"))
        self.video_start_var.set(data.get("video_start", "00:00:00")) 
        self.video_end_var.set(data.get("video_end", "End"))
        self.auto_open_var.set(data.get("auto_open", True))
        self.card_theme_var.set(data.get("card_theme", DEFAULT_CARD_THEME))
        self.theme_combo.set(card_render.THEME_LABELS.get(self.card_theme_var.get(), "极简高级"))

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
            "start": self.start_var.get().strip(),
            "end": self.end_var.get().strip(),
            "video_start": self.video_start_var.get().strip(),
            "video_end": self.video_end_var.get().strip(),
            "auto_open": self.auto_open_var.get(),
            "card_theme": self.card_theme_var.get(),
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