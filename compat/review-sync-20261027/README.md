# 审批界面与配置同步修复

适用组合：桥接 **v0.3.24**、AstrBot **4.29.1**、自学习 **4.4.2**、主动消息 **1.2.6 + 本地适配**。此处提供两个增量补丁，基于 [2026-10-26 累计适配](../updates-20261026/README.md)，不是可直接覆盖任意上游版本的完整插件。所有原有角色、群上下文、审批、主动发言及工具权限适配保留。

## 两个入口分别显示什么

- **AstrBot → 插件配置**：开关、名单和学习参数，不是审批数据库的实时列表。
- **AstrBot → 自学习插件页面 → 审查队列**：内嵌审批入口，地址为 `#/plugin-view/astrbot_plugin_self_learning/dashboard`。
- **自学习独立 WebUI → 审查队列**：同一数据库的另一界面。

补丁为内嵌面板补齐好感度审批、成员设置原值/建议值/聊天依据，支持编辑成员建议后批准；页面按完整人格、表达待审列表显示。待审总数按来源去重，包含好感度，不把同时出现在人格聚合接口中的表达建议再计一次。黑话列表最多取 200 条，数量使用服务端总数。

好感度建议的原值不等于当前值、建议超出当前个人上限，或个人设置/关系预设与建议生成时的快照不同，都显示为**失效建议**，附具体原因，不计入有效待审。读取只派生显示状态，不改写原审批决定、不删除证据、不根据日期批量过期、不自动批准。批准时仍会再次校验最新状态与上限；群好感度总上限可能阻止本次批准。

内嵌页面每 60 秒及重新获得焦点时刷新；已选批量项目、正在编辑或打开确认窗口时暂停自动刷新，避免覆盖操作。独立 WebUI 原有每分钟刷新保留，审批后同步刷新总数。AstrBot 配置表单已经打开时，需要重新进入或刷新才能看到另一端保存的新参数。累计消息数等只读运行统计，以运行面板为准。

主动消息控制台仍使用 AstrBot 提供的同一个配置对象。增量修复使写盘失败返回 HTTP 500，并恢复内存原值，避免“保存成功”但重启丢失。**会话单独配置**覆盖全局默认属于既有功能，仍保留，不把两者强制合并。

## 应用

1. 备份两个插件源码、构建产物、配置和自学习数据库。此补丁不包含数据迁移。
2. 对照同名 `.baseline.json` 检查文件 SHA-256；不同基线应三方合并，不能强行覆盖。
3. 在各自插件根目录执行对应补丁，先检查再应用：

   ```powershell
   git -c core.autocrlf=false apply --check 'D:\Plugins\astrbot_plugin_local_codex\compat\review-sync-20261027\astrbot_plugin_self_learning.patch'
   git -c core.autocrlf=false apply 'D:\Plugins\astrbot_plugin_local_codex\compat\review-sync-20261027\astrbot_plugin_self_learning.patch'
   ```

   主动消息目录使用同目录的 `astrbot_plugin_proactive_chat.patch`。

4. 在自学习的 `web_src` 目录构建独立 WebUI：

   ```powershell
   npm exec --yes --package=pnpm@10.20.0 -- pnpm install --frozen-lockfile
   npm exec --yes --package=pnpm@10.20.0 -- pnpm typecheck
   npm exec --yes --package=pnpm@10.20.0 -- pnpm test
   npm exec --yes --package=pnpm@10.20.0 -- pnpm build
   ```

5. 管理员停止后启动 AstrBot，NapCat 无需重启；浏览器刷新页面。回退时恢复备份的源码与构建产物，审批数据不需要回滚。

## 验证范围

自学习后端重点回归 91 项、前端 74 项、桥接个人设置 16 项通过；前端类型检查与生产构建通过。新增真实隔离 SQLite + HTTP 测试覆盖两种审批入口互相可见、失效分类保留原记录及旧有效建议不按时间过期；内嵌 DOM 测试覆盖实际按钮、确认、外部审批刷新和草稿保护。主动消息配置与鉴权检查 8 项通过，包含真实 HTTP 保存成功/写盘失败分支。

另检查 LivingMemory 配置与插件页 106 项、表情包配置与采集审核 78 项、群分析配置和 WebUI 44 项、群语音手记审核和 WebUI 15 项，均通过。测试使用临时存储，不审批实际数据、不发 QQ 测试消息。正式运行进程需重启才能加载新 Python 代码；外部服务和 QQ 消息呈现不在这些测试保证范围内。

## 来源与许可

[自学习上游](https://github.com/NickCharlie/astrbot_plugin_self_learning) 与 [主动消息上游](https://github.com/Pancakes-Labs/astrbot_plugin_proactive_chat) 的精确提交见各基线文件；补丁基于上述累计适配。衍生修改继续遵循各自 AGPL-3.0，完整许可见 [自学习](../self_learning/AGPL-3.0.txt)、[主动消息](../proactive_chat/AGPL-3.0.txt)。不包含用户配置、账号凭据、聊天、审批证据或数据库。
