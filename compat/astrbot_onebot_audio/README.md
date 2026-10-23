# AstrBot OneBot 语音发送补丁

适用于 [AstrBot](https://github.com/AstrBotDevs/AstrBot) 4.29.1 的已安装源码基线。文件哈希见 [baseline.json](baseline.json)，完整修改及新增测试见 [changes.patch](changes.patch)。本目录不包含真实歌曲、账号资料或聊天记录。

## 问题与修复

OneBot 发送器原本调用 `Record.convert_to_base64()`，该方法会先把音频转换为 WAV。一首约 4 分钟、48 kHz 双声道的 MP3 展开后，Base64 消息可能超过 50 MiB；本次验证的 NapCat WebSocket 客户端以此为上限，因此会断开连接并报告 `RangeError: Max payload size exceeded`。

补丁让 OneBot 发送器复用 `MediaResolver` 解析引用和清理临时文件，并按普通文件读取音频，保留原始字节再编码为 Base64。MP3 不再展开为 PCM，SILK 不再解码重编码；音频编解码由 OneBot 后端处理。已检查的 NapCat 实现会将非 SILK 音频转换为 QQ 语音。

本地路径、文件 URI、HTTP 链接、Base64 和 data URI 共用该处理方式；HTTP 链接没有文件后缀也会保留压缩内容。空音频明确报错，图片处理与其他平台的通用 Record 转换不变。不修改 NapCat 上限，也不会压缩或截短歌曲内容。

这项修改消除格式展开引起的体积增长，本身不是下载限额。原文件很大时，Base64 仍会增加约三分之一体积；应使用合适的压缩音频或文件发送方式。文件格式和 QQ 最终播放仍受协议端支持范围限制。

## 安装

这是 AstrBot 框架补丁；仅在插件市场更新本机 Codex 桥接不会自动应用它。在 AstrBot `core` 目录备份基线列出的文件，核对 `before` 哈希（`null` 表示原先不存在），再执行：

```powershell
git -c core.autocrlf=false apply --check 'D:\Plugins\astrbot_plugin_local_codex\compat\astrbot_onebot_audio\changes.patch'
git -c core.autocrlf=false apply 'D:\Plugins\astrbot_plugin_local_codex\compat\astrbot_onebot_audio\changes.patch'
```

将示例路径替换为自己的插件位置。核对 `after` 哈希后，停止再启动 AstrBot，NapCat 保持运行。运行中的 Python 已加载旧模块，重载桥接或点歌插件不足以替换框架发送器。框架升级可能覆盖补丁，届时应重新核对源码，不要强行应用不匹配的补丁。

## 验证

使用 AstrBot 的 Python 及测试依赖 `pytest`、`pytest-asyncio`、`silk-python`，确保 FFmpeg 位于 PATH。将 `ASTRBOT_ROOT` 指向专用临时目录，在 `core` 中执行：

```powershell
$env:ASTRBOT_ROOT = Join-Path $env:TEMP 'astrbot-onebot-audio-tests'
python -m pytest -q tests/unit/test_aiocqhttp_audio_payload.py
```

新增 15 项隔离测试，覆盖真实合成的 244 秒 MP3、六种引用、两种回环 HTTP 地址、WAV/SILK、群和私聊假发送器、空或缺失文件及图片处理。与既有 OneBot 文件、回复、戳一戳和群信息测试合并运行，共 34 项通过，没有跳过。

另以已安装 NapCat 的 `ws` 库和 50 MiB 接收限制建立回环连接验证：旧版消息触发同名超限错误，修复后同一音频的压缩消息完整接收。未连接 QQ、未发送群消息；实际 QQ 播放需重启后由用户验证。

## 来源与许可

框架源码及本衍生补丁遵循上游 GNU AGPL v3，完整许可保留于 [AGPL-3.0.txt](AGPL-3.0.txt)。修改由 Quin520 的 Codex-QQ-AstrBotBridge 于 2026-10-23 提供。补丁保留上下文和测试源码，不重新分发完整 AstrBot 包。
