# LivingMemory 2.7.1 兼容补丁

用于 [lxfight-s-Astrbot-Plugins/astrbot_plugin_livingmemory](https://github.com/lxfight-s-Astrbot-Plugins/astrbot_plugin_livingmemory) **2.7.1** 的所附源码基线，在 AstrBot **4.29.1** 验证。桥接从 v0.3.7 附带补丁，安装桥接不会自动改写 LivingMemory。

## 修复内容

OneBot 开启群成员会话隔离时，成员会话是 `平台实例:GroupMessage:成员_群号`。上游白名单无法用完整群 UMO 覆盖这些成员，使用裸群号又会匹配同号用户。

补丁为白名单增加从真实事件 `get_platform_id()`、`get_group_id()` 构造的规范群 UMO。管理员可以使用 `平台实例:GroupMessage:群号`，同时允许本群各成员，并排除同号私聊、其他群及其他平台实例。旧裸 ID 配置仍按原规则工作。

v0.3.8 累计补丁另增加 `memory_scope_mode=group`：同一平台实例、同一群的长期记忆共用规范群 UMO；私聊保持原会话，`isolated_sessions` 仍优先。摘要输入和来源证据保留真实发送者身份，群摘要模板要求区分昵称、QQ、自述和转述。个人对话与原始总结窗口不合并，近期跨成员接话由桥接的公共上下文提供。

## 安装与配置

1. 由管理员停止 AstrBot，备份插件源码、配置及数据。
2. 对照 `baseline.json` 的 `before` SHA-256 核对源码；若不同，先重新适配。
   此补丁累计包含 v0.3.7 修改，不能直接叠加到已打过旧补丁的文件；先按备份还原到匹配的上游基线，再应用累计补丁。
3. 在 LivingMemory 插件目录执行（替换补丁路径）：

   ```powershell
   git -c core.autocrlf=false apply --check 'D:\Plugins\astrbot_plugin_local_codex\compat\livingmemory\changes.patch'
   git -c core.autocrlf=false apply 'D:\Plugins\astrbot_plugin_local_codex\compat\livingmemory\changes.patch'
   ```

4. 在 LivingMemory 配置或其管理页面设置：

   - `provider_settings.llm_provider_id`：`local_codex`。
   - `provider_settings.embedding_provider_id`：独立的、已启用的 Embedding Provider ID。Codex 聊天提供商不提供向量嵌入；未配置时 LivingMemory 无法完成初始化。
   - `access_control.whitelist_enabled`：`true`。
   - `access_control.allowed_ids`：每行一个完整 UMO，目标群用 `平台实例:GroupMessage:群号`，主人私聊用 `平台实例:FriendMessage:主人QQ`。平台实例是 AstrBot 机器人配置 ID。
   - `filtering_settings.memory_scope_mode`：需要同群共享公开记忆时选择 `group`；需要每个成员完全独立时选择 `session`。`use_session_filtering` 和 `use_persona_filtering` 保持 `true`，不要使用 `global` 来实现群共享。

5. 由管理员启动 AstrBot。更新市场插件可能覆盖补丁，更新后重新核对基线。回退时恢复备份。

桥接只向目标群放行 `recall_long_term_memory`，由工具使用真实事件确定会话或群；不允许模型指定其他私聊或群作为记忆查询范围。写入、删除和其他记忆工具不在群白名单中。LivingMemory 自己的自动记忆整理仍按其配置工作，与桥接“成员设置修改审批”相互独立，不会自动改写称呼、关系或标签。

切换模式只影响新写入和召回的范围，旧长期记忆不会自动迁移。若需要迁移，应停止 AstrBot 并备份，使用上游作用域迁移工具指定已核实的每个精确来源 UMO，只合并同平台实例、同群的成员公开记忆，禁止无来源限制的全库迁移。原始消息和总结游标无需搬迁。已保存的自定义群摘要提示词可能覆盖本补丁的默认模板，需自行补充 QQ 身份归属规则。

## 验证

使用 AstrBot 的 Python 环境，把 `PYTHONPATH` 指向 AstrBot 源码根目录，在打补丁后的 LivingMemory 目录执行：

```powershell
$env:PYTHONPATH = 'D:\AstrBot\core'
python -m unittest discover -s tests -p test_bridge_memory_scope.py -v
```

测试使用虚构成员与真实 AstrBot 事件，覆盖多成员准入和同群共享、同号私聊/别群成员拒绝、平台实例隔离、主人私聊、显式独立会话、缺身份回退和原模式回归；不会读取聊天、初始化记忆库或发送 QQ 消息。桥接仓库的 `tests/test_memory_access.py` 还可通过 `LIVINGMEMORY_PLUGIN_DIR` 指向已打补丁的插件，验证真实回忆工具的执行权限。

## 许可

LivingMemory 原作者为 lxfight，本补丁及其衍生测试遵循上游 **AGPL-3.0**，完整许可见 [AGPL-3.0.txt](AGPL-3.0.txt)。兼容修改由 Quin520 的 Codex-QQ-AstrBotBridge 提供；`changes.patch` 含全部修改源码，`baseline.json` 包含应用前后哈希。包内没有真实用户配置、聊天记录、QQ 身份或凭据。
