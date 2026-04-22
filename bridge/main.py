"""FastAPI bridge: H5 frontend ↔ OpenClaw Gateway WebSocket RPC."""

import asyncio
import json
import os
import re
import uuid
import logging
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Header, Depends, Query, UploadFile, File
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from . import config
from . import users
from . import cos_util
from .ws_client import get_client, add_migrating_listener

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("bridge")


# ── 文件路径检测（用于自动上传 agent 生成的文件）───────────────────
# 匹配常见文件路径 + 扩展名（PPT, PDF, Word, Excel, 图片, 压缩包, 文本等）
_FILE_EXT_PATTERN = re.compile(
    r'(?:^|[\s\'\"\(\[\{,;])'
    r'((?:/root/|/tmp/|/home/|/var/|\.\./|\./)\S+\.'
    r'(?:pptx?|xlsx?|docx?|pdf|png|jpe?g|gif|svg|zip|rar|7z|tar\.gz|txt|csv|json|md|py|js|html?|css)'
    r')'
    r'(?:$|[\s\'\"\)\]\},;.])',
    re.IGNORECASE
)

# 去重：同一个文件只上传一次（按 run 的生命周期）
_uploaded_files_cache: set[str] = set()
_FILTERED_STREAM_TEXTS = {"NO_REPLY", "HEARTBEAT_OK"}


def _extract_file_paths(text: str) -> list[str]:
    """从文本中提取本地文件路径。"""
    matches = _FILE_EXT_PATTERN.findall(text)
    result = []
    for path in matches:
        # 清理尾部标点
        clean = path.rstrip('.,;:!?)')
        if clean not in _uploaded_files_cache and os.path.isfile(clean):
            _uploaded_files_cache.add(clean)
            result.append(clean)
    return result

# ── 全局 agent 事件分发器 + 消息缓存 ──────────────────────────
_agent_run_queues: dict[str, asyncio.Queue] = {}  # sessionKey → queue
_session_event_subscribers: dict[str, dict[str, asyncio.Queue]] = {}  # sessionKey -> subscriberId -> queue
_agent_session_to_run: dict[str, str] = {}  # sessionKey → chat.send runId (for lifecycle end matching)
_run_to_parent_session: dict[str, str] = {}  # runId → parent sessionKey
_child_to_parent: dict[str, str] = {}  # child sessionKey → parent sessionKey (spawn 时注册)
_agent_listener_registered = False
_EVENT_STREAM_HEARTBEAT_SEC = 15

# 运行时模型缓存：sessionKey → model（由 /model 命令或 SSE agent 事件更新）
_session_runtime_model: dict[str, str] = {}

# 消息缓存: runId → { events: [...], status: "streaming"|"done"|"error", created_at: float }
_run_cache: dict[str, dict] = {}
_child_events_cache: dict[str, list] = {}  # parent sessionKey → cached child events
_child_events_cache_created_at: dict[str, float] = {}  # parent sessionKey → first cached timestamp
_child_event_seq = 0
_RUN_CACHE_MAX_AGE = 600  # 缓存保留 10 分钟


def _cache_child_event(parent_sk: str, payload: dict):
    """Append a child-agent event to the parent session cache used by /child-events."""
    global _child_event_seq
    if not parent_sk:
        return
    cache_list = _child_events_cache.setdefault(parent_sk, [])
    if parent_sk not in _child_events_cache_created_at:
        _child_events_cache_created_at[parent_sk] = time.time()
    payload = dict(payload)
    if not payload.get("_child_event_id"):
        _child_event_seq += 1
        payload["_child_event_id"] = f"{parent_sk}:{_child_event_seq}"
    offset = len(cache_list)
    payload["_child_event_offset"] = offset
    payload["child_event_id"] = payload["_child_event_id"]
    payload["child_event_offset"] = offset
    cache_list.append(payload)
    return payload


def _public_child_event(ev: dict) -> dict:
    """Return a frontend-safe child event with stable public id/offset fields."""
    event = dict(ev)
    child_event_id = event.get("child_event_id") or event.get("_child_event_id")
    child_event_offset = event.get("child_event_offset")
    if child_event_offset is None:
        child_event_offset = event.get("_child_event_offset")
    if child_event_id:
        event["child_event_id"] = child_event_id
        event["childEventId"] = child_event_id
    if child_event_offset is not None:
        event["child_event_offset"] = child_event_offset
        event["childEventOffset"] = child_event_offset
    return event


def _cleanup_old_cache():
    """清理过期缓存"""
    now = time.time()
    expired = [rid for rid, v in _run_cache.items() if now - v["created_at"] > _RUN_CACHE_MAX_AGE]
    for rid in expired:
        _run_cache.pop(rid, None)
        _run_to_parent_session.pop(rid, None)
    if expired:
        logger.info("Cleaned up %d expired cache entries", len(expired))

    expired_parent_keys = [
        parent_sk
        for parent_sk, created_at in _child_events_cache_created_at.items()
        if now - created_at > _RUN_CACHE_MAX_AGE
    ]
    for parent_sk in expired_parent_keys:
        _child_events_cache.pop(parent_sk, None)
        _child_events_cache_created_at.pop(parent_sk, None)
    if expired_parent_keys:
        logger.info("Cleaned up %d expired child event cache entries", len(expired_parent_keys))


def _register_session_subscriber(session_key: str, subscriber_id: str) -> asyncio.Queue:
    queue: asyncio.Queue = asyncio.Queue()
    _session_event_subscribers.setdefault(session_key, {})[subscriber_id] = queue
    logger.info("Registered event subscriber: sessionKey=%s subscriber=%s total=%d", session_key[:30], subscriber_id[:8], len(_session_event_subscribers.get(session_key, {})))
    return queue


def _unregister_session_subscriber(session_key: str, subscriber_id: str):
    subscribers = _session_event_subscribers.get(session_key)
    if not subscribers:
        return
    subscribers.pop(subscriber_id, None)
    if not subscribers:
        _session_event_subscribers.pop(session_key, None)
    logger.info("Unregistered event subscriber: sessionKey=%s subscriber=%s remaining=%d", session_key[:30], subscriber_id[:8], len(_session_event_subscribers.get(session_key, {})))


async def _publish_session_event(session_key: str, event: dict):
    if not session_key:
        return
    subscribers = _session_event_subscribers.get(session_key, {})
    if not subscribers:
        return
    stale_ids = []
    for subscriber_id, queue in list(subscribers.items()):
        try:
            queue.put_nowait(event)
        except Exception:
            stale_ids.append(subscriber_id)
    for subscriber_id in stale_ids:
        _unregister_session_subscriber(session_key, subscriber_id)


def _child_event_fields(payload: dict) -> dict:
    """Extract childEventId and childEventOffset from a payload dict."""
    fields = {}
    eid = payload.get("child_event_id") or payload.get("_child_event_id")
    if eid:
        fields["childEventId"] = eid
    eoffset = payload.get("child_event_offset") if payload.get("child_event_offset") is not None else payload.get("_child_event_offset")
    if eoffset is not None:
        fields["childEventOffset"] = eoffset
    return fields


async def _publish_full_result_from_history(session_key: str, run_id: str, client=None):
    """After lifecycle end, pull the complete assistant message from Gateway history and publish as full_result."""
    try:
        await asyncio.sleep(1.5)
        if client is None:
            client = await get_client()
        hist = await client.rpc("session.history", {
            "sessionKey": session_key,
            "limit": 5,
        })
        messages = hist.get("messages", [])
        for msg in reversed(messages):
            if msg.get("role") == "assistant":
                full_text = msg.get("content", "")
                if full_text and isinstance(full_text, str):
                    if full_text.strip() in _FILTERED_STREAM_TEXTS:
                        return
                    await _publish_session_event(session_key, {
                        "eventId": f"evt-{uuid.uuid4().hex[:16]}",
                        "sessionKey": session_key,
                        "runId": run_id,
                        "source": "main",
                        "ts": int(time.time() * 1000),
                        "kind": "full_result",
                        "payload": {"text": full_text},
                    })
                    await _publish_session_event(session_key, {
                        "eventId": f"evt-{uuid.uuid4().hex[:16]}",
                        "sessionKey": session_key,
                        "runId": run_id,
                        "source": "main",
                        "ts": int(time.time() * 1000),
                        "kind": "run.done",
                        "payload": {},
                    })
                    # Update cache status
                    cache = _run_cache.get(run_id)
                    if cache:
                        cache["status"] = "done"
                return
    except Exception as e:
        logger.warning("_publish_full_result_from_history failed: %s", e)


async def _publish_gateway_event(session_key: str, run_id: str, stream: str, payload: dict):
    """Convert raw Gateway agent event into a rich event and publish to persistent subscribers."""
    if not session_key:
        return
    data = payload.get("data", {}) if isinstance(payload, dict) else {}
    is_child = bool(payload.get("_is_child"))
    source = "acp" if is_child else "main"
    child_fields = _child_event_fields(payload)
    event_id = f"evt-{uuid.uuid4().hex[:16]}"
    ts = int(time.time() * 1000)
    base = {"eventId": event_id, "sessionKey": session_key, "runId": run_id, "source": source, "ts": ts}

    if stream == "assistant":
        event = {**base, "kind": "assistant.delta", "payload": {"delta": data.get("delta", ""), **child_fields}}
    elif stream == "item":
        phase = data.get("phase", "")
        kind_map = {"start": "item.started", "update": "item.updated", "end": "item.completed"}
        event = {**base, "kind": kind_map.get(phase, "item.unknown"), "payload": {
            "kind": data.get("kind", ""), "name": data.get("name", ""),
            "phase": phase, "title": data.get("title", ""), "summary": data.get("summary", ""), **child_fields
        }}
    elif stream == "command_output":
        event = {**base, "kind": "command.output", "payload": {"text": data.get("text", ""), **child_fields}}
    elif stream == "lifecycle":
        phase = data.get("phase", "")
        event = {**base, "kind": f"run.{phase}" if phase else "lifecycle", "payload": {"phase": phase}}
        if phase == "end":
            # After lifecycle end, pull full text from history and publish full_result
            asyncio.create_task(_publish_full_result_from_history(session_key, run_id))
    elif stream == "plan":
        event = {**base, "kind": "plan", "payload": {"text": data.get("text", "") or data.get("summary", "")}}
    elif stream == "model":
        event = {**base, "kind": "model.changed", "payload": {"model": data.get("model", ""), "source": data.get("source", "agent")}}
    else:
        event = {**base, "kind": f"{stream}", "payload": {"data": data, **child_fields}}

    await _publish_session_event(session_key, event)


def _register_global_agent_listener(client):
    """注册全局 agent 事件监听器（只注册一次），同时保存到迁移列表以支持重连"""
    global _agent_listener_registered
    if _agent_listener_registered:
        return

    async def on_agent(payload):
        run_id = payload.get("runId", "")
        session_key = payload.get("sessionKey", "")
        stream = payload.get("stream", "")
        data = payload.get("data", {})
        # 详细打印 lifecycle 和 item 事件
        if stream == "lifecycle":
            logger.info("Agent event: runId=%s, sessionKey=%s, stream=lifecycle, phase=%s, data_keys=%s",
                        run_id[:16], session_key[:30], data.get("phase", "?"), list(data.keys()))
        elif stream == "item":
            logger.info("Agent event: runId=%s, sessionKey=%s, stream=item, type=%s, name=%s, data_keys=%s",
                        run_id[:16], session_key[:30], data.get("type", "?"), data.get("name", "?"), list(data.keys()))
        elif stream == "command_output":
            logger.info("Agent event: runId=%s, sessionKey=%s, stream=command_output, data_keys=%s, text=%s",
                        run_id[:16], session_key[:30], list(data.keys()), repr(data.get("text", "")[:100]))
        else:
            logger.info("Agent event: runId=%s, sessionKey=%s, stream=%s", run_id[:16], session_key[:30], stream)

        # 注册子→父映射
        # 方式1: sessions_spawn item end 事件的 title 中可能包含 childSessionKey
        # 方式2: assistant delta 中包含 spawn 返回的 JSON（含 childSessionKey）
        if stream == "item" and data.get("name") == "sessions_spawn" and data.get("phase") == "end":
            title = data.get("title", "")
            m = re.search(r'(agent:[\w:.-]+)', title)
            if m:
                child_sk = m.group(1)
                _child_to_parent[child_sk] = session_key
                logger.info("Registered child->parent mapping from spawn title: %s -> %s", child_sk[:50], session_key[:50])
        if stream == "assistant" and "childSessionKey" in data.get("delta", ""):
            m = re.search(r'childSessionKey["\':=]+\s*["\']?(agent:[\w:.-]+)', data.get("delta", ""))
            if m:
                child_sk = m.group(1)
                _child_to_parent[child_sk] = session_key
                logger.info("Registered child->parent mapping from assistant delta: %s -> %s", child_sk[:50], session_key[:50])

        # 用 sessionKey 匹配活跃 queue
        q = _agent_run_queues.get(session_key)

        # 如果没有匹配的 queue，检查是否是子 agent 的事件，转发到父 session
        if not q:
            # 转发 subagent 或 acp 子 agent 的 assistant 事件到父 session
            is_child = "subagent" in session_key or "acp" in session_key
            if is_child:
                # 优先用精确映射（sessions_spawn 时注册），否则 fallback 遍历
                parent_sk = _child_to_parent.get(session_key)
                if parent_sk:
                    parent_q = _agent_run_queues.get(parent_sk)
                    if parent_q and stream in ("assistant", "item", "lifecycle", "command_output"):
                        logger.info("Forwarding child agent event to parent (mapped): %s -> %s, stream=%s", session_key[:30], parent_sk[:30], stream)
                        payload["_is_child"] = True
                        payload["_delivered_to_parent_queue"] = True
                        payload = _cache_child_event(parent_sk, payload) or payload
                        q = parent_q
                else:
                    # fallback：优先 agent:main:，其次其他非子 agent session
                    best_parent_sk, best_parent_q = None, None
                    for psk, pq in list(_agent_run_queues.items()):
                        if "subagent" in psk or "acp" in psk:
                            continue
                        if psk.startswith("agent:main:"):
                            best_parent_sk, best_parent_q = psk, pq
                            break
                        if best_parent_sk is None:
                            best_parent_sk, best_parent_q = psk, pq
                    if best_parent_q and stream in ("assistant", "item", "lifecycle", "command_output"):
                        logger.info("Forwarding child agent event to parent (fallback): %s -> %s, stream=%s", session_key[:30], best_parent_sk[:30], stream)
                        payload["_is_child"] = True
                        payload["_delivered_to_parent_queue"] = True
                        _child_to_parent[session_key] = best_parent_sk
                        payload = _cache_child_event(best_parent_sk, payload) or payload
                        q = best_parent_q

                if not q and parent_sk and stream in ("assistant", "item", "lifecycle", "command_output"):
                    payload["_is_child"] = True
                    payload["_delivered_to_parent_queue"] = False
                    payload = _cache_child_event(parent_sk, payload) or payload
                    logger.info(
                        "Cached child event without active parent queue: child=%s -> parent=%s, stream=%s, cached=%d",
                        session_key[:30],
                        parent_sk[:30],
                        stream,
                        len(_child_events_cache.get(parent_sk, [])),
                    )

        # 写入缓存（用 chat.send 返回的 runId）
        chat_run_id = _agent_session_to_run.get(session_key, "")
        if not chat_run_id and payload.get("_is_child"):
            parent_sk = _child_to_parent.get(session_key)
            if parent_sk:
                chat_run_id = _agent_session_to_run.get(parent_sk, "")
                if not chat_run_id:
                    for rid, psk in _run_to_parent_session.items():
                        if psk == parent_sk:
                            chat_run_id = rid
                            break

        cache = _run_cache.get(chat_run_id) or _run_cache.get(run_id)
        if cache:
            cache["events"].append(payload)
            data = payload.get("data", {})
            if stream == "lifecycle" and data.get("phase") == "end":
                cache["status"] = "done"

        publish_session_key = session_key
        if payload.get("_is_child"):
            publish_session_key = _child_to_parent.get(session_key) or session_key
        await _publish_gateway_event(publish_session_key, chat_run_id or run_id, stream, payload)

        # 推送到活跃 queue
        if q:
            await q.put(payload)
            logger.info("Put event to queue: sessionKey=%s, stream=%s, queue_size=%d", session_key, stream, q.qsize())
        elif not session_key:
            # 内部系统事件（memory-persona 等），静默忽略
            pass
        else:
            logger.info("No queue for sessionKey=%s (active: %s)", session_key, list(_agent_run_queues.keys()))

    client.on_event("agent", on_agent)
    add_migrating_listener("agent", on_agent)  # 保存以便重连迁移，并避免重复注册
    _agent_listener_registered = True
    logger.info("Global agent listener registered")


async def _unregister_run_queue(run_id):
    """移除 runId 对应的队列"""
    _agent_run_queues.pop(run_id, None)


# ── Lifespan ────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    users.bootstrap()                    # 初始化用户数据库
    client = await get_client()
    _register_global_agent_listener(client)  # 注册全局 agent 事件监听
    logger.info("Bridge started, WS connected=%s", client.connected)
    yield
    logger.info("Bridge shutting down")


app = FastAPI(lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["https://www.nickhome.cloud"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# ── Auth ────────────────────────────────────────────────────────

async def get_current_user(authorization: str = Header(...)):
    """Verify Bearer token, return user dict. All API routes use this."""
    if not authorization.startswith("Bearer "):
        raise HTTPException(401, "Missing Bearer token")
    token = authorization[7:]
    user = users.verify_token(token)
    if not user:
        raise HTTPException(401, "Invalid or expired token")
    return user


# ── Models ──────────────────────────────────────────────────────

class LoginReq(BaseModel):
    username: str
    password: str


class ChatReq(BaseModel):
    message: str
    session_key: str | None = None


class ChangePasswordReq(BaseModel):
    old_password: str
    new_password: str


# ── Routes ──────────────────────────────────────────────────────

async def _build_session_snapshot(session_key: str) -> dict:
    """Build a snapshot of the current session state for reconnection recovery."""
    snapshot = {
        "sessionKey": session_key,
        "ts": int(time.time() * 1000),
        "activeRuns": {},
    }

    # Check if there's an active run for this session
    active_run_id = _agent_session_to_run.get(session_key)
    if active_run_id:
        cache = _run_cache.get(active_run_id)
        if cache:
            # Extract main text from cached events
            main_text = ""
            acp_text = ""
            steps = []
            for ev in cache.get("events", []):
                stream = ev.get("stream", "")
                data = ev.get("data", {})
                if stream == "assistant":
                    delta = data.get("delta", "")
                    if delta:
                        if ev.get("_is_child"):
                            acp_text += delta
                        else:
                            main_text += delta
                elif stream == "item":
                    phase = data.get("phase", "")
                    name = data.get("name", "")
                    summary = data.get("summary", "") or data.get("title", "")
                    if name and phase in ("start", "end"):
                        steps.append({"name": name, "phase": phase, "summary": summary})

            snapshot["activeRuns"][active_run_id] = {
                "status": cache.get("status", "streaming"),
                "mainText": main_text,
                "acpText": acp_text,
                "steps": steps[-20:],  # last 20 steps
                "createdAt": cache.get("created_at", 0),
            }
    else:
        # No active run — check if there's a recent run in cache
        for rid, psk in list(_run_to_parent_session.items()):
            if psk == session_key:
                cache = _run_cache.get(rid)
                if cache:
                    main_text = ""
                    for ev in cache.get("events", []):
                        if ev.get("stream") == "assistant" and not ev.get("_is_child"):
                            main_text += ev.get("data", {}).get("delta", "")
                    if main_text.strip() and main_text.strip() not in _FILTERED_STREAM_TEXTS:
                        snapshot["lastRunId"] = rid
                        snapshot["lastRunText"] = main_text
                        snapshot["lastRunStatus"] = cache.get("status", "done")
                    break

    # Also try to get runtime model
    cached_model = _session_runtime_model.get(session_key, "")
    if cached_model:
        snapshot["model"] = cached_model

    return snapshot


@app.get("/api/events")
async def event_stream(
    sessionKey: str = Query(None),
    clientId: str = Query(None),
    user=Depends(get_current_user),
):
    """Persistent SSE event stream.

    H5 establishes this connection after login and keeps it alive.
    On connect, sends a snapshot of the current session state.
    All Gateway agent events for the given sessionKey are pushed here.
    """
    if not sessionKey:
        raise HTTPException(400, "sessionKey is required")

    agent_id = _agent_id(user['role'])
    stored_sk = users.get_or_create_session(user['id'], agent_id)
    subscriber_id = clientId or str(uuid.uuid4())

    queue = _register_session_subscriber(sessionKey, subscriber_id)

    async def generate():
        try:
            # Send snapshot of current session state
            snapshot = await _build_session_snapshot(sessionKey)
            snap_event = {
                "eventId": f"evt-snap-{uuid.uuid4().hex[:12]}",
                "sessionKey": sessionKey,
                "kind": "snapshot",
                "ts": int(time.time() * 1000),
                "payload": snapshot,
            }
            yield f"data: {json.dumps(snap_event, ensure_ascii=False)}\n\n"
            logger.info("Event stream opened: sessionKey=%s subscriber=%s snapshot_runs=%d", sessionKey[:30], subscriber_id[:8], len(snapshot.get("activeRuns", {})))

            while True:
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=_EVENT_STREAM_HEARTBEAT_SEC)
                    yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
                except asyncio.TimeoutError:
                    yield f"event: ping\ndata: {{\"ts\": {int(time.time() * 1000)}}}\n\n"
        except asyncio.CancelledError:
            logger.info("Event stream cancelled: sessionKey=%s subscriber=%s", sessionKey[:30], subscriber_id[:8])
        finally:
            _unregister_session_subscriber(sessionKey, subscriber_id)

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@app.get("/api/health")
async def health():
    """Lightweight health check — no auth required."""
    return {"status": "ok"}

@app.get("/api/status")
async def status(user=Depends(get_current_user)):
    client = await get_client()
    return {"connected": client.connected, "user": user["username"], "role": user["role"]}


# ── 登录 ────────────────────────────────────────────────────────

@app.post("/api/login")
async def login(req: LoginReq):
    """Username + password → Bearer token."""
    token = users.authenticate(req.username, req.password)
    if not token:
        raise HTTPException(401, "用户名或密码错误")
    user = users.get_user_by_username(req.username)
    return {"token": token, "username": user["username"], "role": user["role"], "display_name": user["display_name"]}


@app.post("/api/logout", status_code=204)
async def logout(user=Depends(get_current_user)):
    """Revoke current token."""
    token = None  # 从 Header 获取太麻烦，直接按用户撤销所有 token
    users.logout_all(user["id"])
    return None


# ── 修改密码 ──────────────────────────────────────────────────

@app.post("/api/change-password")
async def change_password(req: ChangePasswordReq, user=Depends(get_current_user)):
    """用户自助修改密码。验证旧密码，成功后更新并撤销所有旧 token（强制重新登录）。"""
    # 验证旧密码
    stored = users.get_user_by_id(user["id"])
    if not stored or stored["password_hash"] != users._hash_password(req.old_password):
        raise HTTPException(400, "旧密码错误")
    # 校验新密码长度
    if len(req.new_password) < 6:
        raise HTTPException(400, "新密码至少 6 位")
    # 更新密码
    users.update_user(user["id"], password=req.new_password)
    # 撤销所有旧 token，强制重新登录
    users.logout_all(user["id"])
    return {"ok": True, "message": "密码已修改，请重新登录"}


# ── 用户管理（仅管理员）────────────────────────────────────────

@app.get("/api/admin/users")
async def admin_list_users(user=Depends(get_current_user)):
    if user["role"] != "admin":
        raise HTTPException(403, "管理员权限 required")
    return users.list_users()


@app.post("/api/admin/users")
async def admin_create_user(
    username: str,
    password: str,
    role: str = "user",
    display_name: str = "",
    user=Depends(get_current_user),
):
    if user["role"] != "admin":
        raise HTTPException(403, "管理员权限 required")
    u = users.create_user(username, password, role, display_name)
    if not u:
        raise HTTPException(400, "用户名已存在")
    return u


@app.delete("/api/admin/users/{uid}")
async def admin_delete_user(uid: int, user=Depends(get_current_user)):
    if user["role"] != "admin":
        raise HTTPException(403, "管理员权限 required")
    users.delete_user(uid)
    return {"ok": True}


# ── 聊天 ────────────────────────────────────────────────────────

def _agent_id(user_role: str) -> str:
    """admin → dev, user → user"""
    return "dev" if user_role == "admin" else "user"


def _get_session_key(user: dict) -> str:
    """Get current session key from DB (server-side)."""
    agent_id = _agent_id(user['role'])
    return users.get_or_create_session(user['id'], agent_id)


async def _get_active_session_model(client, session_key: str) -> str:
    """Return the model actually attached to the current Gateway session."""
    if not session_key:
        return ""
    sess_res = await client.rpc("sessions.list", {"limit": 100})
    payload = sess_res.get("payload", {}) if sess_res.get("ok") else {}
    sessions = payload.get("sessions", [])
    for sess in sessions:
        if sess.get("key") == session_key or sess.get("sessionKey") == session_key:
            return sess.get("model") or ""
    return ""


def _normalize_model_name(model: str) -> str:
    """Normalize provider/model strings for the H5 model badge."""
    if not model or not isinstance(model, str):
        return ""
    model = model.strip()
    if "/" in model:
        model = model.split("/", 1)[1]
    return model


def _extract_model_value(value) -> str:
    """Best-effort extraction of the effective model from Gateway payloads/events."""
    if isinstance(value, str):
        return _normalize_model_name(value)
    if not isinstance(value, dict):
        return ""

    for key in ("model", "actualModel", "activeModel", "currentModel", "effectiveModel", "resolvedModel"):
        model = _normalize_model_name(value.get(key))
        if model:
            return model

    for key in ("payload", "data", "session", "run", "agent", "meta"):
        model = _extract_model_value(value.get(key))
        if model:
            return model

    return ""


@app.get("/api/session")
async def get_session(user=Depends(get_current_user)):
    """Get current (active) session key (server-managed)."""
    sk = _get_session_key(user)
    return {"sessionKey": sk}


@app.get("/api/sessions")
async def list_sessions(user=Depends(get_current_user)):
    """List all sessions for current user."""
    agent_id = _agent_id(user['role'])
    sessions = users.list_sessions(user['id'], agent_id)
    return {"sessions": sessions}


@app.post("/api/sessions")
async def create_session(req: dict = None, user=Depends(get_current_user)):
    """Create a new session and set it as active."""
    agent_id = _agent_id(user['role'])
    title = ""
    if req and isinstance(req, dict):
        title = req.get("title", "")
    sk = users.new_session(user['id'], agent_id, title=title)
    return {"sessionKey": sk, "created": True}


@app.put("/api/sessions/active")
async def switch_session(req: dict, user=Depends(get_current_user)):
    """Switch active session."""
    agent_id = _agent_id(user['role'])
    sk = req.get("sessionKey", "")
    if not sk:
        raise HTTPException(400, "sessionKey is required")
    result = users.switch_session(user['id'], agent_id, sk)
    if not result:
        raise HTTPException(404, "Session not found")
    return {"sessionKey": sk, "switched": True}


@app.delete("/api/sessions")
async def delete_session(sessionKey: str = Query(...), user=Depends(get_current_user)):
    """Delete a non-active session."""
    agent_id = _agent_id(user['role'])
    ok = users.delete_session(user['id'], agent_id, sessionKey)
    if not ok:
        raise HTTPException(400, "Cannot delete active session or session not found")
    return {"deleted": True}


@app.patch("/api/sessions")
async def rename_session(req: dict, user=Depends(get_current_user)):
    """Rename a session."""
    agent_id = _agent_id(user['role'])
    sk = req.get("sessionKey", "")
    title = req.get("title", "")
    if not sk:
        raise HTTPException(400, "sessionKey is required")
    users.rename_session(user['id'], agent_id, sk, title)
    return {"renamed": True}


@app.post("/api/session/new")
async def new_session_legacy(user=Depends(get_current_user)):
    """Create a new session (legacy /new command compat)."""
    agent_id = _agent_id(user['role'])
    sk = users.new_session(user['id'], agent_id)
    return {"sessionKey": sk}


@app.get("/api/models")
async def models(sessionKey: str = Query(None), user=Depends(get_current_user)):
    client = await get_client()
    res = await client.rpc("models.list")
    if not res.get("ok"):
        raise HTTPException(500, json.dumps(res.get("error")))
    payload = res["payload"]
    sk = sessionKey or _get_session_key(user)
    # 优先用运行时模型缓存（/model 切换后的实际模型）
    cached_model = _session_runtime_model.get(sk, "")
    if cached_model:
        payload["default"] = cached_model
    else:
        try:
            active_model = await _get_active_session_model(client, sk)
            payload["default"] = active_model
        except Exception:
            if sessionKey:
                payload["default"] = ""
                return payload
            try:
                with open("/root/.openclaw/openclaw.json", "r", encoding="utf-8") as f:
                    config = json.load(f)
                primary = config.get("agents", {}).get("defaults", {}).get("model", {}).get("primary", "")
                if "/" in primary:
                    primary = primary.split("/", 1)[1]
                payload["default"] = primary
            except Exception:
                payload["default"] = ""
    return payload


@app.get("/api/history")
async def history(
    limit: int = Query(50),
    sessionKey: str = Query(None),
    user=Depends(get_current_user),
):
    client = await get_client()
    # Use query param sessionKey if provided, otherwise use active session
    if sessionKey:
        sk = sessionKey
    else:
        sk = _get_session_key(user)
    res = await client.rpc("chat.history", {
        "sessionKey": sk,
        "limit": limit,
    })
    if not res.get("ok"):
        raise HTTPException(500, json.dumps(res.get("error")))
    return res["payload"]


@app.post("/api/chat/legacy")
async def chat_send_legacy(req: ChatReq, user=Depends(get_current_user)):
    client = await get_client()
    # Use client-provided session_key, or fall back to server active session
    agent_id = _agent_id(user['role'])
    session_key = req.session_key or users.get_or_create_session(user['id'], agent_id)
    # Touch session (update updated_at)
    users.touch_session(user['id'], agent_id, session_key)
    idem_key = str(uuid.uuid4())

    # 确保全局 agent 监听器已注册
    _register_global_agent_listener(client)

    # 给消息加 [H5] 前缀标识来源
    tagged_message = f"[H5] {req.message}" if not req.message.startswith("[H5]") else req.message

    # 检测 /model 切换命令，更新运行时模型缓存
    model_match = re.match(r'^/model\s+(\S+)', req.message.strip())
    if model_match:
        target_model = model_match.group(1)
        _session_runtime_model[session_key] = _normalize_model_name(target_model)
        logger.info("Detected /model switch: sessionKey=%s → %s", session_key[:30], target_model)
    send_res = await client.rpc("chat.send", {
        "sessionKey": session_key,
        "message": tagged_message,
        "idempotencyKey": idem_key,
    })

    if not send_res.get("ok"):
        raise HTTPException(500, json.dumps(send_res.get("error")))

    run_id = send_res.get("payload", {}).get("runId", "")
    send_model = _extract_model_value(send_res)
    logger.info(
        "chat.send ok, runId=%s, sessionKey=%s, payload_keys=%s, model=%s",
        run_id,
        session_key,
        list(send_res.get("payload", {}).keys()),
        send_model or "",
    )

    # 清理过期缓存
    _cleanup_old_cache()
    # 清理文件上传缓存（每个 run 重新检测）
    _uploaded_files_cache.clear()

    # 注册消息缓存（用 chat.send 返回的 runId）
    _run_cache[run_id] = {
        "events": [],
        "status": "streaming",
        "created_at": time.time(),
    }
    _run_to_parent_session[run_id] = session_key

    # 用 sessionKey 注册事件队列（因为 agent event 用 sessionKey 而非 runId）
    event_queue: asyncio.Queue = asyncio.Queue()
    _agent_run_queues[session_key] = event_queue
    _agent_session_to_run[session_key] = run_id
    logger.info("Registered queue for sessionKey=%s (runId=%s), active queues: %s", session_key, run_id, list(_agent_run_queues.keys()))

    async def stream_response():
        last_model_sent = ""
        model_probe_delays = [0.75, 1.5, 3, 6, 10]
        model_probe_started_at = time.time()
        model_probe_index = 0
        seen_assistant_event_signatures: set[str] = set()

        def model_event(model: str, source: str) -> str:
            nonlocal last_model_sent
            normalized = _normalize_model_name(model)
            if not normalized or normalized == last_model_sent:
                return ""
            last_model_sent = normalized
            logger.info(
                "SSE model event: sessionKey=%s, runId=%s, model=%s, source=%s",
                session_key[:30],
                run_id[:12],
                normalized,
                source,
            )
            return f"data: {json.dumps({'type': 'model', 'model': normalized, 'source': source}, ensure_ascii=False)}\n\n"

        async def emit_session_model(source: str) -> str:
            try:
                # 直接查 Gateway sessions.list 获取运行时模型（含降级信息）
                fresh_client = await get_client()
                runtime_model = await _get_active_session_model(fresh_client, session_key)
                if runtime_model:
                    return model_event(runtime_model, source)
                # fallback: openclaw.json 静态配置
                _agent_id = session_key.split(":")[1] if ":" in session_key else ""
                try:
                    with open("/root/.openclaw/openclaw.json", "r", encoding="utf-8") as cf:
                        _cfg = json.load(cf)
                    for _a in _cfg.get("agents", {}).get("list", []):
                        if _a.get("id") == _agent_id:
                            _primary = _a.get("model", {}).get("primary", "")
                            if _primary:
                                return model_event(_normalize_model_name(_primary), source)
                            break
                except Exception:
                    pass
                return ""
            except Exception as e:
                logger.info("Failed to fetch active model (%s): %s", source, e)
                return ""

        async def emit_scheduled_model_probe(source: str) -> str:
            nonlocal model_probe_index
            event_payload = await emit_session_model(source)
            model_probe_index += 1
            return event_payload

        def seconds_until_next_model_probe() -> float | None:
            if model_probe_index >= len(model_probe_delays):
                return None
            due_at = model_probe_started_at + model_probe_delays[model_probe_index]
            return max(0.05, due_at - time.time())

        # 获取实际模型：优先级 runtime cache > openclaw.json agent config > sessions.list
        actual_model = send_model
        if not actual_model:
            actual_model = _session_runtime_model.get(session_key, "")
        if not actual_model:
            try:
                with open("/root/.openclaw/openclaw.json", "r", encoding="utf-8") as cf:
                    _cfg = json.load(cf)
                _agent_id = session_key.split(":")[1] if ":" in session_key else ""
                for _a in _cfg.get("agents", {}).get("list", []):
                    if _a.get("id") == _agent_id:
                        _primary = _a.get("model", {}).get("primary", "")
                        if _primary:
                            actual_model = _normalize_model_name(_primary)
                        break
                if not actual_model:
                    actual_model = _normalize_model_name(
                        _cfg.get("agents", {}).get("defaults", {}).get("model", {}).get("primary", "")
                    )
            except Exception:
                pass
        if not actual_model:
            try:
                fresh_client = await get_client()
                actual_model = await _get_active_session_model(fresh_client, session_key)
            except Exception:
                pass
        initial_model_event = model_event(actual_model or send_model, "config")
        if initial_model_event:
            yield initial_model_event
        else:
            session_model_event = await emit_session_model("sessions.list:start")
            if session_model_event:
                yield session_model_event

        done = False
        lifecycle_ended = False  # 标记是否已收到 lifecycle end
        has_spawned_subagent = False  # 标记是否 spawn 了子 agent
        _announce_status_sent = [False]  # 用 list 包装以便在闭包内修改
        _is_filtered_reply = [False]  # 标记整个回复是否为过滤文本，避免断连后 full_result 泄漏
        pushed_valid_text = False
        assistant_buffer = ""  # 累积 assistant 文本，用于检测 NO_REPLY/HEARTBEAT_OK
        buffering_assistant_text = True  # 开始时缓冲，确认不是过滤文本后再切换到直推模式
        idle_count = 0  # 连续无事件计数
        refreshed_model_after_first_event = False
        MAX_IDLE_ROUNDS = 30  # lifecycle end 后再等 30 轮（每轮 10s = 300s）确认无后续
        logger.info("stream_response started for sessionKey=%s, runId=%s", session_key, run_id)
        try:
            while not done:
                base_timeout = 10 if (lifecycle_ended and has_spawned_subagent) else 300
                next_probe_timeout = seconds_until_next_model_probe()
                timeout = min(base_timeout, next_probe_timeout) if next_probe_timeout is not None else base_timeout
                try:
                    event = await asyncio.wait_for(event_queue.get(), timeout=timeout)
                except asyncio.TimeoutError:
                    if next_probe_timeout is not None and next_probe_timeout <= base_timeout:
                        probe_event = await emit_scheduled_model_probe(f"sessions.list:probe-{model_probe_index + 1}")
                        if probe_event:
                            yield probe_event
                        continue
                    if lifecycle_ended and has_spawned_subagent:
                        idle_count += 1
                        logger.info("SSE post-lifecycle idle %d/%d for sessionKey=%s", idle_count, MAX_IDLE_ROUNDS, session_key)
                        if idle_count >= MAX_IDLE_ROUNDS:
                            logger.info("SSE no more events after lifecycle end, closing sessionKey=%s", session_key)
                            done = True
                            break
                        continue
                    else:
                        yield f"data: {json.dumps({'type': 'error', 'message': 'timeout'})}\n\n"
                        break

                # 有事件来了，重置 idle 计数
                if lifecycle_ended:
                    idle_count = 0

                stream = event.get("stream", "")
                data = event.get("data", {})
                logger.info("SSE raw event: stream=%s, data_keys=%s, runId=%s", stream, list(data.keys())[:5], event.get("runId", "")[:12])

                event_model = _extract_model_value(event)
                event_model_payload = model_event(event_model, f"agent.{stream}")
                if event_model_payload:
                    yield event_model_payload
                elif not refreshed_model_after_first_event:
                    refreshed_model_after_first_event = True
                    session_model_event = await emit_session_model("sessions.list:first-event")
                    if session_model_event:
                        yield session_model_event

                if stream == "assistant":
                    delta = data.get("delta", "")
                    is_child = event.get("_is_child", False)
                    source = "acp" if is_child else "main"
                    should_skip_delta = False

                    # 去重：某些情况下同一 run 的 assistant payload 会被重复投递。
                    # 对完全相同的 assistant payload 做事件级去重，避免前端文本被重复追加。
                    assistant_sig = None
                    if not is_child:
                        try:
                            assistant_sig = json.dumps(data, ensure_ascii=False, sort_keys=True)
                        except Exception:
                            assistant_sig = f"delta:{delta}"
                        if assistant_sig in seen_assistant_event_signatures:
                            logger.info(
                                "SSE assistant duplicate skipped: runId=%s, sessionKey=%s, delta=%s",
                                run_id[:12],
                                session_key[:30],
                                repr(delta[:50]) if delta else 'EMPTY',
                            )
                            continue
                        seen_assistant_event_signatures.add(assistant_sig)

                    if delta:
                        if buffering_assistant_text:
                            assistant_buffer += delta
                            buffer_stripped = assistant_buffer.strip()
                            is_full_match = buffer_stripped in _FILTERED_STREAM_TEXTS
                            is_prefix = any(
                                filtered_text.startswith(buffer_stripped)
                                for filtered_text in _FILTERED_STREAM_TEXTS
                            )

                            if is_full_match:
                                should_skip_delta = True
                                logger.info("SSE filtered complete text: buffer=%s", repr(buffer_stripped))
                            elif is_prefix:
                                should_skip_delta = True
                                logger.info("SSE buffering possible filtered text prefix: buffer=%s", repr(buffer_stripped))
                            else:
                                buffering_assistant_text = False
                                pushed_valid_text = True
                                logger.info("SSE assistant buffer released: buffer=%s", repr(assistant_buffer[:50]))
                                yield f"data: {json.dumps({'type': 'text', 'content': assistant_buffer, 'source': source, 'childEventId': event.get('_child_event_id') if is_child else None}, ensure_ascii=False)}\n\n"

                                # 检测本地文件路径，自动上传到 COS 并推送文件事件
                                for fpath in _extract_file_paths(assistant_buffer):
                                    file_info = cos_util.upload_local_file(fpath)
                                    if file_info:
                                        logger.info("[COS] Auto-uploaded agent file: %s -> %s", fpath, file_info["url"])
                                        yield f"data: {json.dumps({'type': 'file', 'file': file_info}, ensure_ascii=False)}\n\n"

                                assistant_buffer = ""
                        else:
                            assistant_buffer = ""
                    logger.info(
                        "SSE assistant _is_child=%s, source=%s, delta=%s, skip=%s",
                        is_child,
                        source,
                        repr(delta[:30]) if delta else 'EMPTY',
                        should_skip_delta,
                    )
                    logger.info("SSE assistant delta=%s, data keys=%s, post-lifecycle=%s", repr(delta[:50]) if delta else 'EMPTY', list(data.keys()), lifecycle_ended)
                    if delta and not should_skip_delta and not buffering_assistant_text:
                        pushed_valid_text = True
                        yield f"data: {json.dumps({'type': 'text', 'content': delta, 'source': source, 'childEventId': event.get('_child_event_id') if is_child else None}, ensure_ascii=False)}\n\n"

                        # 检测本地文件路径，自动上传到 COS 并推送文件事件
                        for fpath in _extract_file_paths(delta):
                            file_info = cos_util.upload_local_file(fpath)
                            if file_info:
                                logger.info("[COS] Auto-uploaded agent file: %s -> %s", fpath, file_info["url"])
                                yield f"data: {json.dumps({'type': 'file', 'file': file_info}, ensure_ascii=False)}\n\n"

                    if lifecycle_ended and has_spawned_subagent and not _announce_status_sent[0]:
                        # lifecycle end 之后的 assistant 事件，说明是 spawn 子 agent 完成后的后续回复
                        # 只推一次状态提示，避免刷屏
                        _announce_status_sent[0] = True
                        yield f"data: {json.dumps({'type': 'status', 'message': '子任务完成，继续处理中...'}, ensure_ascii=False)}\n\n"

                elif stream == "item":
                    # tool / command 事件（OpenClaw 格式）
                    # kind: tool | command | message
                    # phase: start | update | end
                    item_kind = data.get("kind", "")
                    item_name = data.get("name", "")
                    item_phase = data.get("phase", "")
                    item_title = data.get("title", "")
                    item_summary = data.get("summary", "")
                    is_child = event.get("_is_child", False)
                    source = "acp" if is_child else "main"
                    logger.info(
                        "SSE item _is_child=%s, source=%s, kind=%s, name=%s, phase=%s",
                        is_child,
                        source,
                        item_kind,
                        item_name,
                        item_phase,
                    )
                    logger.info("SSE item: kind=%s, name=%s, phase=%s, title=%.40s (post-lc=%s)", item_kind, item_name, item_phase, item_title, lifecycle_ended)

                    # 检测是否 spawn 了子 agent
                    if item_name == "sessions_spawn" or "spawn" in item_name.lower():
                        has_spawned_subagent = True
                        logger.info("SSE detected sub-agent spawn. sessionKey=%s", session_key)

                    if item_kind == "tool":
                        if item_phase == "start":
                            payload = {
                                "type": "tool_use",
                                "tool": item_name or item_title or "tool",
                                "name": item_name or item_title or "tool",
                            }
                            if is_child:
                                payload["source"] = "acp"
                                payload["childEventId"] = event.get("_child_event_id")
                            yield f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"
                        elif item_phase == "end":
                            payload = {
                                "type": "tool_result",
                                "tool": item_name or item_title or "tool",
                                "name": item_name or item_title or "tool",
                                "summary": item_summary or item_title or "",
                            }
                            if is_child:
                                payload["source"] = "acp"
                                payload["childEventId"] = event.get("_child_event_id")
                            yield f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"

                elif stream == "command_output":
                    text = data.get("text", "")
                    is_child = event.get("_is_child", False)
                    source = "acp" if is_child else "main"
                    logger.info(
                        "SSE command_output _is_child=%s, source=%s, text=%s",
                        is_child,
                        source,
                        repr(text[:30]) if text else 'EMPTY',
                    )
                    if is_child and text:
                        yield f"data: {json.dumps({'type': 'command_output', 'text': text, 'source': 'acp', 'childEventId': event.get('_child_event_id')}, ensure_ascii=False)}\n\n"
                    else:
                        # exec 命令输出 - 非子 agent 不推送前端，仅日志
                        logger.debug("SSE command_output (suppressed): %s", repr(text[:100]))

                elif stream == "plan":
                    # 计划/思维过程
                    plan_text = data.get("text", "") or data.get("summary", "")
                    if plan_text:
                        yield f"data: {json.dumps({'type': 'plan', 'text': plan_text}, ensure_ascii=False)}\n\n"

                elif stream == "lifecycle":
                    phase = data.get("phase", "unknown")
                    logger.info("SSE lifecycle phase=%s, runId=%s, sessionKey=%s", phase, event.get("runId", "")[:12], session_key[:20])
                    if phase == "end":
                        if buffering_assistant_text and assistant_buffer.strip() in _FILTERED_STREAM_TEXTS:
                            _is_filtered_reply[0] = True
                            logger.info(
                                "SSE lifecycle end: reply is filtered text '%s', will skip full_result",
                                assistant_buffer.strip(),
                            )
                        if not lifecycle_ended:
                            lifecycle_ended = True
                            if has_spawned_subagent:
                                logger.info("SSE lifecycle end (sub-agent active), waiting for announce. sessionKey=%s", session_key)
                            else:
                                # 没有 spawn 子 agent，直接结束
                                logger.info("SSE lifecycle end (no sub-agent), closing immediately. sessionKey=%s", session_key)
                                done = True
                                break
                        else:
                            # 第二次 lifecycle end = announce 完成
                            logger.info("SSE second lifecycle end, closing. sessionKey=%s", session_key)
                            done = True
                            break

        except GeneratorExit:
            # 客户端断开连接（移动端常见），不视为错误
            logger.info("SSE client disconnected for sessionKey=%s (cache will continue)", session_key)
        except Exception as e:
            logger.exception("Stream error")
            yield f"data: {json.dumps({'type': 'error', 'message': str(e)})}\n\n"
        finally:
            # 只清理 queue（停止接收新事件），但保留 session→run 映射和 cache
            # 这样 announce 事件仍能写入 cache，前端可轮询获取
            if _agent_run_queues.get(session_key) is event_queue:
                _agent_run_queues.pop(session_key, None)
                logger.info("SSE queue removed for sessionKey=%s, keeping cache for announce (runId=%s)", session_key[:30], run_id[:12])
            else:
                logger.info(
                    "SSE queue cleanup skipped for sessionKey=%s because a newer queue is registered",
                    session_key[:30],
                )

            # ── 流结束补全：从 Gateway history 拉完整结果 ──
            # 类似 QQBot 的机制，确保前端拿到完整回复
            try:
                await asyncio.sleep(1)  # 等 Gateway 写入 history
                client = await get_client()
                hist = await client.rpc("session.history", {
                    "sessionKey": session_key,
                    "limit": 5,
                })
                messages = hist.get("messages", [])
                # 找最后一条 assistant 消息
                for msg in reversed(messages):
                    if msg.get("role") == "assistant":
                        full_text = msg.get("content", "")
                        if full_text and isinstance(full_text, str):
                            if full_text in _FILTERED_STREAM_TEXTS or _is_filtered_reply[0]:
                                logger.info(
                                    "SSE full_result suppressed for sessionKey=%s, text=%s, pushed_valid_text=%s, filtered_reply=%s",
                                    session_key[:30],
                                    full_text,
                                    pushed_valid_text,
                                    _is_filtered_reply[0],
                                )
                            else:
                                # 推送完整结果（前端可用来补全/替换）
                                yield f"data: {json.dumps({'type': 'full_result', 'text': full_text}, ensure_ascii=False)}\n\n"
                                logger.info("SSE full_result sent for sessionKey=%s, text_len=%d", session_key[:30], len(full_text))
                        break
            except Exception as e:
                logger.warning("SSE history fallback failed: %s", e)

            final_model_event = await emit_session_model("sessions.list:final")
            if final_model_event:
                yield final_model_event

            yield f"data: {json.dumps({'type': 'done'})}\n\n"

            # 延迟清理 cache 和 session→run 映射（给 announce 留 120 秒窗口）
            async def _delayed_cleanup():
                await asyncio.sleep(120)
                if _agent_session_to_run.get(session_key) == run_id:
                    _agent_session_to_run.pop(session_key, None)
                    logger.info("Delayed cleanup: sessionKey=%s, runId=%s (session mapping removed, cache kept)",
                                session_key[:30], run_id[:12])
                else:
                    logger.info(
                        "Delayed cleanup skipped for sessionKey=%s because a newer run mapping is registered",
                        session_key[:30],
                    )
            asyncio.create_task(_delayed_cleanup())

    return StreamingResponse(
        stream_response(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "X-Run-Id": run_id,  # 让前端可以从 header 拿到 runId
        },
    )


@app.post("/api/chat")
@app.post("/api/chat/v2")
async def chat_send(req: ChatReq, user=Depends(get_current_user)):
    """Phase 2: JSON-only chat endpoint (no SSE response).

    Sends the message to Gateway, returns immediately with runId.
    All events are streamed to the persistent /api/events subscribers.
    """
    client = await get_client()
    agent_id = _agent_id(user['role'])
    session_key = req.session_key or users.get_or_create_session(user['id'], agent_id)
    users.touch_session(user['id'], agent_id, session_key)
    idem_key = str(uuid.uuid4())

    _register_global_agent_listener(client)

    tagged_message = f"[H5] {req.message}" if not req.message.startswith("[H5]") else req.message

    # /model command → update runtime cache
    model_match = re.match(r'^/model\s+(\S+)', req.message.strip())
    if model_match:
        _session_runtime_model[session_key] = _normalize_model_name(model_match.group(1))

    send_res = await client.rpc("chat.send", {
        "sessionKey": session_key,
        "message": tagged_message,
        "idempotencyKey": idem_key,
    })

    if not send_res.get("ok"):
        raise HTTPException(500, json.dumps(send_res.get("error")))

    run_id = send_res.get("payload", {}).get("runId", "")
    logger.info("chat_send: runId=%s, sessionKey=%s", run_id[:12], session_key[:30])

    _cleanup_old_cache()
    _uploaded_files_cache.clear()

    _run_cache[run_id] = {"events": [], "status": "streaming", "created_at": time.time()}
    _run_to_parent_session[run_id] = session_key

    event_queue: asyncio.Queue = asyncio.Queue()
    _agent_run_queues[session_key] = event_queue
    _agent_session_to_run[session_key] = run_id

    # Publish run.started event to persistent subscribers
    await _publish_session_event(session_key, {
        "eventId": f"evt-{uuid.uuid4().hex[:16]}",
        "sessionKey": session_key,
        "runId": run_id,
        "source": "main",
        "ts": int(time.time() * 1000),
        "kind": "run.started",
        "payload": {"message": req.message[:100]},
    })

    return {"ok": True, "runId": run_id, "sessionKey": session_key}


@app.get("/api/chat/{run_id}/result")
async def chat_result(run_id: str, user=Depends(get_current_user)):
    """查询已缓存的 run 结果，用于 SSE 断连后恢复。

    返回: { status: "streaming"|"done"|"error", text: "完整文本" }
    """
    cache = _run_cache.get(run_id)
    if not cache:
        raise HTTPException(404, "runId not found or expired")

    # 从缓存事件中提取完整文本
    main_text = ""
    acp_text = ""
    for ev in cache["events"]:
        if ev.get("stream") == "assistant":
            delta = ev.get("data", {}).get("delta", "")
            if delta:
                if ev.get("_is_child", False):
                    acp_text += delta
                else:
                    main_text += delta

    return {
        "runId": run_id,
        "status": cache["status"],
        "text": main_text,
        "mainText": main_text,
        "acpText": acp_text,
    }


@app.get("/api/chat/{run_id}/child-events")
async def chat_child_events(
    run_id: str,
    offset: int = Query(0, ge=0),
    user=Depends(get_current_user),
):
    """查询父 run 对应的子 agent 缓存事件，用于 SSE done 后补拉。"""
    _cleanup_old_cache()

    cache = _run_cache.get(run_id)
    if not cache:
        raise HTTPException(404, "runId not found or expired")

    parent_session_key = _run_to_parent_session.get(run_id)
    if not parent_session_key:
        raise HTTPException(404, "runId not found or expired")

    all_events = [_public_child_event(ev) for ev in _child_events_cache.get(parent_session_key, [])]
    total = len(all_events)
    events = all_events[offset:] if offset < total else []
    undelivered_events = [
        ev for ev in events
        if ev.get("_delivered_to_parent_queue") is False
    ]
    status = cache.get("status", "streaming")

    return {
        "events": events,
        "undeliveredEvents": undelivered_events,
        "offset": offset,
        "nextOffset": total,
        "total": total,
        "status": status,
        "hasMore": status != "done" or offset < total,
    }


@app.post("/api/abort")
async def abort_chat(sessionKey: str = Query(None), user=Depends(get_current_user)):
    client = await get_client()
    if sessionKey:
        sk = sessionKey
    else:
        sk = _get_session_key(user)
    res = await client.rpc("chat.abort", {"sessionKey": sk})
    return {"ok": res.get("ok", False)}


@app.post("/api/upload")
async def upload_file(
    file: UploadFile = File(...),
    user=Depends(get_current_user),
):
    """上传文件到 COS，返回公开访问 URL"""
    if not file.filename:
        raise HTTPException(400, "文件名不能为空")

    # 读取文件内容
    content = await file.read()

    # 限制 50MB
    if len(content) > 300 * 1024 * 1024:
        raise HTTPException(413, "文件大小不能超过 50MB")

    # 根据文件类型设置 Content-Type
    content_type = file.content_type or "application/octet-stream"

    url = cos_util.upload_file(content, file.filename, content_type)
    return {"url": url, "filename": file.filename}
