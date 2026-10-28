# 好感度审批独立开关

适用基线：自学习 **4.4.2 + 本地适配**，已应用 [2026-10-26 累计适配](../updates-20261026/README.md)和 [2026-10-27 审批界面同步](../review-sync-20261027/README.md)。此目录提供增量补丁；桥接本体无需更换模型或修改权限。

## 行为与入口

| 入口 | 配置项 | 默认值 | 作用 |
| --- | --- | --- | --- |
| 自学习配置 → 高级设置 | 学习内容变化需审批 `require_learning_approval` | `false` | 控制表达、人格、黑话等学习内容；保留安装者当前值 |
| 自学习配置 → 好感度系统设置 | 好感度变化需审批 `require_affection_approval` | `false` | 关闭时好感度涨跌直接生效并保存变化明细；开启时生成待审建议 |

两个开关互相独立：学习内容必须审批时，好感度仍可自动变化；也可单独开启好感度审批。旧配置缺少新字段时按关闭处理，不继承旧的总审批开关。

AstrBot 插件配置和独立 WebUI 设置共用同一字段，保存后能在另一入口读取。两个审批页面分别显示当前开关状态。代码首次安装后需重启 AstrBot，以加载新字段和处理逻辑；之后可通过设置页面切换。

切换只影响后续互动。已有好感度待审建议保留，不自动批准、删除或追补到当前数值；原值已变化的建议仍按既有规则显示为失效。实际变化明细继续在独立 WebUI → 审查队列 → 当前好感度与实际变化记录中查看。此补丁不修改实际数据库，也不改变群共享上下文、个人设置审批或其他插件适配。

## 安装

1. 备份自学习源码、前端产物和配置。对照 `astrbot_plugin_self_learning.baseline.json` 校验基线；文件不同应三方合并。
2. 在自学习插件根目录执行检查和应用：

   ```powershell
   git -c core.autocrlf=false apply --check 'D:\Plugins\astrbot_plugin_local_codex\compat\affection-approval-20261028\astrbot_plugin_self_learning.patch'
   git -c core.autocrlf=false apply 'D:\Plugins\astrbot_plugin_local_codex\compat\affection-approval-20261028\astrbot_plugin_self_learning.patch'
   ```

3. 在自学习 `web_src` 目录执行类型检查、测试和构建：

   ```powershell
   npm exec --yes --package=pnpm@10.20.0 -- pnpm install --frozen-lockfile
   npm exec --yes --package=pnpm@10.20.0 -- pnpm typecheck
   npm exec --yes --package=pnpm@10.20.0 -- pnpm test
   npm exec --yes --package=pnpm@10.20.0 -- pnpm build
   ```

4. 停止后启动 AstrBot，保留 NapCat 运行；刷新页面，在“好感度系统设置”确认“好感度变化需审批”为关闭。原来的学习内容审批值保留。

回退时恢复源码、前端产物和配置备份。回退不会撤销已经发生的互动或实际好感度变化。

## 验证

- 后端 149 项检查通过，包含真实隔离 SQLite、配置落盘重载、HTTP 审批状态、旧队列保留与表达/人格审批回归；另验证两个开关的四种组合下分别上涨、下降，共 8 个组合。
- 前端 76 项检查通过，覆盖两类状态分别显示、旧后端缺少状态时不误报已关闭、保留审批证据及已有确认流程；类型检查和生产构建通过。
- 增量补丁在复制的基线上实际应用并校验 SHA-256。测试未改实际好感度、未处理真实审批、未向 QQ 发送消息；正式进程需重启后加载修复。

## 来源与隐私

基于 [自学习上游](https://github.com/NickCharlie/astrbot_plugin_self_learning)及上述本地适配，精确上游提交见基线文件。衍生修改遵循 [AGPL-3.0](../self_learning/AGPL-3.0.txt)。公开文件仅含源码增量、测试与文档，不含用户配置、QQ 号、密码、聊天记录或数据库。
