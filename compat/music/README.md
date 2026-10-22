# 点歌插件 v2.2.0 兼容修复

针对 [Zhalslar/astrbot_plugin_music](https://github.com/Zhalslar/astrbot_plugin_music) v2.2.0 的已安装源码基线，配合桥接 v0.3.14。文件及哈希见 [baseline.json](baseline.json)，差异见 [changes.patch](changes.patch)。不包含用户配置、歌曲缓存或账号资料。

## 修复内容

- 模型工具选择平台时，支持文档列出的已注册别名，例如“网易点歌”；任意 URL 或未注册名称不会成为播放器。
- 本地歌曲下载总超时 180 秒、最大 25 MiB，同时检查 Content-Length 和实际收到的字节；拒绝空文件、本地文件引用和带登录信息的 URL。
- 失败、超时或取消时删除不完整文件；修复 Windows 上异步打开文件被取消后遗留句柄的问题。

25 MiB 限制适用于本地下载。原插件的“语音链接”“文件链接”模式由平台适配器获取音频，不经过该下载器。歌曲、封面和歌词链接来自第三方音乐服务，可用性仍取决于上游。

桥接按权限开放 `play_song_by_name` 和 `query_lyrics_by_name`，支持普通自然语言点歌和按歌名请求歌词图片；它们只接受歌名与固定注册平台，发送到当前事件所在会话，不能指定文件路径或其他会话。普通点歌保留原插件命令；翻唱继续使用桥接 `find_cover_music` → `sing_cover`，保留来源验证、冷却和当前会话检查。

## 安装与验证

备份 `main.py`、`core/downloader.py`，对照基线的 `before` 哈希核对，`null` 表示新增文件，不应已存在。在点歌插件根目录执行：

```powershell
git -c core.autocrlf=false apply --check 'D:\Plugins\astrbot_plugin_local_codex\compat\music\changes.patch'
git -c core.autocrlf=false apply 'D:\Plugins\astrbot_plugin_local_codex\compat\music\changes.patch'
python -m unittest discover -s tests -p test_bridge_music_compat.py -v
```

使用 AstrBot 的 Python，并令 `PYTHONPATH` 包含 AstrBot core。新增 6 项隔离测试通过：实际本机 HTTP 下载成功、响应长度/分块超限、空文件、HTTP 错误、超时、取消清理、无效引用及平台别名。测试不访问真实音乐服务、不发送 QQ 消息。

桥接还提供 `tests/test_media_plugin_tools.py`，通过真实 `PermissionCheckedTool` 和 AstrBot 工具执行器调用已装插件的两个工具入口；用假播放器和发送端验证当前目标群、默认平台、注册别名、不支持的平台、其他群拒绝和关闭插件后撤销权限，共 6 项通过。第三方插件安装在非标准目录时可设置 `ASTRBOT_MUSIC_PLUGIN`；未安装时明确跳过，不把跳过当作互通成功。

由管理员停止再启动 AstrBot 加载修改，NapCat 无需重启。市场更新可能覆盖补丁；基线不一致时重新适配，不强行覆盖。

## 来源与许可

上游源码附 MIT License，Copyright (c) 2025 Zhalslar；完整协议保留于 [MIT.txt](MIT.txt)。本兼容修改由 Quin520 的 Codex-QQ-AstrBotBridge 于 2026-10-22 提供，源码为本目录补丁及其中的测试；衍生部分遵循 MIT，不重新分发上游完整包。
