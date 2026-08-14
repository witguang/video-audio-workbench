# -*- coding: utf-8 -*-
"""微信视频号（channels.weixin.qq.com）自动发布。

个人号没有官方发布 API，这里用 Playwright 浏览器自动化驱动「视频号助手」网页端：
登录态存为 Playwright storage_state（Cookie），发布时自动上传视频、填标题/文案/话题。

⚠️ 风险提示（务必阅读）：
- 微信禁止未经授权自动化；全自动（headless）直发有封号风险，半自动（停在「发表」前）
  风险最低。发布前请在 GUI 中确认所选模式。
- 视频号网页版选择器随平台改版可能失效：所有选择器集中在 SEL_* 常量里，报
  SelectorNotFoundError 时只需更新这里的常量（这是本模块唯一的维护点）。

Playwright 懒加载：本模块可在未安装 playwright 时正常被 import（workbench_app.py 顶部
import 是安全的），首次使用请先：
    pip install playwright
    python -m playwright install chrome     # 复用系统 Chrome，无需下载自带浏览器
"""
import argparse
import os
import re
import time
from dataclasses import dataclass, field
from typing import Callable, Optional

APP_DIR = os.path.dirname(os.path.abspath(__file__))
CHANNELS_URL = "https://channels.weixin.qq.com/"
DEFAULT_COOKIE_FILE = os.path.join(APP_DIR, "sph_storage_state.json")
DUMP_FILE = os.path.join(APP_DIR, "sph_page_dump.txt")

# —— 选择器集中管理（唯一维护点）——
# 这些值需在实现后对着真实页面确认/校准；平台改版时只改这里即可。
# 优先使用文字/语义定位，比 CSS class 更抗改版。
SEL_LOGIN_MARKER = "text=发表视频"          # 登录后必然存在的元素（侧栏「发表视频」入口）
SEL_QR_LOGIN_MARKER = "text=扫码登录"       # 未登录时出现的元素（扫码登录页）
SEL_CREATE_BTN = "text=发表视频"            # 发布入口（点开上传弹窗）
SEL_FILE_INPUT = "input[type=file]"         # 弹窗内的文件选择 input
SEL_VIDEO_PREVIEW = "video"                 # 上传完成后出现的视频预览元素（判定上传成功）
SEL_DESC_EDITOR = "div[contenteditable]"    # 文案编辑器（可编辑区域，含 plaintext-only 等）
SEL_SUCCESS_MARKER = "text=已发表"          # 发表成功后的确认文案（全自动模式等待）

# 上传等待超时（视频较大时转码较久）
_UPLOAD_TIMEOUT_MS = 180_000
_NAV_TIMEOUT_MS = 60_000
# 全自动点击「发表」后等待成功提示
_PUBLISH_TIMEOUT_MS = 120_000


class SphError(Exception):
    """sph_publish 所有错误的基类。"""


class PlaywrightNotInstalledError(SphError):
    """未安装 playwright（或未安装浏览器内核）。"""


class CookieExpiredError(SphError):
    """登录态失效/缺失（微信 Cookie 约 7-30 天过期）。"""


class SelectorNotFoundError(SphError):
    """页面结构与脚本预期不符（平台改版或流程偏离）。"""


@dataclass
class PublishFields:
    """由输出字段拆解出的视频号发布字段。"""
    title: str                  # 标题：「#」前内容，截断 ≤20 字
    short_title: str = ""       # 短标题：由标题派生，6-16 字（系统也可能自动生成）
    description: str = ""       # 完整文案（含 #话题）
    tags: list = field(default_factory=list)   # 话题列表（已剥离 #）


def build_publish_fields(output_field: str) -> PublishFields:
    """把 app 的输出字段（'歌名 - 歌手 #话题1 #话题2'）拆成视频号发布字段。纯函数。"""
    text = (output_field or "").strip()
    # 标题 = 第一个 # 之前的内容（去尾部空白）
    hash_idx = text.find("#")
    head = (text[:hash_idx] if hash_idx != -1 else text).strip()

    # 话题：匹配所有 #xxx（去空格），剥离 # 符号，去重保序
    tags = []
    for token in re.findall(r"#\S+", text):
        tag = token.lstrip("#").strip()
        if tag and tag not in tags:
            tags.append(tag)

    # 标题截断 ≤20 字（视频号标题字数上限），超长用 … 收尾
    title = head if len(head) <= 20 else head[:20] + "…"

    # 短标题 6-16 字：取标题中段；过短时从文案补齐，过长时截取
    short_title = title.strip()
    if len(short_title) < 6:
        # 标题过短时补后面的文案（去掉话题）
        body = text
        if hash_idx != -1:
            body = text[:hash_idx]
        short_title = (short_title + " " + body.strip()).strip()
        short_title = short_title.replace("\n", " ")
    if len(short_title) > 16:
        short_title = short_title[:16]
    short_title = short_title or title or "视频"

    return PublishFields(title=title, short_title=short_title,
                         description=text, tags=tags)


def _output_field_from_stem(stem: str) -> str:
    """由生成视频文件名（歌名在前 歌手在后）生成输出字段：
    {歌名 - 歌手 #歌名去空格 #各歌手去空格}；多歌手按逗号拆分、各生成一个话题标签。"""
    clean = re.sub(r"[\(\[][^\]\)]*[\)\]]", "", stem).strip()
    parts = [p.strip() for p in clean.split("-") if p.strip()]
    parts = [p for p in parts if not p.isdigit()]

    if len(parts) >= 2:
        song, artist = parts[0], parts[1]      # 生成视频名：歌名在前 歌手在后
    elif len(parts) == 1:
        song, artist = parts[0], ""
    else:
        song, artist = stem.strip(), ""

    tags = []
    if song:
        tags.append("#" + re.sub(r"\s+", "", song))
    if artist:
        for a in artist.split(","):
            a = a.strip()
            if a:
                tags.append("#" + re.sub(r"\s+", "", a))

    head = f"{song} - {artist}" if artist else song
    return " ".join([head] + tags)


# —— 内部工具 ——

def _get_playwright():
    """懒加载 playwright；未安装时抛 PlaywrightNotInstalledError。"""
    try:
        from playwright.sync_api import sync_playwright
        return sync_playwright
    except ImportError:
        raise PlaywrightNotInstalledError(
            "未安装 Playwright。请先运行：\n"
            "pip install playwright\n"
            "python -m playwright install chrome") from None


def _launch_browser(p, headless: bool, log_cb):
    """启动浏览器：优先系统 Chrome（风控更友好），失败回退自带 Chromium。"""
    args = ["--disable-blink-features=AutomationControlled"]
    try:
        return p.chromium.launch(channel="chrome", headless=headless, args=args)
    except Exception as exc:  # 系统 Chrome 不可用（未装/路径异常）时回退
        if log_cb:
            log_cb(f"未找到系统 Chrome（{exc}），回退使用 Playwright 自带 Chromium。")
        return p.chromium.launch(headless=headless, args=args)


def _new_context(browser, cookie_path: str):
    """创建带反检测 + 可选登录态的上下文。cookie_path 为 None 或无效时不再加载。"""
    ctx_opts = {"locale": "zh-CN", "viewport": {"width": 1280, "height": 800}}
    if cookie_path and os.path.isfile(cookie_path):
        try:
            ctx_opts["storage_state"] = cookie_path
        except Exception:
            pass  # 状态文件损坏则忽略，走未登录
    context = browser.new_context(**ctx_opts)
    # 屏蔽自动化标记，降低被识别为机器人的概率
    context.add_init_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined})")
    return context


def _save_state(context, cookie_path: str, log_cb):
    """保存 storage_state；失败不致命（仅刷新有效期失败）。"""
    try:
        context.storage_state(path=cookie_path)
        if log_cb:
            log_cb(f"已刷新登录态缓存：{cookie_path}")
    except Exception as exc:
        if log_cb:
            log_cb(f"保存登录态失败（忽略）：{exc}")


def _goto(page, url: str, log_cb=None, timeout_ms: int = _NAV_TIMEOUT_MS):
    """打开页面：加载异常（超时/网络错误）不中断流程，记录后继续。

    页面 JS 错误或网络抖动常导致 goto 抛错，但 DOM 可能已可用；真正的成败由
    后续 wait_for_selector 判断，这里只负责“尽量把页面打开”。
    """
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
    except Exception as exc:
        if log_cb:
            log_cb(f"页面加载异常（{type(exc).__name__}：{exc}），继续检查页面…")
        try:
            page.wait_for_timeout(3_000)  # 给渲染留缓冲
        except Exception:
            pass


def _page_login_status(page, log_cb) -> bool:
    """探测页面登录态：登录返回 True，未登录返回 False；都不匹配抛 SelectorNotFoundError。"""
    try:
        page.wait_for_selector(SEL_LOGIN_MARKER, timeout=_NAV_TIMEOUT_MS)
        return True
    except Exception:
        pass
    try:
        page.wait_for_selector(SEL_QR_LOGIN_MARKER, timeout=_NAV_TIMEOUT_MS)
        return False
    except Exception:
        raise SelectorNotFoundError(
            f"无法判断登录状态：未找到登录标记({SEL_LOGIN_MARKER}) 或未登录标记"
            f"({SEL_QR_LOGIN_MARKER})。当前地址：{page.url}。平台可能改版，请检查 SEL_* 常量。") from None


# —— 公开 API ——

def get_cookie(cookie_path: str = DEFAULT_COOKIE_FILE,
               log_cb: Optional[Callable[[str], None]] = None) -> None:
    """有头扫码登录并保存登录态。

    打开系统 Chrome 到视频号助手，用户微信扫码，登录成功后将 Cookie 写入 cookie_path。
    """
    sync_playwright = _get_playwright()
    if log_cb:
        log_cb("正在打开浏览器，请用微信扫描二维码登录视频号…")
    p = sync_playwright().start()
    try:
        browser = _launch_browser(p, headless=False, log_cb=log_cb)
        try:
            context = _new_context(browser, cookie_path)
            page = context.new_page()
            _goto(page, CHANNELS_URL, log_cb)
            # 等待登录完成（用户扫码后自动跳转）
            page.wait_for_selector(SEL_LOGIN_MARKER, timeout=300_000)
            _save_state(context, cookie_path, log_cb)
            if log_cb:
                log_cb("登录成功，已保存登录态。")
        finally:
            browser.close()
    finally:
        p.stop()


def check_cookie(cookie_path: str = DEFAULT_COOKIE_FILE,
                 log_cb: Optional[Callable[[str], None]] = None) -> bool:
    """检查登录态是否有效。有效返回 True；失效返回 False；结构异常抛 SelectorNotFoundError。"""
    sync_playwright = _get_playwright()
    p = sync_playwright().start()
    try:
        browser = _launch_browser(p, headless=True, log_cb=log_cb)
        try:
            context = _new_context(browser, cookie_path)
            page = context.new_page()
            _goto(page, CHANNELS_URL, log_cb)
            return _page_login_status(page, log_cb)
        finally:
            browser.close()
    finally:
        p.stop()


def _editable_near(page, label_text: str):
    """按标签文字定位其关联的可编辑控件（input/textarea/contenteditable）。

    先试 get_by_label（label/aria 关联），再向上爬父容器找可编辑元素。找不到返回 None。
    """
    # 1) label 关联（<label for> / aria-label / aria-labelledby / 包裹式 label）
    try:
        loc = page.get_by_label(label_text).first
        if loc.count() and loc.is_visible(timeout=2_000):
            try:
                if loc.is_editable():
                    return loc
            except Exception:
                pass
    except Exception:
        pass
    # 2) 文案定位后向上爬：找包含该标签文本的容器里的可编辑元素
    try:
        node = page.get_by_text(label_text, exact=False).first
        if node.is_visible(timeout=2_000):
            for _ in range(4):
                node = node.locator("xpath=..")
                for cand in node.locator("textarea, input:not([type=hidden]), [contenteditable], [role='textbox']").all():
                    try:
                        if cand.is_visible():
                            return cand
                    except Exception:
                        continue
    except Exception:
        pass
    return None


def _fill_near_label(page, label_texts, value, log_cb, label_hint=None) -> bool:
    """按标签文字填入可编辑控件；找不到返回 False。"""
    for label_text in label_texts:
        editor = _editable_near(page, label_text)
        if editor is None:
            continue
        try:
            editor.click(timeout=3_000)
            editor.fill(value)
            if log_cb:
                log_cb(f"{label_hint or label_text}已填写：{value}")
            return True
        except Exception as exc:
            if log_cb:
                log_cb(f"{label_text}填写失败：{exc}")
            return False
    return False


_DROPDOWN_SELS = ("[class*='option']", "[role='option']", "[class*='item']",
                  "[class*='dropdown'] button", "[class*='select'] button",
                  "[class*='list'] button")


def _click_first_visible(page, locator) -> bool:
    """遍历所有匹配元素，点第一个可见的（get_by_text .first 可能拿到隐藏重复节点）。"""
    for el in locator.all():
        try:
            if el.is_visible(timeout=500):
                el.click()
                return True
        except Exception:
            continue
    return False


def _click_first_dropdown_option(page, start_index: int = 0) -> bool:
    """兜底：点下拉里第 start_index+1 个可见的选项元素（start_index=1 表示跳过第一个，
    用于位置这类下拉第一行是搜索框的情形）。"""
    for sel in _DROPDOWN_SELS:
        idx = 0
        try:
            for el in page.locator(sel).all():
                try:
                    if el.is_visible(timeout=400):
                        if idx >= start_index:
                            el.click()
                            return True
                        idx += 1
                except Exception:
                    continue
        except Exception:
            continue
    return False


def _dismiss_blocking_dialog(page, log_cb) -> bool:
    """关闭当前可能出现的拦截弹窗（如草稿确认框「是否保留此次编辑」）。

    这类弹窗盖在表单上方，会让后续点击「元素可见但被拦截」而超时。
    优先点非破坏性的「取消/不保存/关闭」，避免影响已填写的表单。返回是否关掉了弹窗。
    """
    dlg = None
    try:
        for el in page.locator("[class*='weui-desktop-dialog'], .common-dialog, [role='dialog']").all():
            try:
                if el.is_visible(timeout=500):
                    dlg = el
                    break
            except Exception:
                continue
    except Exception:
        pass
    if dlg is None:
        return False

    for btn_text in ("取消", "不保存", "关闭", "知道了"):
        try:
            btn = dlg.get_by_role("button", name=btn_text, exact=False).last
            if btn.is_visible(timeout=600):
                btn.click()
                page.wait_for_timeout(400)
                if log_cb:
                    log_cb(f"已关闭拦截弹窗（点击「{btn_text}」）。")
                return True
        except Exception:
            continue
    # 兜底：点弹窗右上角关闭图标
    try:
        close = dlg.locator("[class*='close'], [aria-label='关闭']").first
        if close.is_visible(timeout=500):
            close.click()
            page.wait_for_timeout(300)
            if log_cb:
                log_cb("已关闭拦截弹窗（点击关闭图标）。")
            return True
    except Exception:
        pass

    if log_cb:
        log_cb("发现拦截弹窗但未找到可关闭按钮，跳过。")
    return False


def _fill_short_title(page, short_title: str, log_cb):
    """填短标题（placeholder「填写短标题有机会获得更多流量」）。"""
    loc = page.locator("input[placeholder*='短标题']").first
    try:
        if loc.is_visible(timeout=3_000):
            loc.fill(short_title)
            if log_cb:
                log_cb(f"短标题已填写：{short_title}")
            return True
    except Exception as exc:
        if log_cb:
            log_cb(f"短标题填写失败：{exc}")
    return False


def _select_collection(page, name: str, log_cb):
    """添加到合集：点触发器打开下拉后，选项是按钮直接列出（无需搜索），点目标合集。
    找不到目标则点下拉里第一个选项按钮（合集只有 Music 一个时它就是目标）。
    找不到下拉/选项则跳过（非必填）。"""
    if not name:
        return
    try:
        # 1) 打开合集下拉。触发器是 <div class="display-text">选择合集</div>（点击后才出现选项）；
        #    注意「添加到合集」只是 label（<div class="label">），点了不会打开下拉。
        opened = False
        for trigger, exact in (("选择合集", True), ("选择合集", False),
                               ("添加到合集", False), ("添加合集", False),
                               ("未选择", False), ("合集", False)):
            try:
                t = page.get_by_text(trigger, exact=exact).first
                if t.is_visible(timeout=1_500):
                    t.click()
                    opened = True
                    if log_cb:
                        log_cb(f"已点击合集下拉触发器「{trigger}」。")
                    break
            except Exception:
                continue
        if not opened:
            if log_cb:
                log_cb("未找到合集下拉触发器，跳过。")
            return
        page.wait_for_timeout(1_500)  # 等下拉选项渲染

        # 2) 点选目标合集（不搜索——下拉选项是按钮，直接列出；
        #    遍历所有匹配、点第一个可见的，避免 .first 命中隐藏重复节点）
        clicked = _click_first_visible(page, page.get_by_text(name, exact=False))
        # 3) 兜底：点下拉里第一个可见选项按钮
        if not clicked:
            clicked = _click_first_dropdown_option(page)
        page.wait_for_timeout(500)
        if clicked:
            if log_cb:
                log_cb(f"已添加到合集：{name}")
        else:
            if log_cb:
                log_cb(f"已打开合集下拉但未找到「{name}」（跳过）。")
    except Exception as exc:
        if log_cb:
            log_cb(f"添加到合集失败（跳过）：{exc}")


def _select_location(page, log_cb):
    """位置：规格为「不显示位置」。

    位置框是下拉控件：点开前的可见触发器是「位置」行里的 .display-text（或 label 本身），
    展开后第一行是「搜索附近位置」搜索框（是 input，不在选项选择器内，不会被点到），
    随后是选项按钮，「不显示位置」就是第一个选项按钮。
    选完会读回显示值核实真正选中了什么；误选会自动重试。
    """
    try:
        # 1) 打开位置下拉：点「位置」label 所在行里的 .display-text；没有则点 label 本身
        label = page.locator("div.label", has_text="位置").first
        if not label.is_visible(timeout=3_000):
            if log_cb:
                log_cb("未找到「位置」行（跳过）。")
            return
        row = label.locator("xpath=..")
        display = row.locator(".display-text")

        def _read_display():
            try:
                return display.first.inner_text().strip()
            except Exception:
                return ""

        try:
            trigger = display.first
            if trigger.is_visible(timeout=2_000):
                trigger.click()
            else:
                label.click()
        except Exception:
            label.click()
        page.wait_for_timeout(1_500)  # 等下拉选项渲染

        # 2) 优先精确匹配「不显示位置」（遍历点第一个可见的，避免 .first 命中隐藏重复节点）
        clicked = _click_first_visible(page, page.get_by_text("不显示位置", exact=False))
        if not clicked:
            clicked = _click_first_visible(page, page.get_by_text("不显示位置", exact=True))
        # 3) 兜底：点下拉里第一个可见选项按钮。搜索框是 input 不在选项选择器里，
        #    所以第一项就是「不显示位置」，不再跳过第一项（上次 start_index=1 点到了第二项=别的城市）
        if not clicked:
            clicked = _click_first_dropdown_option(page, start_index=0)
        page.wait_for_timeout(500)

        if not clicked:
            if log_cb:
                log_cb("位置：下拉已打开但未找到「不显示位置」选项（跳过）。")
            return

        # 4) 读回显示值核实：确认真正选中了「不显示位置」
        actual = _read_display()
        if "不显示位置" in actual:
            if log_cb:
                log_cb(f"位置：已选择「不显示位置」（显示值：{actual}）。")
            return

        # 误选了别的：重新打开下拉，重试精确匹配
        if log_cb:
            log_cb(f"位置：误选了「{actual}」，正在重试选择「不显示位置」…")
        try:
            trigger = display.first
            if trigger.is_visible(timeout=2_000):
                trigger.click()
                page.wait_for_timeout(800)
                _click_first_visible(page, page.get_by_text("不显示位置", exact=False))
                page.wait_for_timeout(500)
                actual = _read_display()
                if "不显示位置" in actual:
                    if log_cb:
                        log_cb(f"位置：重试成功，已选择「不显示位置」（显示值：{actual}）。")
                else:
                    if log_cb:
                        log_cb(f"位置：重试后显示仍为「{actual}」，请手动在浏览器中确认。")
        except Exception:
            pass
    except Exception as exc:
        if log_cb:
            log_cb(f"位置设置失败（跳过）：{exc}")


def _declare_original(page, log_cb):
    """声明原创（用户反馈：是个选择框，点击之后才会出现弹窗）。

    流程：点「声明后，作品将展示原创标记，有机会获得广告收入。」选择框 → 弹窗出现
    → 勾选「我已阅读并…」→ 点弹窗内「声明原创」确认。
    """
    try:
        # 0) 先关掉可能出现的拦截弹窗（如草稿确认框），否则勾选框被盖住会点击超时
        _dismiss_blocking_dialog(page, log_cb)
        # 1) 勾选声明原创选择框（外层 ant-checkbox-wrapper，文案「声明后，作品将展示原创标记…」；
        #    点击后弹出声明原创窗口，disabled 的「声明原创」按钮随之启用）
        try:
            cb = page.locator("label.ant-checkbox-wrapper", has_text="声明后").first
            if not cb.is_visible(timeout=3_000):
                if log_cb:
                    log_cb("未找到「声明原创」选择框（跳过）。")
                return
            cb.click(timeout=5_000)
        except Exception as exc:
            if log_cb:
                log_cb(f"「声明原创」选择框点击失败（可能未启用）：{exc}")
            return
        page.wait_for_timeout(1_500)

        # 2) 弹窗内勾选「我已阅读并…」；找不到则回退勾选任意 ant-checkbox
        checked = False
        try:
            agree = page.locator("label.ant-checkbox-wrapper", has_text="我已阅读并").first
            if agree.is_visible(timeout=3_000):
                agree.click()
                page.wait_for_timeout(500)
                checked = True
        except Exception:
            pass
        if not checked:
            for c in page.locator(".ant-checkbox-wrapper input.ant-checkbox-input").all():
                try:
                    if c.is_visible() and not c.is_checked():
                        c.click()
                        page.wait_for_timeout(500)
                        checked = True
                        break
                except Exception:
                    continue

        # 3) 确认：弹窗内「声明原创」按钮（或 确定/确认/同意）
        for label in ("声明原创", "确定", "确认", "同意"):
            try:
                b = page.get_by_role("button", name=label).last
                if b.is_visible(timeout=1_500):
                    b.click()
                    page.wait_for_timeout(500)
                    if log_cb:
                        log_cb(f"已声明原创（{label}确认）。")
                    return
            except Exception:
                continue
        if log_cb:
            log_cb("声明原创：弹窗已打开但未找到确认按钮（跳过确认）。")
    except Exception as exc:
        if log_cb:
            log_cb(f"声明原创处理失败（跳过）：{exc}")


def publish(video_path: str,
            title: str,
            description: str,
            tags: list,
            mode: str = "semi",
            cookie_path: str = DEFAULT_COOKIE_FILE,
            log_cb: Optional[Callable[[str], None]] = None,
            on_ready: Optional[Callable[[], None]] = None,
            short_title: Optional[str] = None,
            collection: str = "Music",
            declare_original: bool = True) -> None:
    """上传视频并按视频号发布页规格填表，可选自动发表。

    参数：
        mode: "semi" 半自动（填完后停在「发表」前，浏览器保持打开由用户点击）；
              "auto" 全自动（headless，程序点击「发表」，有封号风险）。
        on_ready: semi 模式填写完成瞬间回调（用于 GUI 弹提示/恢复按钮）。
        short_title: 短标题（默认 "音乐music"）；collection: 添加到合集（默认 "Music"）。
        declare_original: 是否自动勾选「声明原创」（含弹窗确认）。
    异常：CookieExpiredError / SelectorNotFoundError / PlaywrightNotInstalledError / SphError。
    """
    mode = mode or "semi"
    headless = (mode == "auto")

    if not os.path.isfile(video_path):
        raise SphError(f"视频文件不存在：{video_path}")

    sync_playwright = _get_playwright()
    fields = PublishFields(title=title, description=description, tags=tags or [])
    if not fields.tags:
        fields.tags = build_publish_fields(description).tags
    # 短标题按用户规格固定填「音乐music」（不沿用派生标题）
    fields.short_title = short_title or "音乐music"

    if log_cb:
        log_cb(f"模式：{'全自动（headless）' if headless else '半自动（停在发表前）'}")
        log_cb(f"视频：{video_path}")

    p = sync_playwright().start()
    try:
        browser = _launch_browser(p, headless=headless, log_cb=log_cb)
        try:
            context = _new_context(browser, cookie_path)
            page = context.new_page()
            _goto(page, CHANNELS_URL, log_cb)

            logged_in = _page_login_status(page, log_cb)
            if not logged_in:
                if headless:
                    raise CookieExpiredError(
                        "登录态已过期（微信 Cookie 约 7-30 天失效）。\n"
                        "全自动模式无法扫码，请先在 app 中点击「重新登录视频号」扫码，"
                        "或改用半自动模式。")
                if log_cb:
                    log_cb("登录态失效，即将弹出浏览器扫码登录…")
                context.close()
                browser.close()
                # 半自动：有头重新登录后继续
                browser = _launch_browser(p, headless=False, log_cb=log_cb)
                context = _new_context(browser, None)
                page = context.new_page()
                _goto(page, CHANNELS_URL, log_cb)
                page.wait_for_selector(SEL_LOGIN_MARKER, timeout=300_000)
                if log_cb:
                    log_cb("已重新登录。")

            # 打开发布弹窗
            try:
                page.locator(SEL_CREATE_BTN).first.click(timeout=_NAV_TIMEOUT_MS)
            except Exception as exc:
                raise SelectorNotFoundError(
                    f"未找到发布入口({SEL_CREATE_BTN})：{exc}。当前地址：{page.url}。"
                    f"平台可能改版，请检查 sph_publish.py 中 SEL_* 常量。") from exc

            # 上传视频
            try:
                page.locator(SEL_FILE_INPUT).set_input_files(video_path, timeout=_NAV_TIMEOUT_MS)
            except Exception as exc:
                raise SelectorNotFoundError(
                    f"未找到文件上传框({SEL_FILE_INPUT})：{exc}。平台可能改版。") from exc
            if log_cb:
                log_cb("视频上传中，请等待转码完成…")
            try:
                page.wait_for_selector(SEL_VIDEO_PREVIEW, timeout=_UPLOAD_TIMEOUT_MS)
            except Exception:
                page.wait_for_timeout(10_000)  # 无 video 预览则保守等待
            page.wait_for_timeout(2_000)

            # 填视频描述（文案 = output_field）
            if not _fill_near_label(page, ("视频描述", "描述"), fields.description, log_cb, label_hint="视频描述"):
                try:
                    desc = page.locator(SEL_DESC_EDITOR).first
                    if desc.is_visible(timeout=3_000):
                        desc.click(timeout=_NAV_TIMEOUT_MS)
                        desc.fill(fields.description)
                        if log_cb:
                            log_cb(f"视频描述已填写：{fields.description}")
                    else:
                        if log_cb:
                            log_cb("未找到视频描述编辑框（跳过）。")
                except Exception as exc:
                    if log_cb:
                        log_cb(f"视频描述填写失败：{exc}")

            # 填短标题（当前版视频号无独立「标题」字段，唯一标题类输入框就是短标题，
            # 因此不再单独填 title——placeholder「标题」会子串匹配到短标题框，覆盖 music）
            _fill_short_title(page, fields.short_title, log_cb)

            # 位置（规格：不显示位置）
            _select_location(page, log_cb)

            # 添加到合集
            _select_collection(page, collection, log_cb)

            # 声明原创（含弹窗确认）
            if declare_original:
                _declare_original(page, log_cb)

            # 链接 / 活动 / 定时发表 / 视频标注：按用户规格保持默认不操作

            _save_state(context, cookie_path, log_cb)

            if not headless:
                if log_cb:
                    log_cb("已填写完成。请在浏览器中手动点击「发表」完成发布，关闭窗口后脚本结束。")
                if on_ready:
                    on_ready()
                # 保持浏览器存活直到用户关闭窗口
                while browser.is_connected():
                    time.sleep(1)
                if log_cb:
                    log_cb("浏览器已关闭，半自动发布流程结束。")
            else:
                if log_cb:
                    log_cb("即将自动点击「发表」…")
                try:
                    page.get_by_role("button", name="发表", exact=True).first.click(timeout=_NAV_TIMEOUT_MS)
                except Exception as exc:
                    raise SelectorNotFoundError(
                        f"未找到「发表」按钮：{exc}。平台可能改版。") from exc
                try:
                    page.wait_for_selector(SEL_SUCCESS_MARKER, timeout=_PUBLISH_TIMEOUT_MS)
                except Exception:
                    page.wait_for_timeout(8_000)  # 成功提示文案可能不同，保守等待
                _save_state(context, cookie_path, log_cb)
                if log_cb:
                    log_cb("已自动点击「发表」，全自动发布流程完成。")
        finally:
            try:
                browser.close()
            except Exception:
                pass
    finally:
        try:
            p.stop()
        except Exception:
            pass


def dump_publish_page(cookie_path: str = DEFAULT_COOKIE_FILE,
                      video_path: Optional[str] = None,
                      log_cb: Optional[Callable[[str], None]] = None) -> None:
    """打开发布页并打印所有可见交互元素（input/按钮/勾选框），用于校准 SEL_* 选择器。

    video_path 可选：指定后先上传视频再 dump（合集/声明原创等字段在上传后才出现）。
    """
    sync_playwright = _get_playwright()
    p = sync_playwright().start()
    try:
        browser = _launch_browser(p, headless=False, log_cb=log_cb)
        try:
            context = _new_context(browser, cookie_path)
            page = context.new_page()
            _goto(page, CHANNELS_URL, log_cb)
            if not _page_login_status(page, log_cb):
                raise CookieExpiredError("登录态无效，请先运行 --login。")
            page.locator(SEL_CREATE_BTN).first.click(timeout=_NAV_TIMEOUT_MS)
            if video_path:
                if not os.path.isfile(video_path):
                    raise SphError(f"视频文件不存在：{video_path}")
                page.locator(SEL_FILE_INPUT).set_input_files(video_path, timeout=_NAV_TIMEOUT_MS)
                if log_cb:
                    log_cb("视频上传中，请等待转码完成…")
                try:
                    page.wait_for_selector(SEL_VIDEO_PREVIEW, timeout=_UPLOAD_TIMEOUT_MS)
                except Exception:
                    page.wait_for_timeout(10_000)
                page.wait_for_timeout(2_000)
            else:
                page.wait_for_timeout(2_000)

            lines = ["===== 发布页交互元素清单 ====="]
            seen = set()
            for el in page.locator("input, textarea, [contenteditable], [role='textbox']").all():
                try:
                    tag = el.evaluate("e => e.tagName")
                    ph = el.get_attribute("placeholder") or ""
                    try:
                        val = el.input_value()
                    except Exception:
                        val = ""
                    key = (tag, ph)
                    if key in seen:
                        continue
                    seen.add(key)
                    html = el.evaluate("e => e.outerHTML")[:200]
                    lines.append(f"[输入] <{tag}> placeholder={ph!r} value={val!r}\n       html={html}")
                except Exception:
                    continue
            for el in page.locator("button, [role='button'], label").all():
                try:
                    t = el.inner_text().strip()
                    if t and len(t) <= 30 and t not in seen:
                        seen.add(t)
                        html = el.evaluate("e => e.outerHTML")[:160]
                        lines.append(f"[控件] {t!r} html={html}")
                except Exception:
                    continue
            for el in page.locator("[type='checkbox'], [role='checkbox']").all():
                try:
                    lines.append(f"[勾选] checked={el.is_checked()} html={el.evaluate('e => e.outerHTML')[:160]}")
                except Exception:
                    continue
            # 弹窗检测：打印当前可见的弹窗及其按钮（草稿确认框等拦截层）
            try:
                for el in page.locator("[class*='weui-desktop-dialog'], .common-dialog, [role='dialog']").all():
                    try:
                        if not el.is_visible():
                            continue
                        lines.append(f"[弹窗] html={el.evaluate('e => e.outerHTML')[:400]}")
                        for b in el.locator("button").all():
                            try:
                                t = b.inner_text().strip()
                                if t:
                                    lines.append(f"[弹窗按钮] {t!r} html={b.evaluate('e => e.outerHTML')[:160]}")
                            except Exception:
                                continue
                    except Exception:
                        continue
            except Exception:
                pass
            # 展开合集下拉，抓下拉选项结构（按钮/选项项）
            try:
                tt = page.get_by_text("选择合集", exact=True).first
                if tt.is_visible(timeout=2_000):
                    tt.click()
                    page.wait_for_timeout(1_500)
                    lines.append("----- 合集下拉展开后 -----")
                    for el in page.locator(
                            "button, [role='button'], [role='option'], [class*='option'], [class*='dropdown']").all():
                        try:
                            if not el.is_visible():
                                continue
                            t = el.inner_text().strip()
                            if not t or len(t) > 30:
                                continue
                            html = el.evaluate("e => e.outerHTML")[:180]
                            lines.append(f"[下拉选项] {t!r} html={html}")
                        except Exception:
                            continue
                    page.keyboard.press("Escape")  # 收起合集下拉，避免干扰位置下拉
                    page.wait_for_timeout(500)
            except Exception:
                pass
            # 展开位置下拉，抓「不显示位置」选项结构
            try:
                label_pos = page.locator("div.label", has_text="位置").first
                if label_pos.is_visible(timeout=2_000):
                    try:
                        trig_pos = label_pos.locator("xpath=..").locator(".display-text").first
                        if trig_pos.is_visible(timeout=1_500):
                            trig_pos.click()
                        else:
                            label_pos.click()
                    except Exception:
                        label_pos.click()
                    page.wait_for_timeout(1_500)
                    lines.append("----- 位置下拉展开后 -----")
                    for el in page.locator(
                            "button, [role='button'], [role='option'], [class*='option'], [class*='dropdown']").all():
                        try:
                            if not el.is_visible():
                                continue
                            t = el.inner_text().strip()
                            if not t or len(t) > 30:
                                continue
                            html = el.evaluate("e => e.outerHTML")[:180]
                            lines.append(f"[位置选项] {t!r} html={html}")
                        except Exception:
                            continue
                    page.keyboard.press("Escape")
                    page.wait_for_timeout(500)
            except Exception:
                pass
            # 关键词定位：打印含「合集/位置/声明/原创/添加」文字的可见元素（含 div，抓下拉触发器）
            for kw in ("合集", "位置", "声明", "原创", "添加"):
                try:
                    for el in page.get_by_text(re.compile(kw)).all():
                        try:
                            if not el.is_visible():
                                continue
                            t = el.inner_text().strip().replace("\n", "␤")
                            if not t or len(t) > 40:
                                continue
                            tag = el.evaluate("e => e.tagName")
                            html = el.evaluate("e => e.outerHTML")[:220]
                            lines.append(f"[关键词:{kw}] <{tag}> text={t!r} html={html}")
                        except Exception:
                            continue
                except Exception:
                    continue
            lines.append("===== 清单结束 =====")

            text = "\n".join(lines)
            try:
                with open(DUMP_FILE, "w", encoding="utf-8") as fh:
                    fh.write(text)
            except OSError as exc:
                if log_cb:
                    log_cb(f"写入 dump 文件失败：{exc}")
            for line in lines:
                print(line, flush=True)
            if log_cb:
                log_cb(f"清单已保存到：{DUMP_FILE}")
        finally:
            browser.close()
    finally:
        p.stop()


def _main():
    parser = argparse.ArgumentParser(description="微信视频号自动发布（Playwright）")
    parser.add_argument("--login", action="store_true", help="有头扫码登录并保存登录态")
    parser.add_argument("--check", action="store_true", help="检查登录态是否有效（exit 0=有效）")
    parser.add_argument("--video", help="要发布的视频文件路径")
    parser.add_argument("--mode", choices=["semi", "auto"], default="semi")
    parser.add_argument("--cookie", default=DEFAULT_COOKIE_FILE, help="登录态文件路径")
    parser.add_argument("--title", help="覆盖标题（默认由输出字段生成）")
    parser.add_argument("--desc", help="覆盖文案（默认由输出字段生成）")
    parser.add_argument("--tag", action="append", default=[], help="话题（可重复，如 --tag #歌名）")
    parser.add_argument("--short-title", default=None, help="短标题（默认 音乐music）")
    parser.add_argument("--collection", default="Music", help="添加到合集名（默认 Music）")
    parser.add_argument("--no-original", action="store_true", help="不自动勾选声明原创")
    parser.add_argument("--dump", action="store_true", help="打开发布页并打印交互元素（校准选择器用，可配 --video 先上传）")
    args = parser.parse_args()

    log = lambda msg: print(msg, flush=True)

    if args.login:
        get_cookie(args.cookie, log_cb=log)
        return
    if args.check:
        ok = check_cookie(args.cookie, log_cb=log)
        print("登录有效" if ok else "登录已过期")
        raise SystemExit(0 if ok else 1)
    if args.dump:
        dump_publish_page(args.cookie, video_path=args.video, log_cb=log)
        return
    if args.video:
        stem = os.path.splitext(os.path.basename(args.video))[0]
        output_field = args.desc or args.title or _output_field_from_stem(stem)
        fields = build_publish_fields(output_field)
        title = args.title or fields.title
        description = args.desc or fields.description
        tags = args.tag or fields.tags
        publish(args.video, title, description, tags, mode=args.mode,
                cookie_path=args.cookie, log_cb=log,
                short_title=args.short_title, collection=args.collection,
                declare_original=not args.no_original)
        return
    parser.print_help()


if __name__ == "__main__":
    _main()
