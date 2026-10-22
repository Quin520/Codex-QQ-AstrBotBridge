# 主动消息 1.2.6 兼容补丁

用于 [Pancakes-Labs/astrbot_plugin_proactive_chat](https://github.com/Pancakes-Labs/astrbot_plugin_proactive_chat) **1.2.6** 的所附源码基线，已在 AstrBot **4.29.1** 验证。桥接从 v0.3.7 附带此补丁，安装桥接不会自动改写其他插件。

## 修复内容

- OneBot 开启群成员会话隔离后，`成员_群号` 无法匹配主动插件的群配置。补丁仅把主动调度的 QQ 群标识归一为群号，合并历史计时状态并保留最近发言者；不修改收到的事件或普通聊天的独立上下文。
- 新版 AstrBot 把平台消息记录在完整 UMO 下。主动插件原先查询裸群号，无法读到成员消息。现在仅聚合同平台、同群最近流水，排除私聊、别群及无法区分来源的旧裸 ID；主动回复也写回完整 UMO。
- 主动请求在钩子前标记为插件来源，让表情包等插件使用正确的作用范围。
- 钩子注入的工具由 AstrBot 原有执行器和工具循环执行，保留请求的模型与临时上下文；有调用次数上限，没有最终回复时明确报错。只执行请求中已经通过桥接过滤的工具，不追加全局工具。

## 安装与配置

1. 停止 AstrBot，备份主动插件源码、配置及数据。
2. 核对 `baseline.json` 的 `before` SHA-256；版本或源文件已变化时先重新适配。
3. 在主动插件目录执行（替换补丁路径）：

   ```powershell
   git -c core.autocrlf=false apply --check 'D:\Plugins\astrbot_plugin_local_codex\compat\proactive_chat\changes.patch'
   git -c core.autocrlf=false apply 'D:\Plugins\astrbot_plugin_local_codex\compat\proactive_chat\changes.patch'
   ```

4. 如果要在主动消息中自动配图，表情包管理器的 `generation.trigger.scope` 设为 `chat_and_plugin_llm`，图包需有图片。
5. 启动 AstrBot。继续通过插件启用/停用控制主动消息；保持目标群与桥接一致。群沉默时长结束后还会等待配置的随机间隔，免打扰和无人回应限制仍生效。

回退时恢复源码和数据备份。市场更新可能覆盖补丁，更新后重新核对基线。此补丁和测试没有真实 QQ、群号、昵称、聊天记录或登录数据。

## 验证

在主动插件目录设置 `ASTRBOT_CORE` 为 AstrBot 源码根目录，并使用它的 Python 环境：

```powershell
$env:ASTRBOT_CORE = 'D:\AstrBot\core'
$env:PYTHONPATH = $env:ASTRBOT_CORE
python -m unittest discover -s tests -p test_bridge_sessions.py -v
python tests/test_proactive_llm.py
```

测试使用真实 AstrBot 事件、调度器、工具执行器和临时 SQLite，覆盖多成员共享计时、活动取消任务、停用清理、历史迁移、群范围隔离、工具多轮执行及错误。模型为测试替身，定时任务不会实际发送 QQ 消息。

## 许可

此补丁内主动插件的衍生代码遵循上游 AGPL-3.0，完整协议见 [AGPL-3.0.txt](AGPL-3.0.txt)，保留上游作者与版权声明。兼容修改由 Quin520 的 Codex-QQ-AstrBotBridge 提供；`changes.patch` 为全部修改源码，`baseline.json` 包含应用前后哈希。
