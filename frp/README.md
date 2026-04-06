# FRP 动态端口穿透配置

## 架构说明

Servermanager 使用 FRP 的 **stcp（secret TCP）** 模式将容器 SSH 端口安全地暴露到 VPS：

```
┌─────────────────┐         stcp (secret)           ┌─────────────────┐
│   Servermanager │  ═══════════════════════════════► │      VPS        │
│   (本地服务器)   │    container-{name}              │   (frps 7000)   │
│                 │                                 │                 │
│  ┌───────────┐  │                                 │  ┌───────────┐  │
│  │ Container │──┼──► local:22 ──► frpc ──────────►│  │  visitor  │  │
│  │  ssh:22   │  │    (stcp sk=xxx)                 │  │  (port X) │  │
│  └───────────┘  │                                 │  └─────┬─────┘  │
│  ┌───────────┐  │                                 │        │        │
│  │ Container │──┼──► local:22 ──► frpc ──────────►│        ▼        │
│  │  ssh:22   │  │                                 │  User access    │
│  └───────────┘  │                                 │  ssh -p X user@ │
└─────────────────┘                                 └─────────────────┘
```

## 组件

### 1. frpc-container@<container>（Servermanager 本地）

- **配置文件**: `/etc/frp/containers/<container>.ini`
- **功能**: 为每个运行中的容器维护独立 stcp 隧道
- **触发方式**: 容器创建/删除时由 Servermanager 自动增删和启停

### 2. frpc-visitors（VPS 上）

- **配置文件**: `/etc/frp/frpc-visitors.ini`
- **功能**: 为每个容器创建 visitor，在 VPS 上暴露访问端口
- **触发方式**: Clustermanager 启动时同步，或手动触发

## 部署步骤

### Servermanager（本地服务器）

1. **安装 frp**:
```bash
wget https://github.com/fatedier/frp/releases/download/v0.58.1/frp_0.58.1_linux_amd64.tar.gz
tar -xzf frp_0.58.1_linux_amd64.tar.gz
sudo cp frp_0.58.1_linux_amd64/frpc /usr/local/bin/
```

2. **创建 systemd 服务**:
```bash
sudo cp frp/frpc-container@.service /etc/systemd/system/
sudo cp frp/frpc-api.service /etc/systemd/system/
sudo systemctl daemon-reload
```

3. **配置环境变量**（在 `start.sh` 或 systemd 中）:
```bash
export FRP_ENABLED=true
export FRP_SERVER_ADDR="your-vps-public-ip"
export FRP_SERVER_PORT=7000
export FRP_TOKEN="your-frp-secret-token"
```

4. **启动服务**:
```bash
sudo systemctl enable frpc-api
sudo systemctl start frpc-api
./start.sh
```

说明：实例 SSH 隧道不需要手工启 `frpc-container@...`，Servermanager 会在实例创建、删除和同步时自动管理。

### Clustermanager（VPS）

1. **安装 frp**（同上）

2. **启动 frps**（如果还没有）:
```ini
# /etc/frp/frps.ini
[common]
bind_port = 7000
token = your-frp-secret-token
dashboard_port = 7500
dashboard_user = admin
dashboard_pwd = admin123
```

```bash
sudo frps -c /etc/frp/frps.ini
```

3. **创建 visitor systemd 服务**:
```bash
sudo cp frp/frpc-visitors.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable frpc-visitors
sudo systemctl start frpc-visitors
```

4. **配置 Clustermanager**:
确保 `config.py` 中的 FRP_TOKEN 与 Servermanager 一致。

## API 端点

### Servermanager

- `GET /api/frp/containers` - 获取所有容器的 FRP 配置（含 secret key）
- `GET /api/frp/containers/{name}` - 获取单个容器的 FRP 配置
- `POST /api/frp/sync` - 手动触发配置同步

### Clustermanager

- `GET /api/frp/containers` - 获取所有容器的访问映射
- `POST /api/frp/sync` - 手动触发 visitor 配置更新
- `POST /api/cluster/instances/{id}/connect` - 获取实例连接信息

## 故障排查

### 检查 frpc 状态

```bash
# Servermanager
sudo systemctl status "frpc-container@<container>.service"
sudo journalctl -u "frpc-container@<container>.service" -f
sudo systemctl status frpc-api
sudo journalctl -u frpc-api -f

# VPS
sudo systemctl status frpc-visitors
sudo journalctl -u frpc-visitors -f
```

### 检查配置

```bash
# Servermanager
ls /etc/frp/containers
cat /etc/frp/containers/<container>.ini

# VPS
cat /etc/frp/frpc-visitors.ini
```

### 旧模式清理

旧版聚合模式使用 `/etc/frp/frpc-containers.ini` + `frpc-containers.service`。

- 新部署不再使用该模式
- `frp/install.sh` 会尝试停用并删除旧服务文件
- `Servermanager` 仍会在迁移期间读取 legacy 配置并清理旧 section，避免 ghost recovery

### 测试连接

```bash
# 从 Clustermanager API 获取访问端口
curl -H "Authorization: Bearer $TOKEN" http://localhost:8000/api/frp/containers

# 直接连接
ssh -p <vps_port> root@your-vps-ip
```
