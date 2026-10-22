# 我想听歌！0.1.3 兼容补丁

针对 [57Darling02 的听歌插件](https://github.com/57Darling02/astrbot_plugin_listen_music)已安装的 `astrbot_plugin_listen_music` **0.1.3** 源码基线，配合桥接 **v0.3.12** 和 AstrBot **4.29.1**。这是指定基线的差异补丁，不会自动更新市场插件。

## 修复内容

- 视频详情请求失败时保留上游错误；全部失败不再误报成“没有找到可播放音乐”。部分候选可用时保留它们，并记录失败状态。
- 同实例限制详情请求并缓存短期结果，减少重复请求；不会更换代理、绕过认证或无限重试。
- `find_music` 的 `title` 支持用户提供的 BV/AV、`bilibili.com/video/...` 链接以及 `b23.tv` 短链接。确切视频直接生成分 P 候选，不另搜同名歌曲。
- 短链手动处理有限重定向，限定 HTTPS、允许域名和视频路径，不请求视频落地网页；不能访问内网、任意域名或带认证信息的网址。
- 候选仍经过当前会话快照、一次性选择和媒体大小/时长检查，翻唱工具没有新增任意网址或文件参数。

桥接同时为发给机器人的点歌/翻唱链接请求避开自动链接预览，防止预览发送后默认模型流程被跳过。群内每条请求仍需真实 @机器人。普通链接分享保持原行为；没有重置 AstrBot 的发送状态，也没有全局禁用解析插件。

## 安装

1. 备份当前听歌插件，核对 `baseline.json` 中的 `before` 哈希；文件不匹配时先重新适配，不能强行覆盖。
2. 在听歌插件目录执行：

   ```powershell
   git -c core.autocrlf=false apply --check 'D:\Plugins\astrbot_plugin_local_codex\compat\listen_music\changes.patch'
   git -c core.autocrlf=false apply 'D:\Plugins\astrbot_plugin_local_codex\compat\listen_music\changes.patch'
   ```

3. 同时安装桥接 v0.3.12，由管理员重启 AstrBot。NapCat 不需要重启。

补丁不包含账号凭据、用户资料、聊天历史、模型文件或私有路径；保留上游作者归属。市场更新可能覆盖修改，回退时恢复备份。此补丁只针对上面的目录名和源码版本，不能直接用于改名后的其他版本。

## 验证

在打补丁后的听歌插件目录使用 AstrBot 的 Python 执行 `python -m unittest discover -s tests -q`；桥接的 `tests/test_music_routing.py` 使用真实 AstrBot 流程检查链接让路及正常预览，不发送 QQ 消息。网络限制可能随时间变化；搜索失败后应检查具体 HTTP/API 错误，不能从错误推断歌曲不存在。

本次增加 16 项回归，相关 49 项测试通过。听歌插件在 Windows 上的完整 126 项测试仍有 7 项原有路径相关失败，与原始 110 项测试的失败一致；此补丁未修改账号存储或删除这些测试，不宣称完整测试全部通过。

## Windows 账号保存补丁（v0.3.14）

另提供独立的 [windows-credentials.patch](windows-credentials.patch) 和 [windows-credentials-baseline.json](windows-credentials-baseline.json)，不替换上面的 v0.3.12 搜索补丁。它只修改 `core/accounts.py`，并新增 `tests/test_windows_credentials.py`。

原版无条件调用 `os.fchmod`，Windows 扫码确认后会因该 API 不存在而无法保存。修复在写入凭据前，用系统 `whoami.exe` 取得当前用户 SID，再用系统 `icacls.exe` 将临时空文件设为不继承权限、仅该用户可访问；随后写入并原子替换。权限设置失败会中止并保留旧文件，不会回退到宽松权限。系统子进程隐藏运行且有超时；非 Windows 保持 0600。

先备份并按独立基线核对哈希，再在听歌插件根目录执行：

```powershell
git -c core.autocrlf=false apply --check 'D:\Plugins\astrbot_plugin_local_codex\compat\listen_music\windows-credentials.patch'
git -c core.autocrlf=false apply 'D:\Plugins\astrbot_plugin_local_codex\compat\listen_music\windows-credentials.patch'
python -m unittest discover -s tests -p test_windows_credentials.py -v
```

Windows 的 ACL 验证测试需要 AstrBot 环境中的 `pywin32`；运行时修复只依赖系统工具。新增 2 项隔离测试通过：虚构账号登录持久化、登出及实际 DACL 验证；ACL 失败时旧文件不变且临时文件被清理。测试不登录真实账号、不发送 QQ 消息。由管理员重启 AstrBot 加载修复；它不解除 B 站 HTTP 412。

已安装的上游 0.1.3 包未附 LICENSE。本目录保留作者及仓库来源，只提供指定基线的修改差异，不声明上游代码采用本桥接仓库的许可。请从上游获取原始插件。
