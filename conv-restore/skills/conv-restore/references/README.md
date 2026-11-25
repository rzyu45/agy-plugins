# Conversation Restore for Antigravity CLI（`agy`历史对话诊断与恢复）

`conv-restore`面向Antigravity CLI（`agy`）历史对话突然消失、`/resume`列表为空或某项目历史会话不可见的故障场景，提供零第三方依赖的只读诊断、预演、自动时间戳备份、离线定稿与完整性校核脚本。一旦上游`agy-cli`修复相关缺陷，可通过一条命令停用或卸载本插件。

## 支持诊断与恢复的四类根因

1. **模式A（`default-cli-project.json`工作区污染，上游缺陷`#1657`）**：`~/.gemini/config/projects/default-cli-project.json`中的`projectResources.resources[].folderUri`残留了旧项目路径。每次启动`agy`时都会将该路径与当前工作目录一并挂载，并按百分号编码的`file://` URI字典序排序`WorkspaceURIs`，而`/resume`按`WorkspaceURIs[0]`归组，导致所有URI字典序排在污染路径之后的项目在`/resume`中整体隐藏。该污染同时持久化在`conversations/<cid>.db`（`trajectory_metadata_blob`）、`conversation_summaries.db`（`workspace_uris`、`raw_summary`、`conversations_fts`）、`jetbox_summaries_proto.pb`与`cache/conversation_metadata.json`中；其中启动时`jetbox_summaries_proto.pb`优先级高于SQLite，因此离线清理时必须一并备份并移除陈旧的`jetbox_summaries_proto.pb`与`cache/conversation_metadata.json`，交由下次启动时的原生`reconcile_summaries.go`重建干净索引。
2. **模式B（0步`.db`存根遮蔽`.pb`，上游缺陷`#1602`）**：并发启动或文件锁竞争导致`conversations/`下同时存在空存根`<cid>.db`（`steps=0`、`trajectory_meta=(0,0)`、`trajectory_metadata_blob`为空）与非空`<cid>.pb`。`SqliteStore`优先读取空`.db`并遮蔽`.pb`，使摘要归零。经三重空库条件确认后，将0步`.db`存根（含`-wal`/`-shm`）移入备份目录的`stub_dbs/`子目录并移除陈旧摘要缓存，下次启动即可由`.pb`完整恢复。
3. **模式C（健康`.db`的摘要缺失或归零）**：`<cid>.db`实际包含有效步骤（`steps>0`），但在`conversation_summaries.db`中缺失对应记录，或`step_count=0`、`last_modified_time`停留在`0001-01-01`。移除陈旧摘要缓存与归零行后，下次启动会自动重建准确摘要。
4. **模式D（已归档注解）**：`annotations/<cid>.pbtxt`包含`archived: true`，默认不显示在`/resume`列表中。仅在用户显式指定`--unarchive`与`--cid`（或`--all-archived`）时，通过`ConversationService/UpdateConversation`取消归档。

## 安全保护边界

- **只读诊断零侵入**：`--diagnose`与`--dry-run`不会写入任何实时文件，读取SQLite前自动将`.db`、`-wal`、`-shm`复制到临时目录，避免在无`-shm`的WAL状态下失败或锁表。
- **自动时间戳备份**：执行`--restore`或`--finalize`前，所有待改动文件均按原相对目录结构备份至`/tmp/agy-conv-restore-backup-<YYYYMMDD_HHMMSS>/`并生成`manifest.json`。永不删除`brain/`记录、`.pb`文件或非空`.db`文件。
- **运行态保护**：默认检测到任意`agy`进程运行时拒绝写入，防止运行中的进程在退出时用内存中的陈旧摘要覆盖`jetbox_summaries_proto.pb`。在代理会话内部只能使用`--live-inactive-only`修复非活跃历史`.db`，退出全部`agy`后再在普通终端执行一次`--finalize`。
- **配置权限边界**：在代理会话内（检测到`ANTIGRAVITY_LS_ADDRESS`或写入受限时）绝不绕过`~/.gemini/config/`保护，而是输出可供用户在普通终端直接执行的安全命令。
- **Protobuf写前校验与完整性门控**：每次修改二进制Protobuf前均先做wire-type解析与回环重解析校验，确保不会清空工作区列表且保留全部非工作区字段；每次写库后立即校验`PRAGMA integrity_check == 'ok'`，异常时自动从备份回滚。

## 常用命令

```bash
SCRIPT=~/.gemini/config/plugins/conv-restore/skills/conv-restore/scripts/conv-restore.py

# 1. 只读诊断全部四类异常（含HUB存储与运行进程检查）
python3 "$SCRIPT" --diagnose

# 2. 指定额外需要剥离的污染路径进行预演（不修改任何文件）
python3 "$SCRIPT" --dry-run --strip-uri /path/to/polluter-project

# 3. 退出全部agy进程后，在普通终端执行完整离线恢复（推荐）
python3 "$SCRIPT" --restore

# 4. 在agy运行期间仅安全修复非活跃历史.db，退出agy后再执行--finalize定稿
python3 "$SCRIPT" --restore --live-inactive-only
# 退出全部agy后在普通终端执行：
python3 "$SCRIPT" --finalize

# 5. 恢复后执行完整性与零残留核验（可选--verify-staging在/tmp沙箱实测agy --hub重建）
python3 "$SCRIPT" --verify
python3 "$SCRIPT" --verify --verify-staging

# 6. 按需取消归档指定会话（需在存在LS环境变量的会话中或传入对应环境）
python3 "$SCRIPT" --restore --unarchive --cid <conversation-id>
```

## 上游修复后的停用与卸载

当`agy-cli`官方修复上述对话索引问题后，可随时一键停用或卸载本插件：

- 停用插件（从`~/.gemini/config/config.json`关闭，不再加载本技能，保留记录且重装不覆盖）：`agy plugin disable conv-restore`
- 如需重新启用：`agy plugin enable conv-restore`
- 彻底卸载：`agy plugin uninstall conv-restore`
