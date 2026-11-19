# Done Notify for Antigravity CLI (`agy`)

`done-notify`参考Claude Code本地`done-notify`插件实现，通过`agy`的`SessionStart`、`PreInvocation`、`PreToolUse`与`Stop`生命周期钩子，在主会话回答完毕、后台任务唤醒执行结束、轮次异常失败、`agy`向用户提问或等待提供权限时，弹出macOS通知中心横幅并播放系统提示音。默认关闭焦点过滤限制（即使当前聚焦在该CLI窗口也会发出提示音），同时保留可选的窗口/标签页焦点静默模式。

## 触发与静默规则

1. 回答完毕与后台任务结束：监听`Stop`事件，正常完成（`NO_TOOL_CALL`、`TERMINAL_STEP_TYPE`、`INJECTED_RESPONSE`）时显示项目名、实时会话名、耗时与去除Markdown格式后的回答摘要（默认最多180字符），播放`sound`（默认`Glass`）；若该轮由后台任务或子代理完成自动唤醒触发，副标题自动标注`task done in Xs`（中文模式为`后台任务完成`）。多工作区会话下优先按所属`agy`进程的启动目录解析真实项目名，并从`conversation_summaries.db`与`annotations/<id>.pbtxt`实时读取会话名称（可通过`show_conversation=false`关闭）。
2. 任务失败与超限中止：当`terminationReason`为`ERROR`、`MAX_INVOCATIONS`、`MAX_FORCED_INVOCATIONS`、`MAX_TOKEN_BUDGET_EXCEEDED`或`TERMINAL_CUSTOM_HOOK`时，副标题标注失败原因与耗时，正文展示错误信息，并播放`error_sound`（默认`Sosumi`）。
3. 提问确认提醒：当模型调用`ask_question`暂停等待用户选择时，立即弹出`等待你的确认`（英文为`waiting for input`）横幅并显示问题内容，播放`prompt_sound`（默认`Ping`）。
4. 权限确认提醒：会话运行期间由轻量级进程绑定当前`agy`实例日志，仅当`agy`实际弹出工具权限确认界面（如沙盒外执行终端命令、敏感命令确认、工作区外读写文件、MCP工具确认、`default`模式工具审批或子代理操作等待人工授权）时，才弹出`等待权限确认`（英文为`waiting for permission`）横幅并播放`prompt_sound`（默认`Ping`）；自动放行的工具与已设为始终允许的命令不会触发提示音，当前`agy`退出后监听进程自动结束。
5. 焦点与静默策略：
   - 默认值`focus_mode=always`且`prompt_focus_mode=always`：不启用焦点静默限制，无论当前是否聚焦在终端窗口，完成、失败、提问与权限确认均会发出提示音与横幅通知。
   - 可选开启焦点过滤：若希望正在查看当前CLI时不通知，可将`focus_mode`（或`prompt_focus_mode`）设为`tab`（检查macOS前台应用及`Terminal.app`、`iTerm2`、`tmux`、`WezTerm`、`Kitty`的活动标签页或窗格）或`app`（仅检查前台应用）。
   - 用户主动按中断键取消（`USER_CANCELED`）、子代理内部普通轮次结束、非交互模式（如`agy -p`/`--print`或`acp`服务）及SSH远程连入会话均自动静默。

## 常用检查与配置命令

配置持久化保存在`~/.config/agy-done-notify/config.json`，修改后立即生效，无需重启`agy`：

- 测试完成提醒（`Glass`）：`python3 ~/.gemini/config/plugins/done-notify/skills/done-notify/scripts/done-notify.py --test`
- 测试提问提醒（`Ping`）：`python3 ~/.gemini/config/plugins/done-notify/skills/done-notify/scripts/done-notify.py --test --question`
- 测试权限确认提醒（`Ping`）：`python3 ~/.gemini/config/plugins/done-notify/skills/done-notify/scripts/done-notify.py --test --permission`
- 测试失败提醒（`Sosumi`）：`python3 ~/.gemini/config/plugins/done-notify/skills/done-notify/scripts/done-notify.py --test --error`
- 查看终端宿主识别、权限监听状态与当前配置详情：`python3 ~/.gemini/config/plugins/done-notify/skills/done-notify/scripts/done-notify.py --status`
- 切换中文/英文通知文案：`python3 ~/.gemini/config/plugins/done-notify/skills/done-notify/scripts/done-notify.py --set lang=zh`（改回英文为`lang=en`）
- 修改完成、提问/权限与失败提示音（取自`/System/Library/Sounds`）：`python3 ~/.gemini/config/plugins/done-notify/skills/done-notify/scripts/done-notify.py --set sound=Glass --set prompt_sound=Ping --set error_sound=Sosumi`
- 调整声音播放方式：`--set sound_mode=notification`（默认，由通知中心随横幅播放并遵循专注模式）、`afplay`（直接调用系统音频播放）、`both`（两者同时启用）或`off`（静音横幅）
- 调整焦点策略：`--set focus_mode=always --set prompt_focus_mode=always`（默认，始终提醒）；如需在聚焦当前终端时静默可改为`tab`或`app`
- 单独开关提问、权限或会话名显示：`--set notify_on_question=true --set notify_on_permission=true --set show_conversation=true`
- 设置最短耗时阈值（例如短于5秒的快速回答不通知）：`--set min_duration_sec=5`

## macOS通知权限说明

插件与Claude Code版`done-notify`一致，通过`/usr/bin/osascript`发送系统通知，通知归属应用为macOS内置的“脚本编辑器”（Script Editor）。若横幅或提示音未出现，请在macOS“系统设置 -> 通知 -> 脚本编辑器”中确认已开启“允许通知”与“为通知播放声音”，并确认未开启拦截通知的专注模式。
