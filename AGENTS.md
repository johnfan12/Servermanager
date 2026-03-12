# 任务：构建实验室多GPU服务器用户分区管理系统

## 项目背景
我有一台多GPU服务器（Ubuntu 22.04），需要将其改造成支持多用户的GPU实例管理平台。
用户可以通过Web界面自选GPU数量、内存容量，创建独立的Docker容器实例，通过SSH连接使用。

## 技术栈要求
- 后端：Python 3.10+ / FastAPI
- 容器：Docker + NVIDIA Container Toolkit（已安装）
- 前端：单文件HTML（内嵌CSS/JS，无需构建工具）
- 数据库：SQLite（via SQLAlchemy）
- 认证：JWT Token
- 无需 Kubernetes，保持轻量

---

## 需要实现的功能模块

### 1. GPU资源管理器 `gpu_manager.py`
- 调用 `nvidia-smi` 实时查询每张GPU的：显存占用、GPU利用率、温度
- 判断GPU是否空闲（利用率<10% 且 已用显存<500MB 视为空闲）
- 维护 GPU 分配表（哪些GPU被哪个容器占用）
- 提供 `allocate(user, gpu_indices, memory_gb, cpu_cores)` 方法
- 提供 `release(container_name)` 方法释放GPU
- 使用文件锁（filelock库）防止并发分配冲突

### 2. Docker容器管理器 `container_manager.py`
- 使用 docker-py 库操作容器
- 创建容器时的参数：
  - `--gpus "device=0,1"` 指定GPU
  - `--memory` 限制内存上限
  - `--shm-size` 设为内存的一半（PyTorch训练需要）
  - `--cpus` 按GPU数量*8分配CPU核数
  - `-p {host_port}:22` 暴露SSH端口
  - `-v /data/users/{username}:/root/workspace` 持久化用户数据目录
  - 容器命名规则：`gpu_user_{username}_{timestamp}`
- 容器启动后自动执行：启动SSH服务、设置root密码为随机8位字符串
- 支持操作：创建、停止、重启、删除、查看日志（最后100行）
- 端口分配范围：20000-29999，随机选取未占用端口

### 3. 数据库模型 `models.py`（SQLAlchemy）
需要以下表：

**users表**
- id, username, password_hash, email
- quota_gpu（最多可同时使用GPU数，默认4）
- quota_memory_gb（最多可用内存GB，默认64）
- quota_max_instances（最多同时开启实例数，默认3）
- is_admin（布尔值）
- created_at

**instances表**
- id, user_id(FK), container_name, container_id
- gpu_indices（JSON字段，存储[0,1]这样的列表）
- memory_gb, cpu_cores, ssh_port, ssh_password
- image_name（使用的镜像）
- status（running/stopped/error）
- created_at, stopped_at
- expire_at（到期时间，可为空表示不限时）

**gpu_allocations表**
- gpu_index, instance_id(FK), allocated_at

### 4. FastAPI 后端 `main.py`

实现以下API端点：

**认证**
- POST `/api/auth/login` → 返回JWT token
- POST `/api/auth/register` → 注册新用户（需要admin审批或开放注册开关）

**实例管理**
- GET `/api/instances` → 获取当前用户所有实例列表
- POST `/api/instances` → 创建新实例，body：
```json
  {
    "num_gpus": 2,
    "memory_gb": 32,
    "image": "pytorch",
    "expire_hours": 24
  }
```
- DELETE `/api/instances/{instance_id}` → 删除实例
- POST `/api/instances/{instance_id}/stop` → 停止实例
- POST `/api/instances/{instance_id}/restart` → 重启实例
- GET `/api/instances/{instance_id}/logs` → 获取容器日志

**资源查询**
- GET `/api/gpus/status` → 返回所有GPU当前状态（显存、利用率、占用者）
- GET `/api/quota/me` → 返回当前用户配额使用情况

**管理员**
- GET `/api/admin/users` → 用户列表
- PUT `/api/admin/users/{user_id}/quota` → 修改用户配额
- GET `/api/admin/instances` → 所有用户实例列表
- DELETE `/api/admin/instances/{instance_id}` → 强制删除任意实例

### 5. 前端页面 `static/index.html`（单文件）

实现一个简洁的Web管理界面，包含：

**顶部导航栏**
- 显示当前用户名、已用/总配额（GPU数、内存）
- 退出登录按钮

**GPU状态面板**（顶部横向卡片）
- 每张GPU一个卡片，显示：GPU编号、型号、显存使用进度条、利用率、温度
- 颜色区分：空闲（绿）、占用（红）、部分占用（黄）

**创建实例表单**（右侧面板）
- GPU数量选择：1 / 2 / 4 / 8（按钮组，超出配额的置灰）
- 内存滑块：8G 到 maxQuota，步长8G
- 镜像下拉框：
  - PyTorch 2.3 (CUDA 12.1)
  - PyTorch 2.1 (CUDA 11.8)
  - TensorFlow 2.15
  - Ubuntu 22.04 Base
- 到期时间：不限 / 24小时 / 3天 / 7天
- 创建按钮，创建中显示loading状态

**实例列表**（主内容区）
- 每个实例显示为卡片：
  - 实例名、状态badge（运行中/已停止）
  - 使用的GPU编号、内存、镜像
  - SSH连接命令（可一键复制）：`ssh -p {port} root@{server_ip}`
  - SSH密码（点击显示/隐藏）
  - 到期倒计时
  - 操作按钮：停止、重启、删除、查看日志
- 日志弹窗：黑色背景终端样式，自动滚动到底部

**样式要求**
- 深色主题（背景 #0f1117，卡片 #1a1d27）
- 主色调蓝色 #4f8ef7
- 无需引入UI框架，纯CSS实现
- 响应式布局

### 6. Docker镜像准备 `docker/Dockerfile.pytorch`
```dockerfile
FROM pytorch/pytorch:2.3.0-cuda12.1-cudnn8-runtime
RUN apt-get update && apt-get install -y openssh-server vim git wget curl htop tmux
RUN mkdir /var/run/sshd
RUN echo 'PermitRootLogin yes' >> /etc/ssh/sshd_config
EXPOSE 22
CMD ["/usr/sbin/sshd", "-D"]
```

### 7. 后台任务 `scheduler.py`
- 每60秒同步一次：检查运行中的容器实际状态，更新数据库status字段
- 每5分钟检查一次：到期的实例自动停止
- 每10分钟刷新一次：GPU分配表与实际docker容器的一致性校验
- 使用 APScheduler 库实现

### 8. 配置文件 `config.py`
```python
SERVER_IP = "192.168.1.100"     # 服务器对外IP，用于生成SSH命令
DATA_DIR = "/data/users"         # 用户数据根目录
PORT_RANGE = (20000, 29999)      # SSH端口分配范围
JWT_SECRET = "change-this-secret"
JWT_EXPIRE_HOURS = 24
ALLOW_REGISTER = True            # 是否开放注册
ADMIN_USERNAME = "admin"
ADMIN_PASSWORD = "admin123"

# 可用镜像映射
AVAILABLE_IMAGES = {
    "pytorch": "lab/pytorch:2.3-cuda12.1",
    "pytorch_old": "lab/pytorch:2.1-cuda11.8",
    "tensorflow": "lab/tensorflow:2.15",
    "base": "ubuntu:22.04",
}
```

### 9. 项目入口与启动脚本

`start.sh`：
```bash
#!/bin/bash
uvicorn main:app --host 0.0.0.0 --port 8888 --reload
```

`requirements.txt` 需包含所有依赖。

`README.md` 包含完整部署步骤。

---

## 代码要求

1. **错误处理**：所有Docker操作和nvidia-smi调用都要有try/except，返回清晰的错误信息
2. **日志**：使用Python logging模块，记录所有实例创建/删除操作到 `logs/app.log`
3. **并发安全**：GPU分配使用文件锁，防止同时创建时分配到同一GPU
4. **代码注释**：每个函数写docstring，关键逻辑写行内注释
5. **类型注解**：所有函数参数和返回值加类型注解
6. **测试**：每一个主要代码文件都要进行测试，然后删除测试文件