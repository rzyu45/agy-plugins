# Antigravity CLI最大上下文窗口配置指南

尽管1M窗口模型标称支持1,000,000个token的上下文容量，但`agy` `v1.5.10`实际根据`CheckpointConfig.MaxTokenLimit`触发自动对话压缩，而服务端模型目录为各模型下发的默认阈值约为500,000至512,000个token。在公开发行版`agy` `v1.5.10`中，真正将自动压缩阈值提高到`900,000`个token需要同时满足两项条件，即选用`Gemini 4 Argon`模型家族，并在启动时设置环境变量`AGY_CLI_AUTO_COMPACT_OVERRIDE=900000`。

```bash
# 在agy v1.5.10中为Gemini 4 Argon开启900,000个token的自动压缩阈值
AGY_CLI_AUTO_COMPACT_OVERRIDE=900000 agy --model "Gemini 4 Argon (High)"

# store.AutoCompactOverride同样支持百分比写法，会自动按模型maxTokens的90%换算
AGY_CLI_AUTO_COMPACT_OVERRIDE=90% agy --model "Gemini 4 Argon (High)"
```

如需在交互式终端中长期生效，可将`export AGY_CLI_AUTO_COMPACT_OVERRIDE=900000`写入`~/.zshrc`或`~/.bashrc`。

## 为什么仅配置`"contextWindow": "900K"`仍会在约50万token处压缩

对`agy` `v1.5.10`中的`store.(*Store).ContextWindow`、`ServerBackend.buildCascadeConfigInternal`与`utils.GetCheckpointConfigForModelInfo`进行ARM64反汇编核验后可以看到，客户端在读取`settings.json`之前设有两道拦截关卡。

- **硬编码的`Gemini 4 Argon`单模型白名单。**`third_party/jetski/cli/store/context_window.go`中位于`0x102ef5bf0`的`Store.ContextWindow`与位于`0x103144fa0`的`SelectorModel.offersContextWindow`先通过`0x102f43090`处的`ModelEffortDisplay`提取当前模型家族名，再检查其是否属于全局切片`0x10d3dcbc0`。该切片长度和容量均为`1`，唯一元素是`"Gemini 4 Argon"`。当选用`"Gemini 4 Carbon (High)"`或`"Orchestrator (Carbon)"`时，`Store.ContextWindow`在`0x102ef5cd8`直接返回空字符串`""`，甚至不会进入`0x102ef5cf0`处的`AutoCompactOverride`环境变量读取分支。因此在`v1.5.10`中，`Gemini 4 Carbon`会同时忽略`settings.json`和`AGY_CLI_AUTO_COMPACT_OVERRIDE`，始终按服务端下发的默认`CheckpointConfig.MaxTokenLimit`在约50万至51.2万token处自动压缩。
- **`settings.json`预设档位受未接通的`"context-window-options"`特性开关门控。**当切换到`Gemini 4 Argon`通过白名单后，`Store.ContextWindow`会先调用`0x102ef59c0`处的`AutoCompactOverride`读取`AGY_CLI_AUTO_COMPACT_OVERRIDE`，一旦非空便立即在`0x102ef5d24`返回该阈值字符串，再由`0x1022e6f68`处的`buildCascadeConfigInternal`写入`CascadeConfig.CheckpointConfig.MaxTokenLimit`。若未设置该环境变量，`Store.ContextWindow`在`0x102ef5cfc..0x102ef5d68`处内联`store.go`第521行，对`Store+0x58`字段做`0x10a1ad5b0`处的`*types.FeatureFlagReader`接口类型断言以查询`"context-window-options"`开关。由于生产态没有任何后端向`Store+0x58`注入该接口，只有`0x102f496d0`处的单元测试辅助函数`Store.SetFeatureFlagForTest`会写入该槽位，Go编译器在编译期直接消除了预设分支，使三条路径全部跳转到`0x102ef5d30`返回`""`，导致真正读取`settings.json`预设值的`Store.ContextWindowChoice`在`0x102ef5b90`处的调用点数量为`0`。

## 向前兼容的`settings.json`预设档位参考

在macOS或Linux的`~/.gemini/antigravity-cli/settings.json`以及Windows的`%USERPROFILE%\.gemini\antigravity-cli\settings.json`中保留`"contextWindow": "900K"`可以通过配置校验，待后续版本接通`"context-window-options"`特性开关后即可直接生效。

```json
{
  "$schema": "https://antigravity.google/schemas/v1/settings.json",
  "contextWindow": "900K"
}
```

经核对`0x1022d68a0`处的`backend.ParsePresetContextWindow`、`0x1022d6970`处的`backend.ContextWindow.MaxTokenLimit`、`0x1022d6a20`处的`backend.ContextWindow.QuotaMultiplier`与`model.renderQuotaNote`，客户端内置的三个合法预设字符串如下表所示。

| `"contextWindow"` | `MaxTokenLimit` | `/model`界面配额提示 |
| :--- | :--- | :--- |
| `"256K"` | `256,000` | 基准档位且无额外提示 |
| `"512K"` | `512,000` | `about 1.3x quota per turn` |
| `"900K"` | `900,000` | `about 1.8x quota per turn` |

- **合法预设枚举名。**必须填写`"900K"`而非`"1M"`，因为`ParsePresetContextWindow`仅接受`"256K"`、`"512K"`、`"900K"`三个枚举值，写成`"1M"`会静默回退到`"256K"`。
- **系统写保护边界。**请在`agy`之外的常规终端或编辑器中修改`~/.gemini/antigravity-cli/settings.json`，因为`agy`对该文件设有硬编码写保护边界，会拦截代理发起的`write_file`与`edit_file`调用。
- **命令行`--settings`参数限制。**不要把`"$schema"`传给`agy --settings '{...}'`命令行参数，因为该参数拒绝未知键，而将`"$schema"`保存在磁盘上的`settings.json`中可以正常加载。
- **配额倍率说明。**`about 1.3x quota per turn`与`about 1.8x quota per turn`是客户端在`/model`界面展示的每轮配额估算文案，实际配额扣减由服务端结算。

## `settings.json`一键原子合并命令

在常规终端执行以下命令，可在保留全部原有字段的前提下，以原子写入方式新建或更新`~/.gemini/antigravity-cli/settings.json`并校验JSON格式。

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

## 本机二进制复核脚本

在macOS ARM64环境安装`agy` `v1.5.10`后，可运行以下只读脚本直接核验`0x10d3dcbc0`处的单元素白名单`['Gemini 4 Argon']`，以及`0x102ef5b90`处`Store.ContextWindowChoice`调用次数为`0`的事实。

```bash
/usr/bin/python3 -c '
import shutil, struct
agy_path = shutil.which("agy") or "/Users/Shared/agy"
with open(agy_path, "rb") as f:
    b = f.read()
ncmds = struct.unpack_from("<I", b, 16)[0]
off, segs = 32, []
for _ in range(ncmds):
    cmd, cmdsize = struct.unpack_from("<II", b, off)
    if cmd == 0x19:
        seg = b[off+8:off+24].split(b"\x00")[0].decode()
        vma, vms, fo, fs = struct.unpack_from("<QQQQ", b, off+24)
        segs.append((seg, vma, fo, fs))
        if seg == "__TEXT":
            nsects = struct.unpack_from("<I", b, off+64)[0]
            for i in range(nsects):
                so = off + 72 + i * 80
                if b[so:so+16].split(b"\x00")[0] == b"__text":
                    t_va, t_sz = struct.unpack_from("<QQ", b, so+32)
                    t_fo = struct.unpack_from("<I", b, so+48)[0]
    off += cmdsize
va2fo = lambda va: next(fo + (va - vma) for _, vma, fo, fs in segs if vma <= va < vma + fs)
ptr, slen, scap = struct.unpack_from("<QQQ", b, va2fo(0x10d3dcbc0))
sptr, sslen = struct.unpack_from("<QQ", b, va2fo(ptr))
print("whitelist:", [b[va2fo(sptr):va2fo(sptr)+sslen].decode()], f"(len={slen}, cap={scap})")
callers = [hex(t_va + p - t_fo) for p in range(t_fo, t_fo + t_sz, 4)
           if (struct.unpack_from("<I", b, p)[0] >> 26) == 0b100101 and
           (t_va + p - t_fo) + ((struct.unpack_from("<I", b, p)[0] & 0x3ffffff) - ((1 << 26) if (struct.unpack_from("<I", b, p)[0] & 0x2000000) else 0)) * 4 == 0x102ef5b90]
print("ContextWindowChoice (0x102ef5b90) callers:", callers)
'
```

启用配置后，可在`agy`会话中执行`/context`查看当前上下文上限，或配合[`token-weather`](../README.md#2-token-weather-three-line-mode-quotacolab--context-status-band)在状态栏实时观察上下文分母与填充比例。
