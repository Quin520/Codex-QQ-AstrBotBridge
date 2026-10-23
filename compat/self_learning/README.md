# 自学习插件个人设置审批适配

适用于本机 Codex 桥接 **v0.3.6** 与 [NickCharlie/astrbot_plugin_self_learning](https://github.com/NickCharlie/astrbot_plugin_self_learning) **4.3.1** 的所附基线。此目录只含兼容补丁和说明，不含用户配置或聊天数据。

`changes.patch` 修改消息采集后的通知、人格审查服务及审查卡片，增加前端交互回归测试。通过活动插件注册表调用桥接，未安装桥接时原有功能保持可用。个人字段不会交给全局人格更新器处理。`baseline.json` 记录涉及源文件应用前后的 SHA-256；如果文件已更新或被修改，应先重新适配，不能强行覆盖。

补丁中的人格/个人设置审批接口在免密模式下只允许本机回环地址访问。启用自学习 WebUI 密码后，远程访问必须通过现有登录认证。不要用没有额外认证的反向代理公开免密服务。

## 安装

1. 停止 AstrBot，备份自学习插件目录及两个插件的配置。
2. 在自学习插件目录执行（路径按安装位置替换）：

   ```powershell
   git -c core.autocrlf=false apply --check 'D:\Plugins\astrbot_plugin_local_codex\compat\self_learning\changes.patch'
   git -c core.autocrlf=false apply 'D:\Plugins\astrbot_plugin_local_codex\compat\self_learning\changes.patch'
   ```

   `--check` 不通过时不要继续。应用后必须编译前端，否则页面仍是旧版。

3. 使用已有 Node.js，在自学习插件的 `web_src` 目录执行：

   ```powershell
   npm exec --yes --package=pnpm@10.20.0 -- pnpm install --frozen-lockfile
   npm exec --yes --package=pnpm@10.20.0 -- pnpm typecheck
   npm exec --yes --package=pnpm@10.20.0 -- pnpm test
   npm exec --yes --package=pnpm@10.20.0 -- pnpm build
   ```

   构建产物位于插件的 `web_res/static/dashboard`。

4. 启动 AstrBot，开启桥接的“从自学习插件生成个人设置审批建议”。自学习插件需启用消息采集，采集范围包括目标群，筛选模型可使用 `local_codex`。
5. 在自学习管理页的“审查队列 → 人格与个人设置”检查建议。没有充分新证据时没有建议是正常情况；功能不会为所有成员强行生成关系评价。

回退时恢复备份的源码和前端产物，关闭桥接的个人学习开关。不要把数据目录或审批备份提交到 Git。插件市场更新可能覆盖补丁，更新后重新检查兼容性。

## v0.3.14 对话上下文增量补丁

`dialogue-context.patch` 是独立增量，基于上述审批兼容已安装的自学习 **4.3.1**，用于桥接 **v0.3.14**；`dialogue-context-baseline.json` 记录这次五个文件应用前后的 SHA-256。原 `changes.patch` 与 `baseline.json` 保持原样。

- **首次安装**：先按上节应用 `changes.patch` 并构建前端，再应用本增量，最后启动 AstrBot。
- **已经安装旧审批兼容补丁**：只追加本增量，不重复应用 `changes.patch`，也无需重新构建前端。
- 应用前核对增量的 `before` 哈希并备份。若不匹配，先重新适配，不能强行覆盖。

在停止 AstrBot 后，于自学习插件目录执行：

```powershell
git -c core.autocrlf=false apply --check 'D:\Plugins\astrbot_plugin_local_codex\compat\self_learning\dialogue-context.patch'
git -c core.autocrlf=false apply 'D:\Plugins\astrbot_plugin_local_codex\compat\self_learning\dialogue-context.patch'
```

本增量解决两个对话问题：

- 主动消息插件用最近发言者作为群事件的兼容占位信息。以前自学习会据此把该成员的好感、关系、行为指导和个人目标注入面向全群的发言。现在只识别已加载的主动插件真实事件类，主动群消息保留群级心理状态、表达风格及共享上下文，普通群聊和主动私聊仍按实际对象处理。
- 当真实活动的 Codex 桥开启 `natural_chat_enabled` 时，自学习在桥授权的 QQ 群和主人私聊中保留最近回复避重，停止随机推荐另一种人格风格、详细解释、引导式提问或反问语气。开关关闭、桥未启用或会话不在桥授权范围时保持原行为。多样性缓存按这些选项隔离，切换后不会复用相反模式的旧内容。后台学习和 JSON 审批分析的模型提示词不变。

该增量不改变人格文本、好感数据库、个人审批规则或主动消息频率。部署后由管理员重启 AstrBot，NapCat 可以保持运行。

使用 AstrBot 的 Python 环境，在自学习插件目录验证：

```powershell
$env:PYTHONPATH = 'D:\AstrBot\core'
python -m unittest discover -s tests -p test_bridge_proactive_social.py -v
python -m unittest discover -s tests -p test_bridge_natural_style.py -v
```

测试需要同级安装的主动消息插件和本机 Codex 桥，也可用 `PROACTIVE_PLUGIN_DIR`、`CODEX_BRIDGE_PLUGIN_DIR` 指定目录。两组共 14 项测试使用真实事件类、实际上下文处理代码、虚构成员和隔离目录，覆盖群身份、授权范围、插件开关、缓存切换及后台 JSON 学习；不调用真实模型、不读取真实聊天、不发送 QQ 消息。新增兼容源码和测试随 `dialogue-context.patch` 完整提供，同样遵循下述 AGPL-3.0 许可。

## v0.3.18 全部学习审批增量补丁

`approval-gate.patch` 基于以上两个补丁均已应用的自学习 **4.3.1**。应用前核对 `approval-gate-baseline.json`，备份源码、前端、配置和 `data/plugin_data/astrbot_plugin_self_learning/messages.db`；备份 SQLite 数据库时应停止 AstrBot 或使用 SQLite 的在线备份接口，不能直接复制正在写入的主文件。

在停止 AstrBot 后，于自学习插件目录执行：

```powershell
git -c core.autocrlf=false apply --check 'D:\Plugins\astrbot_plugin_local_codex\compat\self_learning\approval-gate.patch'
git -c core.autocrlf=false apply 'D:\Plugins\astrbot_plugin_local_codex\compat\self_learning\approval-gate.patch'
```

随后按上节重新构建 `web_src`，在自学习配置的高级设置中开启“学习变化全部先审批”，再启动 AstrBot。公开默认关闭，原始消息采集和临时群上下文仍正常工作。

- **好感度**：每次非零变化先生成带有聊天依据、旧值和建议值的审批项。批准才更新数值并记录历史；拒绝不改变数值。重复点击不会重复应用，旧值或上限变更会阻止过期建议，也不会自动扣减其他成员。旧版缺失的变化历史无法补造。
- **表达和人格**：学习批次、表达提炼、记住表达命令、临时风格及情绪引起的人格修改均先入队。表达批准后进入对应群、人格和成员的表达库，不追加到全局人格的示范对话中。
- **黑话**：模型推断和联网补充只生成候选，不因重复出现或推断成功自动变成可用黑话；已有人工审查入口保持可用。
- **旧数据**：首次开启以事务把没有匹配批准记录的旧表达移回待审队列，每组最多十条，保留全部候选和已有审核结果。现有好感度与已审核黑话保留。若旧表达存在生成历史外键引用，会明确报错并回滚，需按实际数据库适配迁移，不能删除历史强行启动。
- **管理页面**：审查队列增加审批状态、好感度待审及处理历史、当前数值和实际变化明细，表达和人格列表不再仅取前几项。批次状态会正确显示完成或失败；学习的时间、消息数及证据门槛仍有效。

现有桥接成员设置审批继续使用原流程。LivingMemory 的事实记忆、原始聊天和临时上下文不属于此开关；不要把“全部学习审批”理解为所有插件数据库写入都需要审批。管理页免密时，审批操作和好感度依据仅允许本机回环访问；启用密码后沿用现有登录认证。

验证使用隔离 SQLite、真实审批 API 和虚构聊天，覆盖批准、拒绝、重复操作、过期建议、事务回滚、旧数据迁移及作用域保留；前端验证批准确认与取消操作。测试不发送 QQ 消息。回退应在停止 AstrBot 后恢复同一时点的源码、构建产物、配置及数据库备份，避免只回退源码却留下不匹配的数据状态。

## 许可与来源

补丁中的自学习插件衍生部分遵循上游 AGPL-3.0，完整协议见 [AGPL-3.0.txt](AGPL-3.0.txt)。上游作者和版权声明保留。此兼容修改由 Quin520 的 Codex-QQ-AstrBotBridge 提供；其源码就是本目录补丁以及桥接的 `profile_learning.py` / `profile_reviews.py`。未重新发布上游完整包或二进制依赖。
