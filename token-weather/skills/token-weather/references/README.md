# Token Weather for Antigravity CLI (`agy`)

`token-weather`将Antigravity CLI（`agy`）的实时`statusLine`会话遥测渲染为气象预报风格的三行状态栏，对应Claude Code中`token-weather`的核心体验，并集成Google Colab剩余计算单元（Compute Units）监控。

## 默认三行布局说明

默认采用清晰的三行分层结构（`lines=3`，按终端宽度自适应折中或截断尾部次要信息）：

1. **模式与模型（第1行）**：当前执行模式标签（在`agy`中通过`Shift+Tab`或`--mode`切换：`[default]`灰、`[accept-edits]`琥珀/`[edits]`、`[auto]`亮绿、`[plan]`青蓝，开启Vim模式时一并显示`[VIM:N]`/`[VIM:I]`等）、当前模型名称、推理档位与代理实时状态（如`thinking`），以及活跃子代理数、后台任务数、估算费用与Git分支状态。
2. **模型配额与Colab用量（第2行）**：自动匹配当前模型配额池（Gemini、Argon、Carbon或第三方`3p`模型组）的5小时与每周配额用量、重置倒计时及消耗超前提示（`↑`），右侧并列展示Google Colab剩余计算单元（如`Colab 1000 CU`；当远端存在活跃实例时自动附加每小时消耗速率与实例数，如`Colab 942.5 CU · 1.9/h · 1 VM`）。Colab余额通过后台分离进程异步刷新并持久化缓存，阻塞时间为零。
3. **上下文用量（第3行）**：上下文气象等级、百分比、进度条与`已用/窗口上限`词元数，后接最近8轮上下文变化迷你柱状图（`·····▄▆█`）、上一轮净增量（`Δ+24.1k`，压缩后显示绿色`Δ-42k`）以及Prompt缓存命中量与TTL倒计时（`TTL 4m15s` / `TTL 38s` / `TTL cold`）。

## 气象分级说明

- `☀ Clear` / `晴朗`（`< 25%`）：上下文空间充裕。
- `☁ Cloudy` / `多云`（`25% - 49%`）：上下文正常积累。
- `☂ Showers` / `阵雨`（`50% - 74%`）：上下文过半，注意大文件读取与长历史。
- `☇ Storm` / `雷暴`（`75% - 89%`）：接近窗口上限，建议收尾当前子任务。
- `↯ Compact soon` / `建议压缩`（`>= 90%`）：建议执行`/compact`或`/clear`开启新会话。

## 启用方式

在`agy`交互会话中运行以下命令即可启用：

```text
/statusline ~/.gemini/config/plugins/token-weather/skills/token-weather/scripts/token-weather.py
```

若希望同时保留`agy`默认状态栏并将天气条叠放在其下方，可在`~/.gemini/antigravity-cli/settings.json`的`statusLine`对象中添加`"stack_with_default": true`：

```json
{
  "statusLine": {
    "type": "command",
    "command": "~/.gemini/config/plugins/token-weather/skills/token-weather/scripts/token-weather.py",
    "enabled": true,
    "stack_with_default": true
  }
}
```

## 常用配置与预览

可通过脚本`--set`持久化偏好（写入`~/.config/agy-token-weather/config.json`），无需改动`settings.json`或重新执行`/statusline`：

- 预览5档气象、执行模式、模型配额与Colab余额效果：`python3 <script> --preview`（或加`--lang zh`、`--lines 2`、`--lines 1`）
- 立即刷新Colab计算单元缓存：`python3 <script> --refresh-colab`
- 切换显示行数（默认`3`）：`python3 <script> --set lines=3`（可选`1`、`2`、`3`）
- 控制Colab余额显示与认证方式：`python3 <script> --set show_colab=true`、`--set colab_auth=auto`（可选`auto`、`oauth2`、`adc`）、`--set colab_ttl=180`（空闲刷新间隔秒数，有活跃实例时自动缩短至60秒）
- 控制执行模式标签显示：`python3 <script> --set show_mode=always`（默认全部显示）/ `non-default`（仅非default时显示）/ `off`（隐藏）
- 切换中英文标签：`python3 <script> --set lang=zh`或`--set lang=en`
- 将模型配额显示从已用占比切换为剩余占比：`python3 <script> --set quota_metric=remaining`（改回已用为`used`）
- 隐藏或开启模型5h/周配额段：`python3 <script> --set show_quota=false` / `true`
- 纯ASCII模式：`python3 <script> --set ascii=true`
- 调整缓存TTL倒计时（秒，`0`为关闭）：`python3 <script> --set cache_ttl=300`
- 调整进度条宽度（默认`8`）：`python3 <script> --set bar_width=10`
- 隐藏Git分支：`python3 <script> --set show_git=false`
