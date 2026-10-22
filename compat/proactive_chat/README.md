# 主动消息 1.2.6 兼容补丁

用于 [Pancakes-Labs/astrbot_plugin_proactive_chat](https://github.com/Pancakes-Labs/astrbot_plugin_proactive_chat) **1.2.6** 的所附源码基线，已在 AstrBot **4.29.1** 验证。桥接从 v0.3.7 附带此补丁，安装桥接不会自动改写其他插件。

## 修复内容

- OneBot 开启群成员会话隔离后，`成员_群号` 无法匹配主动插件的群配置。补丁仅把主动调度的 QQ 群标识归一为群号，合并历史计时状态并保留最近发言者；不修改收到的事件或普通聊天的独立上下文。
- 新版 AstrBot 把平台消息记录在完整 UMO 下。主动插件原先查询裸群号，无法读到成员消息。现在仅聚合同平台、同群最近流水，排除私聊、别群及无法区分来源的旧裸 ID；主动回复也写回完整 UMO。
- 主动请求在钩子前标记为插件来源，让表情包等插件使用正确的作用范围。
- 钩子注入的工具由 AstrBot 原有执行器和工具循环执行，保留请求的模型与临时上下文；有调用次数上限，没有最终回复时明确报错。只执行请求中已经通过桥接过滤的工具，不追加全局工具。
- v0.3.9 支持工具直接发送语音等媒体后结束回复：核实投递成功，再完成历史记录、计时与计数，不重复发文字或再次执行 TTS。没有实际投递、发送失败仍会报错。

## 安装与配置

1. 停止 AstrBot，备份主动插件源码、配置及数据。
2. 核对 `baseline.json` 的 `before` SHA-256；版本或源文件已变化时先重新适配。
3. 在主动插件目录执行（替换补丁路径）：

   ```powershell
   git -c core.autocrlf=false apply --check 'D:\Plugins\astrbot_plugin_local_codex\compat\proactive_chat\changes.patch'
   git -c core.autocrlf=false apply 'D:\Plugins\astrbot_plugin_local_codex\compat\proactive_chat\changes.patch'
   ```

4. 如果要在主动消息中自动配图，表情包管理器的 `generation.trigger.scope` 设为 `chat_and_plugin_llm`，图包需有图片。使用桥接的按需 `send_voice` 时，关闭主动插件好友和群聊的“启用 TTS”，避免把每条主动文字都转换为语音；语音提供商在桥接中单独指定。
5. 启动 AstrBot。继续通过插件启用/停用控制主动消息；保持目标群与桥接一致。群沉默时长结束后还会等待配置的随机间隔，免打扰和无人回应限制仍生效。

此文件是基于原版 1.2.6 的累计补丁；已安装旧补丁时，先恢复原版源码或逐文件核对哈希后合并，不要直接叠加。回退时恢复源码和数据备份。市场更新可能覆盖补丁，更新后重新核对基线。此补丁和测试没有真实 QQ、群号、昵称、聊天记录或登录数据。

## v0.3.15：随机间隔模式

`interval-scheduling.patch` 是在上述累计补丁基础上的独立增量。先确认已经安装累计补丁，并核对 `interval-scheduling-baseline.json` 的 `before` 哈希；然后在主动插件目录执行：

```powershell
git -c core.autocrlf=false apply --check 'D:\Plugins\astrbot_plugin_local_codex\compat\proactive_chat\interval-scheduling.patch'
git -c core.autocrlf=false apply 'D:\Plugins\astrbot_plugin_local_codex\compat\proactive_chat\interval-scheduling.patch'
```

重启 AstrBot 并刷新管理页面，然后在主动插件的群聊配置中设置：

- **群聊主动发言触发方式**：`interval`（按随机间隔发言）。未设置时默认仍为 `idle`。
- **最小/最大触发间隔**：例如 45 / 60 分钟。启动时直接抽取一次间隔，成功发送后再抽取下一轮；群员新消息及机器人正常回复不会推迟当前任务。
- **按随机间隔发言的提示词**：可自定义。预设要求结合最近真实群聊、记忆和原人格，简短说一句，不假定群里冷场，不固定总结或反问。

此模式不叠加“自动触发等待”或“群聊沉默时间”；“启用自动主动消息功能”是 `idle` 模式的启动等待开关，`interval` 由群聊总开关和插件启停控制。全局与单个会话均支持新模式及提示词，单个会话的覆盖配置优先。更新后请刷新旧管理页面再保存配置。

免打扰时段到点会跳过发送并重排；连续无人回应达到上限后暂停，群员再发消息会清零计数并重新安排。生成期间有人聊天不会丢弃本轮，但插件停用、目标群关闭或进入免打扰后会阻止后续投递，包括工具语音和分段消息。发送失败不增加未回复次数，稍后重新安排。模型生成和语音合成需要时间，因此 45–60 分钟是触发间隔，不保证精确到秒收到消息。

定时与手动主动任务共用同群互斥，避免重复生成；普通 `@` 对话仍可正常并行。调度会持久化，重启保留有效任务；切换模式或改变间隔时会丢弃不匹配的旧任务。倒计时显示在普通任务卡，`interval` 不再创建群沉默计时卡。继续使用已配置的群上下文来源、人格、模型和工具，保留已有 TTS/文字发送设置。

## 验证

在主动插件目录设置 `ASTRBOT_CORE` 为 AstrBot 源码根目录，并使用它的 Python 环境：

```powershell
$env:ASTRBOT_CORE = 'D:\AstrBot\core'
$env:PYTHONPATH = $env:ASTRBOT_CORE
python -m unittest discover -s tests -p test_bridge_sessions.py -v
python tests/test_proactive_llm.py
python -m unittest discover -s tests -p 'test_*.py' -v
```

测试使用真实 AstrBot 事件、调度器、工具执行器和临时 SQLite，覆盖多成员共享计时、活动取消任务、停用清理、历史迁移、群范围隔离、工具多轮执行、直接媒体投递只发送一次及失败处理。模型为测试替身，定时任务不会实际发送 QQ 消息。

叠加随机间隔补丁后共 43 项：原兼容测试 17 项，间隔调度 13 项，发送控制 8 项，配置页面和会话覆盖 5 项。覆盖持续活跃、生成期插话、到期分发竞态、重启及模式切换、无人回应暂停恢复、免打扰和停用取消；配置页面测试通过 Node 执行真实 JavaScript 保存逻辑，需要本机 Node。

## 许可

此补丁内主动插件的衍生代码遵循上游 AGPL-3.0，完整协议见 [AGPL-3.0.txt](AGPL-3.0.txt)，保留上游作者与版权声明。兼容修改由 Quin520 的 Codex-QQ-AstrBotBridge 提供；`changes.patch` 及 `interval-scheduling.patch` 提供修改源码，各自对应的 baseline JSON 包含应用前后哈希。
