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

## 许可与来源

补丁中的自学习插件衍生部分遵循上游 AGPL-3.0，完整协议见 [AGPL-3.0.txt](AGPL-3.0.txt)。上游作者和版权声明保留。此兼容修改由 Quin520 的 Codex-QQ-AstrBotBridge 提供；其源码就是本目录补丁以及桥接的 `profile_learning.py` / `profile_reviews.py`。未重新发布上游完整包或二进制依赖。
