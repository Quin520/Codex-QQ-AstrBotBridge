# 链接解析结果进入群上下文

适用于链接解析插件 **1.5.7** 与本机 Codex 桥接 **v0.3.18**。`changes.patch` 仅修改发送完成后的记录回调；`baseline.json` 记录源文件应用前后的 SHA-256。文件不匹配时需重新适配，不能强行覆盖。

原解析插件直接调用发送接口，合并转发中的摘要不会经过桥接的常规回复记录入口。补丁在解析消息成功发送后，把标题、简介、文字和来源交给已启用桥接的 `observe_parsed_link`。桥接仅在公共上下文开启的目标群内接收，记录实际分享者、平台与去除查询参数的来源网址，并标记为外部材料。

不会读取旧聊天转发、下载额外视频或把简介当作完整影音摘要。其他群和私聊不进入目标群记忆；关闭任一插件或关闭公共上下文即可停止记录。发送失败不记录，成功的纯文本回退仍可记录，重复回调会去重。音乐链接请求的原有路由保持不变。

停止 AstrBot 并备份解析插件后，在解析插件目录执行：

```powershell
git -c core.autocrlf=false apply --check 'D:\Plugins\astrbot_plugin_local_codex\compat\parser\changes.patch'
git -c core.autocrlf=false apply 'D:\Plugins\astrbot_plugin_local_codex\compat\parser\changes.patch'
```

升级桥接并开启“共享目标群的近期公开聊天”，再启动 AstrBot。可以先分享新链接，解析发送成功后再 @机器人询问其标题或摘要。解析插件没有提供的信息仍可能答不出来；外部网站限制不由此补丁修复。

使用 AstrBot 的 Python 环境验证（纯假发送，不连接 QQ）：

```powershell
$env:PYTHONPATH = 'D:\AstrBot\core'
$env:PARSER_PLUGIN_DIR = 'D:\AstrBot\core\data\plugins\astrbot_plugin_parser'
python 'D:\Plugins\astrbot_plugin_local_codex\compat\parser\test_delivery.py'
```

衍生补丁沿用上游 MIT 许可，保留 Les Freire 的版权声明，见 [LICENSE](LICENSE)。本目录不包含用户配置、平台登录信息或聊天内容。
