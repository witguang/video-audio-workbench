# -*- coding: utf-8 -*-
import os
import re
import tempfile
import urllib.request
from dataclasses import dataclass
from typing import Callable, Optional
from urllib.parse import urlparse

from ffmpeg_utils import run_ffmpeg

SOURCE_LOCAL = "local"
SOURCE_R2 = "r2"


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


# ==================== 歌词解析处理模块 ====================

def _parse_lrc(lrc_path: str) -> list:
    """解析带有时间轴的 LRC 文件。"""
    lines = []
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
            
    for line in content.splitlines():
        match = pattern.match(line.strip())
        if match:
            minutes = int(match.group(1))
            seconds = int(match.group(2))
            frac_str = match.group(3) or "0"
            text = match.group(4).strip()
            
            if len(frac_str) == 1:
                ms_val = int(frac_str) * 100
            elif len(frac_str) == 2:
                ms_val = int(frac_str) * 10
            else:
                ms_val = int(frac_str[:3])
                
            total_ms = minutes * 60000 + seconds * 1000 + ms_val
            lines.append((total_ms, text))
            
    lines.sort()
    return lines


def _format_srt_time(ms: int) -> str:
    h = ms // 3600000
    ms %= 3600000
    m = ms // 60000
    ms %= 60000
    s = ms // 1000
    ms %= 1000
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def _convert_lrc_to_srt(lrc_path: str) -> str:
    """将 LRC 文件转换为相对路径下的临时 srt 歌词。"""
    parsed_lines = _parse_lrc(lrc_path)
    if not parsed_lines:
        return ""
        
    temp_srt_filename = "temp_lyrics.srt"
    temp_srt_path = os.path.join(os.getcwd(), temp_srt_filename)
    
    with open(temp_srt_path, "w", encoding="utf-8") as f:
        for idx, (ms, text) in enumerate(parsed_lines):
            start_time = _format_srt_time(ms)
            if idx + 1 < len(parsed_lines):
                end_ms = parsed_lines[idx+1][0]
            else:
                end_ms = ms + 4000  # 最后一句默认展示 4 秒
            end_time = _format_srt_time(end_ms)
            
            f.write(f"{idx+1}\n")
            f.write(f"{start_time} --> {end_time}\n")
            f.write(f"{text}\n\n")
            
    return temp_srt_filename


# ==================== 图像渲染模块 ====================

def _build_static_video_command(image_path: str, audio_out: str, video_out: str):
    """静态背景合成（直接利用已裁好的音频进行快速单步组装）。"""
    audio_codec_args = ["-c:a", "aac", "-b:a", "192k"] if video_out.lower().endswith(".mp4") else ["-c:a", "copy"]
    return [
        "-y",
        "-loop",
        "1",
        "-framerate",
        "1",
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
        "-vf",
        "scale=trunc(iw/2)*2:trunc(ih/2)*2,format=yuv420p",
        *audio_codec_args,
        "-shortest",
        video_out,
    ]


def _build_vinyl_video_command(video_path: str, image_path: str, audio_out: str, video_out: str, srt_filename: str, *args, **kwargs) -> list:
    """动态极简圆角歌词视频完整合成（不带裁剪，保持歌词原轴绝对对齐）。"""
    # 1. 智能拆分歌名与歌手
    base_name = os.path.splitext(os.path.basename(video_path))[0]
    clean_name = re.sub(r"[\(\[][^\]\)]*[\)\]]", "", base_name).strip()
    parts = [p.strip() for p in clean_name.split("-") if p.strip()]
    
    if len(parts) >= 2:
        artist = parts[0]
        title = parts[1]
    else:
        artist = ""
        title = base_name

    # 2. 构造文本绘制滤镜（坐标随封面缩至 300 像素进行黄金分割重排）
    title_escaped = title.replace("'", "'\\''")
    artist_escaped = artist.replace("'", "'\\''")
    
    drawtext_filters = []
    drawtext_filters.append(
        f"drawtext=font='Microsoft YaHei':text='{title_escaped}':fontcolor=white:fontsize=26:x=(w-text_w)/2:y=465"
    )
    if artist:
        drawtext_filters.append(
            f"drawtext=font='Microsoft YaHei':text='{artist_escaped}':fontcolor=white@0.5:fontsize=15:x=(w-text_w)/2:y=505"
        )
    drawtext_str = "," + ",".join(drawtext_filters)

    # 3. 构造歌词字幕滤镜
    sub_filter = ""
    if srt_filename:
        sub_filter = f",subtitles='{srt_filename}':force_style='Fontname=Microsoft YaHei,FontSize=20,PrimaryColour=&H00FFFFFF,Outline=1,OutlineColour=&H00000000,MarginV=45'"

    # 4. 数学半径与极简抗锯齿圆角公式定义 (300x300 圆角，20px半径)
    x_dist = "if(lt(X,20),20-X,if(gt(X,280),X-280,0))"
    y_dist = "if(lt(Y,20),20-Y,if(gt(Y,280),Y-280,0))"
    d = f"sqrt(({x_dist})*({x_dist})+({y_dist})*({y_dist}))"
    mask_expr = f"if(gt({x_dist},0),if(gt({y_dist},0),if(lte({d},18),255,if(gte({d},20),0,127.5*(20-{d}))),255),255)"

    # 5. 极简极清滤镜链：高斯模糊磨砂背景 + 极高清晰度圆角封面 + 底部滚动字幕
    filter_complex = (
        # A. 磨砂背景 (1280x720)
        f"[1:v]scale=1280:720:force_original_aspect_ratio=increase:flags=lanczos,crop=1280:720,gblur=sigma=45,setsar=1[bg_blurred];"
        
        # B. 圆角封面：高画质裁剪缩放到 300x300，应用亚像素抗锯齿圆角遮罩
        f"[1:v]crop='min(iw,ih):min(iw,ih)',scale=300:300:flags=lanczos,setsar=1[cover_raw];"
        f"color=s=300x300:c=black:r=25,setsar=1,geq=lum='{mask_expr}':cb=128:cr=128[cover_mask];"
        f"[cover_raw][cover_mask]alphamerge[cover_rounded];"
        
        # C. 全画面层级极简组装（x=490, y=120）
        f"[bg_blurred][cover_rounded]overlay=x=490:y=120{drawtext_str}{sub_filter},setsar=1[outv]"
    )

    return [
        "-y",
        "-i", audio_out,     # input 0 (完整基准音频)
        "-loop", "1", "-framerate", "25", "-i", image_path, # input 1
        "-filter_complex", filter_complex,
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
        video_out
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
    video_start_time="00:00:00", # 【新增参数】
    video_end_time="End",        # 【新增参数】
):
    video_path = video_path.strip()
    image_path = image_path.strip()
    audio_out = audio_out.strip()
    video_out = video_out.strip()
    lrc_path = lrc_path.strip()
    video_start_time = video_start_time.strip()
    video_end_time = video_end_time.strip()
    
    temp_video_file_path = None
    temp_srt_filename = None
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
                _emit(logger, "已启用：动态极简圆角歌词卡片模式。")
                
                # A. 提取完整长度音频流，作为歌词与封面的无错对齐基准
                temp_full_audio_path = os.path.join(os.getcwd(), "temp_full_audio.aac")
                _emit(logger, "正在提取完整长度音频流以进行字幕时间轴对齐...")
                full_audio_cmd = _build_full_audio_command(source_video_path, temp_full_audio_path)
                if not run_ffmpeg(full_audio_cmd, log=logger):
                    return ProcessResult(False, "完整音频提取失败。")

                if lrc_path and os.path.exists(lrc_path):
                    _emit(logger, f"正在解析 LRC 歌词: {lrc_path}")
                    temp_srt_filename = _convert_lrc_to_srt(lrc_path)
                    if temp_srt_filename:
                        _emit(logger, "歌词解析并转换成功。")
                    else:
                        _emit(logger, "歌词解析为空或不规范，将不渲染歌词。")
                
                # B. 合成整首歌长度的视频 (歌词与原歌曲完整对齐，完全不在此处做中间裁剪)
                temp_full_video_path = os.path.join(os.getcwd(), "temp_full_video.mp4")
                _emit(logger, "正在进行全时值高清晰度歌词视频合成...")
                
                vinyl_cmd = _build_vinyl_video_command(
                    video_path, image_path, temp_full_audio_path, temp_full_video_path, 
                    temp_srt_filename or ""
                )
                if not run_ffmpeg(vinyl_cmd, log=logger):
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
                    "-crf", "17",
                    "-c:a", "aac",
                    "-b:a", "192k",
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
        # 自动清理相对路径下的临时 srt 歌词文件
        if temp_srt_filename:
            full_temp_srt = os.path.join(os.getcwd(), temp_srt_filename)
            if os.path.exists(full_temp_srt):
                try:
                    os.remove(full_temp_srt)
                    _emit(logger, "已清理临时歌词文件。")
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