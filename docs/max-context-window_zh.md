# Antigravity CLI（`agy`）最大上下文窗口配置（`900K` / `1M`满血档）

在`~/.gemini/antigravity-cli/settings.json`（Windows为`%USERPROFILE%\.gemini\antigravity-cli\settings.json`）中写入`"contextWindow": "900K"`，即可将`agy`的全局默认上下文容量永久设为最高档位：

```json
{
  "$schema": "https://antigravity.google/schemas/v1/settings.json",
  "contextWindow": "900K"
}
```

## 预设档位与说明（`agy` `v1.5.9`）

经核对`agy`的`backend.ParsePresetContextWindow`、`backend.ContextWindow.MaxTokenLimit`、`backend.ContextWindow.QuotaMultiplier`（`context_window.go`）与`model.renderQuotaNote`（`model_selector_context_window.go`），`agy`仅识别以下三个预设字符串：

| `"contextWindow"` | `MaxTokenLimit` | `/model`界面配额提示（`QuotaMultiplier`） |
| :--- | :--- | :--- |
| `"256K"`（默认） | `256,000` | 基准档位（无额外提示） |
| `"512K"` | `512,000` | `about 1.3x quota per turn` |
| `"900K"`（`1M`满血档） | `900,000` | `about 1.8x quota per turn` |

- **必须写`"900K"`，不要写`"1M"`**：`ParsePresetContextWindow`仅接受`"256K"`、`"512K"`、`"900K"`三个合法枚举，写成`"1M"`会静默回退到`"256K"`。
- **在`agy`之外的常规终端中修改**：`agy`对`~/.gemini/antigravity-cli/settings.json`设有硬编码的系统写保护边界（因该文件控制权限与外部钩子，会拦截代理的`write_file`/`edit_file`调用），请在本地终端或编辑器中执行修改。
- **不要把`"$schema"`传给`--settings`参数**：命令行`agy --settings '{...}'`会校验未知键并报错拒绝，但将`"$schema"`写入磁盘上的`settings.json`可以正常加载。
- **配额倍率说明**：`1.3x`与`1.8x`是客户端在支持切换上下文档位的模型上展示的每轮配额估算提示（`/model`界面文案），实际扣减由服务端结算。

## 一键原子合并命令（macOS / Linux）

在常规终端执行以下命令，可在保留原有全部配置字段（如`model`、`permissions`、`statusLine`、`trustedWorkspaces`等）的前提下，以原子写入方式新建或更新`~/.gemini/antigravity-cli/settings.json`并校验JSON格式：

```bash
python3 -c '
import json, os, tempfile
p = os.path.expanduser("~/.gemini/antigravity-cli/settings.json")
os.makedirs(os.path.dirname(p), exist_ok=True)
d = json.load(open(p, "r", encoding="utf-8")) if os.path.exists(p) else {}
merged = {
    "$schema": "https://antigravity.google/schemas/v1/settings.json",
    **d,
    "contextWindow": "900K",
}
fd, tmp = tempfile.mkstemp(dir=os.path.dirname(p), suffix=".tmp")
with os.fdopen(fd, "w", encoding="utf-8") as f:
    json.dump(merged, f, ensure_ascii=False, indent=2)
    f.write("\n")
os.replace(tmp, p)
' && python3 -m json.tool ~/.gemini/antigravity-cli/settings.json
```

## 验证生效

更新完成后，重新打开一个`agy`会话（或在`/model`中重新选择一次模型），并在`agy`内运行`/context`确认当前上下文上限。若同时启用了[`token-weather`](../README.md#2-token-weather-context--quota-forecast-status-line)，状态栏的上下文分母会在下一轮自动更新。
