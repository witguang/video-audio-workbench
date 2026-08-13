# -*- coding: utf-8 -*-
import os
import tempfile
import urllib.request
from dataclasses import dataclass
from typing import Callable, Optional
from urllib.parse import urlparse

import card_render
from ffmpeg_utils import run_ffmpeg

SOURCE_LOCAL = "local"
SOURCE_R2 = "r2"

DEFAULT_CARD_THEME = "minimal"


@dataclass
class ProcessResult:
    success: bool
    message: str
    audio_output: str = ""
    video_output: str = ""


def _is_remote_url(value: str) -> bool:
    parsed = urlparse(value)
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc)


def _emit(log: Optional[Callable[[str], None]], message: str):
    print(message)
    if log:
        log(message)


def _ensure_parent_dir(file_path: str):
    if not file_path:
        return
    parent_dir = os.path.dirname(os.path.abspath(file_path))
    if parent_dir:
        os.makedirs(parent_dir, exist_ok=True)


def _build_clip_args(start_time: str, end_time: str):
    clip_args = []
    start_time = start_time.strip()
    end_time = end_time.strip()

    if start_time:
        clip_args.extend(["-ss", start_time])
    if end_time and end_time.lower() != "end":
        clip_args.extend(["-to", end_time])
    return clip_args


def _download_remote_video(video_url: str, log: Optional[Callable[[str], None]]) -> str:
    """流式下载 R2 视频，并实时反馈百分比与下载大小。"""
    _emit(log, f"正在从 R2 下载视频: {video_url}")
    temp_file = tempfile.NamedTemporaryFile(delete=False, suffix=".mp4")
    temp_file_path = temp_file.name
    temp_file.close()

    try:
        req = urllib.request.Request(video_url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=30) as response, open(temp_file_path, "wb") as out_file:
            total_size = int(response.info().get("Content-Length", 0))
            bytes_downloaded = 0
            block_size = 1024 * 64
            last_reported_percent = -1

            while True:
                buffer = response.read(block_size)
                if not buffer:
                    break
                out_file.write(buffer)
                bytes_downloaded += len(buffer)

                if total_size > 0:
                    percent = int((bytes_downloaded / total_size) * 100)
                    if percent % 5 == 0 and percent != last_reported_percent:
                        _emit(
                            log,
                            f"下载进度: {percent}% ({bytes_downloaded // (1024 * 1024)}MB / {total_size // (1024 * 1024)}MB)"
                        )
                        last_reported_percent = percent
                else:
                    if bytes_downloaded % (1024 * 1024 * 10) == 0:
                        _emit(log, f"已下载: {bytes_downloaded // (1024 * 1024)}MB")

        _emit(log, f"视频下载完成，临时保存于: {temp_file_path}")
        return temp_file_path
    except Exception as exc:
        if os.path.exists(temp_file_path):
            try:
                os.remove(temp_file_path)
            except OSError:
                pass
        raise RuntimeError(f"网络下载失败: {exc}")


def _resolve_video_source(video_source: str, source_mode: str, log: Optional[Callable[[str], None]]):
    video_source = video_source.strip()
    if not video_source:
        raise ValueError("请填写视频来源。")

    if source_mode == SOURCE_R2:
        if not _is_remote_url(video_source):
            raise ValueError("R2 模式下，视频路径必须是有效的 http/https URL。")
        temp_path = _download_remote_video(video_source, log)
        return temp_path, temp_path

    if not os.path.isfile(video_source):
        raise FileNotFoundError(f"输入视频文件不存在: {video_source}")

    return video_source, None


def _build_audio_command(video_path: str, audio_out: str, start_time: str, end_time: str):
    """提取裁剪后的纯音频文件。"""
    codec_args = ["-c:a", "libmp3lame", "-q:a", "2"] if audio_out.lower().endswith(".mp3") else ["-c:a", "aac"]
    return [
        "-y",
        *_build_clip_args(start_time, end_time),
        "-i",
        video_path,
        "-vn",
        *codec_args,
        audio_out,
    ]


def _build_full_audio_command(video_path: str, audio_out: str):
    """提取未裁剪的完整音频，用于第一步无错对齐。"""
    return [
        "-y",
        "-i", video_path,
        "-vn",
        "-c:a", "aac",
        audio_out,
    ]


# ==================== 图像渲染模块 ====================

def _build_static_video_command(image_path: str, audio_out: str, video_out: str):
    """静态背景合成（直接利用已裁好的音频进行快速单步组装）。"""
    audio_codec_args = ["-c:a", "aac", "-b:a", "160k", "-ac", "2"] if video_out.lower().endswith(".mp4") else ["-c:a", "copy"]
    return [
        "-y",
        "-loop",
        "1",
        "-framerate",
        str(card_render.VIDEO_FPS),
        "-i",
        image_path,
        "-i",
        audio_out,
        "-c:v",
        "libx264",
        "-preset",
        "ultrafast",
        "-tune",
        "stillimage",
        "-r",
        str(card_render.VIDEO_FPS),
        "-vf",
        "scale=trunc(iw/2)*2:trunc(ih/2)*2,format=yuv420p",
        *audio_codec_args,
        "-shortest",
        video_out,
    ]


def mode1_process(
    video_path,
    image_path,
    audio_out,
    video_out,
    start_time="",
    end_time="",
    process_mode=SOURCE_LOCAL,
    logger: Optional[Callable[[str], None]] = None,
    use_vinyl_mode=False,
    lrc_path="",
    video_start_time="00:00:00",
    video_end_time="End",
    card_theme=DEFAULT_CARD_THEME,
    fix_lyric_overlap=True,
):
    video_path = video_path.strip()
    image_path = image_path.strip()
    audio_out = audio_out.strip()
    video_out = video_out.strip()
    lrc_path = lrc_path.strip()
    video_start_time = video_start_time.strip()
    video_end_time = video_end_time.strip()
    if card_theme not in card_render.THEMES:
        card_theme = DEFAULT_CARD_THEME

    temp_video_file_path = None
    temp_lyrics_ass = None
    temp_card_path = None
    temp_bg_path = None
    temp_full_audio_path = None
    temp_full_video_path = None

    try:
        if not audio_out:
            return ProcessResult(False, "请设置导出音频路径。")

        if image_path and not os.path.isfile(image_path):
            return ProcessResult(False, f"背景图片不存在: {image_path}")

        if image_path and not video_out:
            return ProcessResult(False, "已选择背景图，请设置输出视频路径。")

        source_video_path, temp_video_file_path = _resolve_video_source(video_path, process_mode, logger)

        _ensure_parent_dir(audio_out)
        if video_out:
            _ensure_parent_dir(video_out)

        _emit(logger, f"准备处理视频: {source_video_path}")
        _emit(logger, f"音频输出位置: {audio_out}")
        
        # 1. 正常提取并裁剪纯音频文件（满足前台“音频提取”的独立范围设置）
        if not run_ffmpeg(_build_audio_command(source_video_path, audio_out, start_time, end_time), log=logger):
            return ProcessResult(False, "音频提取失败。")

        # 2. 如果指定了图片，合成视频
        if image_path:
            if use_vinyl_mode:
                theme_label = card_render.THEMES[card_theme]["label"]
                _emit(logger, f"已启用：卡片歌词模式（主题：{theme_label}）。")

                # A. 提取完整长度音频流，作为歌词与封面的无错对齐基准
                temp_full_audio_path = os.path.join(os.getcwd(), "temp_full_audio.aac")
                _emit(logger, "正在提取完整长度音频流以进行字幕时间轴对齐...")
                full_audio_cmd = _build_full_audio_command(source_video_path, temp_full_audio_path)
                if not run_ffmpeg(full_audio_cmd, log=logger):
                    return ProcessResult(False, "完整音频提取失败。")

                # B. 歌词(LRC/SRT/VTT/plain) -> ASS（当前行高亮、上一行压暗、淡入淡出）
                if lrc_path and os.path.exists(lrc_path):
                    _emit(logger, f"正在解析歌词文件: {lrc_path}")
                    _lrc_ext = os.path.splitext(lrc_path)[1].lower()
                    if _lrc_ext in (".plain", ".txt"):
                        _emit(logger, "纯歌词文件：将自动匹配同目录同名 SRT 的时间轴。")
                    elif _lrc_ext in (".srt", ".vtt"):
                        _lrc_base = os.path.splitext(lrc_path)[0]
                        if any(os.path.isfile(_lrc_base + c) for c in (".plain", ".txt")):
                            _emit(logger, "检测到同目录同名纯歌词(.plain)：以纯歌词文本和行为准，借用 SRT 时间轴对齐。")
                    if fix_lyric_overlap:
                        _emit(logger, "已启用：消除歌词时间重叠（相邻行自动截断）。")
                    temp_lyrics_ass = card_render.convert_lrc_to_ass(
                        lrc_path, card_theme, fix_overlap=fix_lyric_overlap)
                    if temp_lyrics_ass:
                        _emit(logger, "歌词解析并转换为 ASS 高亮字幕成功。")
                    else:
                        _emit(logger, "歌词解析为空或不规范，将不渲染歌词。")

                # C. 预渲染静态层（封面卡 + 排版 + 装饰）
                _emit(logger, "正在预渲染封面卡片与排版层...")
                temp_card_path = card_render.render_static_layer(
                    image_path, os.path.basename(video_path), card_theme)

                # D. 一次性合成静态背景（模糊+暗角+遮罩+卡片层只渲染一帧，大幅加速编码）
                temp_bg_path = os.path.join(os.getcwd(), "temp_bg.png")
                _emit(logger, "正在合成静态背景（仅渲染一帧，加速整体编码）...")
                bg_cmd = card_render.build_background_command(
                    image_path, temp_card_path, card_theme, temp_bg_path)
                if not run_ffmpeg(bg_cmd, log=logger):
                    return ProcessResult(False, "静态背景合成失败。")

                # E. 合成整首歌长度的视频（歌词与原歌曲完整对齐，不在此处做中间裁剪）
                temp_full_video_path = os.path.join(os.getcwd(), "temp_full_video.mp4")
                _emit(logger, "正在进行全时值高清歌词视频合成...")
                card_cmd = card_render.build_card_command(
                    temp_full_audio_path, temp_bg_path,
                    temp_lyrics_ass or "", card_theme, temp_full_video_path)
                if not run_ffmpeg(card_cmd, log=logger):
                    return ProcessResult(False, "完整视频合成失败。")
                
                # C. 二次精细剪切成品视频，完美同步画面、音轨和滚动歌词 [1]
                _emit(logger, "正在根据设定的【视频裁剪范围】对成品视频进行二次精准剪切...")
                trim_cmd = [
                    "-y",
                    "-ss", video_start_time,
                ]
                if video_end_time and video_end_time.lower() != "end":
                    trim_cmd.extend(["-to", video_end_time])
                    
                trim_cmd.extend([
                    "-i", temp_full_video_path,
                    "-c:v", "libx264",
                    "-preset", "ultrafast",  # 极速重编码，精准定位时间点到毫秒
                    "-crf", str(card_render.VIDEO_CRF),
                    "-c:a", "aac",
                    "-b:a", card_render.AUDIO_BITRATE,
                    "-ac", "2",
                    "-aspect", "16:9",
                    video_out
                ])
                
                if not run_ffmpeg(trim_cmd, log=logger):
                    return ProcessResult(False, "视频二次裁剪失败。")
            else:
                # 普通静态模式（不需要提取完整音频和二次裁剪，直接用前台已经裁剪好的音频单步组装即可）
                static_cmd = _build_static_video_command(image_path, audio_out, video_out)
                if not run_ffmpeg(static_cmd, log=logger):
                    return ProcessResult(False, "普通静态视频合成失败。")
                    
            return ProcessResult(True, "视频处理成功。", audio_out, video_out)

        return ProcessResult(True, "音频提取完成。", audio_out, "")

    except Exception as exc:
        return ProcessResult(False, f"处理过程中发生异常: {exc}")
    finally:
        # 清理临时原视频文件
        if temp_video_file_path and os.path.exists(temp_video_file_path):
            try:
                os.remove(temp_video_file_path)
                _emit(logger, f"已清理临时视频文件: {temp_video_file_path}")
            except OSError:
                pass
        # 自动清理临时 ASS 歌词文件
        if temp_lyrics_ass:
            full_temp_ass = os.path.join(os.getcwd(), temp_lyrics_ass)
            if os.path.exists(full_temp_ass):
                try:
                    os.remove(full_temp_ass)
                    _emit(logger, "已清理临时歌词文件。")
                except OSError:
                    pass
        # 自动清理临时封面卡片静态层
        if temp_card_path:
            if os.path.exists(temp_card_path):
                try:
                    os.remove(temp_card_path)
                    _emit(logger, "已清理临时封面卡片层。")
                except OSError:
                    pass
        # 自动清理临时静态背景图
        if temp_bg_path and os.path.exists(temp_bg_path):
            try:
                os.remove(temp_bg_path)
                _emit(logger, "已清理临时静态背景图。")
            except OSError:
                pass
        # 自动清理后台提取的临时基准完整音频流
        if temp_full_audio_path and os.path.exists(temp_full_audio_path):
            try:
                os.remove(temp_full_audio_path)
                _emit(logger, "已清理临时完整音频流。")
            except OSError:
                pass
        # 自动清理临时生成的未裁剪完整视频
        if temp_full_video_path and os.path.exists(temp_full_video_path):
            try:
                os.remove(temp_full_video_path)
                _emit(logger, "已清理临时完整视频。")
            except OSError:
                pass