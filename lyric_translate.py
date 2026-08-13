# -*- coding: utf-8 -*-
"""歌词中文翻译模块：调用各家 LLM 官方 Chat Completions API 逐行翻译英文歌词。

统一使用 OpenAI 兼容的 ``/chat/completions`` 接口（各家官方均已提供），
因此所有 provider 共用同一套请求/解析逻辑，接入新厂商只需在 PROVIDERS 表里
补一条 base_url + 默认 model。

当前已实现的官方接口（GUI 下拉可直接选择）：
  deepseek   https://api.deepseek.com/chat/completions          deepseek-chat
  qwen       https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions  qwen-plus
  moonshot   https://api.moonshot.cn/v1/chat/completions        moonshot-v1-8k
  zhipu      https://open.bigmodel.cn/api/paas/v4/chat/completions               glm-4-flash
  openai     https://api.openai.com/v1/chat/completions         gpt-4o-mini

key 可写在界面里（存入本机 app_settings.json），也可设对应环境变量，
环境变量优先级低于界面输入。
"""
import hashlib
import json
import os
import re
import urllib.error
import urllib.request

# 翻译缓存目录：同一首歌词只调一次 API，后续直接复用，省 token
CACHE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "cache", "lyric_zh")

PROVIDERS = {
    "deepseek": {
        "label": "DeepSeek",
        "base_url": "https://api.deepseek.com/chat/completions",
        "model": "deepseek-chat",
        "api_key_env": "DEEPSEEK_API_KEY",
    },
    "qwen": {
        "label": "通义千问",
        "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions",
        "model": "qwen-plus",
        "api_key_env": "DASHSCOPE_API_KEY",
    },
    "moonshot": {
        "label": "Kimi",
        "base_url": "https://api.moonshot.cn/v1/chat/completions",
        "model": "moonshot-v1-8k",
        "api_key_env": "MOONSHOT_API_KEY",
    },
    "zhipu": {
        "label": "智谱 GLM",
        "base_url": "https://open.bigmodel.cn/api/paas/v4/chat/completions",
        "model": "glm-4-flash",
        "api_key_env": "ZHIPU_API_KEY",
    },
    "openai": {
        "label": "OpenAI",
        "base_url": "https://api.openai.com/v1/chat/completions",
        "model": "gpt-4o-mini",
        "api_key_env": "OPENAI_API_KEY",
    },
}

BATCH_SIZE = 50  # 每批翻译行数，避免单次请求过长


def _emit(logger, message: str):
    print(message)
    if logger:
        logger(message)


def _cache_key(lines, provider, base_url, model) -> str:
    """翻译缓存键：接口 + 模型 + 英文歌词全文（翻译只由这三者决定）。"""
    digest = hashlib.sha256()
    digest.update(f"{provider}\n{base_url}\n{model}\n".encode("utf-8"))
    for line in lines:
        digest.update((line + "\n").encode("utf-8"))
    return digest.hexdigest()


def _cache_load(key):
    path = os.path.join(CACHE_DIR, key + ".zh")
    try:
        with open(path, "r", encoding="utf-8") as f:
            return [ln.rstrip("\n") for ln in f.read().splitlines()]
    except (OSError, UnicodeDecodeError):
        return None


def _cache_store(key, zh_lines):
    try:
        os.makedirs(CACHE_DIR, exist_ok=True)
        with open(os.path.join(CACHE_DIR, key + ".zh"), "w", encoding="utf-8") as f:
            f.write("\n".join(zh_lines))
    except OSError:
        pass  # 缓存写失败不影响主流程


def clear_translation_cache():
    """删除全部翻译缓存，下次处理会重新调用 API 翻译。"""
    if os.path.isdir(CACHE_DIR):
        for name in os.listdir(CACHE_DIR):
            if name.endswith(".zh"):
                try:
                    os.remove(os.path.join(CACHE_DIR, name))
                except OSError:
                    pass


def translate_lines(lines, api_key="", provider="deepseek", base_url="", model="",
                    logger=None, use_cache=True) -> list:
    """把英文歌词逐行翻译成中文，返回与 lines 等长的中文行列表。

    - api_key：界面输入的 key；留空时回退到 PROVIDERS 里的环境变量。
    - 分批调用 chat/completions，携带行号要求逐行返回「行号. 中文」，
      解析后按行号归位；模型漏译/多译的行以空串兜底，保证下标与英文行一一对应。
    - use_cache=True（默认）时，以「接口+模型+英文歌词全文」为键缓存结果，
      同一首歌重复处理不再调用 API（省 token）；英文歌词或接口/模型变化自动重新翻译。
    - API key 缺失、网络失败或响应无法解析时抛异常，由调用方决定失败策略。
    """
    if not lines:
        return []
    if provider not in PROVIDERS:
        raise ValueError(f"未知翻译服务: {provider}")
    cfg = PROVIDERS[provider]
    api_key = (api_key or "").strip() or os.environ.get(cfg["api_key_env"], "")
    if not api_key:
        raise ValueError(f"未配置 {cfg['label']} API Key（请在界面填写或设置环境变量 {cfg['api_key_env']}）")
    base_url = (base_url or "").strip() or cfg["base_url"]
    model = (model or "").strip() or cfg["model"]

    if use_cache:
        key = _cache_key(lines, provider, base_url, model)
        cached = _cache_load(key)
        if cached is not None and len(cached) == len(lines):
            _emit(logger, f"命中歌词翻译缓存（{len(cached)} 行），跳过 API 调用。")
            return cached

    zh = {}
    for start in range(0, len(lines), BATCH_SIZE):
        batch = lines[start:start + BATCH_SIZE]
        _emit(logger, f"正在调用 {cfg['label']} 翻译歌词第 {start + 1}-{start + len(batch)} 行...")
        content = _chat_completion(base_url, api_key, model, _build_messages(batch))
        zh.update(_parse_translated_response(content, len(batch), start))
    result = [zh.get(i, "") for i in range(len(lines))]
    if use_cache:
        _cache_store(key, result)
    return result


def _build_messages(batch: list) -> list:
    numbered = "\n".join(f"{idx + 1}. {line}" for idx, line in enumerate(batch))
    return [
        {"role": "system", "content": (
            "你是专业的歌词翻译助手。把用户提供的英文歌词逐行翻译成通顺自然的中文，"
            "严格保持行数与输入一致，每一行只输出对应中文，格式为「行号. 中文」，"
            "不要输出时间戳、序号说明、英文原文、注释或任何解释。"
            "若某行无法翻译，输出「行号. 」留空。"
        )},
        {"role": "user", "content": numbered},
    ]


def _chat_completion(base_url: str, api_key: str, model: str, messages: list) -> str:
    body = json.dumps({
        "model": model,
        "messages": messages,
        "temperature": 0.6,
        "max_tokens": 4096,
    }).encode("utf-8")
    req = urllib.request.Request(
        base_url, data=body, method="POST",
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = exc.read().decode("utf-8", "ignore")[:300]
        except Exception:
            pass
        raise RuntimeError(f"翻译 API 请求失败（HTTP {exc.code}）: {detail or exc.reason}") from exc
    except Exception as exc:
        raise RuntimeError(f"翻译 API 请求失败: {exc}") from exc
    try:
        return data["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as exc:
        raise RuntimeError(f"翻译 API 响应格式异常: {str(data)[:200]}") from exc


def _parse_translated_response(content: str, n: int, offset: int = 0) -> dict:
    """解析模型译文：优先识别「行号. 中文」，缺失时按顺序逐行兜底。

    返回 {全局行号: 中文}，行号范围 [offset, offset + n)，保证 len(结果) <= n。
    """
    out = {}
    for line in content.splitlines():
        m = re.match(r"^\s*(\d+)\s*[.、:]\s*(.*)$", line)
        if m:
            idx = int(m.group(1)) - 1
            if 0 <= idx < n and (offset + idx) not in out:
                out[offset + idx] = m.group(2).strip()
    if len(out) < max(1, n // 2):
        # 模型可能没带编号直接逐行输出，去掉可能的编号前缀后按顺序补位
        seq = [re.sub(r"^\s*\d+\s*[.、:]\s*", "", ln).strip() for ln in content.splitlines()]
        seq = [ln for ln in seq if ln]
        for k, txt in enumerate(seq):
            if k < n and (offset + k) not in out and txt:
                out[offset + k] = txt
    return out
