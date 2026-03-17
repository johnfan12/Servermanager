# GPU Server Manager

一个面向实验室多 GPU 服务器的轻量级用户分区管理系统。用户通过 Web 页面选择 GPU 数量、内存、镜像和到期时间，系统自动创建带 SSH 服务的独立 Docker 容器实例。

## 功能概览

- FastAPI + SQLite 后端，JWT 登录鉴权
- Docker 容器编排，支持 NVIDIA GPU 映射和 SSH 端口分配
- GPU 实时状态查询与文件锁并发分配保护
- 单文件前端页面，支持登录、注册、实例管理、日志查看、管理员配额管理
- APScheduler 后台任务，自动同步容器状态、停止到期实例、校验 GPU 分配表

## 项目结构

```text
.
|-- auth.py
|-- config.py
|-- container_manager.py
|-- database.py
|-- docker/
|   `-- Dockerfile.pytorch
|-- gpu_manager.py
|-- logs/
|-- main.py
|-- models.py
|-- requirements.txt
|-- runtime/
|-- scheduler.py
|-- start.sh
`-- static/
    `-- index.html
```

## 环境要求

- Ubuntu 22.04
- Python 3.10+
- Docker
- NVIDIA Container Toolkit
- 单 GPU 或多 GPU 都可启动；开发环境只有单 GPU 时，系统会按可见 GPU 数量展示和分配

## Quickstart

下面这组命令面向"单个 GPU 节点 + 一台 VPS 跳板机"的最小可用部署；执行完成后，节点会同时提供：

- `Servermanager` Web/API：监听节点本机 `18881`
- 容器 SSH 的 STCP server：由 `frpc-containers` 自动维护
- 节点 API 的 tcp 穿透：由单独的 `frpc-api` 提供，供 `Clustermanager` 调用

先在 GPU 节点设置变量：

```bash
export VPS_IP=YOUR_VPS_PUBLIC_IP
export JWT_SECRET=change-this-to-a-strong-secret-key
export INTERNAL_SERVICE_TOKEN=change-this-internal-service-token
export FRP_TOKEN=change-this-frp-token
export ADMIN_PASSWORD=change-this-admin-password
```

然后在 `Servermanager` 目录执行：

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

mkdir -p logs runtime
sudo mkdir -p /data/users /etc/frp
sudo chmod 777 /data/users

cat > .env <<EOF
SERVER_IP=${VPS_IP}
DATA_DIR=/data/users
PORT_RANGE=20000-29999
JWT_SECRET=${JWT_SECRET}
JWT_EXPIRE_HOURS=24
INTERNAL_SERVICE_TOKEN=${INTERNAL_SERVICE_TOKEN}
ADMIN_USERNAME=admin
ADMIN_PASSWORD=${ADMIN_PASSWORD}
ALLOW_REGISTER=true
GPU_COUNT=1
DATABASE_URL=sqlite:///./servermanager.db
FRP_ENABLED=true
FRP_SERVER_ADDR=${VPS_IP}
FRP_SERVER_PORT=7000
FRP_TOKEN=${FRP_TOKEN}
FRP_CONFIG_DIR=/etc/frp
FRP_CONTAINER_SK_PREFIX=gpu-container
FRP_API_ENABLED=true
FRP_API_REMOTE_PORT=18881
EOF

docker build -t lab/pytorch:2.3-cuda12.1 -f docker/Dockerfile.pytorch .

cd frp
sudo bash install.sh
cd ..

sudo tee /etc/frp/frpc-api.ini >/dev/null <<EOF
[common]
server_addr = ${VPS_IP}
server_port = 7000
token = ${FRP_TOKEN}

[servermanager-api]
type = tcp
local_ip = 127.0.0.1
local_port = 18881
remote_port = 18881
EOF

sudo tee /etc/systemd/system/frpc-api.service >/dev/null <<'EOF'
[Unit]
Description=FRP Client for Servermanager API
After=network.target

[Service]
Type=simple
User=root
Restart=always
RestartSec=5
ExecStart=/usr/local/bin/frpc -c /etc/frp/frpc-api.ini
ExecReload=/bin/kill -HUP $MAINPID

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
chmod +x start.sh
nohup ./start.sh > logs/servermanager.log 2>&1 &
sleep 5
curl http://127.0.0.1:18881/

sudo systemctl enable --now frpc-containers frpc-api
```

如果你直接用 `./start.sh` 启动，脚本现在也会自动根据 `.env` 生成 `runtime/frpc-api.ini` 并拉起一个用户态 `frpc` 进程，把节点 API 暴露到 VPS；这样即使忘记手动启动 `frpc-api.service`，也能避免 `Clustermanager -> 127.0.0.1:18881` 连不上。

验证节点已经可供 VPS 聚合：

```bash
curl -s http://127.0.0.1:18881/api/auth/login \
  -H 'Content-Type: application/json' \
  -d '{"username":"admin","password":"'"${ADMIN_PASSWORD}"'"}'
```

记下返回里的 `access_token`，后面填入 VPS 上 `Clustermanager` 的 `NODES_JSON.node1.admin_token`。

## 部署步骤

### 1. 安装 Python 依赖

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### 2. 配置参数（.env 文件）

复制模板文件并修改：

```bash
cp .env.copy .env
nano .env
```

至少修改以下配置：

| 配置项 | 说明 | 示例 |
|--------|------|------|
| `SERVER_IP` | 本机 IP 地址 | `192.168.1.100` |
| `JWT_SECRET` | JWT 密钥（生产环境必须修改） | `your-strong-secret` |
| `ADMIN_PASSWORD` | 管理员密码 | `your-admin-password` |
| `FRP_SERVER_ADDR` | VPS 公网 IP（启用 FRP 时需要） | `1.2.3.4` |
| `FRP_TOKEN` | FRP 认证令牌 | `your-frp-token` |

> **提示**：配置优先级：环境变量 > `.env` 文件 > `config.py` 默认值

建议先创建数据目录：

```bash
sudo mkdir -p /data/users
sudo chmod 777 /data/users
```

### 3. 构建示例镜像

先构建 PyTorch 2.3 镜像：

```bash
docker build -t lab/pytorch:2.3-cuda12.1 -f docker/Dockerfile.pytorch docker
```

如果需要补齐前端可选镜像，可基于相同模式额外构建：

- `lab/pytorch:2.1-cuda11.8`
- `lab/tensorflow:2.15`
- 或直接修改 `config.py` 中 `AVAILABLE_IMAGES`

### 4. 启动服务

```bash
chmod +x start.sh
./start.sh
```

默认监听 `http://0.0.0.0:18881`。

若 `.env` 中同时配置了 `FRP_ENABLED=true`、`FRP_API_ENABLED=true`、`FRP_SERVER_ADDR`、`FRP_TOKEN`，`start.sh` 会额外自动启动 API FRP client，并把日志写到 `logs/frpc-api.log`。

## 默认账号

首次启动会自动创建管理员账号：

- 用户名：`admin`
- 密码：`admin123`

生产环境请立即修改 `config.py` 并重建数据库或手动修改账号密码。

## Docker / GPU 运行说明

- 容器命名：`gpu_user_<username>_<timestamp>`
- 用户目录挂载：`/data/users/<username>` -> `/root/workspace`
- SSH 端口：随机分配 `20000-29999`
- 容器内会自动设置随机 8 位 root 密码并启动 SSH 服务
- 显卡空闲判断：GPU 利用率 < 10% 且已用显存 < 500MB

## API 概览

### 认证

- `POST /api/auth/login`
- `POST /api/auth/register`

### 用户实例管理

- `GET /api/instances`
- `POST /api/instances`
- `DELETE /api/instances/{instance_id}`
- `POST /api/instances/{instance_id}/stop`
- `POST /api/instances/{instance_id}/restart`
- `GET /api/instances/{instance_id}/logs`

### 资源与配额

- `GET /api/gpus/status`
- `GET /api/quota/me`

### 管理员

- `GET /api/admin/users`
- `PUT /api/admin/users/{user_id}/quota`
- `GET /api/admin/instances`
- `DELETE /api/admin/instances/{instance_id}`

## 日志与数据库

- 应用日志：`logs/app.log`
- SQLite 数据库：`servermanager.db`

## 单 GPU 开发说明

当前项目已按“开发环境只有单 GPU”场景兼容处理：

- GPU 面板只渲染当前可见 GPU
- 前端 GPU 数量按钮会根据实际空闲数量自动置灰
- 当开发环境未检测到可用 GPU 时，接口会返回空列表，前端显示提示信息

## FRP 容器端口穿透（可选）

如需让 VPS 能够访问容器 SSH 端口，启用 FRP 功能：

### 1. 安装 frpc

```bash
cd frp
sudo bash install.sh
```

### 2. 配置 .env

```bash
# 启用 FRP
FRP_ENABLED=true
FRP_SERVER_ADDR=your-vps-public-ip
FRP_SERVER_PORT=7000
FRP_TOKEN=your-frp-secret-token
```

### 3. 启动 frpc 服务

```bash
sudo systemctl enable --now frpc-containers
```

如果还要让 VPS 访问节点 API，请额外创建 `/etc/frp/frpc-api.ini` 和 `frpc-api.service`；完整命令见前面的 Quickstart。

容器创建/删除时会自动更新 `frpc-containers.ini`，但节点 API 穿透配置不会自动生成。

详细说明见 [FRP_DEPLOYMENT_GUIDE.md](../FRP_DEPLOYMENT_GUIDE.md)

## 生产建议

- 将 `JWT_SECRET` 改为强随机值
- 关闭 `ALLOW_REGISTER` 或在注册逻辑中加入审批流程
- 使用反向代理（Nginx / Caddy）暴露 8888 端口
- 为 Docker 镜像预装你们实验室常用环境
- 按需增加容器删除后的目录配额清理逻辑
