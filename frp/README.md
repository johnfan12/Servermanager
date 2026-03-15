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

### 1. frpc-containers（Servermanager 本地）

- **配置文件**: `/etc/frp/frpc-containers.ini`
- **功能**: 为每个运行的容器创建 stcp 隧道
- **触发方式**: 容器创建/删除时自动更新

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
sudo cp frp/frpc-containers.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable frpc-containers
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
sudo systemctl start frpc-containers
python main.py  # 或启动你的服务
```

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
sudo systemctl status frpc-containers
sudo journalctl -u frpc-containers -f

# VPS
sudo systemctl status frpc-visitors
sudo journalctl -u frpc-visitors -f
```

### 检查配置

```bash
# Servermanager
cat /etc/frp/frpc-containers.ini

# VPS
cat /etc/frp/frpc-visitors.ini
```

### 测试连接

```bash
# 从 Clustermanager API 获取访问端口
curl -H "Authorization: Bearer $TOKEN" http://localhost:8000/api/frp/containers

# 直接连接
ssh -p <vps_port> root@your-vps-ip
```
