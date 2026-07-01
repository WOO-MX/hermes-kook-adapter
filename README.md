# hermes-kook-adapter

Hermes Agent 的 KOOK（开黑啦）平台适配器插件。

WebSocket + REST API 全双工连接，支持群聊 @mention、私信、图片/文件/语音上传（KOOK CDN）。

## 架构

```
adapter.py (1042 行)
├── WebSocket 网关  ── 实时消息事件
├── REST API (httpx) ── 消息发送、资源上传、频道查询
├── @mention 门控   ── 群聊只响应 @bot 的消息
├── 自消息过滤       ── 通过 /user/me 获取 bot ID
├── 断线重连         ── 指数退避，最大 5 次
└── 独立发送器       ── 无需长连接，cron 投递用
```

## 文件

| 文件 | 说明 |
|------|------|
| `adapter.py` | 核心适配器 |
| `__init__.py` | 包入口，导出 `register` |
| `plugin.yaml` | Hermes 插件清单 |

## 依赖

```
aiohttp>=3.8
httpx>=0.24
aiohttp-socks  # 可选，SOCKS5 代理用
```

## 配置

环境变量：

| 变量 | 必填 | 说明 |
|------|------|------|
| `KOOK_TOKEN` | 是 | KOOK Bot Token（`Bot xxxx` 格式） |
| `KOOK_HOME_CHANNEL` | 否 | 默认频道 ID（cron 投递） |
| `KOOK_ALLOWED_USERS` | 否 | 允许交互的用户 ID，逗号分隔 |
| `KOOK_ALLOW_ALL_USERS` | 否 | 开发模式：允许所有用户 |
| `KOOK_PROXY` | 否 | 代理 URL（SOCKS5/HTTP） |

或在 `config.yaml`：

```yaml
gateway:
  platforms:
    kook:
      enabled: true
      extra:
        token: "Bot_xxxxxxxx"
        home_channel: "channel_id"
        allowed_users: ["user_id1"]
        allow_all_users: false
```

## 安装

```bash
pip install aiohttp httpx
hermes plugins enable kook
```

## 许可证

MIT
