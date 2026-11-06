企业智能体对话客户端 V41（macOS / Ubuntu / Windows）

需要已安装 Python 3.11 或以上；客户端和 nats-py 依赖随包提供，安装不访问公网、不需要 sudo。
管理员另行提供每人独立的个人接入包 .zip。此通用包不含任何身份私钥。

先使用管理员独立交付的可信验证器 package_trust.py 和固定发行者公钥验签：
python3 package_trust.py --package <客户端ZIP> --signature <signature.json> --trusted-key <publisher.json>
验证器和公钥不能来自尚未验证的 ZIP。验证后解压运行：
bash install.command --package <客户端ZIP> --signature <signature.json> --trusted-key <publisher.json>
按提示输入个人接入包路径，选择 codex、claude 或 both 安装技能。
也可：python3 install.py --package <客户端ZIP> --signature <signature.json> --trusted-key <publisher.json> --enrollment /路径/个人接入包.zip --skills codex
然后在新的智能体会话中说“通过 company-agent-chat 问 Mac 上的项目进展”。
先申请、管理员批准后才能对话；安装不自动批准，不自动启动远端模型。

默认安装目录：
Mac：~/Library/Application Support/CompanyAgentClient
Ubuntu：~/.local/share/CompanyAgentClient
Windows：%LOCALAPPDATA%\CompanyAgentClient
已存在安装或同名技能时停止，不覆盖。升级可使用 upgrade.py；默认预览，停止 worker 且无在途/未确认结果后提供 --signature <签名> --trusted-key <外部可信公钥> --apply --worker-stopped，先备份并保留凭据。技能仍需按新模板更新。
卸载只需删除上述本次安装目录及所选工具的 skills/company-agent-chat 目录。
Windows：解压公开 ZIP，在 company-agent-client 目录运行（替换两份 ZIP 的路径）：
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\install_windows.ps1 -PackageZip C:\Downloads\company-agent-client-v41.zip -Signature C:\Downloads\company-agent-client-v41.signature.json -TrustedKey C:\Trusted\publisher.json -EnrollmentZip C:\Downloads\enrollment.zip -PythonPath "C:\Program Files\Python312\python.exe"
Windows 不依赖 WSL，不内置 Python。
Windows 登录计划任务仅在提供 WorkerConfig 时建立；模型 CLI 及账户需设备所有者另行配置。
此版不是已签名 .pkg/.deb/.msi。
接入包是私人凭据，请单独安全交付，不能群发。证书过期须联系管理员重新签发。

V19 成员注册：先取得管理员的 member-invitation.json，运行 python3 register.py，填写姓名和设备名称。把生成的 registration.json 私下交给管理员。管理员审核后给你个人接入包，再运行安装器。没有普通成员网页账户，不向管理员提供模型账户密码。

首次加入且尚无个人接入包时：
python3 install.py --registration-only --skills codex
（Claude Code 使用 --skills claude，两者使用 both）
然后新会话说“使用 company-agent-register 注册成员”，提供邀请文件路径、姓名和设备名。

安装后可让 company-agent-chat 执行连接诊断（--doctor），区分配置、证书、连接、账户、目标和授权问题，不调用模型。

V35：Windows 可使用现有 DeepSeek Harness 0.2.0-rc.2 桌面运行时，无需另装 Node。
使用 dsh_desktop_adapter.py --home <独立绝对路径> --prepare 创建独立配置；之后在 worker.json 固定该命令（去掉 --prepare）。
只在本机复制桌面模型选择与凭据，不复制原聊天记录。适配器每次先检查版本和组合配置，禁用文件、Shell、Web、子智能体等工具，每个问题创建新会话。
详见仓库 docs/WINDOWS_CLIENT.md；本轮使用 deepseek-account / deepseek-flash 云端模型，V34 已完成 1800 秒真实模型 36/36；不代表真实业务质量或重启/休眠验收。

V35：新增 --project-progress <项目ID> --target <目标ID> 查询显式发布记录；设备 worker.json 的 projects 与 capabilities 须明确声明。
DeepSeek 输出的未执行 DSML/工具标记不会作为成功回答回传。

V35 资料：raw 按问题选择获批文件的标题/行号片段，回包提供 material_coverage（哈希、选中/遗漏、截断）；不递归扫描。
notes_mode=local 与 local_summary_model=qwen2.5:7b 可启用本机 Ollama 摘要；只输出带片段 ID 的文档声明，不上传本轮原文；模型/引用校验失败不退回 raw。默认不拉模型。
V35 历史：history_exports_enabled 默认关闭；只支持设备所有者明确导出的 company_harness_history_v1 JSONL，不扫描既有桌面聊天。详见 docs/WINDOWS_CLIENT.md 的 owner policy 示例。
worker_doctor.py 只读检查；工具状态未发布时保持未知。Mac 本轮冻结，不要求同步升级。

V36：升级/安装生成 installed-manifest.json；自查识别文件损坏和失效历史策略。运行摘要排除旧备份，截断片段提供独立哈希，空正文不表示完整审阅。

V38 企业查询：--status --target <目标ID> 获取平台当前工作元数据；--list 返回共同登记 project_ids。状态查询不调用模型，idle 不表示项目完成，桌面状态未接入时保持未知。
TLS 连接失败与连接后回包超时分别提示；刚重启的准备状态以 --list 为准，不重复提交已受理问题。

V40 Windows 原桌面实时状态：worker.json 的 native_work={"enabled":true,"app":"<Harness 安装绝对目录>","home":"<用户 .dsh 绝对目录>"} 显式开启。
--status 本机读取固定回环 session/list API，返回 native_work 的 idle/running、live_sessions/executing_count 与时间；接口不可用返回 unknown，绝不沿用旧 idle。
浏览器会话凭据只在本机读取、短时使用，不复制、不回传；不调用模型，不查询聊天正文、不回传标题/目录/会话 ID。后台 Shell 作业和计划任务不在本状态范围，progress=null。
适配器验证 Harness 0.2.0-rc.2；升级其他版本需重新验证接口契约。Mac 保持冻结。

V41：公开 ZIP 签名在离线环境验证，应用安装/升级必须使用外部预置可信公钥。本轮开发密钥不是企业签名身份。
Windows 支持 UserLogon 和显式 Unattended（AtStartup/Password/Limited）；由系统保存所有者凭据，整机重启/休眠仍须现场验收。configure_windows_worker.ps1 默认预览，应用前排空并停止正式 worker。
维护标记只作用于所选目标；Windows 本地 .runtime/maintenance 也使 worker 排空，不影响原桌面。
