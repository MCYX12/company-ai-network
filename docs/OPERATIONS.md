# 运维手册

## 服务

单机 PoC 可由 `scripts/run_local.py` 前台运行；生产/长期运行使用 systemd 用户服务：

```bash
systemctl --user status company-agent-stack.service
systemctl --user status company-agent-dashboard.service
journalctl --user -u company-agent-stack.service -n 100 --no-pager
```

模板位于 `packaging/systemd/`。服务只管理自己启动的子进程，不要用全局 kill 影响其他系统服务。

## 健康检查

```bash
.venv/bin/python -m app.healthcheck --identity ag-a
```

健康状态覆盖证书到期、NATS/Router、目录在线数、磁盘、OIDC 同步和项目记录新鲜度。健康检查失败不会自动放行；管理员页面显示具体检查项。

## 日志与审计

- 运行日志在 `.runtime*/`，按服务/组件分文件。
- 审计为 JSONL；生产应外发到独立存储，并设置保留期限。
- 不要把日志中的任务正文、路径或凭据复制到公开 Issue。

## 备份与恢复

```bash
.venv/bin/python -m scripts.runtime_backup backup --runtime .runtime --output backups/$(date +%Y%m%dT%H%M%S)
```

- 备份使用 SQLite 在线备份 API 和 SHA256 清单。
- 恢复只写到新目录，核对后再切换；未完成任务进入人工复核，不自动重放。
- 生产环境需要加密异地副本，并定期演练 RTO/RPO。

## 证书续期

```bash
.venv/bin/python -m scripts.renew_credentials --runtime .runtime
```

续期前检查在职状态；离职/撤权身份不得续期。生产环境使用企业 CA/OCSP，并监控到期时间。

## 维护与回滚

- 维护开始前排空任务、等待在途结果落盘，再停服务。
- 升级前备份 `.runtime*` 和当前版本信息；回滚时恢复上一份运行目录。
- 发布前运行 `scripts.check_public` 与 `scripts.verify_release --gate`。
- 现场变更（地址、证书、ACL）要记录时间和操作人，不要只依赖聊天记录。

## 故障排查

| 现象 | 优先检查 |
|---|---|
| `TIMEOUT` | NATS 地址、防火墙、反向转发、Router 日志 |
| `FORBIDDEN` | 项目范围、目标身份、授权是否撤销 |
| 目录离线 | 心跳、证书时间、NATS subject ACL |
| 健康检查失败 | `latest.json` 的具体检查项，不要直接改状态文件 |
| 客户端升级失败 | 运行中任务/未确认结果、签名、安装目录权限 |

## 生产前必须补齐

真实 IdP、物理故障域、容量/长稳、WORM/异地 DR、值班告警、员工试点与三方签字。详见 [剩余工作](REMAINING_WORK.md)。
