import os
import shutil
import subprocess
import sys
from typing import Callable, Optional, Sequence


def get_ffmpeg_path() -> str:
    """自动定位 FFmpeg 可执行文件路径。"""
    candidate_roots = [
        os.path.dirname(os.path.abspath(sys.argv[0])),
        os.path.dirname(os.path.abspath(__file__)),
        os.getcwd(),
    ]
    checked = set()

    for root in candidate_roots:
        candidate = os.path.normpath(os.path.join(root, "ffmpeg", "ffmpeg.exe"))
        if candidate in checked:
            continue
        checked.add(candidate)
        if os.path.exists(candidate):
            return candidate

    # 使用 shutil 寻找系统环境变量中的 ffmpeg
    system_ffmpeg = shutil.which("ffmpeg")
    if system_ffmpeg:
        return system_ffmpeg

    return "ffmpeg"


def get_ffprobe_path() -> str:
    """定位 ffprobe.exe：优先与 ffmpeg.exe 同目录，其次系统 PATH。"""
    ffmpeg = get_ffmpeg_path()
    if os.path.dirname(ffmpeg):
        candidate = os.path.normpath(os.path.join(os.path.dirname(ffmpeg), "ffprobe.exe"))
        if os.path.exists(candidate):
            return candidate
    return shutil.which("ffprobe") or "ffprobe"


def probe_media_duration(path: str, log: Optional[Callable[[str], None]] = None) -> Optional[float]:
    """探测媒体时长（秒）。失败返回 None，不抛异常。

    用于剪辑范围「End」等需要源时长的场景：先探测再减起点，作为渲染硬上限。
    """
    command = [
        get_ffprobe_path(),
        "-v", "error",
        "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1",
        path,
    ]
    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if sys.platform == "win32" else 0
    try:
        proc = subprocess.run(
            command,
            capture_output=True,
            text=False,
            creationflags=creationflags,
            timeout=30,
        )
        text = proc.stdout.decode("utf-8", errors="ignore").strip()
        if proc.returncode == 0 and text:
            return float(text)
        _emit(log, f"探测媒体时长失败: {path}")
    except Exception as exc:
        _emit(log, f"探测媒体时长失败: {exc}")
    return None


def _format_command(args: Sequence[str]) -> str:
    parts = []
    for arg in args:
        text = str(arg)
        if not text:
            parts.append('""')
        elif any(char.isspace() for char in text) or '"' in text:
            escaped = text.replace('"', '\\"')
            parts.append(f'"{escaped}"')
        else:
            parts.append(text)
    return " ".join(parts)


def _emit(log: Optional[Callable[[str], None]], message: str):
    print(message)
    if log:
        log(message)


def _decode_line(raw_bytes: bytes) -> str:
    """尝试使用不同编码解析 FFmpeg 的输出，防止 Windows 环境下因编码问题导致乱码。"""
    for encoding in ("utf-8", "gbk", "cp936", "latin1"):
        try:
            return raw_bytes.decode(encoding)
        except UnicodeDecodeError:
            continue
    return raw_bytes.decode("utf-8", errors="ignore")


def run_ffmpeg(args: Sequence[str], log: Optional[Callable[[str], None]] = None) -> bool:
    """执行 FFmpeg 命令并实时输出日志。"""
    if isinstance(args, str):
        raise TypeError("run_ffmpeg 需要传入参数列表，而不是命令字符串。")

    command = [get_ffmpeg_path(), *[str(arg) for arg in args]]
    _emit(log, f"\n--- 开始执行 FFmpeg ---\n命令: {_format_command(command)}")

    # Windows 下禁止 ffmpeg 弹出命令行窗口（CREATE_NO_WINDOW = 0x08000000）
    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if sys.platform == "win32" else 0

    try:
        # 以二进制模式读取，避免 Python 默认的编码转换器在不匹配时报错
        process = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            bufsize=0,
            shell=False,
            creationflags=creationflags,
        )

        if process.stdout:
            for line_bytes in iter(process.stdout.readline, b""):
                line = _decode_line(line_bytes).rstrip("\r\n")
                if line:
                    _emit(log, line)

        process.wait()
        _emit(log, f"\n--- 任务结束，返回码: {process.returncode} ---\n")
        return process.returncode == 0
    except FileNotFoundError:
        _emit(log, "执行出错: 未找到 FFmpeg，请确认 ffmpeg.exe 已放到项目目录或已加入系统 PATH。")
        return False
    except Exception as exc:
        _emit(log, f"执行出错: {exc}")
        return False