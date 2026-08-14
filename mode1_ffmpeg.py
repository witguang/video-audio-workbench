# -*- coding: utf-8 -*-
import os
import tempfile
import urllib.request
from dataclasses import dataclass
from typing import Callable, Optional
from urllib.parse import urlparse

import card_render
import lyric_translate
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


def _time_to_ms(value: str) -> int:
    """把 'HH:MM:SS[.fff]' / 'MM:SS[.fff]' / 'SS[.fff]' 解析为毫秒；空/'End'/非法返回 0。"""
    s = (value or "").strip()
    if not s or s.lower() == "end":
        return 0
    s = s.replace(",", ".")
    parts = s.split(":")
    try:
        if len(parts) == 3:
            h, m, sec = int(parts[0]), int(parts[1]), float(parts[2])
        elif len(parts) == 2:
            h, m, sec = 0, int(parts[0]), float(parts[1])
        else:
            h, m, sec = 0, 0, float(parts[0])
        return int((h * 3600 + m * 60 + sec) * 1000)
    except (ValueError, IndexError):
        return 0


def _clip_range(video_start: str, video_end: str, audio_start: str, audio_end: str):
    """确定统一处理范围（导出音频与卡片视频共用）。

    视频范围未设置（默认整片 00:00:00~End）时，跟随音频范围——这样用户只填一个
    范围（音频或视频），导出音频、字幕、视频就一起走同一段并保持同步，也不会白
    渲染整首歌。返回 (clip_start, clip_end)。
    """
    v_start = (video_start or "").strip()
    v_end = (video_end or "").strip()
    if v_start in ("", "0", "00:00:00", "0:00:00") and v_end.lower() in ("", "end"):
        return (audio_start or "").strip(), (audio_end or "").strip()
    return v_start, v_end


def _clip_duration_seconds(video_path: str, clip_start: str, clip_end: str,
                           log: Optional[Callable[[str], None]]):
    """计算剪辑范围时长（秒），作为渲染的硬性时长上限。

    clip_end 为已知时间时直接相减；为 End/空时探测源视频总时长再减起点。
    失败返回 None（调用方回退依赖 -shortest，仅可能有轻微尾帧过冲）。
    """
    start_ms = _time_to_ms(clip_start)
    end_ms = _time_to_ms(clip_end)
    if end_ms > 0:
        return max(0.0, (end_ms - start_ms) / 1000.0)
    try:
        from ffmpeg_utils import probe_media_duration
        total_s = probe_media_duration(video_path, log)
        if total_s:
            return max(0.0, total_s - start_ms / 1000.0)
    except Exception as exc:
        _emit(log, f"探测源视频时长失败（回退依赖 -shortest）: {exc}")
    return None


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
    zh_lyrics_path="",
    zh_api_config=None,
    orientation="landscape",
    lyric_style="spotify",
    quality_preset=card_render.DEFAULT_VIDEO_PRESET,
    watermark_enabled=False,
    watermark_text="",
    watermark_position="bottom_right",
):
    video_path = video_path.strip()
    image_path = image_path.strip()
    audio_out = audio_out.strip()
    video_out = video_out.strip()
    lrc_path = lrc_path.strip()
    zh_lyrics_path = (zh_lyrics_path or "").strip()
    video_start_time = video_start_time.strip()
    video_end_time = video_end_time.strip()
    if card_theme not in card_render.THEMES:
        card_theme = DEFAULT_CARD_THEME
    qcfg = card_render.VIDEO_PRESETS.get(quality_preset,
                                         card_render.VIDEO_PRESETS[card_render.DEFAULT_VIDEO_PRESET])
    # 滚动歌词文字持续移动、压缩效率低，叠加 CRF 加成以控制体积
    crf = qcfg["crf"] + (card_render.SCROLL_CRF_BOOST if lyric_style == "scroll" else 0)
    watermark_id = ""   # 数字指纹唯一 ID（启用水印时生成）

    temp_video_file_path = None
    temp_lyrics_ass = None
    temp_card_path = None
    temp_bg_path = None
    temp_clip_audio_path = None

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

        # 0. 统一处理范围：视频范围优先，未设时跟随音频范围。
        #    导出音频 / 卡片视频 / 字幕共用同一段，只填一个即可保持同步。
        eff_start, eff_end = _clip_range(video_start_time, video_end_time, start_time, end_time)

        # 1. 提取并裁剪纯音频文件（统一范围，与视频/字幕严格同步）
        if not run_ffmpeg(_build_audio_command(source_video_path, audio_out, eff_start, eff_end), log=logger):
            return ProcessResult(False, "音频提取失败。")

        # 2. 如果指定了图片，合成视频
        if image_path:
            if use_vinyl_mode:
                theme_label = card_render.THEMES[card_theme]["label"]
                _orientation_label = "竖屏 1080×1920（9:16）" if orientation == "portrait" else "横屏 1920×1080（16:9）"
                _lyric_style_label = "滚动歌词" if lyric_style == "scroll" else "三行高亮"
                _emit(logger, f"已启用：卡片歌词模式（主题：{theme_label}，{_orientation_label}，歌词显示：{_lyric_style_label}）。")

                # A. 确定视频剪辑范围：视频范围未设（默认整片）时跟随音频范围，
                #    只渲染需要的一段，避免白渲染整首歌导致大范围时卡死
                clip_start, clip_end = eff_start, eff_end
                clip_ms = _time_to_ms(clip_start)
                # 渲染时长硬上限：-shortest 在音频上混/重编码时会多出尾帧，
                # 显式 -t 才能精确卡到所选范围（整片/End 时探测源视频总时长）
                clip_dur_s = _clip_duration_seconds(source_video_path, clip_start, clip_end, logger)
                if clip_dur_s:
                    _emit(logger, f"渲染时长硬上限：{clip_dur_s:.2f}s")
                _emit(logger, f"视频时间范围：{clip_start or '00:00:00'} ~ {clip_end or 'End'}"
                              + (f"（字幕整体平移 {clip_start} → 相对 0 点）" if clip_ms else "（整片，字幕保持原时间轴）"))

                # B. 提取剪辑范围的音频作为视频基准（只提取需要的一段，速度快）
                temp_clip_audio_path = os.path.join(os.getcwd(), "temp_clip_audio.aac")
                _emit(logger, "正在提取所选时间范围的音频作为视频基准...")
                if not run_ffmpeg(_build_audio_command(source_video_path, temp_clip_audio_path,
                                                       clip_start, clip_end), log=logger):
                    return ProcessResult(False, "视频范围音频提取失败。")

                # C. 歌词(LRC/SRT/VTT/plain) -> ASS（当前行高亮、上一行压暗、淡入淡出）
                #    字幕按 clip_start 整体平移，与截取后的音频对齐（spotify/scroll 均生效）
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
                    # 中文来源二选一：API 自动翻译（优先） 或 中文翻译文件
                    _zh_api = zh_api_config or {}
                    if _zh_api.get("api_key"):
                        _provider = _zh_api.get("provider", "deepseek")
                        _label = lyric_translate.PROVIDERS.get(_provider, {}).get("label", _provider)
                        _emit(logger, f"已启用双语字幕（主英附中）：调用 {_label} API 自动翻译中文。")

                        def _translate_zh(en_texts):
                            return lyric_translate.translate_lines(
                                en_texts,
                                api_key=_zh_api["api_key"],
                                provider=_provider,
                                base_url=_zh_api.get("base_url", ""),
                                model=_zh_api.get("model", ""),
                                logger=logger)

                        temp_lyrics_ass = card_render.convert_lrc_to_ass(
                            lrc_path, card_theme, fix_overlap=fix_lyric_overlap,
                            zh_translate_fn=_translate_zh, orientation=orientation,
                            lyric_style=lyric_style, time_offset_ms=clip_ms)
                    else:
                        if zh_lyrics_path:
                            if os.path.isfile(zh_lyrics_path):
                                _emit(logger, f"已启用双语字幕（主英附中）：中文翻译文件 {zh_lyrics_path}")
                            else:
                                _emit(logger, "已指定中文翻译文件但文件不存在，将忽略中文。")
                                zh_lyrics_path = ""
                        temp_lyrics_ass = card_render.convert_lrc_to_ass(
                            lrc_path, card_theme, fix_overlap=fix_lyric_overlap,
                            zh_path=zh_lyrics_path or None, orientation=orientation,
                            lyric_style=lyric_style, time_offset_ms=clip_ms)
                    if temp_lyrics_ass:
                        _emit(logger, "歌词解析并转换为 ASS 高亮字幕成功。")
                    else:
                        _emit(logger, "歌词解析为空或不规范，将不渲染歌词。")

                # C. 预渲染静态层（封面卡 + 排版 + 装饰）
                _emit(logger, "正在预渲染封面卡片与排版层...")
                temp_card_path = card_render.render_static_layer(
                    image_path, os.path.basename(video_path), card_theme,
                    orientation=orientation)

                # D. 一次性合成静态背景（模糊+暗角+遮罩+卡片层只渲染一帧，大幅加速编码）
                temp_bg_path = os.path.join(os.getcwd(), "temp_bg.png")
                _emit(logger, "正在合成静态背景（仅渲染一帧，加速整体编码）...")
                bg_cmd = card_render.build_background_command(
                    image_path, temp_card_path, card_theme, temp_bg_path,
                    orientation=orientation)
                if not run_ffmpeg(bg_cmd, log=logger):
                    return ProcessResult(False, "静态背景合成失败。")

                # 数字指纹：生成唯一 ID，叠加可见水印（位置由 watermark_position 指定）
                if watermark_enabled:
                    watermark_id = card_render._watermark_id()
                    card_render.apply_watermark(temp_bg_path, watermark_text, watermark_id,
                                                position=watermark_position,
                                                theme_key=card_theme, orientation=orientation)
                    _emit(logger, f"已嵌入数字指纹 ID:{watermark_id}")

                # E. 直接渲染最终视频：只渲染所选时间范围（字幕已按偏移对齐），
                #    无需先合成整首歌再裁剪——大范围时从分钟级提速到秒级
                _emit(logger, "正在渲染所选时间范围的歌词视频（仅渲染需要的一段，速度快）...")
                # 不可见数字指纹：写入视频 metadata（ffprobe 可查，不破坏画面）。
                # mp4 容器只保留标准键，故用 comment 存唯一 ID、copyright 存自定义文字。
                md = []
                if watermark_enabled and watermark_id:
                    md.extend(["-metadata", f"comment=wm_id:{watermark_id}"])
                    if watermark_text.strip():
                        md.extend(["-metadata", f"copyright={watermark_text.strip()}"])
                card_cmd = card_render.build_card_command(
                    temp_clip_audio_path, temp_bg_path,
                    temp_lyrics_ass or "", card_theme, video_out,
                    orientation=orientation, quality_preset=quality_preset,
                    crf_override=crf, metadata=md or None, duration=clip_dur_s)
                if not run_ffmpeg(card_cmd, log=logger):
                    return ProcessResult(False, "视频合成失败。")

                # 内容指纹登记：从最终成品视频的「内容本身」提取感知指纹，写入本地登记库。
                # 即使盗版视频被重压/裁剪/去水印，日后仍可经「验证指纹」比对命中。
                if watermark_enabled and watermark_id:
                    try:
                        import fingerprint
                        fingerprint.register(video_out, watermark_id, os.path.basename(video_out))
                        _emit(logger, f"内容指纹已登记 ID:{watermark_id}（日后可用「验证指纹」识别盗版副本）。")
                    except Exception as exc:
                        _emit(logger, f"内容指纹登记失败（不影响出片）: {exc}")
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
        # 自动清理临时剪辑范围音频
        if temp_clip_audio_path and os.path.exists(temp_clip_audio_path):
            try:
                os.remove(temp_clip_audio_path)
                _emit(logger, "已清理临时剪辑范围音频。")
            except OSError:
                pass