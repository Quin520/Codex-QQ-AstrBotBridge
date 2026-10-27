# 解析器下载缓存过期清理

此增量补丁基于万能解析器 1.6.1 加本仓库 `updates-20261026` 累计适配。原有 B 站、抖音、小黑盒解析及群共享上下文保持不变。

原清理任务会删除并重建整个缓存目录，可能碰到刚下载的文件。补丁改为逐文件检查最后写入或复用时间，只删除超过保留时长的普通缓存文件；锁定文件留到下次，跳过链接、子目录和多硬链接文件。媒体复用时刷新时间，Cookies 等其他目录不在清理范围。空 Cron 配置会正确关闭任务。

建议配置：

- `clean_cron`: `0 * * * *`，每小时检查。
- `cache_retention_hours`: `24`，范围 1–720 小时。

对已应用上述累计适配的解析器目录，备份后执行：

```powershell
git -c core.autocrlf=false apply --check 'D:\Plugins\astrbot_plugin_local_codex\compat\parser\cache-expiry.patch'
git -c core.autocrlf=false apply 'D:\Plugins\astrbot_plugin_local_codex\compat\parser\cache-expiry.patch'
```

文件基线及修改后哈希见 `cache-expiry.baseline.json`。上游文件不同应重新合并，勿强制覆盖。安装补丁后重载解析器或重启 AstrBot；不需要重启 NapCat。

验证：解析器原有用例及新增缓存边界用例合计 46 项通过，覆盖缓存命中延长保留时间、过期删除、锁定文件、私有目录保护和空计划。没有调用 QQ 发送接口。补丁继续遵循本目录的上游许可证。
