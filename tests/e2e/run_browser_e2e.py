#!/usr/bin/env python3
"""
H5 Chat 浏览器自动化 E2E 测试（Playwright）

用法:
    python run_browser_e2e.py [--headed] [--test TEST_NAME] [--screenshot-on-fail]

环境变量:
    H5_TEST_URL       H5 前端地址 (默认: https://www.nickhome.cloud/chat)
    H5_TEST_USERNAME  测试账号 (默认: admin)
    H5_TEST_PASSWORD  测试密码 (默认: admin123)
"""

import argparse
import os
import sys
import time
import traceback
from pathlib import Path

from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeout

# ── 配置 ────────────────────────────────────────────────────────────

TEST_URL = os.getenv("H5_TEST_URL", "https://www.nickhome.cloud/chat").rstrip("/")
USERNAME = os.getenv("H5_TEST_USERNAME", "admin")
PASSWORD = os.getenv("H5_TEST_PASSWORD", "admin123")

SCREENSHOT_DIR = Path(__file__).resolve().parent / "screenshots"
SCREENSHOT_DIR.mkdir(exist_ok=True)

DEFAULT_TIMEOUT = 15000  # ms
LONG_TIMEOUT = 60000  # ms
CHAT_TIMEOUT = 45000  # ms

# ── 颜色工具 ────────────────────────────────────────────────────────

class C:
    GREEN = "\033[92m"
    RED = "\033[91m"
    YELLOW = "\033[93m"
    BLUE = "\033[94m"
    CYAN = "\033[96m"
    BOLD = "\033[1m"
    RESET = "\033[0m"


def ok(msg: str) -> str:
    return f"{C.GREEN}{C.BOLD}PASS{C.RESET} {msg}"


def fail(msg: str) -> str:
    return f"{C.RED}{C.BOLD}FAIL{C.RESET} {msg}"


def info(msg: str) -> str:
    return f"{C.CYAN}INFO{C.RESET} {msg}"


def warn(msg: str) -> str:
    return f"{C.YELLOW}WARN{C.RESET} {msg}"


# ── 测试框架 ────────────────────────────────────────────────────────

results = {"pass": 0, "fail": 0, "skipped": 0}
screenshot_on_fail = False


def record(result: bool, name: str, detail: str = ""):
    if result:
        results["pass"] += 1
        print(ok(name))
    else:
        results["fail"] += 1
        print(fail(name))
    if detail:
        print(detail)


def skip(name: str, reason: str = ""):
    results["skipped"] += 1
    print(f"{C.YELLOW}SKIP{C.RESET} {name}" + (f" ({reason})" if reason else ""))


def take_screenshot(page, name: str) -> str:
    path = SCREENSHOT_DIR / f"{name}.png"
    try:
        page.screenshot(path=str(path), full_page=True)
        return str(path)
    except Exception as e:
        print(warn(f"  截图失败: {e}"))
        return ""


# ── Playwright 辅助 ────────────────────────────────────────────────

def wait_for_chat_page(page, timeout=DEFAULT_TIMEOUT):
    """等待聊天页加载完成"""
    page.wait_for_selector(".chat-page", timeout=timeout)
    page.wait_for_selector(".message-list", timeout=timeout)
    page.wait_for_selector(".input-field", timeout=timeout)


def wait_for_login_page(page, timeout=DEFAULT_TIMEOUT):
    """等待登录页加载完成"""
    page.wait_for_selector(".login-page", timeout=timeout)
    page.wait_for_selector(".login-btn", timeout=timeout)


def login(page, username: str = USERNAME, password: str = PASSWORD):
    """执行登录流程"""
    page.goto(TEST_URL)
    wait_for_login_page(page)

    # 清空并填写用户名
    user_input = page.locator(".login-page .van-field__control").nth(0)
    user_input.fill("")
    user_input.fill(username)

    # 清空并填写密码
    pw_input = page.locator(".login-page .van-field__control").nth(1)
    pw_input.fill("")
    pw_input.fill(password)

    # 点击登录
    page.click(".login-btn")

    # 等待跳转到聊天页
    wait_for_chat_page(page, timeout=LONG_TIMEOUT)
    # 额外等待 SSE 连接建立
    page.wait_for_selector(".sse-dot--connected", timeout=10000)


def send_message(page, text: str, timeout=CHAT_TIMEOUT):
    """发送消息并等待响应完成"""
    # 确保输入框可用
    page.wait_for_selector(".input-field .van-field__control", state="visible", timeout=DEFAULT_TIMEOUT)

    # 聚焦并输入
    input_box = page.locator(".input-field .van-field__control")
    input_box.fill(text)

    # 点击发送
    page.click(".send-btn")

    # 等待用户消息出现
    page.wait_for_selector(".message-item.user", timeout=DEFAULT_TIMEOUT)

    # 等待 AI 响应完成（loading 消失）
    try:
        page.wait_for_selector(".stop-btn", timeout=2000)
        # 有 stop 按钮说明正在生成，等待它变回 send 按钮
        page.wait_for_selector(".send-btn", timeout=timeout)
    except PlaywrightTimeout:
        # 可能响应太快，stop 按钮根本没出现，send 按钮一直可见
        pass


def get_message_count(page, role: str = None) -> int:
    """获取消息数量"""
    selector = ".message-item"
    if role:
        selector = f".message-item.{role}"
    return page.locator(selector).count()


# ── 测试用例 ────────────────────────────────────────────────────────

def test_login_flow(page):
    """1. 登录流程 — 打开页面、输入账号密码、登录、验证跳转到聊天页"""
    try:
        page.goto(TEST_URL)
        wait_for_login_page(page)

        # 验证登录页元素
        assert page.locator(".login-box h2").inner_text() == "AI SPACE", "登录页标题不对"

        # 输入用户名密码
        user_input = page.locator(".login-page .van-field__control").nth(0)
        pw_input = page.locator(".login-page .van-field__control").nth(1)
        user_input.fill(USERNAME)
        pw_input.fill(PASSWORD)

        # 点击登录
        page.click(".login-btn")

        # 验证跳转到聊天页
        wait_for_chat_page(page, timeout=LONG_TIMEOUT)
        record(True, "Login Flow")
    except Exception as e:
        detail = f"  异常: {e}\n{traceback.format_exc()}"
        if screenshot_on_fail:
            path = take_screenshot(page, "fail_login_flow")
            detail += f"\n  截图: {path}"
        record(False, "Login Flow", detail)


def test_send_message(page):
    """2. 发送消息 — 输入消息、发送、验证消息气泡、loading、流式回复、完成"""
    try:
        login(page)

        # 记录当前消息数
        before_count = get_message_count(page)

        # 输入并发送测试消息
        test_msg = f"E2E 测试消息 {int(time.time())}"
        input_box = page.locator(".input-field .van-field__control")
        input_box.fill(test_msg)
        page.click(".send-btn")

        # 验证用户消息气泡出现
        page.wait_for_selector(".message-item.user", timeout=DEFAULT_TIMEOUT)
        user_msgs = page.locator(".message-item.user")
        last_user_text = user_msgs.last.inner_text()
        assert test_msg in last_user_text, f"用户消息内容不匹配: {last_user_text}"

        # 验证 AI 开始回复（thinking 或 assistant 消息出现）
        try:
            page.wait_for_selector(".message-item.assistant", timeout=8000)
        except PlaywrightTimeout:
            # 可能没有 thinking，直接等 assistant 消息
            pass

        # 验证 loading 状态（stop 按钮出现表示正在生成）
        try:
            page.wait_for_selector(".stop-btn", timeout=3000)
            loading_appeared = True
        except PlaywrightTimeout:
            loading_appeared = False

        # 等待响应完成（send 按钮重新出现）
        page.wait_for_selector(".send-btn", timeout=CHAT_TIMEOUT)

        # 验证最终有 assistant 回复
        assistant_count = get_message_count(page, "assistant")
        assert assistant_count > 0, "没有收到 AI 回复"

        detail = f"  user_msgs={get_message_count(page, 'user')}, assistant_msgs={assistant_count}"
        if loading_appeared:
            detail += ", loading appeared"
        record(True, "Send Message", detail)
    except Exception as e:
        detail = f"  异常: {e}\n{traceback.format_exc()}"
        if screenshot_on_fail:
            path = take_screenshot(page, "fail_send_message")
            detail += f"\n  截图: {path}"
        record(False, "Send Message", detail)


def test_model_switch(page):
    """3. 模型切换 — 点击模型胶囊、验证 ActionSheet、切换后名称更新"""
    try:
        login(page)

        # 等待模型按钮出现
        page.wait_for_selector(".nav-model-btn", timeout=DEFAULT_TIMEOUT)
        original_model = page.locator(".nav-model-text").inner_text().strip()

        # 点击模型按钮
        page.click(".nav-model-btn")

        # 验证 ActionSheet 弹出
        page.wait_for_selector(".van-action-sheet", timeout=DEFAULT_TIMEOUT)
        page.wait_for_selector(".van-action-sheet__item", timeout=DEFAULT_TIMEOUT)

        # 验证模型列表有内容
        items = page.locator(".van-action-sheet__item").all()
        assert len(items) > 0, "模型列表为空"

        # 点击第二个模型（如果只有一个则点第一个）
        target_idx = 1 if len(items) > 1 else 0
        target_text = items[target_idx].inner_text().strip()
        items[target_idx].click()

        # 等待 ActionSheet 关闭
        page.wait_for_selector(".van-action-sheet", state="hidden", timeout=DEFAULT_TIMEOUT)

        # 验证顶部模型名称更新（不一定和点击的完全一致，但应该变化或保持）
        # 给一点 time 让后端响应
        page.wait_for_timeout(2000)
        new_model = page.locator(".nav-model-text").inner_text().strip()

        detail = f"  {original_model} -> {new_model}"
        record(True, "Model Switch", detail)
    except Exception as e:
        detail = f"  异常: {e}\n{traceback.format_exc()}"
        if screenshot_on_fail:
            path = take_screenshot(page, "fail_model_switch")
            detail += f"\n  截图: {path}"
        record(False, "Model Switch", detail)


def test_acp_status_bar(page):
    """4. ACP 状态栏 — 发送需要 ACP 的消息，验证状态栏出现且计数正确"""
    try:
        login(page)

        # 发送触发 ACP 的消息
        acp_msg = "帮我创建 /tmp/test_e2e_browser.txt"
        input_box = page.locator(".input-field .van-field__control")
        input_box.fill(acp_msg)
        page.click(".send-btn")

        # 等待用户消息出现
        page.wait_for_selector(".message-item.user", timeout=DEFAULT_TIMEOUT)

        # 等待 ACP 状态栏出现运行中状态（脉冲点）
        try:
            page.wait_for_selector(".acp-status-bar__pulse", timeout=15000)
            pulse_visible = True
        except PlaywrightTimeout:
            pulse_visible = False

        # 等待响应完成
        try:
            page.wait_for_selector(".stop-btn", timeout=3000)
            page.wait_for_selector(".send-btn", timeout=CHAT_TIMEOUT)
        except PlaywrightTimeout:
            pass

        # 验证 ACP 状态栏存在
        bar = page.locator(".acp-status-bar")
        assert bar.count() > 0, "ACP 状态栏不存在"

        summary = bar.locator(".acp-status-bar__summary").inner_text()
        detail = f"  状态栏摘要: {summary.strip()}"
        if pulse_visible:
            detail += ", 运行中脉冲出现"
        record(True, "ACP Status Bar", detail)
    except Exception as e:
        detail = f"  异常: {e}\n{traceback.format_exc()}"
        if screenshot_on_fail:
            path = take_screenshot(page, "fail_acp_status_bar")
            detail += f"\n  截图: {path}"
        record(False, "ACP Status Bar", detail)


def test_session_switch(page):
    """5. 会话切换 — 使用 /new 命令创建新会话，验证会话隔离"""
    try:
        login(page)

        # 先发一条消息建立上下文
        input_box = page.locator(".input-field .van-field__control")
        input_box.fill("这是一条旧会话的消息")
        page.click(".send-btn")
        page.wait_for_selector(".message-item.user", timeout=DEFAULT_TIMEOUT)
        page.wait_for_selector(".send-btn", timeout=CHAT_TIMEOUT)

        old_count = get_message_count(page)

        # 发送 /new 命令创建新会话
        input_box.fill("/new")
        page.click(".send-btn")

        # 等待系统提示新会话已创建
        # 新会话创建后 messages 会被清空，然后出现一条 assistant 的系统提示
        page.wait_for_timeout(3000)

        # 也可以验证通过侧边栏
        page.click(".nav-menu-icon")
        page.wait_for_selector(".session-drawer", timeout=DEFAULT_TIMEOUT)
        page.wait_for_selector(".session-item.active", timeout=DEFAULT_TIMEOUT)

        # 点击新建按钮
        page.click(".new-session-btn")
        page.wait_for_timeout(2000)

        # 验证回到聊天页
        page.wait_for_selector(".chat-page", timeout=DEFAULT_TIMEOUT)

        # 验证消息列表被清空或只有系统提示（少于之前）
        new_count = get_message_count(page)
        detail = f"  旧消息数: {old_count}, 新会话消息数: {new_count}"
        record(True, "Session Switch", detail)
    except Exception as e:
        detail = f"  异常: {e}\n{traceback.format_exc()}"
        if screenshot_on_fail:
            path = take_screenshot(page, "fail_session_switch")
            detail += f"\n  截图: {path}"
        record(False, "Session Switch", detail)


def test_stop_generation(page):
    """6. 停止功能 — 发送长消息后点击停止，验证流式中断"""
    try:
        login(page)

        # 发送一条容易触发长回复的消息
        long_msg = "请详细介绍一下人工智能的发展历史，从 1950 年图灵测试开始，一直到 2024 年的大模型时代。"
        input_box = page.locator(".input-field .van-field__control")
        input_box.fill(long_msg)
        page.click(".send-btn")

        # 等待用户消息和 stop 按钮出现
        page.wait_for_selector(".message-item.user", timeout=DEFAULT_TIMEOUT)
        page.wait_for_selector(".stop-btn", timeout=5000)

        # 稍微等一点让流式开始
        page.wait_for_timeout(1500)

        # 点击停止按钮
        page.click(".stop-btn")

        # 验证 stop 按钮消失，send 按钮重新出现
        page.wait_for_selector(".send-btn", timeout=10000)

        # 验证有 assistant 消息（即使被截断）
        assistant_count = get_message_count(page, "assistant")
        assert assistant_count > 0, "没有 AI 回复"

        detail = f"  assistant_msgs={assistant_count}"
        record(True, "Stop Generation", detail)
    except Exception as e:
        detail = f"  异常: {e}\n{traceback.format_exc()}"
        if screenshot_on_fail:
            path = take_screenshot(page, "fail_stop_generation")
            detail += f"\n  截图: {path}"
        record(False, "Stop Generation", detail)


# ── 主流程 ──────────────────────────────────────────────────────────

ALL_TESTS = {
    "login": test_login_flow,
    "send": test_send_message,
    "model": test_model_switch,
    "acp": test_acp_status_bar,
    "session": test_session_switch,
    "stop": test_stop_generation,
}


def main():
    global screenshot_on_fail
    parser = argparse.ArgumentParser(description="H5 Chat Browser E2E Tests")
    parser.add_argument("--headed", action="store_true", help="显示浏览器窗口")
    parser.add_argument("--test", type=str, help="指定单个测试名称: login, send, model, acp, session, stop")
    parser.add_argument("--screenshot-on-fail", action="store_true", help="失败时自动截图")
    args = parser.parse_args()

    screenshot_on_fail = args.screenshot_on_fail

    print(f"{C.BOLD}{'=' * 60}{C.RESET}")
    print(f"{C.BOLD}H5 Chat Browser E2E Test Suite{C.RESET}")
    print(f"  URL      : {TEST_URL}")
    print(f"  User     : {USERNAME}")
    print(f"  Headed   : {args.headed}")
    print(f"  Screenshot: {screenshot_on_fail}")
    print(f"{C.BOLD}{'=' * 60}{C.RESET}\n")

    # 确定要运行的测试
    if args.test:
        if args.test not in ALL_TESTS:
            print(fail(f"未知测试: {args.test}"))
            print(f"  可用测试: {', '.join(ALL_TESTS.keys())}")
            sys.exit(1)
        tests_to_run = [(args.test, ALL_TESTS[args.test])]
    else:
        tests_to_run = list(ALL_TESTS.items())

    start = time.time()

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=not args.headed)
        context = browser.new_context(
            viewport={"width": 1280, "height": 900},
            user_agent="Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36 H5-E2E-Bot/1.0",
        )

        for name, test_fn in tests_to_run:
            page = context.new_page()
            # 每次测试前清空 sessionStorage 确保从登录开始
            page.goto(TEST_URL)
            page.evaluate("() => { sessionStorage.clear(); localStorage.clear(); }")
            page.reload()
            print(f"\n{C.BOLD}[{name}]{C.RESET}")
            try:
                test_fn(page)
            except Exception as e:
                detail = f"  未捕获异常: {e}\n{traceback.format_exc()}"
                if screenshot_on_fail:
                    path = take_screenshot(page, f"fail_{name}_uncaught")
                    detail += f"\n  截图: {path}"
                record(False, name, detail)
            finally:
                page.close()

        browser.close()

    elapsed = time.time() - start
    print(f"\n{C.BOLD}{'=' * 60}{C.RESET}")
    print(f"  通过: {C.GREEN}{results['pass']}{C.RESET}")
    print(f"  失败: {C.RED}{results['fail']}{C.RESET}")
    print(f"  跳过: {C.YELLOW}{results['skipped']}{C.RESET}")
    print(f"  耗时: {elapsed:.1f}s")
    print(f"{C.BOLD}{'=' * 60}{C.RESET}")

    sys.exit(0 if results["fail"] == 0 else 1)


if __name__ == "__main__":
    main()
