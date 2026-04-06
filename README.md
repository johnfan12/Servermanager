# Servermanager Node API

轻量级单节点 GPU 实例管理后端（FastAPI + Docker + PostgreSQL）。

## 核心功能

- JWT 鉴权与节点 API
- 按配额创建 GPU 容器实例（镜像、GPU 数、内存、到期时间）
- 实例管理：查看、停止、重启、删除、日志
- GPU 状态与配额统计
- FRP 容器隧道（每实例独立 `frpc-container@<container>.service`）
- 节点 API 穿透（`frpc-api`，供 Clustermanager 调用）

## 当前定位

- `Clustermanager` 是唯一正式 Web 前端
- `Servermanager` 仅提供节点侧 API 与执行能力
- 根路径 `/` 不再提供管理页面，只返回最小服务说明

## 快速启动

### 新部署（推荐流程）

适用于首次在新机器部署，按顺序执行：

#### 1. 安装 FRP

执行 FRP 安装脚本：

```bash
cd frp
./install.sh
```

安装完成后，确认以下服务配置正确：
- 容器 SSH 隧道：由后端自动维护 per-instance 配置到 `/etc/frp/containers/*.ini`
- 节点 API 隧道：使用 `/etc/frp/frpc-api.ini` + `frpc-api.service`

#### 2. 安装并初始化 PostgreSQL

安装 PostgreSQL（如未安装）：

```bash
# Ubuntu/Debian
sudo apt update
sudo apt install postgresql postgresql-contrib

# 启动 PostgreSQL 服务
sudo systemctl start postgresql
sudo systemctl enable postgresql
```

创建数据库和账号：

```sql
sudo -u postgres psql
CREATE USER server_user WITH PASSWORD 'server_pass';
CREATE DATABASE server_manager OWNER server_user;
\q
```

#### 3. 安装并验证 Docker

安装 Docker（如未安装）：

```bash
# 检查是否已安装
docker --version

# 如未安装，执行以下命令（Ubuntu/Debian）
curl -fsSL https://get.docker.com | sh
sudo usermod -aG docker $USER
# 重新登录以使权限生效
```

验证 Docker 正常工作：

```bash
docker run hello-world
```

#### 4. 配置环境变量

进入项目目录并安装 Python 依赖：

```bash
cd Servermanager

python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

cp .env.copy .env
mkdir -p logs runtime
```

编辑 `.env`，至少确认以下配置：

```env
DATABASE_URL=postgresql+psycopg://server_user:server_pass@127.0.0.1:5432/server_manager
JWT_SECRET=your-jwt-secret
INTERNAL_SERVICE_TOKEN=your-internal-token
FRP_TOKEN=your-frp-token
```

初始化数据库：

```bash
alembic current
alembic upgrade head
alembic current
```

如果系统找不到 `alembic` 命令，可替代为：

```bash
python3 -m alembic -c alembic.ini current
python3 -m alembic -c alembic.ini upgrade head
python3 -m alembic -c alembic.ini current
```

确认 `current` 显示最新 revision 后，启动服务：

```bash
chmod +x start.sh
./start.sh
```

默认服务地址：`http://127.0.0.1:18881`

说明：该地址现在默认提供 API 服务与最小服务说明，不再提供节点本地管理前端。

`start.sh` 会先执行 `alembic upgrade head`，然后再启动 `uvicorn`。

### 构建即开即用 PyTorch + HuggingFace 镜像

```bash
docker build -t lab/pytorch:2.9-cuda12.8 -f docker/Dockerfile.pytorch .
```

这个镜像内置了常用深度学习与 HuggingFace 生态包（如 `transformers`、`datasets`、`huggingface_hub`、`accelerate`、`peft`、`timm` 等），创建实例后可直接使用。

### 构建更全但更大的 PyTorch Full 镜像

```bash
docker build -t lab/pytorch:2.3-cuda12.1-full -f docker/Dockerfile.pytorch_full .
```

可选开启重型组件（编译时间长、体积更大）：

```bash
docker build \
  --build-arg INSTALL_DEEPSPEED=1 \
  --build-arg INSTALL_XFORMERS=1 \
  --build-arg INSTALL_FLASH_ATTN=1 \
  -t lab/pytorch:2.3-cuda12.1-full \
  -f docker/Dockerfile.pytorch_full .
```

## 关键配置

- `JWT_SECRET`：需与 Clustermanager 保持一致
- `INTERNAL_SERVICE_TOKEN`：节点与聚合端服务间调用密钥
- `DATABASE_URL`：节点 PostgreSQL 连接串
- `FRP_TOKEN`：frps/frpc 共用 token
- `FRP_CONTAINER_CONFIG_DIR`：默认 `/etc/frp/containers`
- `ALLOW_REGISTER`：是否开放注册
- `GPU_HOURS_DEFAULT_QUOTA`：已迁移到 Clustermanager 配置，节点侧不再管理默认卡时
- Alembic 配置：`alembic.ini`

### 获取节点 `admin_token`（用于 Clustermanager 的 `NODES_JSON`）

```bash
curl -s http://127.0.0.1:18881/api/auth/login \
  -H 'Content-Type: application/json' \
  -d '{"username":"admin","password":"<ADMIN_PASSWORD>"}' \
  | python3 -c 'import sys,json; print(json.load(sys.stdin)["access_token"])'
```

将输出的 token 填到 `NODES_JSON.node1.admin_token`。

## 常见问题（QA）

### Q0: 服务启动时报数据库连接失败
- 先确认 PostgreSQL 已启动，且 `DATABASE_URL` 中的库、用户、密码正确。
- 再手动执行：`alembic upgrade head`
- 若 migration 失败，先检查数据库权限是否允许建表和建索引。

### Q1: 创建实例后 SSH 显示端口正常，但连接 `Connection closed`
- 先查本机实例服务：`systemctl status "frpc-container@<container>.service"`
- 再查 VPS visitor 服务：`systemctl status "frpc-visitor@<container>.service"`
- 两侧任一未 active，隧道都不可用。

### Q2: 日志出现 `Interactive authentication required` / `sudo: a password is required`
- 原因：Servermanager 运行用户没有权限管理 `frpc-container@*`。
- 需给运行用户配置 sudoers（免密 systemctl start/stop/restart/reload/is-active）。

### Q3: Clustermanager 显示的是本地 SSH 端口，不是 VPS 端口
- 说明 `vps_access` 没回写成功，先检查节点日志是否有 `/api/instances/{name}/vps-access` 404。
- 执行一次节点 FRP 同步并确认实例存在：`POST /api/frp/sync`。

### Q4: Clustermanager 无法访问节点 API（127.0.0.1:18881 refused）
- 检查节点 `Servermanager` 是否运行在 18881。
- 检查 `frpc-api` 是否启动并连到 VPS frps。
- 检查 VPS `frps.ini` 的 `allow_ports` 是否包含 `18881`。
- 若日志出现 `proxy [servermanager-api] already exists`：
  - 避免并行运行 systemd `frpc-api.service` 与 `start.sh` 内置 frpc-api 客户端。
  - 为每个节点设置唯一 `FRP_API_PROXY_NAME`（如 `servermanager-api-node1`/`servermanager-api-node2`）。

### Q5: 新增实例后旧实例 SSH 掉线
- 确认你已切换到 per-instance 模式：
  - 节点侧使用 `frpc-container@*.service`
  - VPS 侧使用 `frpc-visitor@*.service`
- 若节点上仍残留 `frpc-containers.service`，请停用并删除，只保留 `frpc-container@*.service`。
- VPS 侧同样不要并行运行旧的聚合 visitor 服务。

## 相关文件

- 配置：`config.py`、`.env.copy`
- 数据库：`database.py`、`models.py`、`alembic.ini`、`alembic/`
- 启动：`start.sh`
- FRP：`frp/install.sh`
