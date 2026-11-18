# Safe Allow for Antigravity CLI (`agy`)

`safe-allow`通过`PreToolUse`生命周期钩子与`settings.json`规则同步双层机制减少`Ctrl+K`中断。在`agy` `v1.5.x`中，`hooks.json`里的`PreToolUse`钩子仅作用于主协调代理，`/swarm`后台`worker`只读取`~/.gemini/antigravity-cli/settings.json`中的`permissions.allow`、`permissions.ask`与`permissions.deny`（严格按`Deny > Ask > Allow`优先级判定）。因此安装插件或变更配置后，需在`agy`外部终端运行一次`--sync-settings`，将已启用分类、本地`remote_ssh`白名单与高危`ask`拦截至静态Go RE2规则中，使`/swarm`子代理与主代理保持一致，同时高危指令仍由`ask`拦截并交由用户确认。

## 五类自动放行范围

1. `readonly_shell`（只读查看与检索）：放行`ls`、`rg`、`grep`、`find`（禁止`-delete`、`-exec`、`-ok`）、`wc`、`jq`、`cat`、`head`、`tail`、`pdfinfo`、`pdftotext`、`pdffonts`、`ps`、`pmset -g`、`git status/diff/log/show/branch列表`等只读命令，支持对`&&`、`;`、`|`复合命令逐段检验，且禁止读取`~/.ssh`、`.env`、`*.pem`、`*.key`等凭据文件。
2. `doc_and_edit`（本地文档编译与工作区编辑）：放行目标路径位于当前工作区、brain目录或临时目录内（且非`.git/`、`.agents/hooks.json`、`.agents/plugins/`、`settings.json`及密钥路径）的`write_file`、`edit_file`，以及工作区内的`latexmk`（禁止`-e`与`-shell-escape`）、`xelatex`、`pdflatex`、`lualatex`、`bibtex`、`biber`、`make`文档目标、`pdftoppm`、`pdftocairo`、`gs`、`qpdf`、`sips`、`officecli`与`git add/commit`。
3. `mcp_and_web`（只读类MCP与网页读取）：放行`search_web`、`read_url`、`paper-search-mcp`全部工具、`drawio`全部工具以及`zotero`的检索、元数据与PDF读取工具；删除、批量改写、新建条目或合并重复项等写入类`zotero`工具保持返回`ask`。
4. `workspace_python`（工作区Python与检查命令）：放行位于工作区、brain/scratch或`~/.gemini/config/{skills,plugins}`下的`.py`脚本运行、`pytest`、`python3 -m py_compile|pytest|unittest|json.tool`，以及沙盒内无危险系统调用的`python3 -c`片段。
5. `remote_gpu_ssh`（已授权远端服务器SSH/SCP/rsync）：默认不预设任何远端主机，仅放行用户通过`--add-ssh-target`与`--add-ssh-prefix`显式加入本地配置文件的`user@host:port`及远端目录前缀（默认含`/tmp`），禁止端口转发参数、远端`rm`/`sudo`/`killall`及`rsync --delete`。

## 高危硬拦截规则

无论是否处于沙盒模式，只要命中以下特征一律强制返回`ask`并交由人工确认（配合`done-notify`响铃提醒）：
- 删除或磁盘写操作：`rm`、`rmdir`、`unlink`、`shred`、`srm`、`trash`、`truncate`、`dd`、`mkfs`、`fdisk`、`diskutil`、`find -delete/-exec`、`sed -i`、`rsync --delete`
- 提权与系统改动：`sudo`、`su`、`osascript`、`launchctl`、`defaults write/delete`、`kill`、`killall`、`pkill`、`chown`、`chmod`
- 破坏性Git与包管理操作：`git push`、`git reset --hard/--merge`、`git clean`、`git checkout --/-f`、`git rebase`、`git branch -D`、`pip/conda/brew install|uninstall`
- 远程脚本管道执行：`curl`/`wget`通过管道直接传入`sh`/`bash`/`zsh`/`python`/`node`等解释器

## 常用检查与配置命令

配置文件默认保存于`~/.config/agy-safe-allow/config.json`，审计日志保存于`/tmp/agy-safe-allow-<uid>/audit.jsonl`（保留最近200条记录）：

- 查看当前配置与统计摘要：`python3 ~/.gemini/config/plugins/safe-allow/skills/safe-allow/scripts/safe-allow.py --status`
- 查看最近拦截记录：`python3 ~/.gemini/config/plugins/safe-allow/skills/safe-allow/scripts/safe-allow.py --recent --ask-only`
- 预演测试某条命令是否放行：`python3 ~/.gemini/config/plugins/safe-allow/skills/safe-allow/scripts/safe-allow.py --check-cmd "cd docs && make pdf" --bypass-sandbox`
- 添加已授权远端SSH主机与目录前缀：`python3 ~/.gemini/config/plugins/safe-allow/skills/safe-allow/scripts/safe-allow.py --add-ssh-target alice@gpu-server.example:2222 --add-ssh-prefix /data/alice`
- 同步生成`/swarm`子代理所需的`settings.json`规则（请在`agy`外部的普通终端运行，自动备份为`settings.json.bak`）：`python3 ~/.gemini/config/plugins/safe-allow/skills/safe-allow/scripts/safe-allow.py --sync-settings`
- 仅预览合并后的`settings.json`内容而不写入：`python3 ~/.gemini/config/plugins/safe-allow/skills/safe-allow/scripts/safe-allow.py --print-settings`
- 切换生效范围（仅子代理或全量）：`python3 ~/.gemini/config/plugins/safe-allow/skills/safe-allow/scripts/safe-allow.py --set scope=subagents_only`
- 开关单个分类：`python3 ~/.gemini/config/plugins/safe-allow/skills/safe-allow/scripts/safe-allow.py --set remote_gpu_ssh=false`
- 临时停用或卸载插件：`agy plugin disable safe-allow`或`agy plugin uninstall safe-allow`
