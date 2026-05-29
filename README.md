# 简化版 Servermanager

这个入口只做节点侧内网穿透，不包含 Docker、GPU 实例、注册、计费或配额。

## 能力范围

- 使用本机 Linux 账号通过 PAM 登录。
- 节点固定暴露一个 SSH 公网端口到 VPS/frps。
- 返回 SSH 命令，例如 `ssh -p 30000 user@vps.example.com`。
- 管理员账号可查看和删除所有隧道。
- 独立使用 `simple_servermanager.db`、`runtime/simple-frpc-tunnels.ini` 和 `logs/simple-*.log`。

## 启动

```bash
pip install -r requirements.txt
./start.sh
```

节点 API 默认监听 `18881`。如果节点在内网，`start.sh` 会默认启动一个 API FRP 客户端，把节点 API 暴露到 VPS 的同端口。

## 常用环境变量

```bash
SIMPLE_SERVERMANAGER_PORT=18881
SIMPLE_PUBLIC_HOST=vps.example.com

SIMPLE_JWT_SECRET=replace-with-a-long-random-secret
SIMPLE_INTERNAL_SERVICE_TOKEN=replace-with-a-long-random-token

SIMPLE_FRP_SERVER_ADDR=vps.example.com
SIMPLE_FRP_SERVER_PORT=7000
SIMPLE_FRP_TOKEN=replace-with-frp-token
SIMPLE_FRP_CLIENT_BIN=/usr/local/bin/frpc

SIMPLE_API_FRP_ENABLED=true
SIMPLE_API_REMOTE_PORT=18881

SIMPLE_SSH_TUNNEL_ENABLED=true
SIMPLE_SSH_REMOTE_PORT=30000
SIMPLE_SSH_LOCAL_HOST=127.0.0.1
SIMPLE_SSH_LOCAL_PORT=22
SIMPLE_ALLOW_NON_LOOPBACK=false
SIMPLE_ADMIN_USERS=admin
SIMPLE_ALLOWED_GROUPS=
```

`SIMPLE_INTERNAL_SERVICE_TOKEN` 必须和简化版 Clustermanager 保持一致。
`SIMPLE_PUBLIC_HOST` 填纯 IP 或域名，不要带 `http://` 或 `https://`。

## 账号规则

默认只允许 UID 大于等于 `1000` 且 shell 不是 `nologin/false` 的本机用户登录。可通过这些变量调整：

- `SIMPLE_ALLOWED_UID_MIN`
- `SIMPLE_ALLOW_SYSTEM_USERS`
- `SIMPLE_ALLOWED_GROUPS`
- `SIMPLE_ADMIN_USERS`
- `SIMPLE_ADMIN_GROUPS`

PAM 依赖可用 `pip install python-pam` 安装；部分系统也可以使用发行版包 `python3-pam`。

## API

- `POST /api/login`
- `GET /api/auth/me`
- `GET /api/tunnels`
- `GET /api/ssh-access`
- `GET /api/internal/tunnels`
- `GET /api/internal/ssh-access`

`/api/internal/*` 由 Clustermanager 调用，需要 `X-Internal-Token`、`X-User` 和 `X-User-Is-Admin` 请求头。

旧的 `POST/DELETE /api/*/tunnels` 会保留兼容返回，但固定端口模式下不再创建或删除用户级 FRP 进程。
