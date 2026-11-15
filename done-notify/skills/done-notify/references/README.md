# Done Notify for Antigravity CLI (`agy`)

`done-notify`参考Claude Code本地`done-notify`插件实现，通过`agy`的`Stop`与`PreToolUse`生命周期钩子，在主会话回答完毕、后台任务唤醒执行结束、轮次异常失败或`ask_question`等待确认，且macOS当前焦点不在该CLI窗口/标签页上时，弹出macOS通知中心横幅并播放系统提示音。

## 触发与静默规则

1. 回答完毕与后台任务结束：监听`Stop`事件，正常完成（`NO_TOOL_CALL`、`TERMINAL_STEP_TYPE`、`INJECTED_RESPONSE`）时显示项目名、耗时与去除Markdown格式后的回答摘要（默认最多180字符），播放`sound`（默认`Glass`）；若该轮由后台任务或子代理完成自动唤醒触发，副标题自动标注`task done in Xs`（中文模式为`后台任务完成`）。
2. 任务失败与超限中止：当`terminationReason`为`ERROR`、`MAX_INVOCATIONS`、`MAX_FORCED_INVOCATIONS`、`MAX_TOKEN_BUDGET_EXCEEDED`或`TERMINAL_CUSTOM_HOOK`时，副标题标注失败原因与耗时，正文展示错误信息，并播放`error_sound`（默认`Sosumi`）。
3. 等待用户选择：当模型调用`ask_question`暂停等待用户选择且当前CLI未聚焦时，弹出`waiting for input`提醒。
4. 自动静默场景：
   - 当前CLI正处于焦点状态（`focus_mode=tab`默认同时检查macOS前台应用与`Terminal.app`、`iTerm2`、`tmux`、`WezTerm`、`Kitty`的活动标签页/窗格；若用户在焦点内则不打扰）。
   - 用户主动按中断键取消（`USER_CANCELED`）。
   - 子代理内部轮次（`parentConversationId`非空或`rootConversationId`不等于当前会话ID）。
   - 非交互模式（如`agy -p`/`--print`或`acp`服务）及SSH远程连入会话。

## 常用检查与配置命令

配置持久化保存在`~/.config/agy-done-notify/config.json`，修改后立即对下一次钩子触发再生效，无需重启`agy`：

- 立即发送测试横幅与完成提示音：`python3 ~/.gemini/config/plugins/done-notify/skills/done-notify/scripts/done-notify.py --test`
- 立即发送失败样式测试横幅与提示音：`python3 ~/.gemini/config/plugins/done-notify/skills/done-notify/scripts/done-notify.py --test --error`
- 查看终端宿主识别与当前焦点判定详情：`python3 ~/.gemini/config/plugins/done-notify/skills/done-notify/scripts/done-notify.py --status`
- 切换中文/英文通知副标题：`python3 ~/.gemini/config/plugins/done-notify/skills/done-notify/scripts/done-notify.py --set lang=zh`（改回英文为`lang=en`）
- 修改成功与失败提示音（取自`/System/Library/Sounds`）：`python3 ~/.gemini/config/plugins/done-notify/skills/done-notify/scripts/done-notify.py --set sound=Glass --set error_sound=Sosumi`
- 调整声音播放通道：`--set sound_mode=notification`（默认，由通知中心随横幅播放并遵循专注模式）、`afplay`（直接调用系统音频播放）、`both`（两者同时启用）或`off`（静音横幅）
- 调整焦点判定粒度：`--set focus_mode=tab`（默认，检查前台应用及活动终端标签页/窗格）、`app`（仅检查前台应用）或`always`（无论是否聚焦均通知）
- 设置最短耗时阈值（例如短于5秒的快速回答不通知）：`--set min_duration_sec=5`

## macOS通知权限说明

插件与Claude Code版`done-notify`一致，通过`/usr/bin/osascript`发送系统通知，通知归属应用为macOS内置的“脚本编辑器”（Script Editor）。若横幅或提示音未出现，请在macOS“系统设置 -> 通知 -> 脚本编辑器”中确认已开启“允许通知”与“为通知播放声音”，并确认未开启拦截通知的专注模式。
