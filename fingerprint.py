# -*- coding: utf-8 -*-
"""内容指纹（感知指纹）模块：用于「视频被盗用后仍可识别」的登记与比对。

与 metadata 指纹（写进文件头、一重压即失效）不同，这里的指纹是从视频的
「内容本身」算出来的：

- 音频指纹：对音轨做短时傅里叶变换（STFT），抽取频谱峰值并两两配对成
  「地标哈希（landmark hash）」。重编码、改码率、轻度裁剪都不会改变峰值，
  因此指纹几乎不变——这就是 Shazam / YouTube Content ID 的原理。
- 视频指纹：在整段视频均匀抽取若干帧，对每帧计算 dHash（感知哈希）。
  重编码、改分辨率后 dHash 基本不变，作为音频被替换时的补充证据。

工作流：
1. 出片时调用 register() 把指纹连同唯一 ID 存入本地登记库。
2. 将来拿到疑似盗版视频，调用 verify() 算指纹并和登记库比对相似度。

对外接口：
    register(video_path, wm_id, source="") -> dict|None
    verify(video_path) -> dict
    compute_fingerprint(video_path) -> dict
"""
import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import time

import numpy as np

from ffmpeg_utils import get_ffmpeg_path

try:
    from scipy.signal import stft as _scipy_stft
    _HAS_SCIPY = True
except Exception:  # scipy 缺失时退化为仅视频指纹
    _scipy_stft = None
    _HAS_SCIPY = False

try:
    from PIL import Image
    _HAS_PIL = True
except Exception:
    Image = None
    _HAS_PIL = False

REGISTRY_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fingerprint_registry.json")

# —— 音频指纹参数 ——
AUDIO_SR = 11025            # 解码采样率（Hz），固定以保持一致
N_FFT = 1024                # FFT 窗长
HOP = 512                   # 帧移
PEAK_PERCENTILE = 96        # 峰值阈值（频谱幅值分位数）：只保留最强峰，地标更少、更抗重压
DELTA_T = 30                # 地标配对的目标时间窗（帧）
DELTA_F = 40                # 地标配对的目标频率窗（bin）

# —— 视频指纹参数 ——
VIDEO_FRAMES = 16           # 均匀抽取帧数
VIDEO_DHASH_THRESHOLD = 12  # 64 位 dHash 的汉明距离阈值（≤ 视为该帧相似）

# —— 匹配判定阈值 ——
AUDIO_MATCH_MIN = 0.15      # 音频 Jaccard 相似度 ≥ 该值判定为命中
VIDEO_MATCH_MIN = 0.5       # 相似帧占比 ≥ 该值判定为命中

_registry_lock = threading.Lock()


# ==================== 底层工具 ====================

def _ffmpeg_bin():
    return get_ffmpeg_path()


def _creationflags():
    return getattr(subprocess, "CREATE_NO_WINDOW", 0) if sys.platform == "win32" else 0


def _probe_duration(path: str):
    """用 ffmpeg -i 解析视频时长（秒），无 ffprobe 环境下的替代方案。"""
    try:
        p = subprocess.run(
            [_ffmpeg_bin(), "-i", path],
            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
            creationflags=_creationflags(),
        )
        m = re.search(rb"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)", p.stderr)
        if not m:
            return None
        return int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3))
    except Exception:
        return None


def _decode_audio(path: str):
    """解码音轨为单声道 float 数组，返回 (signal, sample_rate) 或 (None, None)。

    带重试：出片后紧接着读取，Windows 下文件句柄可能尚未完全释放，
    首次 ffmpeg 解码偶发返回空，稍候重试即可恢复。
    """
    if not _HAS_SCIPY:
        return None, None
    cmd = [_ffmpeg_bin(), "-i", path, "-vn", "-ac", "1", "-ar", str(AUDIO_SR), "-f", "s16le", "-"]
    for attempt in range(3):
        try:
            p = subprocess.run(
                cmd,
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                creationflags=_creationflags(),
            )
        except Exception:
            return None, None
        if p.returncode == 0 and p.stdout:
            signal = np.frombuffer(p.stdout, dtype=np.int16).astype(np.float32) / 32768.0
            if signal.size >= N_FFT * 2:
                return signal, AUDIO_SR
        if attempt < 2:
            time.sleep(0.6)
    return None, None


def _pick_landmarks(S: np.ndarray):
    """从频谱幅值矩阵 (freq_bins, time_frames) 抽取地标哈希集合。

    每帧取局部频峰作为锚点，在其后的时间-频率目标窗内找最强峰配对，
    把 (f1, f2, dt) 编码成整数哈希。重编码只轻微扰动幅值，峰值位置稳定。
    """
    nb, nt = S.shape
    if nt < 4 or nb < 8:
        return set()
    thresh = np.percentile(S, PEAK_PERCENTILE)

    # 逐帧挑频峰（±3 bin 邻域内的局部最大，且高于全局阈值）
    anchors = []
    for j in range(nt - 1):
        col = S[:, j]
        for i in range(3, nb - 3):
            if col[i] < thresh:
                continue
            if col[i] >= col[i - 3:i + 4].max():
                anchors.append((i, j))

    hashes = set()
    for f1, t1 in anchors:
        t_lo = t1 + 1
        t_hi = min(nt, t1 + DELTA_T + 1)
        if t_lo >= t_hi:
            continue
        f_lo = max(0, f1 - DELTA_F)
        f_hi = min(nb, f1 + DELTA_F + 1)
        zone = S[f_lo:f_hi, t_lo:t_hi]
        idx = int(np.argmax(zone))
        f2_rel, dt_rel = np.unravel_index(idx, zone.shape)
        f2 = f_lo + int(f2_rel)
        dt = (t_lo + int(dt_rel)) - t1
        # 编码成整数：f1/f2 各 10 位、dt 12 位（bin ≤ 512、dt ≤ 4096 内安全）
        hashes.add((int(f1) << 22) | (int(f2) << 12) | int(dt))
    return hashes


def _audio_fingerprint(path: str):
    """返回 (landmark_list, duration) 或 (None, None)。"""
    signal, sr = _decode_audio(path)
    if signal is None:
        return None, None
    try:
        _, _, Z = _scipy_stft(signal, fs=sr, nperseg=N_FFT, noverlap=HOP, window="hann")
    except Exception:
        return None, None
    S = np.log1p(np.abs(Z))
    landmarks = _pick_landmarks(S)
    duration = signal.size / float(sr)
    return sorted(landmarks), duration


def _dhash(img) -> int:
    """计算单帧 64 位 dHash。"""
    small = img.convert("L").resize((9, 8), Image.LANCZOS)
    px = list(small.getdata())
    bits = 0
    for row in range(8):
        base = row * 9
        for col in range(8):
            bits <<= 1
            if px[base + col] < px[base + col + 1]:
                bits |= 1
    return bits


def _video_fingerprint(path: str, duration=None):
    """均匀抽取若干帧计算 dHash 序列，返回 (hash_list, duration) 或 (None, None)。"""
    if not _HAS_PIL:
        return None, None
    if duration is None:
        duration = _probe_duration(path)
    if not duration or duration <= 1:
        return None, None

    tmpdir = tempfile.mkdtemp(prefix="fp_")
    try:
        fps = VIDEO_FRAMES / float(duration)
        pattern = os.path.join(tmpdir, "f_%02d.png")
        cmd = [
            _ffmpeg_bin(), "-y", "-i", path,
            "-vf", f"fps={fps:.6f},scale=256:144",
            "-frames:v", str(VIDEO_FRAMES),
            "-f", "image2", pattern,
        ]
        subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                       creationflags=_creationflags())

        hashes = []
        for i in range(1, VIDEO_FRAMES + 1):
            fp = pattern % i
            if not os.path.exists(fp):
                continue
            try:
                with Image.open(fp) as im:
                    hashes.append(_dhash(im))
            except Exception:
                continue
        if len(hashes) < 3:
            return None, None
        return hashes, duration
    except Exception:
        return None, None
    finally:
        try:
            import shutil
            shutil.rmtree(tmpdir, ignore_errors=True)
        except Exception:
            pass


# ==================== 对外接口 ====================

def compute_fingerprint(video_path: str) -> dict:
    """计算给定视频的内容指纹，返回 dict（不含任何 IO 写盘）。"""
    result = {
        "path": video_path,
        "duration": None,
        "audio_landmarks": None,   # list[int] 或 None
        "video_hashes": None,      # list[int] 或 None
    }
    if not os.path.isfile(video_path):
        return result

    audio_landmarks, dur_a = _audio_fingerprint(video_path)
    if audio_landmarks is not None:
        result["audio_landmarks"] = audio_landmarks
        result["duration"] = dur_a

    video_hashes, dur_v = _video_fingerprint(video_path, duration=result["duration"])
    if video_hashes is not None:
        result["video_hashes"] = video_hashes
        if result["duration"] is None:
            result["duration"] = dur_v

    return result


def _load_registry() -> list:
    if not os.path.exists(REGISTRY_FILE):
        return []
    try:
        with open(REGISTRY_FILE, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        return data.get("entries", []) if isinstance(data, dict) else []
    except (OSError, json.JSONDecodeError):
        return []


def _save_registry(entries: list):
    tmp = REGISTRY_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        # 紧凑输出（无缩进换行），避免每个地标占一行导致登记库膨胀
        json.dump({"entries": entries}, fh, ensure_ascii=False, separators=(",", ":"))
    os.replace(tmp, REGISTRY_FILE)


def register(video_path: str, wm_id: str, source: str = "") -> dict:
    """登记一个视频的内容指纹，返回存入库中的条目（失败抛异常，由调用方兜底）。"""
    fp = compute_fingerprint(video_path)
    if not fp.get("audio_landmarks") and not fp.get("video_hashes"):
        raise RuntimeError("未提取到有效内容指纹（可能无音轨/无画面，或时长过短）。")

    entry = {
        "wm_id": wm_id,
        "source": source or os.path.basename(video_path),
        "duration": round(fp["duration"], 2) if fp["duration"] else None,
        "audio_landmarks": fp["audio_landmarks"],
        "video_hashes": fp["video_hashes"],
    }

    with _registry_lock:
        entries = _load_registry()
        # 同 ID 重复登记则覆盖
        entries = [e for e in entries if e.get("wm_id") != wm_id]
        entries.append(entry)
        _save_registry(entries)
    return entry


def _jaccard(a, b) -> float:
    if not a or not b:
        return 0.0
    sa, sb = set(a), set(b)
    inter = len(sa & sb)
    union = len(sa | sb)
    return inter / union if union else 0.0


def _hamming(a: int, b: int) -> int:
    return bin(a ^ b).count("1")


def _compare(entry: dict, fp: dict) -> dict:
    """对比登记条目与待验证指纹，返回各分量相似度。"""
    audio_score = 0.0
    video_score = 0.0

    if entry.get("audio_landmarks") and fp.get("audio_landmarks"):
        audio_score = _jaccard(entry["audio_landmarks"], fp["audio_landmarks"])

    eh, fh = entry.get("video_hashes"), fp.get("video_hashes")
    if eh and fh:
        n = min(len(eh), len(fh))
        if n:
            matched = sum(1 for i in range(n) if _hamming(eh[i], fh[i]) <= VIDEO_DHASH_THRESHOLD)
            video_score = matched / n

    return {"audio_score": audio_score, "video_score": video_score}


def verify(video_path: str) -> dict:
    """对疑似视频算指纹，与登记库比对，返回最匹配结果。"""
    fp = compute_fingerprint(video_path)
    if not fp.get("audio_landmarks") and not fp.get("video_hashes"):
        return {"matched": False, "reason": "无法提取该视频的内容指纹（可能无音轨/无画面）。",
                "query": fp, "results": []}

    with _registry_lock:
        entries = _load_registry()

    if not entries:
        return {"matched": False, "reason": "登记库为空，请先在生成视频时勾选「数字指纹」完成登记。",
                "query": fp, "results": []}

    results = []
    for entry in entries:
        cmp = _compare(entry, fp)
        cmp["wm_id"] = entry.get("wm_id", "")
        cmp["source"] = entry.get("source", "")
        cmp["duration"] = entry.get("duration")
        results.append(cmp)

    results.sort(key=lambda r: (r["audio_score"], r["video_score"]), reverse=True)
    best = results[0]

    matched = (
        best["audio_score"] >= AUDIO_MATCH_MIN
        or best["video_score"] >= VIDEO_MATCH_MIN
    )
    return {"matched": matched, "best": best, "query": fp, "results": results}
