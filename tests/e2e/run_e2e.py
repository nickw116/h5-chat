#!/usr/bin/env python3
"""
H5 Chat Bridge 端到端自动化测试

用法:
    python run_e2e.py [--verbose]

环境变量:
    H5_TEST_BASE_URL    桥接层 API 地址 (默认: http://127.0.0.1:8080)
    H5_TEST_USERNAME    测试账号 (默认: admin)
    H5_TEST_PASSWORD    测试密码 (默认: admin123)
    H5_TEST_NEW_PASS    修改密码测试用新密码 (默认: NewPass123!)
    GW_URL              Gateway WebSocket 地址 (默认: ws://127.0.0.1:22533)
    GW_TOKEN            Gateway 认证 token (默认从 .env 读取)
"""

import argparse
import asyncio
import json
import os
import sys
import time
import traceback
import uuid
from pathlib import Path

import httpx
import websockets

# ── 配置 ────────────────────────────────────────────────────────────

BASE_URL = os.getenv("H5_TEST_BASE_URL", "http://127.0.0.1:8080").rstrip("/")
USERNAME = os.getenv("H5_TEST_USERNAME", "admin")
PASSWORD = os.getenv("H5_TEST_PASSWORD", "admin123")
NEW_PASSWORD = os.getenv("H5_TEST_NEW_PASS", "NewPass123!")
GW_URL = os.getenv("GW_URL", "ws://127.0.0.1:22533")
GW_TOKEN = os.getenv("GW_TOKEN", "")

# 尝试从项目 .env 读取缺失的 token
if not GW_TOKEN:
    env_path = Path(__file__).resolve().parents[2] / ".env"
    if env_path.exists():
        with open(env_path, "r", encoding="utf-8") as f:
            for line in f:
                if line.startswith("GW_TOKEN="):
                    GW_TOKEN = line.strip().split("=", 1)[1]
                    break

VERBOSE = False

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


def vprint(*args):
    if VERBOSE:
        print(*args)


# ── 测试框架 ────────────────────────────────────────────────────────

results = {"pass": 0, "fail": 0, "skipped": 0}


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


# ── HTTP 客户端 ─────────────────────────────────────────────────────

class ApiClient:
    def __init__(self, base_url: str):
        self.base = base_url
        self.client = httpx.Client(timeout=30.0, follow_redirects=True)
        self.token: str | None = None
        self.session_key: str | None = None

    def set_token(self, token: str):
        self.token = token

    def headers(self) -> dict:
        h = {"Content-Type": "application/json"}
        if self.token:
            h["Authorization"] = f"Bearer {self.token}"
        return h

    def get(self, path: str, **kwargs):
        return self.client.get(f"{self.base}{path}", headers=self.headers(), **kwargs)

    def post(self, path: str, json_data=None, **kwargs):
        return self.client.post(
            f"{self.base}{path}",
            headers=self.headers(),
            json=json_data,
            **kwargs,
        )

    def close(self):
        self.client.close()


api = ApiClient(BASE_URL)


# ── 测试用例 ────────────────────────────────────────────────────────

def test_health():
    """1. 健康检查 — GET /api/health"""
    try:
        r = api.get("/api/health")
        vprint(f"  status={r.status_code}, body={r.text}")
        if r.status_code != 200:
            return record(False, "Health Check", f"  期望 200, 得到 {r.status_code}")
        data = r.json()
        if data.get("status") != "ok":
            return record(False, "Health Check", f"  响应缺少 status=ok: {data}")
        record(True, "Health Check")
    except Exception as e:
        record(False, "Health Check", f"  异常: {e}")


def test_login():
    """2. 登录认证 — POST /api/login"""
    try:
        r = api.post("/api/login", json_data={"username": USERNAME, "password": PASSWORD})
        vprint(f"  status={r.status_code}, body={r.text}")
        if r.status_code != 200:
            return record(False, "Login", f"  期望 200, 得到 {r.status_code}: {r.text}")
        data = r.json()
        token = data.get("token")
        if not token:
            return record(False, "Login", f"  响应缺少 token: {data}")
        api.set_token(token)
        vprint(f"  token={token[:20]}... role={data.get('role')}")
        record(True, "Login")
    except Exception as e:
        record(False, "Login", f"  异常: {e}")


def test_status():
    """验证登录后的 status 端点"""
    try:
        r = api.get("/api/status")
        vprint(f"  status={r.status_code}, body={r.text}")
        if r.status_code != 200:
            return record(False, "Status", f"  期望 200, 得到 {r.status_code}: {r.text}")
        data = r.json()
        if data.get("user") != USERNAME:
            return record(False, "Status", f"  user 不匹配: {data}")
        record(True, "Status")
    except Exception as e:
        record(False, "Status", f"  异常: {e}")


def test_models():
    """3. 模型列表 — GET /api/models"""
    try:
        r = api.get("/api/models")
        vprint(f"  status={r.status_code}, body={r.text[:500]}")
        if r.status_code != 200:
            return record(False, "Models List", f"  期望 200, 得到 {r.status_code}: {r.text}")
        data = r.json()
        models = data.get("models", [])
        if not models or not isinstance(models, list):
            return record(False, "Models List", f"  响应缺少 models 列表: {data}")
        vprint(f"  models count={len(models)}, default={data.get('default')}")
        record(True, f"Models List ({len(models)} models)")
    except Exception as e:
        record(False, "Models List", f"  异常: {e}")


def test_model_switch():
    """4. 模型切换 — POST /api/model/switch"""
    try:
        # 先获取当前模型列表，选一个切换
        r = api.get("/api/models")
        data = r.json()
        models = data.get("models", [])
        if not models:
            return skip("Model Switch", "无可用的模型列表")
        target = models[0].get("id", "")
        if not target:
            return skip("Model Switch", "模型缺少 id")
        r2 = api.post("/api/model/switch", json_data={"model": target})
        vprint(f"  status={r2.status_code}, body={r2.text}")
        if r2.status_code != 200:
            return record(False, "Model Switch", f"  期望 200, 得到 {r2.status_code}: {r2.text}")
        sw = r2.json()
        if sw.get("model") != target:
            return record(False, "Model Switch", f"  返回 model 不匹配: {sw}")
        record(True, f"Model Switch -> {target}")
    except Exception as e:
        record(False, "Model Switch", f"  异常: {e}")


async def test_gateway_ws():
    """5. Gateway WS 连接测试 (bridge 内部链路)"""
    if not GW_TOKEN:
        return skip("Gateway WS", "GW_TOKEN 未设置")
    try:
        async with websockets.connect(
            GW_URL,
            max_size=10 * 1024 * 1024,
            ping_interval=None,
            origin="https://www.nickhome.cloud",
        ) as ws:
            raw = await asyncio.wait_for(ws.recv(), timeout=5.0)
            msg = json.loads(raw)
            vprint(f"  challenge={msg}")
            if msg.get("event") != "connect.challenge":
                return record(False, "Gateway WS", f"  未收到 challenge: {msg}")
            # 不完成完整握手，只要能收到 challenge 就说明 Gateway 在线且 WS 可用
            record(True, "Gateway WS (challenge received)")
    except (OSError, websockets.exceptions.InvalidHandshake, asyncio.TimeoutError) as e:
        record(False, "Gateway WS", f"  连接失败: {e}")
    except Exception as e:
        record(False, "Gateway WS", f"  异常: {e}")


def test_session_management():
    """7. 会话管理 — 创建、列表、切换、历史"""
    errors = []
    try:
        # 获取当前 sessionKey
        r = api.get("/api/session")
        vprint(f"  /session status={r.status_code}")
        if r.status_code == 200:
            api.session_key = r.json().get("sessionKey")

        # 创建新会话
        r2 = api.post("/api/sessions", json_data={"title": "E2E Test Session"})
        vprint(f"  /sessions POST status={r2.status_code}, body={r2.text}")
        if r2.status_code != 200:
            errors.append(f"创建会话失败: {r2.status_code} {r2.text}")
        else:
            new_sk = r2.json().get("sessionKey")
            if not new_sk:
                errors.append("创建会话返回空 sessionKey")
            else:
                # 切换会话
                r3 = api.client.put(
                    f"{api.base}/api/sessions/active",
                    headers=api.headers(),
                    json={"sessionKey": new_sk},
                )
                vprint(f"  /sessions/active PUT status={r3.status_code}, body={r3.text}")
                if r3.status_code != 200:
                    errors.append(f"切换会话失败: {r3.status_code} {r3.text}")

                # 历史消息
                r4 = api.get("/api/history", params={"limit": 10, "sessionKey": new_sk})
                vprint(f"  /history status={r4.status_code}, body={r4.text[:300]}")
                if r4.status_code != 200:
                    errors.append(f"获取历史失败: {r4.status_code} {r4.text}")

                # 切回原来的会话（清理）
                if api.session_key:
                    api.client.put(
                        f"{api.base}/api/sessions/active",
                        headers=api.headers(),
                        json={"sessionKey": api.session_key},
                    )

        # 列出所有会话
        r5 = api.get("/api/sessions")
        vprint(f"  /sessions GET status={r5.status_code}")
        if r5.status_code != 200:
            errors.append(f"列出会话失败: {r5.status_code}")
        else:
            sessions = r5.json().get("sessions", [])
            vprint(f"  sessions count={len(sessions)}")

        if errors:
            record(False, "Session Management", "\n".join(f"  {e}" for e in errors))
        else:
            record(True, "Session Management")
    except Exception as e:
        record(False, "Session Management", f"  异常: {e}\n{traceback.format_exc()}")


def test_chat_legacy_sse():
    """6. 发送消息 + SSE 流式响应 — POST /api/chat/legacy"""
    try:
        # 先获取 sessionKey
        r = api.get("/api/session")
        if r.status_code != 200:
            return record(False, "Chat + SSE", f"  获取 session 失败: {r.status_code}")
        session_key = r.json().get("sessionKey")
        if not session_key:
            return record(False, "Chat + SSE", "  sessionKey 为空")

        # 发送一条简短消息（用 /model 命令避免触发真实 LLM 调用，或发送无害问题）
        msg = "你好，这是一个端到端测试消息。请回复 'OK'。"
        r2 = api.client.post(
            f"{api.base}/api/chat/legacy",
            headers=api.headers(),
            json={"message": msg, "session_key": session_key},
            timeout=60.0,
        )
        vprint(f"  /chat/legacy status={r2.status_code}")
        if r2.status_code != 200:
            return record(False, "Chat + SSE", f"  期望 200, 得到 {r2.status_code}: {r2.text[:300]}")

        # 解析 SSE 流
        content_type = r2.headers.get("content-type", "")
        if "text/event-stream" not in content_type:
            # 某些代理可能吞掉 SSE，fallback: 检查是否是 JSON 错误
            return record(False, "Chat + SSE", f"  响应不是 SSE: content-type={content_type}, body={r2.text[:300]}")

        events = []
        for line in r2.text.splitlines():
            if line.startswith("data: "):
                payload = line[6:]
                try:
                    ev = json.loads(payload)
                    events.append(ev)
                except json.JSONDecodeError:
                    events.append({"raw": payload})

        vprint(f"  SSE events count={len(events)}")
        # 期望至少收到 model + delta/done/error 中的一种
        types = {e.get("type") for e in events}
        vprint(f"  event types={types}")

        has_model = "model" in types
        has_delta = "delta" in types or "text" in types
        has_done = "done" in types or "complete" in types

        if has_model or has_delta or has_done:
            record(True, f"Chat + SSE ({len(events)} events, types={types})")
        else:
            # 如果没有 delta，但至少收到了 run.started 或 snapshot 也算部分成功
            if any(e.get("type") in ("run.started", "snapshot", "acp_status") for e in events):
                record(True, f"Chat + SSE ({len(events)} events, no delta but stream ok)")
            else:
                record(False, "Chat + SSE", f"  未收到预期事件类型: {types}, events={events[:5]}")
    except Exception as e:
        record(False, "Chat + SSE", f"  异常: {e}\n{traceback.format_exc()}")


def test_upload():
    """8. 文件上传 — POST /api/upload"""
    try:
        # 构造一个临时文本文件
        content = b"Hello, this is an E2E upload test file.\n"
        files = {"file": ("e2e_test.txt", content, "text/plain")}
        r = api.client.post(
            f"{api.base}/api/upload",
            headers={k: v for k, v in api.headers().items() if k != "Content-Type"},
            files=files,
            params={"storage": "local"},
        )
        vprint(f"  /upload status={r.status_code}, body={r.text}")
        if r.status_code != 200:
            return record(False, "File Upload", f"  期望 200, 得到 {r.status_code}: {r.text}")
        data = r.json()
        url = data.get("url")
        if not url:
            return record(False, "File Upload", f"  响应缺少 url: {data}")
        record(True, f"File Upload -> {url}")
    except Exception as e:
        record(False, "File Upload", f"  异常: {e}")


def test_change_password():
    """9. 修改密码 — POST /api/change-password"""
    try:
        r = api.post(
            "/api/change-password",
            json_data={"old_password": PASSWORD, "new_password": NEW_PASSWORD},
        )
        vprint(f"  /change-password status={r.status_code}, body={r.text}")
        if r.status_code == 200:
            data = r.json()
            if data.get("ok"):
                record(True, "Change Password")
                # 可选：用新密码重新登录验证
                r2 = api.post("/api/login", json_data={"username": USERNAME, "password": NEW_PASSWORD})
                if r2.status_code == 200:
                    api.set_token(r2.json().get("token"))
                    print(info("  Re-login with new password: OK"))
                    # 恢复密码（不影响其他测试）
                    api.post("/api/change-password", json_data={"old_password": NEW_PASSWORD, "new_password": PASSWORD})
                return
            return record(False, "Change Password", f"  响应 ok=false: {data}")
        # 如果旧密码错误，说明当前环境密码不是默认的
        if r.status_code == 400:
            return skip("Change Password", f"旧密码不匹配 (当前环境密码可能不是 {PASSWORD})")
        record(False, "Change Password", f"  期望 200, 得到 {r.status_code}: {r.text}")
    except Exception as e:
        record(False, "Change Password", f"  异常: {e}")


# ── 主流程 ──────────────────────────────────────────────────────────

async def main():
    global VERBOSE
    parser = argparse.ArgumentParser(description="H5 Chat Bridge E2E Tests")
    parser.add_argument("--verbose", "-v", action="store_true", help="详细输出")
    args = parser.parse_args()
    VERBOSE = args.verbose

    print(f"{C.BOLD}{'=' * 60}{C.RESET}")
    print(f"{C.BOLD}H5 Chat Bridge E2E Test Suite{C.RESET}")
    print(f"  Base URL : {BASE_URL}")
    print(f"  Gateway  : {GW_URL}")
    print(f"  User     : {USERNAME}")
    print(f"  Verbose  : {VERBOSE}")
    print(f"{C.BOLD}{'=' * 60}{C.RESET}\n")

    start = time.time()

    # 1. 无需认证
    test_health()

    # 2. 登录（后续测试依赖 token）
    test_login()
    if not api.token:
        print(warn("\n登录失败，跳过所有需要认证的测试。"))
        api.close()
        return

    test_status()
    test_models()
    test_model_switch()
    test_session_management()
    test_chat_legacy_sse()
    test_upload()
    test_change_password()

    # 5. Gateway WS（异步，独立）
    await test_gateway_ws()

    api.close()

    elapsed = time.time() - start
    print(f"\n{C.BOLD}{'=' * 60}{C.RESET}")
    print(f"  通过: {C.GREEN}{results['pass']}{C.RESET}")
    print(f"  失败: {C.RED}{results['fail']}{C.RESET}")
    print(f"  跳过: {C.YELLOW}{results['skipped']}{C.RESET}")
    print(f"  耗时: {elapsed:.1f}s")
    print(f"{C.BOLD}{'=' * 60}{C.RESET}")

    sys.exit(0 if results["fail"] == 0 else 1)


if __name__ == "__main__":
    asyncio.run(main())
