# Codex-QQ-AstrBotBridge

把本机已登录 ChatGPT 的 Codex CLI 接入 QQ 私聊和群聊的 AstrBot 插件，适用于同一台 Windows 电脑上的本地部署。

```text
QQ 普通账号 ⇄ NapCat ⇄ AstrBot / OneBot v11 ⇄ 本机 Codex CLI
```

私聊直接发文字，指定群内任何成员 @机器人即可聊天；`/new` 显式开启自己的新会话。插件不要求填写 OpenAI API Key，也不需要为它创建 AstrBot 模型提供商。

## 支持范围

| 项目 | 当前行为 |
| --- | --- |
| 私聊 | 只响应配置的个人 QQ 账号 |
| 群聊 | 只响应配置的一个群，任何成员必须实际 @机器人 |
| 连续对话 | 按平台、聊天和发送者分别保存，私聊与群聊互不串联 |
| 新会话 | 私聊 `/new`；群聊 `@机器人 /new` |
| 输入输出 | 文本；不处理图片、语音或附件，不执行本机命令 |
| 并发 | 全插件一次处理一个请求；忙时提示稍后重发 |
| 平台 | Windows 原生运行，AstrBot 的 OneBot v11（`aiocqhttp`）适配器 |

机器人显示为登录 NapCat 的 QQ 账号，头像、昵称和群内身份由该账号决定。账号需通过正常流程加入目标群；此链路不使用 QQ 官方机器人的 Webhook。

## 准备环境

- AstrBot `>=4.29.1,<5`，安装在 Windows 本机。
- NapCat 已登录用作机器人的 QQ 账号。
- 同一个 Windows 用户下安装 Codex CLI，并完成 **Sign in with ChatGPT** 登录。
- 已验证组合：Windows、Python 3.12、AstrBot 4.29.1、Codex CLI 0.162.0。其他 CLI 版本需确认支持本文使用的参数和功能开关。

如果已经安装 Node.js，可以在 PowerShell 安装并检查 Codex：

```powershell
npm install -g @openai/codex
codex login
codex login status
codex --version
```

`login status` 应显示 `Logged in using ChatGPT`。本插件拒绝 API Key 登录模式，使用已登录账号的 Codex 额度，受账号权限和用量限制影响。ChatGPT 订阅不是通用的 API Key；本插件调用官方 CLI，不提取、转换或转发登录令牌。参见 [Codex 安装与登录说明](https://github.com/openai/codex#readme)。

AstrBot、NapCat 和 Codex 必须能在同一台电脑上正常运行。当前版本不能直接把 Windows 宿主机的 `codex.exe` 路径填进 Linux 或 Docker 里的 AstrBot。

## 1. 连接 NapCat 与 AstrBot

以下地址适用于两者运行在同一台 Windows 电脑的情况。

1. 在 AstrBot 的“机器人”页面创建 **OneBot v11** 实例并启用。
2. 反向 WebSocket 主机填 `127.0.0.1`，端口填 `6199`，设置自己的 Token。
3. 在 NapCat 的网络配置中新增并启用 **WebSocket 客户端**。
4. URL 填 `ws://127.0.0.1:6199/ws`，Token 与 AstrBot 完全一致；消息格式选 `array`，保存配置。
5. 用个人 QQ 私聊机器人发送 `/help`，先确认 AstrBot 能正常回复。

更多说明见 [AstrBot OneBot v11 接入文档](https://docs.astrbot.app/platform/aiocqhttp.html) 和 [NapCat 项目](https://github.com/NapNeko/NapCatQQ)。

## 2. 安装插件

在 AstrBot 的插件页面选择通过仓库链接安装，填入：

```text
https://github.com/Quin520/Codex-QQ-AstrBotBridge
```

也可以在 GitHub 点击 **Code → Download ZIP**，然后在 AstrBot 中通过本地文件安装。仓库根目录含 `metadata.yaml`、`main.py` 和 `_conf_schema.json`，可直接作为插件安装。

插件名称为“本机 Codex 桥接”，内部名称为 `astrbot_plugin_local_codex`。如果此前用本地 ZIP 安装过，请更新同名插件，避免手工复制出多个实例。

## 3. 填写插件配置

公开版不预填任何个人 QQ、群号或电脑路径。安装后打开插件配置：

| 配置项 | 如何填写 |
| --- | --- |
| `owner_qq` / 你的 QQ 号 | 填个人 QQ 号，**不是机器人 QQ 号**；必填，留空时所有消息都不处理 |
| `group_qq` / 允许使用的 QQ 群号 | 填一个目标群号；留空则只允许个人私聊，不支持“所有群”或多个群号 |
| `codex_exe` / 本机 Codex 程序路径 | 原生 `codex.exe` 的完整绝对路径，不是 `codex.cmd`、`codex.ps1` 或目录 |
| `model` / Codex 模型 | 建议先留空，使用 CLI 内置默认模型；手动填写时使用当前账号可用的模型 ID |
| `timeout_seconds` / 超时 | 默认 180 秒，范围 30–600 秒 |

### 查找原生 codex.exe

如果通过独立安装包安装，在 PowerShell 尝试：

```powershell
Get-Command codex.exe -ErrorAction SilentlyContinue |
    Select-Object -ExpandProperty Source
```

如果通过 npm 全局安装，用下面的命令查找：

```powershell
$codexPackage = Join-Path ((npm root -g).Trim()) '@openai\codex'
Get-ChildItem -LiteralPath $codexPackage -Filter codex.exe -Recurse |
    Select-Object -ExpandProperty FullName
```

把输出中与你的 Windows 架构匹配的原生 `.exe` 路径填到插件里；通常位于带 `win32-x64` 或 `win32-arm64` 的目录下。若没有结果，先完成 Codex 安装。

保存配置并重载插件。模型名称以当前账号在 Codex 中能使用的模型为准；不要填写“Pro”“ChatGPT”或套餐名称。插件使用 `--ignore-user-config`，不会读取个人 `config.toml` 中的模型和工具配置。

## 4. 开始聊天

| 场景 | 发出的消息 | 效果 |
| --- | --- | --- |
| 个人私聊 | `你好，帮我解释一下递归` | 直接开始或继续对话 |
| 指定群聊 | `@机器人 帮我解释一下递归` | 该成员自己的连续对话 |
| 私聊重开 | `/new` | 下次提问使用新的 Codex 会话 |
| 群内重开 | `@机器人 /new` | 只重开当前成员在当前群的会话 |
| 兼容旧用法 | `/codex 你的问题`、`/codex_reset` | 仍可使用；群内同样要 @机器人 |

群内必须在 QQ 中实际选择 @该账号；手打昵称、普通群消息、@全体成员均不会触发。`/help` 等已注册命令仍交给对应插件处理，以 `/` 开头的未知命令不会作为普通聊天输入。

### `/new` 与 AstrBot 内置命令

AstrBot 自带一个 `/new`。为避免两个命令同时触发、以及普通群成员遇到内置命令权限检查，本插件加载时通过 AstrBot 命令管理接口将内置命令改名为 `/astrbot_new`，把 `/new` 留给 Codex。

这是一次持久化的命令配置变更，影响该 AstrBot 实例。原命令配置会保存在插件数据目录的 `builtin-new-command.json`。卸载插件后如果要恢复，请在 AstrBot 的命令管理页面把内置命令改回 `new`。

`/new` 只移除当前聊天中自己的会话映射，不删除 Codex 历史文件，也不清空其他成员的上下文。重启插件会保留会话映射；插件不使用 `resume --last`，不会主动接续桌面上其他 Codex 对话。

## 数据与隐私

仓库只包含源码、空配置模板、文档和使用虚构身份的测试，不需要上传真实配置、聊天记录或登录文件。

运行时数据主要位于 AstrBot 数据目录中：

| 相对路径 | 内容 |
| --- | --- |
| `data/config/astrbot_plugin_local_codex_config.json` | 本机路径、个人 QQ、群号等配置 |
| `data/plugin_data/astrbot_plugin_local_codex/sessions.json` | 聊天与发送者到 Codex 会话 ID 的映射 |
| 同目录的 `last-run.jsonl`、`last-error.log` | 最近一次已结束调用的输出与错误诊断，可能包含回复内容或本机信息 |
| 同目录的 `builtin-new-command.json` | 修改前的 AstrBot 内置命令配置 |
| 同目录的 `workspace/` | 插件专用工作目录 |

Codex 自身也会保存登录和会话数据。QQ 中提交的文字会传给已登录的 Codex 服务，回复会回到对应私聊或群聊。插件不自动接收整段群历史，群聊回复对群成员可见。

调用使用只读沙箱、独立工作目录、标准输入传递提示词，并关闭本地命令执行、应用、浏览器、电脑操作、图片读取、插件和多智能体等工具。这些限制依赖 Codex CLI 的实现，不等同于操作系统级完全隔离。

`.gitignore` 已排除常见配置、日志、数据库、登录文件和运行目录。反馈问题时只提供脱敏后的错误片段；不要上传完整 AstrBot 数据目录、Codex 登录文件或真实聊天日志。

## 常见问题

| 现象 | 检查顺序 |
| --- | --- |
| `/help` 也不回复 | 确认 QQ 在线、NapCat WebSocket **客户端**已启用、两端地址和 Token 一致；查看 AstrBot 是否显示 OneBot 已连接及是否限制了消息唤醒 |
| `/help` 正常，聊天不回复 | 确认插件已启用并重载，`owner_qq` 已填，私聊来自该账号，群号正确且实际 @了机器人 |
| `/new` 提示管理员权限或触发了两个回复 | 查看插件加载日志及命令管理，确认内置命令已改为 `/astrbot_new`，检查其他插件是否也注册了 `new` |
| 提示登录失败 | 在运行 AstrBot 的同一 Windows 用户下执行 `codex login`，选择 ChatGPT 登录，再检查 `codex login status` |
| 程序路径错误 | 填原生 `.exe` 的完整路径；Codex 更新后重新确认路径 |
| 模型不可用 | 先将 `model` 留空；指定模型时确认当前账号拥有该模型权限 |
| 提示未知参数或功能开关 | 核对 CLI 版本；本版在 0.162.0 验证过，不要通过关闭沙箱或启用执行工具解决 |
| 提示忙或超时 | 等上一条完成后重发；按需增加超时秒数。超时会停止本插件启动的子进程 |
| 额度不足、限流或请求失败 | 查看本地错误日志中的实际原因；订阅用量和服务限制仍然适用 |
| 上下文异常 | 发送 `/new`，再重新提问 |

本插件直接调用本机 Codex，不经过 AstrBot 的普通模型提供商流程，因此 AstrBot 人格、知识库和其他 LLM 工具不会自动加入这条对话链路。电脑、AstrBot 和 NapCat 需要持续运行才能回复。

## 开发与验证

使用已经安装 AstrBot 依赖的 Python 3.12 环境，在本仓库根目录执行。若 AstrBot 源码未安装到 Python 路径中，先将其源码根目录加入 `PYTHONPATH`：

```powershell
$env:PYTHONPATH = 'D:\Apps\AstrBot'
python -m unittest tests.test_bridge -v
```

将示例路径替换为自己的 AstrBot 源码位置，并确认 `python` 是该 AstrBot 环境的解释器。测试将 AstrBot 数据根目录指向临时目录，使用虚构 QQ 身份，不调用真实 Codex、不发送 QQ 消息、不修改运行中的配置。

13 项回归测试覆盖访问范围、实际 @判断、连续对话隔离、当前用户重开、忙时重置、其他命令保留、内置命令迁移、AstrBot 实际唤醒阶段、子进程参数与标准输入、登录检查、超时清理和输出完整性。

可选的真实 Codex 验证会发起三次模型请求，使用当前账号额度，但不会发送 QQ 消息：

```powershell
python -m tests.smoke --codex-exe 'D:\Apps\Codex\codex.exe'
```

可追加 `--model 模型ID`。它验证首次回复、第二次记住前文、`/new` 后获得不同会话；插件测试日志位于临时目录并在结束后清理，Codex 自身的历史仍按 CLI 的规则保存。

本地测试通过不代表 QQ 收发链路已验收。安装后请分别验证个人私聊、普通群成员 @机器人，以及 `@机器人 /new`。
