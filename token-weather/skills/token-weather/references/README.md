# Token Weather for Antigravity CLI (`agy`)

`token-weather` 将 Antigravity CLI（`agy`）的实时 `statusLine` 会话遥测渲染为气象预报风格的状态条，对应 Claude Code 中 `token-weather` / `token-weather-usage` 的核心体验。

## 气象分级与指标说明

- `☀ Clear` / `晴朗`（`< 25%`）：上下文空间充裕。
- `☁ Cloudy` / `多云`（`25% - 49%`）：上下文开始积累。
- `☂ Showers` / `阵雨`（`50% - 74%`）：上下文过半，注意无关文件与历史。
- `☇ Storm` / `雷暴`（`75% - 89%`）：接近窗口上限，建议准备收敛任务。
- `↯ Compact soon` / `建议压缩`（`>= 90%`）：建议执行 `/compact` 或 `/clear` 开启新会话。

状态条按终端可用宽度自适应拼接以下分段（过窄时自动截断低优先级尾部信息）：

1. 当前执行模式标签（在 `agy` 中通过 `Shift+Tab` 或 `--mode` 切换：`[default]` 灰、`[accept-edits]` 琥珀/`[edits]`、`[auto]` 亮绿、`[plan]` 青蓝，若开启 Vim 模式则一并显示 `[VIM:N]`/`[VIM:I]` 等）、气象状态、百分比、进度条与上下文用量。
2. 5 小时与每周配额用量、重置倒计时与速率超前提示（自动匹配当前模型的配额池：Gemini、Argon、Carbon 或第三方 `3p` 模型组；当消耗比例显著快于时间窗口进度时显示 `↑` 预警）。
3. 最近 8 轮上下文变化迷你柱状图（`······▁▄`）与上一轮净增量（`Δ+18.4k`，压缩后显示绿色 `Δ-42k`）。
4. 当前模型名称、推理档位与代理状态（如 `thinking`）。
5. Prompt 缓存命中量与 5 分钟缓存 TTL 倒计时（`TTL 4m15s` / `TTL 45s` / `TTL cold`）。
6. 活跃子代理数、后台任务数、估算费用及 Git 分支状态。

## 启用方式

在 `agy` CLI 交互会话中运行以下命令即可启用：

```text
/statusline ~/.gemini/config/plugins/token-weather/skills/token-weather/scripts/token-weather.py
```

若希望同时保留 `agy` 默认状态栏并将天气条叠放在其下方，可在 `~/.gemini/antigravity-cli/settings.json` 的 `statusLine` 对象中添加 `"stack_with_default": true`：

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

可通过脚本 `--set` 持久化偏好（写入 `~/.config/agy-token-weather/config.json`），无需改动 `settings.json` 或重新执行 `/statusline`：

- 控制执行模式标签显示：`python3 <script> --set show_mode=always`（默认全部显示）/ `non-default`（仅非 default 时显示）/ `off`（隐藏）
- 切换中文标签：`python3 <script> --set lang=zh`
- 切换英文标签：`python3 <script> --set lang=en`
- 启用两行模式（第一行执行模式与上下文/缓存，第二行带进度条的 5h/周配额与模型/Git）：`python3 <script> --set lines=2`
- 改回单行模式：`python3 <script> --set lines=1`
- 将配额显示从“已用占比”切换为“剩余占比”：`python3 <script> --set quota_metric=remaining`（改回已用为 `used`）
- 隐藏或开启 5h/周配额段：`python3 <script> --set show_quota=false` / `true`
- 纯 ASCII 模式（不依赖特殊符号或 Nerd Font）：`python3 <script> --set ascii=true`
- 调整缓存 TTL 倒计时（秒，`0` 为关闭）：`python3 <script> --set cache_ttl=300`
- 调整进度条宽度（默认 `8`）：`python3 <script> --set bar_width=10`
- 隐藏 Git 分支：`python3 <script> --set show_git=false`
- 预览 5 档气象、执行模式与配额效果：`python3 <script> --preview` 或 `python3 <script> --preview --lines 2`
