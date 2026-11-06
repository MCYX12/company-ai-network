# 部署指南

本文覆盖开发机、局域网和 Windows 终端的部署。所有示例地址均为文档保留地址，请替换成你的内网地址与证书。

## 前置条件

- Linux（推荐 Ubuntu 22.04+）或 macOS 开发机
- Python 3.11+
- [uv](https://docs.astral.sh/uv/)
- OpenSSL
- Docker（仅 `prepare_server.py` 提取固定 NATS 二进制时需要；也可以直接提供原生 `nats-server`）
- Windows 客户端需要 Python 3.11+ 和 PowerShell 5.1+

## 本机 PoC

```bash
git clone https://github.com/MCYX12/company-ai-network.git
cd company-ai-network
uv sync --frozen
.venv/bin/python scripts/bootstrap.py
.venv/bin/python scripts/prepare_server.py
.venv/bin/python scripts/run_local.py
```

`bootstrap.py` 生成 7 天有效的测试 CA、服务端/客户端证书和 `.runtime/nats.conf`；`prepare_server.py` 提取并校验 NATS 2.11.9；`run_local.py` 启动 NATS、Router 和测试 Agent `ag-b`。

另一个终端验证：

```bash
.venv/bin/python -m app.client --list
.venv/bin/python -m app.client --action get_status --target ag-b
```

控制台：

```bash
POC_RUNTIME=.runtime NATS_URL="$(cat .runtime/endpoint.txt)" \
  .venv/bin/python -m app.dashboard --identity ag-a --port 8787 --admin
```

打开 <http://127.0.0.1:8787>。

## 局域网部署

1. 准备 NATS 二进制和运行目录：

```bash
.venv/bin/python scripts/prepare_server.py
```

2. 使用站点 profile 生成 LAN 运行时（示例地址 `192.0.2.10`）：

```bash
.venv/bin/python scripts/render_site.py --site config/site.example.json \
  --runtime .runtime-lan --nats-conf .runtime-lan/nats.conf --listen 192.0.2.10:14222
.venv/bin/python scripts/prepare_lan.py --ip 192.0.2.10
```

3. 启动：

```bash
.venv/bin/python scripts/run_lan.py
```

4. 用 systemd 托管（模板在 `packaging/systemd/`）：

```bash
mkdir -p ~/.config/systemd/user
cp packaging/systemd/company-agent-stack.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now company-agent-stack.service
```

> 生产环境应把证书、私钥和运行目录放在所有者独占目录，不要提交到 Git，也不要把同一个测试 CA 分发给多个终端。

## Windows 终端

- 客户端安装包与 PowerShell 脚本在 `packaging/windows/`。
- 以安装所有者权限执行安装脚本，按提示提供服务器地址、证书和身份。
- 客户端只监听回环地址；业务仍经 mTLS/NATS。
- 客户端配置示例见 `config/site.example.json` 的 `agents` 部分。

## 跨网段 / 跨 Wi-Fi

当 Windows 无法直接路由到 NATS 地址时，可在受控 Linux 主机上使用 `scripts/lan_reverse_relay.py` 建立受限 SSH 反向转发：

- 使用专用标准账号，禁止命令会话和额外监听；
- 只转发 NATS 回环端口，保留 mTLS 校验；
- 用 systemd 用户服务托管，并设置自动重启；
- 地址/NAT 来源变化后重新核对白名单。

更完整的安装与回滚步骤见脚本内说明和 `packaging/systemd/company-agent-lan-relay.service`。

## 配置

| 文件 | 用途 |
|---|---|
| `config/site.example.json` | 站点、项目、Agent 和默认目标示例 |
| `config/registry.json` | 受控注册表；默认空 |
| `config/net_acceptance_plan.example.json` | 网络验收计划模板 |
| `.runtime/` 或 `.runtime-lan/` | 本机生成的证书、配置、账本和日志（已 gitignore） |

运行目录包含凭据，禁止提交、打包分发或跨终端复用。需要重建时先停服务并备份原目录。

## 排障

```bash
.venv/bin/python -m scripts.healthcheck_alert --identity ag-a --runtime .runtime
.venv/bin/python -m scripts.check_public
.venv/bin/python -m scripts.verify_release --gate
```

- NATS 启动失败：检查证书路径、SAN、时间和端口占用。
- 客户端 `FORBIDDEN`：检查项目范围、目标身份和授权是否仍有效。
- `TIMEOUT`：检查 NATS 地址、防火墙、反向转发和 Router 日志。
