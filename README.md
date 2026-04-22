# h5-bridge

> H5 聊天桥接层 — FastAPI 后端服务，连接前端与 OpenClaw Gateway

## 架构概览

```
Vue 前端 (HTTPS)
    │
    ├── /api/* ──► h5-bridge (:8080) ──WebSocket RPC──► OpenClaw Gateway (:22533) ──► LLM
    │
    └── /chat ──► Caddy ──► 静态文件 (/var/www/chat)
```

## 技术栈

- **框架**: FastAPI + Uvicorn
- **协议**: SSE (前端→bridge) + WebSocket RPC (bridge→Gateway)
- **认证**: SQLite 用户表 + JWT Bearer Token + Ed25519 设备签名
- **文件存储**: 腾讯云 COS (nickstorage.top)
- **Python**: 3.x

## 项目结构

```
h5-chat/
├── run.py                    # 启动入口: uvicorn bridge.main:app
└── bridge/
    ├── __init__.py
    ├── main.py               # FastAPI 应用主文件，所有路由定义
    ├── config.py             # 环境变量配置（Gateway URL/Token/端口等）
    ├── ws_client.py          # Gateway WebSocket RPC 客户端（Ed25519 签名握手 + 自动重连）
    ├── device.py             # Ed25519 密钥管理（生成/加载/签名）
    ├── users.py              # 用户管理（SQLite CRUD + JWT 认证）
    ├── cos_config.py         # 腾讯云 COS 配置（Bucket/Region/域名）
    ├── cos_util.py           # COS 文件上传工具
    ├── cli.py                # 用户管理命令行工具
    ├── requirements.txt      # Python 依赖
    └── users.db              # SQLite 用户数据库（运行时生成）
```

## API 接口

### 认证

| 方法 | 路径 | 说明 | 鉴权 |
|------|------|------|------|
| POST | `/api/login` | 用户登录，返回 Bearer Token | 无 |
| POST | `/api/logout` | 注销所有 Token | Bearer |
| GET | `/api/status` | 查询连接状态 | Bearer |

### 聊天

| 方法 | 路径 | 说明 | 鉴权 |
|------|------|------|------|
| GET | `/api/history` | 获取聊天历史 | Bearer |
| POST | `/api/chat` | 发送消息（SSE 流式返回） | Bearer |
| POST | `/api/abort` | 中断当前回复 | Bearer |
| GET | `/api/models` | 查询可用模型列表 | Bearer |

### 文件

| 方法 | 路径 | 说明 | 鉴权 |
|------|------|------|------|
| POST | `/api/upload` | 上传文件到 COS，返回公开 URL | Bearer |

### 管理员（仅 admin 角色）

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/api/admin/users` | 列出所有用户 |
| POST | `/api/admin/users` | 创建用户 |
| DELETE | `/api/admin/users/{uid}` | 删除用户 |

## 核心机制

### Agent 路由

用户角色自动映射到不同 Agent：

- `admin` → OpenClaw `main` agent（GLM-5.1，完整能力）
- `user` → OpenClaw `user` agent（MiniMax-M2.7，受限工具集）

Session Key 格式：`agent:{agent_id}:h5-{username}`

### WebSocket RPC 鉴权

使用 Ed25519 签名的设备认证协议（v3），握手流程：

1. 连接 → 收到 `connect.challenge`（含 nonce）
2. 构建 payload：`v3|deviceId|clientId|mode|role|scopes|signedAt|token|nonce|platform|family`
3. Ed25519 签名 payload → 发送 connect 请求
4. Gateway 验证签名 → 返回 `hello-ok`

密钥持久化在 `/root/.openclaw/h5-bridge-device.json`

### SSE 流式推送

`POST /api/chat` 返回 `text/event-stream`，事件格式：

- `{"type": "text", "content": "..."}` — 增量文本
- `{"type": "done"}` — 回复完成
- `{"type": "error", "message": "..."}` — 错误

## 环境变量

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `GW_URL` | `ws://127.0.0.1:22533` | Gateway WebSocket 地址 |
| `GW_TOKEN` | (已配置) | Gateway 认证 Token |
| `DEVICE_KEY_PATH` | `/root/.openclaw/h5-bridge-device.json` | Ed25519 密钥文件路径 |
| `H5_USER_TOKEN` | (已配置) | H5 用户 Token |
| `BRIDGE_PORT` | `8080` | 监听端口 |
| `USER_DB_PATH` | `/root/h5-chat/bridge/users.db` | 用户数据库路径 |
| `COS_SECRET_ID` | (已配置) | 腾讯云 COS SecretId |
| `COS_SECRET_KEY` | (已配置) | 腾讯云 COS SecretKey |

## 部署

### systemd 服务

服务文件：`/etc/systemd/system/h5-bridge.service`

```bash
systemctl start h5-bridge    # 启动
systemctl stop h5-bridge     # 停止
systemctl restart h5-bridge  # 重启
systemctl status h5-bridge   # 查看状态
journalctl -u h5-bridge -f   # 查看日志
```

配置了 `Restart=always` + `RestartSec=5`，崩溃后 5 秒自动重启。

### 依赖安装

```bash
pip install -r bridge/requirements.txt
```

### 默认管理员

首次启动自动创建：`admin` / `admin123`

### 用户管理 CLI

```bash
python3 -m bridge.cli list                              # 列出用户
python3 -m bridge.cli create <用户名> <密码> [角色] [显示名]
python3 -m bridge.cli delete <用户ID>
```
