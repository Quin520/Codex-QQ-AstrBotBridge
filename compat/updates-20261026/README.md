# 2026-10-26 插件兼容更新

配合桥接 **v0.3.21**、AstrBot **4.29.1**、Windows / Python 3.12。所有启用插件的上游已拉取并与“原始版本、已安装适配、新版”三方比较。保留旧补丁作为历史记录；本目录是对应新版的**累计补丁**，不能再叠加旧累计补丁。

| 插件 | 本次采用版本 | 适配与处理 |
| --- | --- | --- |
| 我想听歌 → bili 播放器 | 0.2.5 | [累计补丁](astrbot_plugin_listen_music.patch)：新 `find_in_bilibili` / `deliver_media` 工具、翻唱音源借用、短链校验、HTTP 412 分类、Windows 凭据 ACL；现有旧目录安装保留 `astrbot_plugin_listen_music` 标识和账号目录 |
| 点歌 | 3.3.1 | [累计补丁](astrbot_plugin_music.patch)：迁移平台别名、25 MiB / 180 秒下载限制及取消清理；桥接识别 `netease.py` 与旧 `ncm.py`，继续提供网易云翻唱保底 |
| LivingMemory | 2.7.2 | [累计补丁](astrbot_plugin_livingmemory.patch)：保留本群共享记忆及人格过滤，合入 SQLite 写入恢复修复 |
| 链接解析 | 1.6.1 | [累计补丁](astrbot_plugin_parser.patch)：保留成功发送后的来源摘要和群上下文、受限页面预览等适配 |
| 自学习 | 4.4.2 | [累计补丁](astrbot_plugin_self_learning.patch)：保留个人设置、好感度与表达审批、自然表达及群身份适配；新版好感度排序、状态清理和情绪面板一并保留 |
| 主动消息 | 正式版 1.2.6 + 修复 | [累计补丁](astrbot_plugin_proactive_chat.patch)：保留随机间隔、停用取消、群共享上下文、受限工具和实际发送结果统计；回移固定 `no-auth` 令牌不能绕过密码，以及保存密码后立即生效的修复 |
| 表情包管理器 | 5.2.6 | 采用上游版本，保留原有图库、启用状态、收集范围和待审核队列 |
| 群分析 | 5.8.4 | 采用上游版本，保留桥接本群报告权限；含停止运行中分析等上游修复 |
| 戳一戳 / 群语音手记 | 2.1.1 / 0.2.1 | 上游无新版本，保留已有本地适配 |

主动消息主分支虽仍标记 1.2.6，实际已合并未发布的 1.3 架构重写。本次使用最近正式标签 `635fc18eeb84e3c7b58661425359244552286835` 加累计适配，未将现有调度流程整体替换。未启用的表情包小偷保持原状态。

## 学习审批与数据

“学习变化全部先审批”继续覆盖原有学习流程。新版三阶段遗忘会自动修改或删除已审核表达、黑话，尚未接入逐项审批队列；因此**审批模式下自动遗忘被明确禁止**，面板显示原因，人工逐项确认删除仍可用。新情绪面板并不代表所有插件事实记忆都进入学习审批队列，范围仍以 [自学习审批说明](../self_learning/README.md) 为准。

本次本地实例未自动启用新增的情绪学习闸门，避免升级后减少原有学习候选；可在新配置中自行开启。成员称呼、标签、关系、经历、群共享记忆、RVC 音色、插件开关和群名单沿用原配置。数据库只由上游原有迁移器添加新字段和统计表；升级前应做在线一致性备份。已在私有数据库副本验证原有表行内容不变，不分发数据库或聊天内容。

## 应用累计补丁

每份同名 `.baseline.json` 列出精确上游提交以及应用前后 SHA-256（UTF-8、LF）。先取得对应提交的原始源码，核对后备份并执行：

```powershell
git -c core.autocrlf=false apply --check 'D:\Plugins\astrbot_plugin_local_codex\compat\updates-20261026\插件目录名.patch'
git -c core.autocrlf=false apply 'D:\Plugins\astrbot_plugin_local_codex\compat\updates-20261026\插件目录名.patch'
```

上面的“插件目录名”需替换为表格所链接补丁的实际名字。补丁已经逐一从干净基线执行 `apply --check`、实际应用和结果哈希核对。已有改动应重新三方合并，不强制覆盖。采用 bili 播放器新目录安装时，桥接也识别 `astrbot_plugin_bili_player`；旧实例保留目录和标识，可继续读取原有凭据。

点歌 2.x 与 3.x 配置不兼容，必须先迁移。以下命令只写新的私有文件，不覆盖原文件；输出包含原配置中的密钥，禁止上传：

```powershell
python 'D:\Plugins\astrbot_plugin_local_codex\compat\music\migrate_config.py' --input 'D:\Private\music-old.json' --plugin 'D:\Plugins\astrbot_plugin_music' --output 'D:\Private\music-migrated.json'
```

迁移保留发送顺序、默认平台、代理、卡片密钥和自建音源地址；只启用原来的网易云、网易云 API、TXQQ 三个来源，新增平台保持关闭。未知旧默认平台会明确报错。确认输出后，用它替换原点歌配置。旧版听歌实例的新 `media_priority` 配置采用 `audio_first`，保持模糊“来一首”请求默认发音频。

自学习累计补丁含前端源码，应用后在 `web_src` 目录执行：

```powershell
npm exec --yes --package=pnpm@10.20.0 -- pnpm install --frozen-lockfile
npm exec --yes --package=pnpm@10.20.0 -- pnpm typecheck
npm exec --yes --package=pnpm@10.20.0 -- pnpm test
npm exec --yes --package=pnpm@10.20.0 -- pnpm build
```

构建结果写入 `web_res/static/dashboard`。修改完成后由管理员停止再启动 AstrBot；NapCat、普通 QQ 无需重启。

## 验证范围

已通过：听歌 164 项、点歌适配 6 项、链接解析 41 项、LivingMemory 1,165 项、主动消息 44 项、戳一戳 12 项、群分析 1,020 项、自学习重点审批与兼容回归 60 项、群语音手记 32 项、表情包管理器 1,069 项、桥接 298 项及配置迁移 2 项。自学习界面 71 项、类型检查和正式构建通过；群分析 Ruff、格式检查及 Pyright 通过。群分析和表情包上游测试共 7 项按其原有条件跳过，未计入通过数。

11 个启用插件均通过真实 AstrBot 包路径的隔离导入。桥接的音乐工具测试使用新版真实插件处理器与 AstrBot 工具执行器，替换发送端和音源；权限、当前群绑定、关闭插件撤销权限均保留。测试没有调用真实模型或向 QQ 发送消息；正式进程重启后的加载、外部音源可用性与 QQ 呈现仍需实测。

## 来源与许可

上游仓库： [bili 播放器](https://github.com/57Darling02/astrbot_plugin_bili_player)、[点歌](https://github.com/Zhalslar/astrbot_plugin_music)、[LivingMemory](https://github.com/lxfight-s-Astrbot-Plugins/astrbot_plugin_livingmemory)、[链接解析](https://github.com/Zhalslar/astrbot_plugin_parser)、[自学习](https://github.com/NickCharlie/astrbot_plugin_self_learning)、[主动消息](https://github.com/Pancakes-Labs/astrbot_plugin_proactive_chat)。精确 SHA 见各基线文件。

自学习、LivingMemory、主动消息的衍生部分遵循各自上游 AGPL-3.0，完整协议见 [自学习](../self_learning/AGPL-3.0.txt)、[LivingMemory](../livingmemory/AGPL-3.0.txt)、[主动消息](../proactive_chat/AGPL-3.0.txt)。点歌和解析补丁分别沿用 [MIT](../music/MIT.txt) 与 [解析许可](../parser/LICENSE)。听歌上游包未附许可证，仅提供改动差异、保留作者来源，不将其声明为本桥接许可。兼容修改由 Quin520 的 Codex-QQ-AstrBotBridge 提供，不重新分发用户数据或模型。
