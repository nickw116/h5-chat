"""FastAPI bridge: H5 frontend ↔ OpenClaw Gateway WebSocket RPC."""

import asyncio
import glob
import json
import os
import re
import uuid
import logging
import time
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Optional

from fastapi import FastAPI, HTTPException, Header, Depends, Query, UploadFile, File, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse, FileResponse
from pydantic import BaseModel

from . import config
from . import users
from . import cos_util
from .ws_client import get_client, add_migrating_listener

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("bridge")


# ── 文件路径检测（用于自动上传 agent 生成的文件）───────────────────
# 匹配常见文件路径 + 扩展名（PPT, PDF, Word, Excel, 图片, 压缩包, 文本等）
# 使用非路径字符作为前后边界，能匹配中文、标点、括号等后面的本地路径
_FILE_EXT_PATTERN = re.compile(
    r'(?:^|(?<![\w/.\-]))'
    r'((?:/root/|/tmp/|/home/|/var/|\.\./|\./)[\w/.\-]+\.'
    r'(?:pptx?|xlsx?|docx?|pdf|png|jpe?g|gif|svg|zip|rar|7z|tar\.gz|txt|csv|json|md|py|js|html?|css))'
    r'(?:$|(?![\w/.\-]))',
    re.IGNORECASE
)

# 去重：同一个文件只上传一次（按 run 的生命周期）
_uploaded_files_cache: set[str] = set()

# 已上传文件的本地路径 → COS URL 映射（用于跨 delta 文本替换）
_uploaded_file_url_map: dict[str, str] = {}


def _replace_uploaded_paths(text: str) -> str:
    """将文本中所有已上传的本地路径替换为对应的 COS URL。"""
    if not _uploaded_file_url_map:
        return text
    for match in _FILE_EXT_PATTERN.finditer(text):
        path = match.group(1).rstrip('.,;:!?)')
        url = _uploaded_file_url_map.get(path)
        if url:
            text = text.replace(path, url)
    return text
_FILTERED_STREAM_TEXTS = {"NO_REPLY", "HEARTBEAT_OK"}

# H5 前端不显示主 agent (dev) 的工具调用卡片
# 注意：仅对主 agent 生效；ACP 子 agent (subagent/acp 会话) 的同名工具不会被过滤
_H5_FILTERED_MAIN_AGENT_TOOLS = frozenset({
    "sessions_spawn",
    "sessions_yield",
    "sessions_list",
    "subagents",
    "subagents_list",
    "subagents_kill",
    "subagents_steer",
    "session_status",
    "memory_get",
    "memory_search",
    "read",
    "write",
    "edit",
    "exec",
})

_ACP_FORCE_PATTERNS = [
    r'\b(?:acp|claude|claude code|cursor|copilot|gemini(?: +cli)?|qwen|kiro|kimi|opencode)\b',
    r'用(?:acp|claude|claude code|cursor|copilot|gemini|qwen|kiro|kimi)',
    r'交给(?:acp|claude|子代理|子 ?agent|编码代理)',
    r'(?:走|开启|切到|进入)(?:acp|编码模式|子代理)',
    r'直接用(?:acp|claude|子代理)',
]

_ACP_SOFT_PATTERNS = [
    r'修(?:一下|复)?(?:这个|一下)?\s*(?:bug|问题|报错)',
    r'重构',
    r'改(?:一下|造)?代码',
    r'看(?:一下)?(?:这个|项目|仓库|代码库)',
    r'实现(?:一个|一下)?(?:功能|需求)',
    r'新增(?:一个|一下)?(?:功能|接口|页面|组件)',
    r'创建(?:文件|项目|页面|组件|脚本)',
    r'写(?:一个|一下)?(?:脚本|函数|组件|页面|接口)',
    r'帮我(?:修改|实现|开发|排查)',
    r'排查(?:一下)?(?:代码|项目|问题)',
]

_ACP_NEGATIVE_PATTERNS = [
    r'^/model\b',
    r'^/new\b',
    r'^/stop\b',
    r'只是问问',
    r'先别改',
    r'不要改代码',
    r'不用acp',
    r'不要acp',
    r'别用acp',
    r'仅分析',
    r'只读',
]


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


def _matches_any(text: str, patterns: list[str]) -> bool:
    return any(re.search(pattern, text, re.IGNORECASE) for pattern in patterns)


def _classify_acp_route(message: str) -> tuple[str, str]:
    """Return (mode, reason): force|suggest|off."""
    raw = (message or "").strip()
    if not raw:
        return ("off", "empty")

    if _matches_any(raw, _ACP_NEGATIVE_PATTERNS):
        return ("off", "negative")

    if _matches_any(raw, _ACP_FORCE_PATTERNS):
        return ("force", "explicit")

    if _matches_any(raw, _ACP_SOFT_PATTERNS):
        return ("suggest", "heuristic")

    return ("off", "default")


def _decorate_h5_message(message: str) -> tuple[str, str, str]:
    """Attach H5 source tag + ACP routing hint for dual-layer control."""
    raw = (message or "").strip()
    mode, reason = _classify_acp_route(raw)

    if raw.startswith("[H5]"):
        base = raw
    else:
        base = f"[H5] {raw}"

    if mode == "force":
        hint = (
            "\n\n[H5 ACP ROUTE]\n"
            "mode=force\n"
            "当用户消息涉及编码/项目修改时，优先使用 sessions_spawn 发起 ACP 子任务。"
            "若只是纯问答或解释，再直接回答。"
        )
        return (f"{base}{hint}", mode, reason)

    if mode == "suggest":
        hint = (
            "\n\n[H5 ACP ROUTE]\n"
            "mode=suggest\n"
            "这条消息疑似为复杂编码/项目任务。若需要查看文件、修改代码、运行命令、跨文件排查，优先使用 sessions_spawn；"
            "若是小修或纯解释，可直接处理。"
        )
        return (f"{base}{hint}", mode, reason)

    return (base, mode, reason)

# ── 全局 agent 事件分发器 + 消息缓存 ──────────────────────────
_agent_run_queues: dict[str, asyncio.Queue] = {}  # sessionKey → queue
_session_event_subscribers: dict[str, dict[str, asyncio.Queue]] = {}  # sessionKey -> subscriberId -> queue
_agent_session_to_run: dict[str, str] = {}  # sessionKey → chat.send runId (for lifecycle end matching)
_run_to_parent_session: dict[str, str] = {}  # runId → parent sessionKey
_child_to_parent: dict[str, str] = {}  # child sessionKey → parent sessionKey (spawn 时注册)
_acp_run_cwd: dict[str, str] = {}  # runId → cwd (for claude:acp runs, to find CC project log)
_agent_listener_registered = False
_active_acp_runs: dict[str, dict] = {}  # runId -> {runId, status, started_at}
_EVENT_STREAM_HEARTBEAT_SEC = 8

# 运行时模型缓存：sessionKey → model（由 /model 命令或 SSE agent 事件更新）
_session_runtime_model: dict[str, str] = {}

# 消息缓存: runId → { events: [...], status: "streaming"|"done"|"error", created_at: float }
_run_cache: dict[str, dict] = {}
_child_events_cache: dict[str, list] = {}  # parent sessionKey → cached child events
_child_events_cache_created_at: dict[str, float] = {}  # parent sessionKey → first cached timestamp
_child_event_seq = 0
_RUN_CACHE_MAX_AGE = 1800  # 缓存保留 30 分钟


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
    expired = [rid for rid, v in _run_cache.items() if now - v.get("last_event_at", v["created_at"]) > _RUN_CACHE_MAX_AGE]
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

    # 清理无活动的 _active_acp_runs（超过5分钟无事件且仍在 running 的残留）
    expired_acp = [
        rid for rid, info in list(_active_acp_runs.items())
        if info.get("status") == "running" and now - info.get("last_event_at", info.get("started_at", 0)) > 300
    ]
    for rid in expired_acp:
        _active_acp_runs[rid]["status"] = "timeout"
    if expired_acp:
        logger.warning("Marked %d inactive active acp runs as timeout", len(expired_acp))
        _publish_acp_status()
    # 移除已 timeout 的条目（给前端同步窗口后清理）
    timed_out = [
        rid for rid, info in list(_active_acp_runs.items())
        if info.get("status") == "timeout"
    ]
    for rid in timed_out:
        _active_acp_runs.pop(rid, None)
        _acp_run_cwd.pop(rid, None)
    if timed_out:
        logger.info("Cleaned up %d timed-out acp runs", len(timed_out))
        _publish_acp_status()


def _register_session_subscriber(session_key: str, subscriber_id: str) -> asyncio.Queue:
    queue: asyncio.Queue = asyncio.Queue()
    # Deduplication: only one active subscriber per sessionKey.
    # Remove and drain any existing subscribers so events don't accumulate.
    existing = _session_event_subscribers.pop(session_key, {})
    if existing:
        for old_id, old_queue in existing.items():
            while not old_queue.empty():
                try:
                    old_queue.get_nowait()
                except asyncio.QueueEmpty:
                    break
    _session_event_subscribers[session_key] = {subscriber_id: queue}
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


def _publish_acp_status():
    """推送当前 ACP 状态到所有活跃的 event subscriber（内部先清理超2分钟无事件的残留）"""
    now = time.time()
    for rid, info in list(_active_acp_runs.items()):
        if info.get("status") == "running" and now - info.get("last_event_at", info.get("started_at", 0)) > 120:
            _active_acp_runs[rid]["status"] = "timeout"
    runs = []
    for rid, info in list(_active_acp_runs.items()):
        # 只推送真正在 running 的 run，已完成/超时的由延迟清理任务处理
        if info.get("status") != "running":
            continue
        run_data = {"runId": rid, "status": info.get("status", "running")}
        steps = info.get("steps", [])
        if steps:
            run_data["steps"] = steps
        runs.append(run_data)
    event = {
        "eventId": f"evt-acp-{uuid.uuid4().hex[:12]}",
        "kind": "acp_status",
        "ts": int(time.time() * 1000),
        "payload": {"count": len(runs), "runs": runs}
    }
    # 推送到所有 session 的 subscriber
    for sk, subs in list(_session_event_subscribers.items()):
        for sid, queue in list(subs.items()):
            try:
                queue.put_nowait(event)
            except Exception:
                pass


async def _publish_session_event(session_key: str, event: dict):
    if not session_key:
        return
    # Try both the full session key and the public (un-prefixed) key,
    # because /api/events registers subscribers with bare keys while
    # _publish_gateway_event may publish with agent-prefixed keys.
    tried_keys = set()
    for candidate_key in (session_key,):
        if candidate_key in tried_keys:
            continue
        tried_keys.add(candidate_key)
        # also add the public key (strip agent:xxx: prefix)
        if candidate_key.startswith("agent:"):
            parts = candidate_key.split(":", 2)
            if len(parts) == 3 and parts[2]:
                tried_keys.add(parts[2])

    all_keys = list(_session_event_subscribers.keys())
    logger.info(
        "_publish_session_event: session_key=%s tried_keys=%s all_subscriber_keys=%s all_subscriber_counts=%s event_kind=%s",
        session_key,
        tried_keys,
        all_keys,
        {k: len(v) for k, v in _session_event_subscribers.items()},
        event.get("kind"),
    )

    found_any = False
    for sk in tried_keys:
        subscribers = _session_event_subscribers.get(sk, {})
        if not subscribers:
            continue
        found_any = True
        logger.info("_publish_session_event: found %d subscribers for key=%s", len(subscribers), sk)
        stale_ids = []
        for subscriber_id, queue in list(subscribers.items()):
            try:
                queue.put_nowait(event)
                logger.info("_publish_session_event: queued event for subscriber=%s key=%s", subscriber_id[:8], sk)
            except Exception as e:
                logger.warning("_publish_session_event: put_nowait failed for subscriber=%s key=%s: %s", subscriber_id[:8], sk, e)
                stale_ids.append(subscriber_id)
        for subscriber_id in stale_ids:
            _unregister_session_subscriber(sk, subscriber_id)

    if not found_any:
        logger.warning("_publish_session_event: NO subscribers found for any tried key. session_key=%s tried_keys=%s", session_key, tried_keys)


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

        # Build candidate session keys for Gateway RPC.
        # Gateway session.history requires agent-prefixed keys.
        session_keys_to_try = [session_key]
        if session_key.startswith("agent:"):
            pass  # already prefixed
        else:
            for agent_id in ("dev", "user", "main"):
                session_keys_to_try.append(f"agent:{agent_id}:{session_key}")

        full_text = ""
        for sk in session_keys_to_try:
            try:
                hist = await client.rpc("session.history", {
                    "sessionKey": sk,
                    "limit": 5,
                })
                messages = hist.get("messages", [])
                for msg in reversed(messages):
                    if msg.get("role") == "assistant":
                        text = msg.get("content", "")
                        if text and isinstance(text, str) and text.strip() not in _FILTERED_STREAM_TEXTS:
                            full_text = text
                        break
                if full_text:
                    break
            except Exception:
                continue

        if not full_text:
            logger.info("_publish_full_result_from_history: no assistant text found for session=%s run=%s", session_key[:30], run_id[:12])
            return

        # Replace local file paths with COS URLs in the full text
        for fpath in _extract_file_paths(full_text):
            if fpath not in _uploaded_file_url_map:
                file_info = await asyncio.to_thread(cos_util.upload_local_file, fpath)
                if file_info:
                    _uploaded_file_url_map[fpath] = file_info["url"]
                    logger.info("[COS] Auto-uploaded agent file (full_result): %s -> %s", fpath, file_info["url"])
                    # Push file event
                    await _publish_session_event(session_key, {
                        "eventId": f"evt-{uuid.uuid4().hex[:16]}",
                        "sessionKey": session_key,
                        "runId": run_id,
                        "source": "main",
                        "ts": int(time.time() * 1000),
                        "kind": "media.file",
                        "payload": {"file": file_info},
                    })
        full_text = _replace_uploaded_paths(full_text)

        await _publish_session_event(session_key, {
            "eventId": f"evt-{uuid.uuid4().hex[:16]}",
            "sessionKey": session_key,
            "runId": run_id,
            "source": "main",
            "ts": int(time.time() * 1000),
            "kind": "full_result",
            "payload": {"text": full_text, "runId": run_id},
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
            cache["last_event_at"] = time.time()
    except Exception as e:
        logger.warning("_publish_full_result_from_history failed: %s", e)


# Buffer for accumulating assistant deltas per run (for cross-delta file path detection)
_assistant_delta_buffer: dict[str, str] = {}  # run_id -> accumulated text

async def _flush_file_paths_for_run(session_key: str, run_id: str, source: str, base_template: dict):
    """Scan accumulated assistant text for local file paths, upload to COS, push file events."""
    buffer_key = run_id
    accumulated = _assistant_delta_buffer.pop(buffer_key, "")
    if not accumulated:
        return
    # Detect file paths in the full accumulated text
    file_paths = _extract_file_paths(accumulated)
    if not file_paths:
        return
    logger.info("[COS] Found %d file paths in accumulated assistant text for run=%s", len(file_paths), run_id[:12])
    for fpath in file_paths:
        file_info = await asyncio.to_thread(cos_util.upload_local_file, fpath)
        if file_info:
            _uploaded_file_url_map[fpath] = file_info["url"]
            logger.info("[COS] Auto-uploaded agent file (persistent flush): %s -> %s", fpath, file_info["url"])
            # Push file event to frontend
            file_event_id = f"evt-{uuid.uuid4().hex[:16]}"
            file_base = {**base_template, "eventId": file_event_id}
            await _publish_session_event(session_key, {**file_base, "kind": "media.file", "payload": {"file": file_info}})

async def _publish_gateway_event(session_key: str, run_id: str, stream: str, payload: dict):
    """Convert raw Gateway agent event into a rich event and publish to persistent subscribers."""
    if not session_key:
        return
    logger.info("_publish_gateway_event: ENTER session_key=%s run_id=%s stream=%s", session_key, run_id[:12], stream)
    data = payload.get("data", {}) if isinstance(payload, dict) else {}
    is_child = bool(payload.get("_is_child"))
    source = "acp" if is_child else "main"
    child_fields = _child_event_fields(payload)
    event_id = f"evt-{uuid.uuid4().hex[:16]}"
    ts = int(time.time() * 1000)
    base = {"eventId": event_id, "sessionKey": session_key, "runId": run_id, "source": source, "ts": ts}

    if stream == "assistant":
        delta_text = data.get("delta", "")
        # Accumulate delta text for cross-fragment file path detection
        buffer_key = run_id
        _assistant_delta_buffer[buffer_key] = _assistant_delta_buffer.get(buffer_key, "") + delta_text
        # Also try immediate detection (for paths that arrive intact in a single delta)
        for fpath in _extract_file_paths(delta_text):
            file_info = await asyncio.to_thread(cos_util.upload_local_file, fpath)
            if file_info:
                _uploaded_file_url_map[fpath] = file_info["url"]
                logger.info("[COS] Auto-uploaded agent file (persistent immediate): %s -> %s", fpath, file_info["url"])
                file_event_id = f"evt-{uuid.uuid4().hex[:16]}"
                file_base = {**base, "eventId": file_event_id}
                await _publish_session_event(session_key, {**file_base, "kind": "media.file", "payload": {"file": file_info}})
        delta_text = _replace_uploaded_paths(delta_text)
        event = {**base, "kind": "assistant.delta", "payload": {"delta": delta_text, **child_fields}}
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
        if phase == "error":
            error_text = data.get("error", "")
            if error_text:
                event["payload"]["error"] = error_text
                # Also publish a standalone run_error event for persistent subscribers
                run_error_event = {**base, "kind": "run_error", "payload": {"error": error_text, "phase": phase}}
                await _publish_session_event(session_key, run_error_event)
        if phase == "end":
            # Flush accumulated assistant text for file path detection
            asyncio.create_task(_flush_file_paths_for_run(session_key, run_id, source, base))
            # After lifecycle end, pull full text from history and publish full_result
            asyncio.create_task(_publish_full_result_from_history(session_key, run_id))
    elif stream == "plan":
        event = {**base, "kind": "plan", "payload": {"text": data.get("text", "") or data.get("summary", "")}}
    elif stream == "model":
        event = {**base, "kind": "model.changed", "payload": {"model": data.get("model", ""), "source": data.get("source", "agent")}}
    else:
        event = {**base, "kind": f"{stream}", "payload": {"data": data, **child_fields}}

    await _publish_session_event(session_key, event)


def _cwd_to_project_dir(cwd: str) -> str:
    """Convert a cwd path to Claude Code project_dir format.
    project_dir = cwd with leading / removed and remaining / replaced by -"""
    if not cwd:
        return ""
    return cwd.replace("/", "-")


def _read_claude_project_log(session_key: str, run_id: str) -> list[dict]:
    """Read Claude Code project log and parse into structured event list.

    Returns list of dicts with keys: type (tool_use|tool_result|thinking), name, content, session_id
    """
    logger.info("_read_claude_project_log: ENTER session=%s run=%s", session_key[:20], run_id[:12])
    events = []
    # Try to find cwd from _acp_run_cwd first
    cwd = _acp_run_cwd.get(run_id, "")
    if not cwd:
        # Fallback: try to derive from session_key (for spawned subagent sessions)
        # session_key format: agent:dev:subagent:{uuid} or agent:dev:h5-admin-...
        pass

    if not cwd:
        # Final fallback: scan all CC project dirs for the latest jsonl
        projects_base = os.path.expanduser("~/.claude/projects")
        if os.path.isdir(projects_base):
            latest_log = None
            latest_mtime = 0
            for pd in os.listdir(projects_base):
                pd_path = os.path.join(projects_base, pd)
                if not os.path.isdir(pd_path):
                    continue
                for f in glob.glob(os.path.join(pd_path, "*.jsonl")):
                    mtime = os.path.getmtime(f)
                    if mtime > latest_mtime:
                        latest_mtime = mtime
                        latest_log = f
            if latest_log:
                project_base = os.path.dirname(latest_log)
                logger.info("_read_claude_project_log: fallback scan found log=%s", latest_log)
            else:
                logger.info("_read_claude_project_log: no cwd and no CC project log found anywhere")
                return events
        else:
            logger.info("_read_claude_project_log: no cwd and ~/.claude/projects not found")
            return events
    else:
        project_dir = _cwd_to_project_dir(cwd)
        project_base = os.path.expanduser(f"~/.claude/projects/{project_dir}")
        if not os.path.isdir(project_base):
            logger.info("_read_claude_project_log: project dir not found for cwd=%s, will fallback scan", cwd)
            # Direct fallback: scan all project dirs for latest log
            projects_base = os.path.expanduser("~/.claude/projects")
            project_base = None
            latest_mtime = 0
            if os.path.isdir(projects_base):
                for pd in os.listdir(projects_base):
                    pd_path = os.path.join(projects_base, pd)
                    if not os.path.isdir(pd_path):
                        continue
                    for f in glob.glob(os.path.join(pd_path, "*.jsonl")):
                        mtime = os.path.getmtime(f)
                        if mtime > latest_mtime:
                            latest_mtime = mtime
                            project_base = pd_path
            if not project_base:
                logger.info("_read_claude_project_log: fallback scan found no CC project dirs")
                return events

    # Find the most recently modified jsonl file (likely the current session)
    jsonl_files = glob.glob(os.path.join(project_base, "*.jsonl"))
    if not jsonl_files:
        logger.info("_read_claude_project_log: no jsonl files in %s", project_base)
        return events

    # Sort by mtime descending; most recent first
    jsonl_files.sort(key=lambda p: os.path.getmtime(p), reverse=True)
    latest_log = jsonl_files[0]
    session_id_from_log = os.path.splitext(os.path.basename(latest_log))[0]
    logger.info("_read_claude_project_log: using log=%s for runId=%s cwd=%s", latest_log, run_id[:12], cwd)

    try:
        with open(latest_log, "r", encoding="utf-8") as f:
            for line_num, line in enumerate(f, 1):
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue

                obj_type = obj.get("type", "")
                msg = obj.get("message", {})
                if not isinstance(msg, dict):
                    continue
                role = msg.get("role", "")
                content_list = msg.get("content", [])
                if not isinstance(content_list, list):
                    content_list = [content_list]

                for item in content_list:
                    if not isinstance(item, dict):
                        continue
                    item_type = item.get("type", "")
                    if item_type == "tool_use":
                        tool_name = item.get("name", "unknown")
                        tool_input = item.get("input", {})
                        # Include truncated input for summary
                        summary = ""
                        if isinstance(tool_input, dict):
                            fp = tool_input.get("file_path", "")
                            cmd = tool_input.get("command", "")
                            if fp:
                                summary = f"file_path={fp}"
                            elif cmd:
                                summary = f"command={cmd[:100]}"
                        events.append({
                            "type": "tool_use",
                            "name": tool_name,
                            "content": json.dumps(tool_input, ensure_ascii=False)[:500],
                            "summary": summary,
                            "session_id": session_id_from_log,
                        })
                    elif item_type == "tool_result":
                        content = item.get("content", "")
                        if isinstance(content, list):
                            content = " ".join(str(c) for c in content)
                        events.append({
                            "type": "tool_result",
                            "name": "",
                            "content": str(content) if content else "ok",
                            "summary": str(content)[:200] if content else "ok",
                            "session_id": session_id_from_log,
                        })
                    elif item_type == "thinking":
                        thinking = item.get("thinking", "")
                        if thinking:
                            events.append({
                                "type": "thinking",
                                "name": "",
                                "content": thinking,
                                "summary": thinking[:200],
                                "session_id": session_id_from_log,
                            })
                    elif item_type == "text":
                        # Skip final text - already delivered via assistant.delta
                        pass

        logger.info("_read_claude_project_log: parsed %d events from %s", len(events), latest_log)
    except Exception as e:
        logger.warning("_read_claude_project_log: failed to read %s: %s", latest_log, e)

    return events


def _parse_cc_log_line(line: str) -> dict | None:
    """解析一行 CC log JSON，转为 Gateway 事件。"""
    try:
        obj = json.loads(line.strip())
    except:
        return None

    msg = obj.get("message", {})
    if not isinstance(msg, dict):
        return None

    content = msg.get("content", [])
    if not isinstance(content, list):
        content = [content]

    for item in content:
        if not isinstance(item, dict):
            continue
        item_type = item.get("type", "")
        if item_type == "tool_use":
            return {
                "kind": "item.started",
                "source": "acp",
                "payload": {
                    "kind": "tool",
                    "name": item.get("name", "unknown"),
                    "title": item.get("name", "unknown"),
                    "phase": "start",
                }
            }
        elif item_type == "tool_result":
            content_str = item.get("content", "")
            if isinstance(content_str, list):
                content_str = " ".join(str(c) for c in content_str)
            summary = str(content_str)[:200]
            return {
                "kind": "item.completed",
                "source": "acp",
                "payload": {
                    "kind": "tool",
                    "name": "",
                    "phase": "end",
                    "summary": summary,
                }
            }
        elif item_type == "thinking":
            thinking = item.get("thinking", "")
            return {
                "kind": "plan",
                "source": "acp",
                "payload": {
                    "text": thinking[:300],
                }
            }
    return None


async def _stream_cc_log(session_key: str, run_id: str, parent_sk: str):
    """在 CC 运行期间实时 tail project log 并推送事件。"""
    # 找到 CC project 目录
    cwd = _acp_run_cwd.get(run_id, "/root/.openclaw/main/workspace/claude")
    project_dir = _cwd_to_project_dir(cwd)
    project_base = os.path.expanduser(f"~/.claude/projects/{project_dir}")

    if not os.path.isdir(project_base):
        logger.info("_stream_cc_log: project dir not found for cwd=%s, will fallback scan", cwd)
        # fallback: 扫描所有 project dir 找最新
        projects_base = os.path.expanduser("~/.claude/projects")
        if os.path.isdir(projects_base):
            latest_log = None
            latest_mtime = 0
            for pd in os.listdir(projects_base):
                pd_path = os.path.join(projects_base, pd)
                if not os.path.isdir(pd_path):
                    continue
                for f in glob.glob(os.path.join(pd_path, "*.jsonl")):
                    mtime = os.path.getmtime(f)
                    if mtime > latest_mtime:
                        latest_mtime = mtime
                        latest_log = f
            if latest_log:
                project_base = os.path.dirname(latest_log)
                logger.info("_stream_cc_log: fallback scan found project_base=%s", project_base)

    if not os.path.isdir(project_base):
        logger.info("_stream_cc_log: project base not found, giving up")
        return

    # 记录启动前已有的文件
    known_files = set(glob.glob(os.path.join(project_base, "*.jsonl")))

    # 等待新文件出现
    cc_file = None
    for _ in range(30):  # 最多等 30 秒
        await asyncio.sleep(1)
        current = set(glob.glob(os.path.join(project_base, "*.jsonl")))
        new = current - known_files
        if new:
            cc_file = max(new, key=os.path.getmtime)
            break

    if not cc_file:
        logger.info("_stream_cc_log: no new CC log file appeared within 30s")
        return

    logger.info("_stream_cc_log: tailing CC log=%s", cc_file)

    seen_lines = 0
    while run_id in _active_acp_runs and _active_acp_runs[run_id].get("status") == "running":
        try:
            with open(cc_file) as f:
                lines = f.readlines()
                new_lines = lines[seen_lines:]
                seen_lines = len(lines)

                for line in new_lines:
                    event = _parse_cc_log_line(line)
                    if event:
                        await _publish_session_event(parent_sk, event)

                await asyncio.sleep(2)  # poll every 2 seconds
        except Exception as e:
            logger.warning("_stream_cc_log: error reading %s: %s", cc_file, e)
            await asyncio.sleep(2)

    logger.info("_stream_cc_log: finished runId=%s", run_id[:12])


async def _push_cc_project_events(session_key: str, run_id: str):
    """Read CC project log on lifecycle end and push structured events to parent session."""
    parent_sk = _child_to_parent.get(session_key)
    if not parent_sk:
        # fallback to best-effort parent: prefer _agent_run_queues then _session_event_subscribers
        for psk in list(_agent_run_queues.keys()):
            if "subagent" not in psk and "acp" not in psk:
                parent_sk = psk
                break
        if not parent_sk:
            for psk in list(_session_event_subscribers.keys()):
                if "subagent" not in psk and "acp" not in psk:
                    parent_sk = psk
                    break
    if not parent_sk:
        # fallback to best-effort parent: prefer _agent_run_queues then _session_event_subscribers
        for psk in list(_agent_run_queues.keys()):
            if "subagent" not in psk and "acp" not in psk:
                parent_sk = psk
                break
        if not parent_sk:
            for psk in list(_session_event_subscribers.keys()):
                if "subagent" not in psk and "acp" not in psk:
                    parent_sk = psk
                    break
    if not parent_sk:
        # Last resort: broadcast to all non-child subscribers
        for psk in list(_session_event_subscribers.keys()):
            if psk.startswith("agent:") and "subagent" not in psk and "acp" not in psk:
                parent_sk = psk
                break
    if not parent_sk:
        logger.info("_push_cc_project_events: no parent session found for sessionKey=%s", session_key[:30])
        return

    cc_events = _read_claude_project_log(session_key, run_id)
    if not cc_events:
        logger.info("_push_cc_project_events: no CC events for sessionKey=%s", session_key[:30])
        return

    logger.info("_push_cc_project_events: publishing %d events to parent=%s", len(cc_events), parent_sk[:40])

    # Build step list for acp_status payload
    step_list = []
    for ev in cc_events:
        ev_type = ev["type"]
        name = ev["name"]
        summary = ev["summary"]
        content = ev["content"]
        if ev_type == "tool_use":
            step_list.append({"type": "tool", "name": name, "status": "done", "summary": summary, "output": content[:200]})
        elif ev_type == "tool_result":
            if step_list:
                step_list[-1]["summary"] = summary[:200]
        elif ev_type == "thinking":
            step_list.append({"type": "plan", "text": content[:200]})

    # Store steps in active run info so acp_status can include them
    if run_id in _active_acp_runs:
        _active_acp_runs[run_id]["steps"] = step_list

    for ev in cc_events:
        ev_type = ev["type"]
        name = ev["name"]
        summary = ev["summary"]
        content = ev["content"]
        event_id = f"evt-cc-{uuid.uuid4().hex[:12]}"
        ts = int(time.time() * 1000)

        if ev_type == "tool_use":
            await _publish_session_event(parent_sk, {
                "eventId": event_id,
                "sessionKey": parent_sk,
                "runId": run_id,
                "source": "acp",
                "ts": ts,
                "kind": "item.started",
                "payload": {
                    "kind": "tool",
                    "name": name,
                    "title": f"执行: {name}",
                    "phase": "start",
                },
            })
        elif ev_type == "tool_result":
            await _publish_session_event(parent_sk, {
                "eventId": event_id,
                "sessionKey": parent_sk,
                "runId": run_id,
                "source": "acp",
                "ts": ts,
                "kind": "item.completed",
                "payload": {
                    "kind": "tool",
                    "name": name,
                    "phase": "end",
                    "summary": summary,
                    "title": f"完成: {name}",
                },
            })
        elif ev_type == "thinking":
            await _publish_session_event(parent_sk, {
                "eventId": event_id,
                "sessionKey": parent_sk,
                "runId": run_id,
                "source": "acp",
                "ts": ts,
                "kind": "plan",
                "payload": {
                    "text": content[:500],
                },
            })

    # After publishing all CC events, mark run as done
    if run_id in _active_acp_runs:
        _active_acp_runs[run_id]["status"] = "done"
        _publish_acp_status()
        async def _delayed_acp_cleanup(rid=run_id):
            await asyncio.sleep(5)
            _active_acp_runs.pop(rid, None)
            _acp_run_cwd.pop(rid, None)
            _publish_acp_status()
        asyncio.create_task(_delayed_acp_cleanup())


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
        # 刷新活跃 run 的最后活动时间（任意 agent 事件都算活动）
        if run_id and run_id in _active_acp_runs and _active_acp_runs[run_id].get("status") == "running":
            _active_acp_runs[run_id]["last_event_at"] = time.time()
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
            meta_raw = data.get("meta", {})
            logger.info("sessions_spawn end: title=%.200s meta_type=%s meta_keys=%s", title, type(meta_raw).__name__, list(meta_raw.keys()) if isinstance(meta_raw, dict) else str(meta_raw)[:200])
            m = re.search(r'(agent:[\w:.-]+)', title)
            if m:
                child_sk = m.group(1)
                _child_to_parent[child_sk] = session_key
                logger.info("Registered child->parent mapping from spawn title: %s -> %s", child_sk[:50], session_key[:50])
            # Also check meta.childSessionKey (title may not contain it)
            meta = data.get("meta", {})
            if isinstance(meta, str):
                try:
                    meta = json.loads(meta)
                except:
                    meta = {}
            child_sk_from_meta = meta.get("childSessionKey", "") if isinstance(meta, dict) else ""
            if child_sk_from_meta and child_sk_from_meta not in _child_to_parent:
                _child_to_parent[child_sk_from_meta] = session_key
                logger.info("Registered child->parent mapping from meta: %s -> %s", child_sk_from_meta[:50], session_key[:50])

        if stream == "assistant" and "childSessionKey" in data.get("delta", ""):
            m = re.search(r'childSessionKey["\':=]+\s*["\']?(agent:[\w:.-]+)', data.get("delta", ""))
            if m:
                child_sk = m.group(1)
                _child_to_parent[child_sk] = session_key
                logger.info("Registered child->parent mapping from assistant delta: %s -> %s", child_sk[:50], session_key[:50])

        # 注册/清理 ACP 活跃 run 状态（全局路径，适用于 /api/chat 等非 legacy 端点）
        if stream == "lifecycle":
            phase = data.get("phase", "")
            is_child_session = "acp" in session_key or "subagent" in session_key
            if phase == "start" and is_child_session and run_id and run_id not in _active_acp_runs:
                now = time.time()
                _active_acp_runs[run_id] = {
                    "runId": run_id,
                    "status": "running",
                    "started_at": now,
                    "last_event_at": now,
                }
                # Capture cwd if available in the lifecycle start payload
                lifecycle_cwd = data.get("cwd", "") or payload.get("cwd", "")
                # Fallback: read from Claude Code session file if not in lifecycle event
                if not lifecycle_cwd and "claude:acp" in session_key:
                    session_id = session_key.replace("agent:claude:acp:", "")
                    session_file = os.path.expanduser(f"~/.openclaw/agents/claude/sessions/{session_id}.jsonl")
                    if os.path.exists(session_file):
                        try:
                            with open(session_file) as f:
                                first_line = f.readline().strip()
                                if first_line:
                                    session_info = json.loads(first_line)
                                    lifecycle_cwd = session_info.get("cwd", "")
                        except Exception:
                            pass
                # Final fallback: known CC workspace directory
                if not lifecycle_cwd:
                    lifecycle_cwd = "/root/.openclaw/main/workspace/claude"
                if lifecycle_cwd:
                    _acp_run_cwd[run_id] = lifecycle_cwd
                    logger.info("Stored ACP run cwd: runId=%s cwd=%s", run_id[:12], lifecycle_cwd)
                _publish_acp_status()
                logger.info("Registered ACP run from global listener: runId=%s sessionKey=%s", run_id[:12], session_key[:30])
                # 启动 CC project log 实时流推送
                if "claude:acp" in session_key:
                    parent_sk = _child_to_parent.get(session_key)
                    if not parent_sk:
                        # fallback: find parent from active subscribers/queues
                        for psk in list(_session_event_subscribers.keys()):
                            if "subagent" not in psk and "acp" not in psk:
                                parent_sk = psk
                                break
                        if not parent_sk:
                            for psk in list(_agent_run_queues.keys()):
                                if "subagent" not in psk and "acp" not in psk:
                                    parent_sk = psk
                                    break
                    if parent_sk:
                        asyncio.create_task(_stream_cc_log(session_key, run_id, parent_sk))
                        logger.info("Started CC log streaming: runId=%s parent=%s", run_id[:12], parent_sk[:30])
            elif phase == "end" and run_id in _active_acp_runs:
                # Mark done immediately so run lifecycle is not blocked by project event push
                _active_acp_runs[run_id]["status"] = "done"
                _publish_acp_status()
                if "claude:acp" in session_key or "acp" in session_key:
                    # Push CC project log events in background; do not block done marking
                    asyncio.create_task(_push_cc_project_events(session_key, run_id))
                async def _delayed_acp_cleanup(rid=run_id):
                    await asyncio.sleep(5)
                    _active_acp_runs.pop(rid, None)
                    _acp_run_cwd.pop(rid, None)
                    _publish_acp_status()
                asyncio.create_task(_delayed_acp_cleanup())
                logger.info("Marked ACP run done from global listener: runId=%s", run_id[:12])

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
                        # Parent mapped but no legacy queue (v2 mode or disconnected).
                        # Do NOT fallback to random queues — just cache for persistent subscribers.
                        if stream in ("assistant", "item", "lifecycle", "command_output"):
                            payload["_is_child"] = True
                            payload["_delivered_to_parent_queue"] = False
                            payload = _cache_child_event(parent_sk, payload) or payload
                            logger.info(
                                "Cached child event (v2/no-queue): child=%s -> parent=%s, stream=%s, cached=%d",
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
            cache["last_event_at"] = time.time()
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


class SwitchModelReq(BaseModel):
    model: str
    session_key: str | None = None


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

            run_status = cache.get("status", "streaming")
            if run_status not in ("done", "error", "timeout"):
                snapshot["activeRuns"][active_run_id] = {
                    "status": run_status,
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
            # 发送当前 ACP 状态（快照前清理僵尸 run）
            now = time.time()
            stale_rids = []
            for rid, info in list(_active_acp_runs.items()):
                if info.get("status") == "running" and now - info.get("last_event_at", info.get("started_at", 0)) > 300:
                    _active_acp_runs[rid]["status"] = "timeout"
                    stale_rids.append(rid)
            if stale_rids:
                logger.warning("Event snapshot: marked %d inactive acp runs as timeout", len(stale_rids))
                _publish_acp_status()
                async def _snapshot_delayed_cleanup(rids):
                    await asyncio.sleep(5)
                    for rid in rids:
                        _active_acp_runs.pop(rid, None)
                        _acp_run_cwd.pop(rid, None)
                    _publish_acp_status()
                asyncio.create_task(_snapshot_delayed_cleanup(stale_rids))
            acp_runs = []
            for rid, info in list(_active_acp_runs.items()):
                if info.get("status") in ("timeout", "done"):
                    continue
                acp_runs.append({"runId": rid, "status": info.get("status", "running")})
            acp_status_event = {
                "eventId": f"evt-acp-init-{uuid.uuid4().hex[:12]}",
                "sessionKey": sessionKey,
                "kind": "acp_status",
                "ts": int(time.time() * 1000),
                "payload": {"count": len(acp_runs), "runs": acp_runs}
            }
            yield f"data: {json.dumps(acp_status_event, ensure_ascii=False)}\n\n"
            logger.info("Event stream opened: sessionKey=%s subscriber=%s snapshot_runs=%d", sessionKey[:30], subscriber_id[:8], len(snapshot.get("activeRuns", {})))

            last_heartbeat = time.time()
            while True:
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=1.0)
                    yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
                    last_heartbeat = time.time()
                except asyncio.TimeoutError:
                    # 只在有活跃 run 时发 SSE 注释心跳，防止工具调用期间空闲超时
                    active_run = _agent_session_to_run.get(sessionKey)
                    if active_run and time.time() - last_heartbeat > _EVENT_STREAM_HEARTBEAT_SEC:
                        yield ": heartbeat\n\n"
                        last_heartbeat = time.time()
        except asyncio.CancelledError:
            logger.info("Event stream cancelled: sessionKey=%s subscriber=%s", sessionKey[:30], subscriber_id[:8])
        except Exception as e:
            logger.warning("Event stream error: sessionKey=%s subscriber=%s error=%s", sessionKey[:30], subscriber_id[:8], e)
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


def _gateway_session_key(session_key: str, agent_id: str) -> str:
    """Return the Gateway-prefixed session key for chat.history RPC."""
    if session_key.startswith("agent:"):
        return session_key
    return f"agent:{agent_id}:{session_key}"


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


@app.post("/api/model/switch")
async def switch_model(req: SwitchModelReq, user=Depends(get_current_user)):
    """Switch the active model for the current session.

    Updates the runtime model cache and notifies Gateway via /model command.
    """
    client = await get_client()
    agent_id = _agent_id(user['role'])
    session_key = req.session_key or users.get_or_create_session(user['id'], agent_id)
    users.touch_session(user['id'], agent_id, session_key)

    target_model = req.model.strip()
    normalized = _normalize_model_name(target_model)
    if not normalized:
        raise HTTPException(400, "Invalid model name")

    # Update runtime cache immediately so /api/models reflects the change
    _session_runtime_model[session_key] = normalized

    # Send /model command to Gateway so subsequent chat uses the new model
    send_res = await client.rpc("chat.send", {
        "sessionKey": session_key,
        "message": f"/model {target_model}",
        "idempotencyKey": str(uuid.uuid4()),
    })
    if not send_res.get("ok"):
        logger.warning("model switch chat.send failed: %s", send_res.get("error"))
        # Don't raise here — local cache is already updated, Gateway may recover

    logger.info("Model switch: sessionKey=%s → %s", session_key[:30], normalized)
    return {"model": normalized, "sessionKey": session_key}


@app.get("/api/history")
async def history(
    limit: int = Query(50),
    sessionKey: str = Query(None),
    user=Depends(get_current_user),
):
    client = await get_client()
    # Gateway history requires agent:{agent_id}:{sessionKey} prefix
    agent_id = _agent_id(user['role'])
    sk = sessionKey or users.get_or_create_session(user['id'], agent_id)
    full_sk = _gateway_session_key(sk, agent_id)
    res = await client.rpc("chat.history", {
        "sessionKey": full_sk,
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

    # 给消息加 [H5] 来源标识，并按双层规则附加 ACP 路由提示
    tagged_message, acp_mode, acp_reason = _decorate_h5_message(req.message)
    logger.info("H5 ACP route: sessionKey=%s mode=%s reason=%s msg=%s", session_key[:30], acp_mode, acp_reason, req.message[:80])

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
    _uploaded_file_url_map.clear()

    # 注册消息缓存（用 chat.send 返回的 runId）
    _run_cache[run_id] = {
        "events": [],
        "status": "streaming",
        "created_at": time.time(),
        "last_event_at": time.time(),
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
                                # 检测本地文件路径，上传 COS 并替换为公网 URL，同时推送 file event
                                for fpath in _extract_file_paths(assistant_buffer):
                                    file_info = cos_util.upload_local_file(fpath)
                                    if file_info:
                                        _uploaded_file_url_map[fpath] = file_info["url"]
                                        logger.info("[COS] Auto-uploaded agent file: %s -> %s", fpath, file_info["url"])
                                        yield f"data: {json.dumps({'type': 'file', 'file': file_info}, ensure_ascii=False)}\n\n"

                                assistant_buffer = _replace_uploaded_paths(assistant_buffer)
                                yield f"data: {json.dumps({'type': 'text', 'content': assistant_buffer, 'source': source, 'childEventId': event.get('_child_event_id') if is_child else None}, ensure_ascii=False)}\n\n"
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
                        # 检测本地文件路径，上传 COS 并替换为公网 URL，同时推送 file event
                        for fpath in _extract_file_paths(delta):
                            file_info = cos_util.upload_local_file(fpath)
                            if file_info:
                                _uploaded_file_url_map[fpath] = file_info["url"]
                                logger.info("[COS] Auto-uploaded agent file: %s -> %s", fpath, file_info["url"])
                                yield f"data: {json.dumps({'type': 'file', 'file': file_info}, ensure_ascii=False)}\n\n"

                        delta = _replace_uploaded_paths(delta)
                        yield f"data: {json.dumps({'type': 'text', 'content': delta, 'source': source, 'childEventId': event.get('_child_event_id') if is_child else None}, ensure_ascii=False)}\n\n"

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
                        # 注：ACP run 的注册由 global agent listener（子 agent lifecycle start）统一处理，
                        # 这里不再重复注册，避免同一任务因父/子 runId 不同而被计数两次。

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
                    if phase == "error":
                        error_text = data.get("error", "")
                        if error_text:
                            yield f"data: {json.dumps({'type': 'error', 'message': error_text}, ensure_ascii=False)}\n\n"
                    elif phase == "end":
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
            # 标记 ACP run 为完成
            if run_id in _active_acp_runs:
                _active_acp_runs[run_id]["status"] = "done"
                _publish_acp_status()
                # 5秒后清理
                async def _delayed_acp_cleanup(rid=run_id):
                    await asyncio.sleep(5)
                    _active_acp_runs.pop(rid, None)
                    _publish_acp_status()
                asyncio.create_task(_delayed_acp_cleanup())
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
                # Gateway session.history requires agent-prefixed keys
                session_keys_to_try = [session_key]
                if not session_key.startswith("agent:"):
                    for agent_id in ("dev", "user", "main"):
                        session_keys_to_try.append(f"agent:{agent_id}:{session_key}")
                full_text = ""
                for sk in session_keys_to_try:
                    try:
                        hist = await client.rpc("session.history", {"sessionKey": sk, "limit": 5})
                        messages = hist.get("messages", [])
                        for msg in reversed(messages):
                            if msg.get("role") == "assistant":
                                text = msg.get("content", "")
                                if text and isinstance(text, str) and text.strip() not in _FILTERED_STREAM_TEXTS:
                                    full_text = text
                                break
                        if full_text:
                            break
                    except Exception:
                        continue
                if full_text and not _is_filtered_reply[0]:
                    yield f"data: {json.dumps({'type': 'full_result', 'text': full_text}, ensure_ascii=False)}\n\n"
                    logger.info("SSE full_result sent for sessionKey=%s, text_len=%d", session_key[:30], len(full_text))
                else:
                    logger.info(
                        "SSE full_result suppressed for sessionKey=%s, text=%s, pushed_valid_text=%s, filtered_reply=%s",
                        session_key[:30],
                        full_text,
                        pushed_valid_text,
                        _is_filtered_reply[0],
                    )
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
async def chat_send(req: ChatReq, request: Request, user=Depends(get_current_user)):
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

    tagged_message, acp_mode, acp_reason = _decorate_h5_message(req.message)
    logger.info("H5 ACP route: sessionKey=%s mode=%s reason=%s msg=%s", session_key[:30], acp_mode, acp_reason, req.message[:80])

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
    _uploaded_file_url_map.clear()

    _run_cache[run_id] = {"events": [], "status": "streaming", "created_at": time.time(), "last_event_at": time.time()}
    _run_to_parent_session[run_id] = session_key
    _agent_session_to_run[session_key] = run_id

    # Only create legacy SSE queue for /api/chat (not /api/chat/v2).
    # In v2 mode events are consumed via persistent /api/events subscribers.
    is_v2 = request.url.path.endswith("/v2")
    if not is_v2:
        event_queue: asyncio.Queue = asyncio.Queue()
        _agent_run_queues[session_key] = event_queue
        logger.info("chat_send: registered legacy queue for sessionKey=%s", session_key[:30])
    else:
        logger.info("chat_send: v2 mode, skipping legacy queue for sessionKey=%s", session_key[:30])

    # Publish run.started event to persistent subscribers
    await _publish_session_event(session_key, {
        "eventId": f"evt-{uuid.uuid4().hex[:16]}",
        "sessionKey": session_key,
        "runId": run_id,
        "source": "main",
        "ts": int(time.time() * 1000),
        "kind": "run.started",
        "payload": {"message": req.message[:100], "acpMode": acp_mode, "acpReason": acp_reason},
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
    storage: str = Query("local", description="存储方式: local(默认) 或 cos"),
):
    """上传文件，默认存本地服务器，storage=cos 时上传到腾讯云 COS"""
    if not file.filename:
        raise HTTPException(400, "文件名不能为空")

    # 读取文件内容
    content = await file.read()

    # 限制 50MB
    if len(content) > 300 * 1024 * 1024:
        raise HTTPException(413, "文件大小不能超过 50MB")

    if storage == "cos":
        # COS 链路
        content_type = file.content_type or "application/octet-stream"
        url = cos_util.upload_file(content, file.filename, content_type)
        return {"url": url, "filename": file.filename, "storage": "cos"}

    # 默认：本地存储
    date_dir = datetime.now().strftime("%Y%m%d")
    assets_dir = os.path.join("/var/www/chat/assets", date_dir)
    os.makedirs(assets_dir, exist_ok=True)

    local_path = os.path.join(assets_dir, file.filename)
    with open(local_path, "wb") as f:
        f.write(content)

    url = f"https://www.nickhome.cloud/chat/assets/{date_dir}/{file.filename}"
    return {"url": url, "filename": file.filename, "storage": "local"}


@app.get("/api/download")
async def download_file(
    url: str = Query(None, description="File URL to download"),
    path: str = Query(None, description="Local file path to download"),
    filename: str = Query(None, description="Optional filename override"),
    user=Depends(get_current_user),
):
    """Proxy file download with proper Content-Disposition header.

    Fetches a file from the given URL or local path and returns it with
    attachment headers, enabling reliable cross-origin downloads on mobile browsers.
    """
    import requests
    from urllib.parse import urlparse, unquote

    # Determine target and fetch strategy
    target_url = None
    local_path = None

    if path and os.path.isfile(path):
        local_path = path
    elif url and url.startswith(("http://", "https://")):
        target_url = url
    elif url and os.path.isfile(url):
        # Some callers pass a local path in the url param
        local_path = url
    else:
        raise HTTPException(400, "Invalid or missing url/path")

    # Determine display filename
    if filename:
        display_name = filename
    elif target_url:
        parsed = urlparse(target_url)
        display_name = unquote(os.path.basename(parsed.path)) or "download"
    elif local_path:
        display_name = os.path.basename(local_path)
    else:
        display_name = "download"

    if local_path:
        # Serve local file directly
        if not os.path.isfile(local_path):
            raise HTTPException(404, "File not found")

        def _local_iter():
            with open(local_path, "rb") as f:
                while True:
                    chunk = f.read(65536)
                    if not chunk:
                        break
                    yield chunk

        # Guess content type from extension
        import mimetypes
        content_type, _ = mimetypes.guess_type(local_path)
        if not content_type:
            content_type = "application/octet-stream"

        headers = {
            "Content-Disposition": f'attachment; filename="{display_name}"',
            "Content-Type": content_type,
        }
        return StreamingResponse(_local_iter(), headers=headers)

    # Proxy remote URL via requests streaming
    loop = asyncio.get_event_loop()

    def _stream():
        resp = requests.get(target_url, stream=True, timeout=60, headers={
            "User-Agent": "Mozilla/5.0 (H5-Chat-Bridge)"
        })
        resp.raise_for_status()
        return resp

    try:
        resp = await loop.run_in_executor(None, _stream)
    except Exception as e:
        logger.warning("Download proxy failed for %s: %s", target_url[:100], e)
        raise HTTPException(502, f"Failed to fetch file: {e}")

    headers = {
        "Content-Disposition": f'attachment; filename="{display_name}"',
    }
    if resp.headers.get("content-type"):
        headers["Content-Type"] = resp.headers["content-type"]

    return StreamingResponse(
        resp.iter_content(chunk_size=65536),
        headers=headers,
    )


@app.get("/api/local-file")
async def serve_local_file(
    path: str = Query(..., description="Local file path"),
    token: str = Query(default="", description="Auth token for img src"),
    authorization: Optional[str] = Header(default=None),
):
    """Serve local files for H5 preview (images and PDFs only).
    Supports Bearer header (normal API) or ?token= query param (img src)."""
    import mimetypes

    # Authenticate via header or query param (img tags can't send custom headers)
    auth_token = ""
    if authorization and authorization.startswith("Bearer "):
        auth_token = authorization[7:]
    elif token:
        auth_token = token

    if not auth_token:
        raise HTTPException(401, "Missing token")

    user = users.verify_token(auth_token)
    if not user:
        raise HTTPException(401, "Invalid token")

    # 安全检查：只允许特定目录
    allowed_dirs = ("/tmp/", "/root/", "/home/", "/var/www/")
    if not any(path.startswith(d) for d in allowed_dirs):
        raise HTTPException(403, "Access denied")

    # 只允许图片和 PDF
    allowed_exts = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg", ".bmp", ".ico", ".pdf"}
    ext = os.path.splitext(path)[1].lower()
    if ext not in allowed_exts:
        raise HTTPException(403, "File type not allowed")

    if not os.path.isfile(path):
        raise HTTPException(404, "File not found")

    content_type, _ = mimetypes.guess_type(path)
    if not content_type:
        content_type = "application/octet-stream"

    return FileResponse(path, media_type=content_type)


# ── ACP Agent Log Streaming ─────────────────────────────────────

def _find_kimi_log_file() -> str | None:
    """Find the current active Kimi Code log file."""
    kimi_log = os.path.expanduser("~/.kimi/logs/kimi.log")
    if os.path.isfile(kimi_log):
        return kimi_log
    logs = glob.glob(os.path.expanduser("~/.kimi/logs/kimi.*.log"))
    if logs:
        return max(logs, key=os.path.getmtime)
    return None


def _find_claude_log_file() -> str | None:
    """Find the most recently modified Claude Code project jsonl file."""
    projects_base = os.path.expanduser("~/.claude/projects")
    if not os.path.isdir(projects_base):
        return None
    latest_file = None
    latest_mtime = 0
    for pd in os.listdir(projects_base):
        pd_path = os.path.join(projects_base, pd)
        if not os.path.isdir(pd_path):
            continue
        for f in glob.glob(os.path.join(pd_path, "*.jsonl")):
            mtime = os.path.getmtime(f)
            if mtime > latest_mtime:
                latest_mtime = mtime
                latest_file = f
    return latest_file


_UUID_RE = re.compile(r'[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}', re.IGNORECASE)


def _find_kimi_sessions() -> list[dict]:
    """Extract active session IDs from kimi.log (last 30 minutes).

    Returns list of {"id": session_id, "label": str, "mtime": float}
    """
    sessions: dict[str, dict] = {}
    kimi_log = os.path.expanduser("~/.kimi/logs/kimi.log")
    if not os.path.isfile(kimi_log):
        return []

    cutoff = time.time() - 1800  # 30 minutes ago
    try:
        # Read last ~2000 lines for efficiency
        with open(kimi_log, "r", encoding="utf-8", errors="replace") as f:
            f.seek(0, 2)
            size = f.tell()
            read_size = min(size, 512 * 1024)  # last 512KB
            f.seek(size - read_size)
            lines = f.readlines()
            # Skip first partial line
            if size > read_size and lines:
                lines = lines[1:]
    except Exception:
        return []

    for line in lines:
        # Parse timestamp: 2026-04-28 08:03:47.627 | ...
        m = re.match(r'(\d{4}-\d{2}-\d{2})\s+(\d{2}:\d{2}:\d{2}\.\d+)', line)
        if not m:
            continue
        try:
            from datetime import datetime
            ts_str = m.group(1) + " " + m.group(2).split('.')[0]
            ts = datetime.strptime(ts_str, "%Y-%m-%d %H:%M:%S").timestamp()
        except Exception:
            continue
        if ts < cutoff:
            continue
        for match in _UUID_RE.finditer(line):
            sid = match.group(0).lower()
            sessions[sid] = {"id": sid, "label": sid[:8], "mtime": ts}

    return sorted(sessions.values(), key=lambda s: s["mtime"], reverse=True)


def _find_claude_sessions() -> list[dict]:
    """List active Claude Code project jsonl files as sessions.

    Only returns files modified within the last 30 minutes.

    Returns list of {"id": session_id, "label": str, "mtime": float, "project": str}
    """
    sessions = []
    projects_base = os.path.expanduser("~/.claude/projects")
    if not os.path.isdir(projects_base):
        return sessions

    cutoff = time.time() - 1800  # 30 minutes ago

    for pd in os.listdir(projects_base):
        pd_path = os.path.join(projects_base, pd)
        if not os.path.isdir(pd_path):
            continue
        for f in glob.glob(os.path.join(pd_path, "*.jsonl")):
            sid = os.path.splitext(os.path.basename(f))[0]
            try:
                mtime = os.path.getmtime(f)
            except OSError:
                continue
            if mtime < cutoff:
                continue
            sessions.append({
                "id": sid,
                "label": sid[:8],
                "mtime": mtime,
                "project": pd,
            })

    return sorted(sessions, key=lambda s: s["mtime"], reverse=True)


def _find_claude_log_by_session(session_id: str) -> str | None:
    """Find Claude jsonl file by session ID."""
    projects_base = os.path.expanduser("~/.claude/projects")
    if not os.path.isdir(projects_base):
        return None
    for pd in os.listdir(projects_base):
        pd_path = os.path.join(projects_base, pd)
        if not os.path.isdir(pd_path):
            continue
        for f in glob.glob(os.path.join(pd_path, "*.jsonl")):
            sid = os.path.splitext(os.path.basename(f))[0]
            if sid.lower() == session_id.lower():
                return f
    return None


def _format_claude_log_line(line: str) -> str | None:
    """Parse a Claude Code JSONL line into human-readable log text.

    Returns formatted text like:
      [10:29:36] [ASSISTANT] tool_use: Bash(command="...")
      [10:29:36] [USER] tool_result: tail: option used in invalid context -- 5
      [10:29:40] [ASSISTANT] thinking: 让我修正 tail 命令...
    """
    try:
        obj = json.loads(line.strip())
    except Exception:
        return None

    ts_raw = obj.get("timestamp", "")
    ts = ""
    if ts_raw:
        try:
            from datetime import datetime
            dt = datetime.fromisoformat(ts_raw.replace("Z", "+00:00"))
            ts = dt.strftime("%H:%M:%S")
        except Exception:
            ts = ts_raw[11:19] if len(ts_raw) >= 19 else ""

    msg_type = obj.get("type", "")
    msg = obj.get("message", {})
    if not isinstance(msg, dict):
        return None

    role = msg.get("role", msg_type)
    content_list = msg.get("content", [])
    if not isinstance(content_list, list):
        content_list = [content_list]

    lines = []
    prefix = f"[{ts}] [{role.upper()}]" if ts else f"[{role.upper()}]"

    for item in content_list:
        if not isinstance(item, dict):
            continue
        item_type = item.get("type", "")
        if item_type == "tool_use":
            name = item.get("name", "unknown")
            inp = item.get("input", {})
            inp_str = json.dumps(inp, ensure_ascii=False)
            if len(inp_str) > 200:
                inp_str = inp_str[:200] + "..."
            lines.append(f"{prefix} tool_use: {name}({inp_str})")
        elif item_type == "tool_result":
            content_str = item.get("content", "")
            if isinstance(content_str, list):
                content_str = " ".join(str(c) for c in content_str)
            is_error = item.get("is_error", False)
            err_flag = " [ERROR]" if is_error else ""
            text = str(content_str).replace("\n", " ")[:300]
            lines.append(f"{prefix} tool_result: {text}{err_flag}")
        elif item_type == "thinking":
            thinking = item.get("thinking", "")
            if thinking:
                text = str(thinking).replace("\n", " ")[:300]
                lines.append(f"{prefix} thinking: {text}")
        elif item_type == "text":
            text = item.get("text", "")
            if text:
                text = str(text).replace("\n", " ")[:300]
                lines.append(f"{prefix} text: {text}")
        else:
            # Unknown item type - include raw for completeness
            raw = json.dumps(item, ensure_ascii=False)[:200]
            lines.append(f"{prefix} {item_type}: {raw}")

    return "\n".join(lines) if lines else None


async def _tail_log_file(
    filepath: str,
    lines: int = 0,
    follow: bool = True,
    poll_interval: float = 0.5,
    is_jsonl: bool = False,
    session_id_filter: str = "",
):
    """Async generator that yields log lines from a file with rotation detection."""
    if not filepath or not os.path.isfile(filepath):
        return

    try:
        stat = os.stat(filepath)
        initial_size = stat.st_size
        initial_ino = stat.st_ino
    except OSError:
        return

    current_ino = initial_ino
    current_path = filepath

    try:
        f = open(current_path, "r", encoding="utf-8", errors="replace")

        # Read initial lines if requested
        if lines > 0:
            all_lines = f.readlines()
            for line in all_lines[-lines:]:
                stripped = line.rstrip("\n")
                if session_id_filter and session_id_filter.lower() not in stripped.lower():
                    continue
                yield stripped
            f.seek(0, 2)  # Seek to end
        else:
            # Read all existing lines
            for line in f:
                stripped = line.rstrip("\n")
                if session_id_filter and session_id_filter.lower() not in stripped.lower():
                    continue
                yield stripped

        if not follow:
            f.close()
            return

        # Follow mode
        while True:
            await asyncio.sleep(poll_interval)

            # Check for rotation
            try:
                new_stat = os.stat(current_path)
                if new_stat.st_ino != current_ino:
                    current_ino = new_stat.st_ino
                    f.close()
                    f = open(current_path, "r", encoding="utf-8", errors="replace")
                    logger.info("Log file rotated (inode changed), reopened: %s", current_path)
                    continue
            except FileNotFoundError:
                # File deleted/rotated - try to find replacement
                if is_jsonl:
                    new_path = _find_claude_log_file()
                else:
                    new_path = _find_kimi_log_file()

                if new_path and new_path != current_path:
                    current_path = new_path
                    current_ino = os.stat(current_path).st_ino
                    f.close()
                    f = open(current_path, "r", encoding="utf-8", errors="replace")
                    logger.info("Log file rotated to new file: %s", current_path)
                    continue
                else:
                    continue
            except OSError:
                continue

            # Read new lines
            new_lines = f.readlines()
            for line in new_lines:
                stripped = line.rstrip("\n")
                if session_id_filter and session_id_filter.lower() not in stripped.lower():
                    continue
                yield stripped

    except asyncio.CancelledError:
        raise
    except Exception as e:
        logger.warning("Tail error for %s: %s", current_path, e)
    finally:
        try:
            f.close()
        except Exception:
            pass


@app.get("/api/acp/log/status")
async def acp_log_status(authorization: str = Header(None), token: str = Query(None)):
    """Return current agent log file availability and active sessions."""
    # Auth: support both header and query param
    _token = None
    if authorization and authorization.startswith("Bearer "):
        _token = authorization[7:]
    elif token:
        _token = token
    if not _token:
        raise HTTPException(401, "Missing Bearer token")
    user = users.verify_token(_token)
    if not user:
        raise HTTPException(401, "Invalid or expired token")

    from datetime import datetime

    result = {
        "agents": {},
        "ts": int(time.time() * 1000),
    }

    # Kimi
    kimi_log = _find_kimi_log_file()
    if kimi_log:
        try:
            st = os.stat(kimi_log)
            result["agents"]["kimi"] = {
                "available": True,
                "path": kimi_log,
                "size": st.st_size,
                "mtime": st.st_mtime,
                "mtimeIso": datetime.fromtimestamp(st.st_mtime).isoformat(),
                "sessions": _find_kimi_sessions(),
            }
        except OSError:
            result["agents"]["kimi"] = {"available": False, "sessions": []}
    else:
        result["agents"]["kimi"] = {"available": False, "sessions": []}

    # Claude
    claude_log = _find_claude_log_file()
    claude_sessions = _find_claude_sessions()
    if claude_log:
        try:
            st = os.stat(claude_log)
            result["agents"]["claude"] = {
                "available": True,
                "path": claude_log,
                "size": st.st_size,
                "mtime": st.st_mtime,
                "mtimeIso": datetime.fromtimestamp(st.st_mtime).isoformat(),
                "sessions": claude_sessions,
            }
        except OSError:
            result["agents"]["claude"] = {"available": False, "sessions": claude_sessions}
    else:
        result["agents"]["claude"] = {"available": False, "sessions": claude_sessions}

    return result


@app.get("/api/acp/log/{agent}")
async def acp_log_stream(
    agent: str,
    authorization: str = Header(None),
    token: str = Query(None),
    follow: bool = Query(True),
    lines: int = Query(50, ge=0, le=500),
    sessionId: str = Query(None),
):
    """SSE stream of agent log files.

    - agent=kimi  -> tail ~/.kimi/logs/kimi.log
    - agent=claude -> tail ~/.claude/projects/<project>/*.jsonl (or specific session)
    - ?follow=true  -> keep streaming new lines
    - ?lines=N      -> return last N lines before following
    - ?token=xxx    -> auth via query param (for EventSource)
    - ?sessionId=xxx -> filter to specific session (kimi: inline filter, claude: direct file)
    """
    # Auth: support both header (Authorization: Bearer xxx) and query param (?token=xxx)
    _token = None
    if authorization and authorization.startswith("Bearer "):
        _token = authorization[7:]
    elif token:
        _token = token
    if not _token:
        raise HTTPException(401, "Missing Bearer token")
    user = users.verify_token(_token)
    if not user:
        raise HTTPException(401, "Invalid or expired token")

    if agent not in ("kimi", "claude"):
        raise HTTPException(400, "agent must be 'kimi' or 'claude'")

    if agent == "kimi":
        filepath = _find_kimi_log_file()
        is_jsonl = False
        session_filter = sessionId or ""
    else:
        if sessionId:
            filepath = _find_claude_log_by_session(sessionId)
            is_jsonl = True
            session_filter = ""
        else:
            filepath = _find_claude_log_file()
            is_jsonl = True
            session_filter = ""

    if not filepath:
        raise HTTPException(404, f"No log file found for agent={agent} sessionId={sessionId}")

    async def generate():
        try:
            async for line in _tail_log_file(
                filepath, lines=lines, follow=follow, is_jsonl=is_jsonl, session_id_filter=session_filter
            ):
                if is_jsonl:
                    formatted = _format_claude_log_line(line)
                    event = {
                        "ts": int(time.time() * 1000),
                        "agent": agent,
                        "line": line[:4000],
                        "formatted": formatted,
                    }
                else:
                    event = {
                        "ts": int(time.time() * 1000),
                        "agent": agent,
                        "line": line[:4000],
                    }
                yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"

            if not follow:
                yield f"event: done\ndata: {{\"agent\": \"{agent}\"}}\n\n"
        except asyncio.CancelledError:
            logger.info("ACP log stream cancelled for agent=%s sessionId=%s", agent, sessionId)
        except Exception as e:
            logger.warning("ACP log stream error for agent=%s sessionId=%s: %s", agent, sessionId, e)
            yield f"event: error\ndata: {{\"error\": \"{str(e)}\"}}\n\n"

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )



