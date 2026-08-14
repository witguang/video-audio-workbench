# 视频处理工作台 — 项目说明

Tkinter GUI + FFmpeg + Pillow 的视频/音频处理工具（Windows 11，Git Bash）。主入口 `workbench_app.py`，处理逻辑在 `mode1_ffmpeg.py`。完整功能描述见 `readme`。

## 核心文件
- `workbench_app.py` — 主 GUI（类 `VideoWorkbenchApp`），含 ttk 深度定制主题（clam + `_build_style()`）
- `mode1_ffmpeg.py` — 处理管线：`mode1_process(...)`（音频提取 / 卡片歌词视频合成 / R2 下载 / 范围剪辑 / 数字指纹）。卡片模式为「Logic A」：先把音频裁到所选范围、字幕整体平移，只渲染这一段；渲染时长用显式 `-t` 硬卡（`_clip_duration_seconds`），避免 `-shortest` 尾帧过冲
- `card_render.py` — 卡片歌词渲染：ASS 生成、静态层、背景合成、画质预设；顶部常量 `SCROLL_LAYOUT`、`VIDEO_FPS/VIDEO_PRESET/VIDEO_CRF/AUDIO_BITRATE` 可调
- `fingerprint.py` — 内容感知指纹（音频地标哈希 + 视频 dHash），登记/验证盗版
- `lyric_translate.py` — 双语字幕 API 翻译（DeepSeek/通义/Kimi/智谱/OpenAI）
- `sph_publish.py` — 发布到微信视频号（Playwright 驱动 channels.weixin.qq.com，个人号无官方 API）；选择器集中在 `SEL_*` 常量，平台改版时只改这里
- `ffmpeg_utils.py` — `get_ffmpeg_path()` / `run_ffmpeg()` / `probe_media_duration()`（探测源时长，供范围剪辑 `End` 场景）；`make_preview.py`、`create_shortcut.py` 为辅助脚本

## 运行
```
python workbench_app.py
```
修改后必跑语法校验：`python -m py_compile workbench_app.py`，并做完整初始化 smoke test（见下）。

## ⚠️ 关键约束（不可违反）
- `app_settings.json` 含用户实时 API key（DeepSeek 等），**gitignored，绝不 stage/commit**。
- 提交一律**显式 `git add <files>`，禁用 `git add -A` / `git add .`**。
- `fingerprint_registry.json` 是本地登记库，不入库。

## UI 主题「黑胶金标 / Vinyl & Gold」（已应用，改动控件时保持风格一致）
- 色板：`PAPER=#f6f2ea` 暖象牙底、`CARD=#fffdf8` 奶油卡、`INK=#1f1a15`、`BODY=#4a4238`、`MUTED=#82796a`、`BRASS=#b5862c` 黄铜金主色、`GREEN=#2f5d50` 复古表头绿、`HAIR=#e7dfd0`
- 控件：按钮三态（active/pressed/disabled）、输入框聚焦黄铜描边、扁平滚动条
- 勾选框指示器：PIL 绘制 16×16 圆点（未选=空心描边 `MUTED`，选中=实心 `BRASS`），`style.element_create("CB.dot","image",...)` + 自定义 `TCheckbutton` layout；图片必须挂在 `self._cb_off_img/_cb_on_img` 防 GC
- `Body.TRadiobutton` 是带圆点的单选样式；有正常可见的 `Body.TCheckbutton`

## 当前进度快照（2026-08-14）
- 已完成：移除顶部提示/信息模块；黑胶金标全面美化；底部「输出字段」（右键复制）；勾选框 ×→圆点；双语字幕三选一、滚动歌词；视频号发布（`sph_publish.py`）
- 已修复（本次）：卡片模式改为 Logic A（只渲染所选范围+字幕平移，解决大范围「卡死」）；`-t` 硬卡渲染时长（解决 `-shortest` 尾帧过冲）；bat 中文乱码（移除 `chcp 65001`/`PYTHONUTF8=1`）
- 已统一：处理范围改为**单项**（音频/视频/字幕共用，以视频范围为准）——`mode1_ffmpeg` 的 `eff_start/eff_end` 统一驱动导出音频与视频，UI 第 3 步只留一行（`video_start_var/video_end_var`），`start_var/end_var/reset_audio_clip` 已移除，load_settings 兼容旧版 start/end 迁移
- 已备份：`D:/Backup/video_merge_tools_20260813/`（已刷新为当前状态，排除 cookie/指纹库）
- 未提交：本批改动仍未 commit。提交前跑一遍完整初始化 smoke test。

## 待办
1. 改动仍未 commit（备份已做，commit 待用户确认；提交时显式 `git add <files>`）

## 改动后自检清单
- `python -m py_compile workbench_app.py`（以及改过的模块，含 `sph_publish.py`）
- smoke test：`python -c "import tkinter as tk; import workbench_app as w; r=tk.Tk(); r.withdraw(); a=w.VideoWorkbenchApp(r); print('OK 初始化成功'); r.destroy()"`（在项目目录跑，会用真实 `app_settings.json`）
- 涉及视频号发布：`python sph_publish.py --check`（exit 0=登录有效）；`--login` 扫码、`--video <mp4> --mode semi` 半自动发布（详见 sph_publish.py 顶部说明）
- 若涉及 `mode1_process` 参数透传，检查 `workbench_app.py` 所有调用点（`_run_single` / `_run_batch`）签名一致
