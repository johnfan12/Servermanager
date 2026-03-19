# Servermanager AGENTS

## 项目定位

Servermanager 是单节点 GPU 实例管理服务，负责：

- 用户认证与权限
- 容器生命周期管理
- GPU 资源分配与配额控制
- 实例 SSH / FRP 容器侧隧道管理

## 当前实现基线

- FastAPI + SQLAlchemy + SQLite
- Docker 容器实例（每实例 SSH）
- per-instance FRP container 服务：`frpc-container@<container>.service`
- 节点 API FRP 服务：`frpc-api.service`

## 安全修复计划（Servermanager 侧）

### 1. CORS 收敛

- [x] 禁止 `allow_origins=["*"]` 与 `allow_credentials=True` 同时使用。
- [x] 改为仅允许白名单来源（Clustermanager 域名/IP）。
- [x] 支持 `CORS_ALLOW_ORIGINS` / `CORS_ALLOW_CREDENTIALS` 环境配置。

### 2. 生产密钥强校验

- [x] 增加 `ENV`（`dev|prod`）。
- [x] 在 `prod` 下，若以下任一为空或为默认值，启动失败：
  - `JWT_SECRET`
  - `INTERNAL_SERVICE_TOKEN`
  - `ADMIN_PASSWORD`
  - `FRP_TOKEN`

### 3. 实例访问权限边界

- 对涉及单实例信息的接口，统一校验 owner/admin 身份。
- 任何基于 `container_name` 的查询，必须做归属验证。

### 4. 服务间接口加固

- `X-Internal-Token` 仅用于服务间接口。
- 内部接口增加失败审计日志（来源、路径、状态码）。
- 对内部写接口（如 `vps-access`）增加请求频率保护。

### 5. FRP 服务可用性与权限

- 确保 `frpc-container@*` 管理命令具备最小 sudo 权限（start/stop/restart/reload/is-active）。
- 无法启动服务时明确记录权限失败原因，不允许静默失败。

### 6. 容器安全加固（最小权限）

- 默认启用 `no-new-privileges`。
- 收敛 Linux capabilities（默认 `cap_drop=["ALL"]`，按需放开）。
- 评估并逐步落地 user namespace remap / seccomp / apparmor。

## 验收要点（Servermanager）

- 非白名单跨域请求无法调用敏感接口。
- 生产环境默认密钥无法启动。
- 普通用户不能获取他人实例连接信息。
- `frpc-container@<container>` 非 active 时可被自动拉起或明确告警。
