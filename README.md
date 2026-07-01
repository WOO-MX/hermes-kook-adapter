# hermes-kook-adapter

Hermes Agent 的 KOOK（开黑啦）平台适配器插件。

WebSocket + REST API 全双工连接，支持群聊 @mention、私信、图片/文件/语音上传（KOOK CDN）。

## 架构

```
adapter.py         — KookAdapter 编排器 + register() 入口
ws_handler.py      — KookWebSocketMixin: 连接、监听、帧处理、重连、清理
messaging.py       — KookMessagingMixin: 消息发送、资源上传、HTTP 请求
standalone.py      — 独立发送器、交互式配置向导
constants.py       — KOOK API 常量（消息类型、信号类型、限制值）
config_helpers.py  — 依赖检查、配置校验、环境变量读取
```

## 安装

### 1. 安装依赖

```bash
pip install aiohttp httpx
```

如果走 SOCKS5 代理，额外装 `aiohttp-socks`（可选）：

```bash
pip install aiohttp-socks
```

### 2. 部署插件文件

把所有 .py 文件和 plugin.yaml 复制到 Hermes 的插件目录下：

```
~/.hermes/plugins/platforms/kook/
├── adapter.py
├── ws_handler.py
├── messaging.py
├── standalone.py
├── constants.py
├── config_helpers.py
├── __init__.py
└── plugin.yaml
```

```bash
# 克隆仓库
git clone https://github.com/WOO-MX/hermes-kook-adapter.git
cd hermes-kook-adapter

# 创建目标目录并复制文件
mkdir -p ~/.hermes/plugins/platforms/kook
cp *.py plugin.yaml ~/.hermes/plugins/platforms/kook/
```

> 如果 Hermes 数据目录不在 `~/.hermes`（比如通过 `HERMES_HOME` 环境变量或启动参数指定了其他路径），把上面的 `~/.hermes` 替换为实际路径。

### 3. 配置

**方式一：环境变量（推荐写在 `~/.hermes/.env` 文件里）**

```bash
# 编辑 ~/.hermes/.env，添加：
KOOK_TOKEN=Bot_xxxxxxxxxxxxxxxx
KOOK_HOME_CHANNEL=频道ID              # 可选，cron 投递目标
KOOK_ALLOWED_USERS=用户ID1,用户ID2    # 可选，交互白名单
KOOK_ALLOW_ALL_USERS=true             # 可选，开发模式放行所有用户
KOOK_PROXY=socks5://127.0.0.1:1080    # 可选，代理
```

**方式二：写在 `~/.hermes/config.yaml`**

```yaml
gateway:
  platforms:
    kook:
      enabled: true
      extra:
        token: "Bot_xxxxxxxx"
        home_channel: "频道ID"
        allowed_users: ["用户ID1", "用户ID2"]
        allow_all_users: false
```

两种方式可以共存，`config.yaml` 中的值优先级更高。

### 4. 重启网关

```bash
hermes gateway restart
```

## 重要提醒：独立 Agent 与权限隔离

KOOK 适配器**不应与其他平台共用同一个 Hermes Agent**。建议在 Agent 配置中为 KOOK 频道单独创建一个 Agent 实例：

- **独立人格** — KOOK 频道有独特的社区文化和交流风格，共享 Agent 会导致回复水土不服，甚至泄露其他平台的上下文信息。
- **权限隔离** — Agent 配置中的 [tools] / [resources] / [policy] 需按 KOOK 频道需求单独划定，避免群成员通过聊天间接触达不该访问的内部工具或数据。
- **安全边界** — `allowed_users` 白名单只管"谁能对话"，不管"Agent 能做什么"。权限由 Agent 自身的 policy 和 tool access 决定，务必在 Agent 层面收紧。

简单说：**一台 Agent = 一个平台，不要复用**。在 Hermes 配置文件里给 `kook` 平台单独绑一个 Agent ID。

## 配置参考

| 配置项 | 类型 | 说明 |
|--------|------|------|
| `token` / `KOOK_TOKEN` | string (**必填**) | KOOK Bot Token，格式 `Bot xxxxxxxx` |
| `home_channel` / `KOOK_HOME_CHANNEL` | string | 默认频道 ID，cron 定时推送用 |
| `allowed_users` / `KOOK_ALLOWED_USERS` | list / 逗号分隔 | 允许交互的用户白名单 |
| `allow_all_users` / `KOOK_ALLOW_ALL_USERS` | bool / `"true"` | 开发模式，放行所有用户 |
| `KOOK_PROXY` | string | 代理地址（仅环境变量），支持 socks5/http |

## 许可证

MIT
