# 测试与发布门禁

## 测试分层

| 层 | 命令 | 范围 |
|---|---|---|
| 离线单元/契约 | `python -m unittest discover -s tests -p 'test_*.py'` | 1036 项；不依赖外部模型 |
| 真实中间件 | `python tests/integration.py` | 启动独立 NATS/Router/Agent，验证认证、越权、超时和恢复 |
| Windows | GitHub `windows-client.yml` | 客户端安装、升级、回滚、SCM、失败路径 |
| 容量/长稳 | `python tests/load_capacity.py` | 受控样本，不等同生产验收 |
| 灾备/HA | `scripts/dr_drill.py`、HA 演练脚本 | 单机/隔离环境样本 |

离线回归需要 `.runtime/bin/nats-server`。全新 clone 可先运行 `prepare_server.py`；CI 会下载固定版 NATS 2.11.9 并校验 SHA256。

## CI

两个工作流在每次 push/PR 运行：

- **Offline checks**：依赖安装、公开源检查、1036 项回归、发布元数据门禁。
- **Windows offline client**：Windows 客户端测试、SCM 生命周期、离线安装包构建。

生产准入是人工触发的独立门禁，不执行部署。

## 发布校验

```bash
# 本机已准备运行时：提交、内容摘要、制品和公开源卫生一起校验
python -m scripts.verify_release --strict

# 全新 clone / CI：允许暂缓缺失 NATS 二进制，但不放宽源码与清单
python -m scripts.verify_release --gate
```

发布清单记录每个源码文件的 SHA256，不能只靠 Git 提交号。`check_public` 会阻断运行时目录、私钥和凭据进入发布集。

## 写测试的约定

- 测试必须显式使用临时运行目录，不读写开发者本机 `.runtime`。
- 不依赖真实内网地址、个人路径或外部模型；模型一律用 stub。
- 失败要保留明确错误码，不用“跳过”冒充通过。
- 新增源码后更新 `release/RELEASE.json` 的内容摘要。

## 结果解释

测试通过只说明当前代码在该环境的行为符合契约，不等于企业生产就绪。现场验收、容量、真实 IdP/模型和签字见 [剩余工作](REMAINING_WORK.md)。
