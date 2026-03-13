# nanobot-web-launcher

A small launcher that starts a `nanobot` gateway and exposes a minimal **web UI + REST API** for chatting and inspecting sessions.

- 给 nanobot 加了一个「网页聊天入口」
- 给 nanobot 增加聊天API
- 几乎未改动原项目，尽量避免源代码升级冲突

---

## Features

- **Minimal REST API**
  - `POST /chat`：一问一答，同一请求内返回结果；
  - `GET /history`：按 `session_key` 拉取单个会话历史；
  - `GET /sessions`：列出当前 workspace 下所有会话（用于在 UI 里做「渠道 / 会话」列表）。

- **原生 Web UI**
  - 纯 HTML + CSS + 原生 JS（无框架，结构简单，方便魔改）；
  - 左侧固定一个 `web` 渠道（网页自己的会话 `web:default`），下面自动列出其它渠道（如 Telegram）的会话，点击可查看历史；
  - 支持 emoji 显示、自动滚动到底部。

---

## Project Structure

本仓库只包含「launcher + UI」相关文件，nanobot 自身和工作区由你自己提供。

```text
bot/
  nano_launch.py       # 单 loop launcher + FastAPI 路由
  nano_launch.json     # 启动配置（示例：config 路径、端口等）
  requirements.txt     # 依赖列表
  ui/
    index.html         # 入口（跳转到 chat）
    chat.html          # 对话页面（左侧渠道 / 右侧消息）
  bots/
    little_accountor/  # 你的 nanobot 工作区（不包含在 Git 示例中）
```


---

## Prerequisites

- Python 3.10+
- 已安装并可独立运行的 [`nanobot-ai`](https://pypi.org/project/nanobot-ai/)
- 确保你原先 nanobot workspace 可用
---

## Installation

```bash
git clone https://github.com/<your-name>/nanobot-web-launcher.git
cd nanobot-web-launcher

python -m venv venv
venv\Scripts\activate   # Windows
pip install -r requirements.txt
# 如果需要：
# pip install nanobot-ai
```

---

## Configuration

### 1. `nano_launch.json`

示例内容：

```json
{
  "config": "bots/little_accountor/config.json",
  "bot_port": 18790,
  "api_port": 8000,
  "workspace": null,
  "verbose": false
}
```

字段说明：

- **config**：nanobot 的配置文件路径（相对或绝对），相当于 `nanobot gateway -c <config>` 的 `-c` 参数。
- **bot_port**：nanobot gateway 的端口，目前主要用于日志展示。
- **api_port**：本项目 FastAPI 对外监听的端口（网页和 REST 都从这里进）。
- **workspace**：可选，覆盖 workspace 目录；留 `null` 则使用配置里的默认。
- **verbose**：`true/false`，是否打开 nanobot 的 debug 日志。

### 2. nanobot config

请根据 nanobot 官方文档准备好 `bots/.../config.json`，包括：

- 模型提供方与 API key；
- 启用哪些 channels（Telegram / Slack / Mochat / ...）；
- workspace 路径等。

---

## Usage

在项目根目录运行：

```bash
python nano_launch.py
```

终端会打印类似：

```text
=== Nano Launch API ===
Base URL: http://127.0.0.1:8000/  (or http://localhost:8000/)

Routes:

  POST  /chat
        Body: { "message": string, "session_key": string }
        说明: 向指定会话发送一条消息，并在同一请求中等待回复。
              - session_key 通常形如 "channel:chat_id"，例如:
                "web:default", "telegram:123456789"
              - 网页内置的 Web 会话使用 "web:default"。

   GET  /history?session_key=...
        说明: 返回【单个会话】的历史消息，而不是全部会话。

   GET  /sessions
        说明: 列出当前 workspace 中的所有会话概览。
```

然后在浏览器访问：

```text
http://127.0.0.1:8000/
# 或
http://localhost:8000/
```

即可打开对话页面：

- 左侧：固定 `web` 渠道（网页自己的会话），以及自动检测到的其它渠道（如 `telegram`）；
- 右侧：当前选中会话的历史消息；
- 下方输入框：仅在 `web` 渠道下可用，其它渠道为只读历史视图。

---

## API Quick Reference

### `POST /chat`

- **Body**

```json
{
  "message": "你好，小助手！",
  "session_key": "web:default"
}
```

- **说明**
  - `session_key` 决定这条消息属于哪一个会话，通常形如 `channel:chat_id`。
  - 网页自身使用 `web:default`，Telegram 会话则类似 `telegram:123456789`。
  - 接口会同步等待 LLM 回复，将最终回复以 `{ "reply": "..." }` 的形式返回。

### `GET /history?session_key=<key>`

- **说明**
  - 返回**单个** `session_key` 的历史消息列表。
  - 只读，不会触发新的回复。
  - Web UI 用它在初始化和切换会话时刷新右侧消息区域。

### `GET /sessions`

- **说明**
  - 返回当前 workspace 中所有会话的简要信息，例如：

```json
{
  "sessions": [
    { "key": "web:default", "created_at": "...", "updated_at": "...", "path": "..." },
    { "key": "telegram:123456789", "created_at": "...", "updated_at": "...", "path": "..." }
  ]
}
```

  - 前端可以用 `key` 前缀（`key.split(':')[0]`）当作「渠道名」分组展示。

---

## How it works (Short Version)

- `nano_launch.py` 读取 `nano_launch.json`，然后按 nanobot 官方 `gateway` 的实现方式手工组装：
  - `MessageBus`
  - `AgentLoop`
  - `ChannelManager`
  - `CronService` / `HeartbeatService`
- 单个 `asyncio` 事件循环内以 `asyncio.gather(...)` 并发运行：
  - `agent.run()`（处理来自 bus 的消息）
  - `channels.start_all()`（跑 Telegram 等渠道）
  - `uvicorn.Server(...).serve()`（对外提供 REST + 静态网页）
- Web UI 完全通过 REST 与 launcher 交互，不直接依赖 nanobot 源码，便于迁移和二次开发。

---

## License

（根据你的喜好选择一个协议，例如：）

- MIT
- Apache-2.0
- GPL-3.0

在仓库根目录添加一个 `LICENSE` 文件，并在这里写上协议名。

---

## Credits

- [nanobot-ai](https://github.com/HKUDS/nanobot) – The underlying personal AI assistant framework.
- This repo – just a small launcher + web UI wrapper around it. 🙂

