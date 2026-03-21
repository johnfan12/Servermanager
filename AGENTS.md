# Servermanager AGENTS

已核对并移除所有已落地的勾选项，以下仅保留未完成事项。

## 待处理问题

### 实例访问权限边界

- [ ] 对涉及单实例信息的接口，统一校验 owner/admin 身份。
- [ ] 任何基于 `container_name` 的查询，必须做归属验证。

### 服务间接口加固

- [ ] 内部接口增加失败审计日志（来源、路径、状态码）。
- [ ] 对内部写接口（如 `vps-access`）增加请求频率保护。

### FRP 服务可用性与权限

- [ ] 确保 `frpc-container@*` 管理命令具备最小 sudo 权限（start/stop/restart/reload/is-active）。
- [ ] 无法启动服务时明确记录权限失败原因，不允许静默失败。

### 容器安全加固

- [ ] 默认启用 `no-new-privileges`。
- [ ] 收敛 Linux capabilities（默认 `cap_drop=["ALL"]`，按需放开）。
- [ ] 评估并逐步落地 user namespace remap / seccomp / apparmor。

### PostgreSQL 迁移

- [x] 在中心侧数据库方案跑通后，启动节点侧改造。
- [x] 第 1 步：等待 `Clustermanager` 完成 PostgreSQL 迁移并稳定运行。
- [x] 第 2 步：为 `Servermanager` 调整 `DATABASE_URL` 接入，兼容 PostgreSQL。
- [x] 第 3 步：重构数据库初始化逻辑，移除仅适用于 SQLite 的固定参数。
- [x] 第 4 步：引入 schema migration 能力，替代长期依赖 `create_all()`。
- [ ] 第 5 步：完成 `users`、`instances`、`gpu_allocations` 的 PostgreSQL schema 初始化。
- [ ] 第 6 步：完成节点侧联调，验证实例与资源管理流程正常。

## 待验收

- [ ] 用户登录、实例列表、创建/停止/重启/删除实例流程正常。
- [ ] `gpu_allocations`、`instances.gpu_indices`、Docker 实际状态一致。
- [ ] `vps_access` 回写、FRP 同步、管理员配额变更无回归。
