"""Gateway WebSocket RPC client with Ed25519 device auth."""

import asyncio
import json
import uuid
import logging
from typing import Callable, Optional

import websockets

from . import device, config

logger = logging.getLogger("ws_client")


class GatewayWSClient:
    def __init__(self):
        self.ws = None
        self.private_key, self.device_id = device.load_or_create_keys(config.DEVICE_KEY_PATH)
        self._connected = False
        self._rpc_callbacks: dict[str, asyncio.Future] = {}
        self._event_handlers: dict[str, list[Callable]] = {}
        self._recv_task: Optional[asyncio.Task] = None
        self._ping_task: Optional[asyncio.Task] = None

    # ── Connection ──────────────────────────────────────────────

    async def connect(self):
        """Establish WS connection and complete the signed handshake."""
        logger.info("Connecting to %s ...", config.GW_URL)

        self.ws = await websockets.connect(
            config.GW_URL,
            max_size=10 * 1024 * 1024,
            ping_interval=None,  # we manage our own ping
            origin="https://www.nickhome.cloud",
        )

        # 1) Wait for connect.challenge
        raw = await self.ws.recv()
        challenge_msg = json.loads(raw)
        if challenge_msg.get("event") != "connect.challenge":
            raise RuntimeError(f"Expected challenge, got: {challenge_msg}")

        nonce = challenge_msg["payload"]["nonce"]
        logger.info("Got challenge nonce=%s…", nonce[:16])

        # 2) Build signed connect request
        device_auth = device.build_device_auth(
            self.private_key, nonce, config.GW_TOKEN,
            client_id="gateway-client",
            client_mode="backend",
        )

        connect_req = {
            "type": "req",
            "id": str(uuid.uuid4()),
            "method": "connect",
            "params": {
                "minProtocol": 3,
                "maxProtocol": 3,
                "client": {
                    "id": "gateway-client",
                    "version": "1.0.0",
                    "platform": "linux",
                    "mode": "backend",
                },
                "role": "operator",
                "scopes": ["operator.read", "operator.write"],
                "caps": [],
                "commands": [],
                "permissions": {},
                "auth": {"token": config.GW_TOKEN},
                "locale": "zh-CN",
                "userAgent": "h5-bridge/1.0.0",
                "device": device_auth,
            },
        }

        await self.ws.send(json.dumps(connect_req))

        # 3) Wait for hello-ok
        hello_raw = await self.ws.recv()
        hello_msg = json.loads(hello_raw)
        if hello_msg.get("type") != "res" or not hello_msg.get("ok"):
            error = hello_msg.get("error", {})
            raise RuntimeError(
                f"Handshake failed: code={error.get('code')} "
                f"message={error.get('message')} "
                f"details={error.get('details')}"
            )

        payload = hello_msg.get("payload", {})
        logger.info(
            "Connected! protocol=%s", payload.get("protocol")
        )

        auth_info = payload.get("auth", {})
        if auth_info.get("deviceToken"):
            logger.info("Got device token")

        self._connected = True

        # Start background tasks
        self._recv_task = asyncio.create_task(self._recv_loop())
        self._ping_task = asyncio.create_task(self._ping_loop())

    # ── Receive loop ────────────────────────────────────────────

    async def _recv_loop(self):
        try:
            async for raw in self.ws:
                msg = json.loads(raw)
                msg_type = msg.get("type")

                if msg_type == "res":
                    req_id = msg.get("id")
                    future = self._rpc_callbacks.pop(req_id, None)
                    if future and not future.done():
                        future.set_result(msg)

                elif msg_type == "event":
                    event_name = msg.get("event", "")
                    handlers = self._event_handlers.get(event_name, [])
                    for handler in handlers:
                        try:
                            await handler(msg.get("payload", {}))
                        except Exception:
                            logger.exception("Event handler error for %s", event_name)
        except websockets.ConnectionClosed as e:
            logger.warning("WS closed: code=%s reason=%s", e.code, e.reason)
            self._connected = False
            # Cancel pending RPCs
            for fut in self._rpc_callbacks.values():
                if not fut.done():
                    fut.set_exception(ConnectionError("WS closed"))
            self._rpc_callbacks.clear()

    async def _ping_loop(self):
        while self._connected:
            await asyncio.sleep(30)
            if self.ws is not None:
                try:
                    await self.ws.ping()
                except Exception:
                    self._connected = False
                    break

    # ── RPC ─────────────────────────────────────────────────────

    async def rpc(self, method: str, params: dict = None, timeout: float = 300) -> dict:
        req_id = str(uuid.uuid4())
        loop = asyncio.get_running_loop()
        future = loop.create_future()
        self._rpc_callbacks[req_id] = future

        frame = {
            "type": "req",
            "id": req_id,
            "method": method,
            "params": params or {},
        }
        await self.ws.send(json.dumps(frame))

        try:
            return await asyncio.wait_for(future, timeout)
        except asyncio.TimeoutError:
            self._rpc_callbacks.pop(req_id, None)
            raise

    # ── Events ──────────────────────────────────────────────────

    def on_event(self, event_name: str, handler: Callable):
        handlers = self._event_handlers.setdefault(event_name, [])
        if handler in handlers:
            return
        handlers.append(handler)

    def off_event(self, event_name: str, handler: Callable):
        handlers = self._event_handlers.get(event_name, [])
        if handler in handlers:
            handlers.remove(handler)

    # ── State ───────────────────────────────────────────────────

    @property
    def connected(self):
        if self.ws is None:
            return False
        try:
            return self._connected and not self.ws.transport.is_closing()
        except Exception:
            return self._connected


# ── Global singleton ────────────────────────────────────────────

_client: Optional[GatewayWSClient] = None
_client_lock = asyncio.Lock()  # 防止并发 get_client() 创建多个实例
_agent_listeners_migration: list[tuple[str, Callable]] = []  # (event_name, handler)


def add_migrating_listener(event_name: str, handler: Callable):
    """Register a listener for reconnect migration without duplicate entries."""
    pair = (event_name, handler)
    if pair in _agent_listeners_migration:
        return
    _agent_listeners_migration.append(pair)


def _migrate_agent_listeners(new_client: GatewayWSClient):
    """Re-register all saved event listeners on a new client instance."""
    for event_name, handler in _agent_listeners_migration:
        new_client.on_event(event_name, handler)
    logger.info("Migrated %d event listeners to new client", len(_agent_listeners_migration))


async def get_client() -> GatewayWSClient:
    """Get or create the WS client singleton, with auto-reconnect.
    Uses an async lock to prevent concurrent reconnect storms.
    """
    global _client
    if _client is not None and _client.connected:
        return _client

    async with _client_lock:
        # Double-check after acquiring lock
        if _client is not None and _client.connected:
            return _client

        # Close old client if exists but disconnected
        old_client = _client
        _client = None
        if old_client is not None:
            try:
                if old_client.ws:
                    await old_client.ws.close()
            except Exception:
                pass

        backoff = [1, 2, 5, 10, 15]
        for i, delay in enumerate(backoff):
            try:
                new_client = GatewayWSClient()
                await new_client.connect()
                _migrate_agent_listeners(new_client)
                _client = new_client
                logger.info("Reconnect succeeded on attempt %d", i + 1)
                return _client
            except Exception as e:
                logger.warning("Connect attempt %d failed: %s", i + 1, e)
                _client = None
                if i < len(backoff) - 1:
                    await asyncio.sleep(delay)
        raise ConnectionError("Failed to connect to Gateway after retries")
