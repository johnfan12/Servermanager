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

## 部署步骤

### 1. 安装 Python 依赖

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### 2. 配置参数

编辑 `config.py`，至少确认以下配置：

- `SERVER_IP`: Web 页面展示的 SSH 连接 IP
- `DATA_DIR`: 用户数据持久化目录
- `JWT_SECRET`: 生产环境必须修改
- `ALLOW_REGISTER`: 是否允许开放注册
- `ADMIN_USERNAME` / `ADMIN_PASSWORD`: 首次启动自动创建管理员账号
- `AVAILABLE_IMAGES`: 前端镜像选项与 Docker 镜像映射

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

默认监听 `http://0.0.0.0:8888`。

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

## 生产建议

- 将 `JWT_SECRET` 改为强随机值
- 关闭 `ALLOW_REGISTER` 或在注册逻辑中加入审批流程
- 使用反向代理（Nginx / Caddy）暴露 8888 端口
- 为 Docker 镜像预装你们实验室常用环境
- 按需增加容器删除后的目录配额清理逻辑
