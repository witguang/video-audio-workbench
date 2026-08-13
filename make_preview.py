# -*- coding: utf-8 -*-
"""
卡片模式设计预览工具（开发用）。
用法: python make_preview.py            # 渲染全部三个主题
      python make_preview.py minimal     # 只渲染指定主题
输出: preview/<主题>_<时间点>s.png
"""
import json
import os
import subprocess
import sys

import card_render
from card_render import THEMES, THEME_LABELS

FFMPEG = os.path.abspath("ffmpeg/ffmpeg.exe")
SAMPLE_LRC = os.path.join("preview", "sample.lrc")
SAMPLE_LRC_CONTENT = """[ti:Like Me]
[ar:Alex Sampson and Mattie Pruitt]
[00:01.00]这是第一句歌词示例，用于预览高亮效果
[00:04.00]这是第二句歌词示例，当前行会高亮放大
[00:07.00]这是第三句歌词示例，上一行会自动压暗
[00:10.00]这是第四句歌词示例，淡入淡出呈现
"""


def _load_media():
    d = json.load(open("app_settings.json", encoding="utf-8"))
    return d["image"], d["video"]


def _prepare_sample_lrc():
    os.makedirs("preview", exist_ok=True)
    if not os.path.exists(SAMPLE_LRC):
        with open(SAMPLE_LRC, "w", encoding="utf-8") as f:
            f.write(SAMPLE_LRC_CONTENT)


def _cleanup(*paths):
    for p in paths:
        if p and os.path.exists(p):
            try:
                os.remove(p)
            except OSError:
                pass


def make_frame(theme_key: str, t: float = 5.5, out_png: str = ""):
    image, video = _load_media()
    _prepare_sample_lrc()
    ass = card_render.convert_lrc_to_ass(SAMPLE_LRC, theme_key)
    card = card_render.render_static_layer(image, os.path.basename(video), theme_key)
    bg = os.path.join("preview", "_temp_bg.png")

    # 一次性合成静态背景，再取最终命令里的 filter_complex 渲染单帧
    bg_cmd = card_render.build_background_command(image, card, theme_key, bg)
    if subprocess.run([FFMPEG, *bg_cmd], capture_output=True, text=True).returncode != 0:
        _cleanup(card, ass, bg)
        print(f"[{theme_key}] 背景合成失败")
        return False

    cmd = card_render.build_card_command("", bg, ass or "", theme_key, "")
    fc = cmd[cmd.index("-filter_complex") + 1]

    args = [
        FFMPEG, "-y",
        "-f", "lavfi", "-t", "13", "-i", "anullsrc=r=44100:cl=stereo",
        "-loop", "1", "-framerate", str(card_render.VIDEO_FPS), "-i", bg,
        "-filter_complex", fc,
        "-map", "[outv]",
        "-frames:v", "1", "-ss", str(t),
        "-pix_fmt", "yuv420p",
        out_png,
    ]
    try:
        r = subprocess.run(args, capture_output=True, text=True)
    finally:
        _cleanup(card, ass, bg)
    if r.returncode != 0:
        print(f"[{theme_key}] 渲染失败:\n{(r.stderr or '')[-500:]}")
        return False
    print(f"[{theme_key}] -> {out_png} ({os.path.getsize(out_png)} bytes)")
    return True


def main():
    themes = sys.argv[1:] or list(THEMES.keys())
    for key in themes:
        if key not in THEMES:
            print(f"未知主题: {key}，可选: {list(THEMES.keys())}")
            continue
        os.makedirs("preview", exist_ok=True)
        make_frame(key, t=5.5, out_png=os.path.join("preview", f"{key}_5.5s.png"))


if __name__ == "__main__":
    main()
