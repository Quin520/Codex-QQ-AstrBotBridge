# 表情包小偷与表情包管理器协同补丁

针对 [astrbot_plugin_stealer](https://github.com/nagatoquin33/astrbot_plugin_stealer) 3.1.6，与已安装的 [meme_manager](https://github.com/anka-afk/astrbot_plugin_meme_manager) 5.2.3 实际回复钩子验证。基线及差异分别见 [baseline.json](baseline.json)、[changes.patch](changes.patch)。

## 修复行为

两个插件同时启用时，管理器先选中表情分类、稍后才构建图片消息，小偷可能在此间隙再安排自动发送。本补丁只调整小偷的自动发送协调：当前事件已有管理器待发图片、语义候选，或选中分类且概率门命中时，交给管理器发送；延迟发送前再次检查，并保留该轮标记，避免待发列表清理后重复发送。

管理器没选图、分类无效或概率未中时，小偷仍正常工作。新一轮清除协调标记；两个插件的概率、图库、采集设置不变。显式 `search_meme` → `send_meme` 仍可调用，桥接按插件归属和目标群权限开放这两项。`steal_meme` 接受原始 URL/文件引用，不直接对群成员开放。

## 安装与验证

备份 `main.py` 和 `core/events/meme_sender_engine.py`，按基线核对原文件哈希；`null` 表示新增测试文件，不应已存在。在小偷插件根目录执行：

```powershell
git -c core.autocrlf=false apply --check 'D:\Plugins\astrbot_plugin_local_codex\compat\stealer\changes.patch'
git -c core.autocrlf=false apply 'D:\Plugins\astrbot_plugin_local_codex\compat\stealer\changes.patch'
python -m unittest discover -s tests -p test_bridge_meme_coexistence.py -v
```

使用 AstrBot 的 Python。测试默认从插件安装位置推导 core，并加载相邻的 `meme_manager`；如果插件目录不在标准位置，设置 `ASTRBOT_CORE` 和 `MEME_MANAGER_PLUGIN` 为各自目录即可。所有数据写入临时目录，图片发送替换为本地记录，不发送 QQ 消息。

新增 8 项实际钩子测试通过：普通/流式回复只保留管理器一次图片发送；管理器未选、概率未中、无效分类仍保留小偷；延迟任务重查、跨轮清理和显式工具发送均通过。由管理员停止再启动 AstrBot 加载修复；NapCat 无需重启。

## 来源与许可

上游插件附 GNU AGPL v3；完整协议保留于 [AGPL-3.0.txt](AGPL-3.0.txt)。原作者 nagatoquin33 及上游归属保留。本兼容修改由 Quin520 的 Codex-QQ-AstrBotBridge 于 2026-10-22 提供，其衍生部分按 AGPL-3.0 发布；对应源码为本目录补丁及其中的测试。没有重新发布上游完整包、模型或二进制依赖。
