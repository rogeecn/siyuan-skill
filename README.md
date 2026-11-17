# siyuan-skill

远程 **SiYuan（思源笔记）** 操作 skill：直接封装 SiYuan HTTP API（`/api/*` + `Token` 认证），
外面包一层**笔记本级 R/W/D 权限 GUARD**。纯 Python stdlib，无第三方依赖。

从 pi extension [pi-siyuan](../pi-siyuan) 迁移而来：工具划分、参数设计、反代兼容策略保持一致，
改为 agent skill（CLI）形态；权限表按**实例地址**分组存放，首次同步后强制人工确认。

```
siyuan-skill/
├── SKILL.md                  # skill 入口：触发词、首次流程、权限模型、命令
├── scripts/siyuan.py         # 全部实现：HTTP 客户端 + GUARD + 权限表 + CLI
├── references/siyuan-api.md  # 实测端点、响应包络、反代坑、上传契约
├── test/smoke.py             # mock SiYuan 端到端自检
└── README.md
```

## 环境变量

| 变量 | 必填 | 说明 |
| --- | --- | --- |
| `SIYUAN_API_URL` | ✅ | 实例地址，如 `https://siyuan.example.com`（尾部 `/` 自动去掉） |
| `SIYUAN_TOKEN` | ✅ | API token（SiYuan → 设置 → 关于 → API token） |
| `SIYUAN_CONFIG` | – | 权限表路径，默认 `~/.config/.siyuan.json` |
| `SIYUAN_TIMEOUT` | – | 请求超时秒数，默认 120 |

实例地址与 token **只从环境变量读取**，仓库内不含任何实例信息。
配置文件里的 `servers.<url>.token` 可作 token 兜底，脚本不会主动写入。

## 安装为 pi skill

```bash
ln -sfn ~/Workspace/siyuan-skill ~/.pi/agent/skills/siyuan
```

（本仓库开发目录用 `~/Workspace/siyuan-skill`，链接名即 skill 名。）

## 快速开始

```bash
export SIYUAN_API_URL=https://siyuan.example.com
export SIYUAN_TOKEN=<API token>

S=scripts/siyuan.py
python3 $S ping            # 连通性 + 首次同步（新库默认 R）
python3 $S perms show      # 看权限表，向用户确认每个库的 R/W/D
python3 $S perms set <notebookId> R,W
python3 $S perms confirm   # 用户确认后才执行
python3 $S search "关键词"
```

## 权限表（`~/.config/.siyuan.json`）

按实例地址分组，一个地址一份记录：

```json
{
  "version": 1,
  "servers": {
    "https://siyuan.example.com": {
      "token": "",
      "reviewed": true,
      "reviewedAt": "2026-11-17T08:00:00+00:00",
      "notebooks": [
        { "id": "20260101120000-abcdef", "name": "个人笔记", "permissions": ["R", "W"], "present": true },
        { "id": "20260101120001-bcdefa", "name": "敏感库", "permissions": ["NONE"], "present": true }
      ]
    }
  }
}
```

- `permissions`：`R`/`W`/`D` 自由组合；`["NONE"]` = 禁止一切操作。
- `name` 仅 human-readable，逻辑一律按 `id`。
- `present`：最近一次同步时该库是否仍存在；已删除的库保留权限记录但**不参与**全局「任一/所有」判定（避免误删后永久拒绝，也避免重名恢复时权限被重置为 R）。
- `reviewed=false` 时，除 `ping` / `notebooks` / `perms` / `selftest` 外全部拒绝。
- 每次数据操作前都会重新 `lsNotebooks` 同步；新库自动补 `["R"]`，name 变化自动更新。同步失败则拒绝执行（权限表可能过期）。
- 清空权限表 = 删掉该地址的条目；下次 `ping` 重新同步并以未确认状态开始。

## 权限规则

| 操作 | 规则 |
| --- | --- |
| `read` | 所属库 `R`（按 docId 反查 `blocks.box`） |
| 全局读（search / tree 无参 / recent / tags / snapshot list） | 任一库 `R` |
| 写类（create/append/update/daily/upload） | 目标库 `W` |
| `move` | 源库 `D` + 目标库 `W` |
| `retag` | **所有**库 `W` |
| `rollback` | **所有**库 `D` |
| `snapshot create` | 任一库 `R` |
| `ping` / `notebooks` / `perms` | 审计豁免 |

`NONE` 不计入任何「任一」放行集合，也永不满足「所有」类要求。

权限表按需同步：普通读写不再每次拉取笔记本列表；仅当 docId 所属库不在表中（新库）时同步一次，`retag`/`rollback` 这类「全库」判定也会强制同步，避免新库绕过。`ping` / `notebooks` 可随时手动刷新。
拒绝消息包含：笔记本 id+name、需要的权限、缺失的权限、操作名；退出码 `2`。

## 命令

```
ping | notebooks | perms {show,set,confirm} | selftest
search | read | create | append | update | move | tree | daily | recent
snapshot {list,create,rollback} | tags | retag | upload
```

完整参数见 `python3 scripts/siyuan.py <cmd> -h` 或 `SKILL.md`。

## 本地 Docker E2E

```bash
mkdir -p /tmp/siyuan-e2e/workspace/conf
echo '{"api":{"token":"e2e-token-123"}}' > /tmp/siyuan-e2e/workspace/conf/conf.json
docker run -d --name siyuan-e2e -p 16806:6806 \
  -v /tmp/siyuan-e2e/workspace:/siyuan/workspace \
  b3log/siyuan serve --workspace=/siyuan/workspace/ --accessAuthCode=e2e-pass
export SIYUAN_API_URL=http://127.0.0.1:16806 SIYUAN_TOKEN=e2e-token-123 SIYUAN_CONFIG=/tmp/siyuan-e2e/skill.json
python3 scripts/siyuan.py ping
```

已实测（3.8.2）：建库/建文档/读写/移动/日记/标签/上传/快照 create+list+rollback 全链路；注意快照需先初始化 repo key，移动必须用 `fromPaths`，详见 `references/siyuan-api.md`。

## 自检

```bash
python3 scripts/siyuan.py selftest   # 权限矩阵/权限解析/同步纯函数断言
python3 test/smoke.py                # 本地 mock SiYuan + CLI 子进程端到端
```

## 与 pi-siyuan 的关系

- 相同的 SiYuan 端点与反代兼容策略（SQL 查询、`getRepoSnapshots`、`renameTag`、`createDocWithMd` 日记）。
- 相同的 R/W/D + `NONE` 语义与全局审计规则。
- 差异：skill 形态（CLI 而非 16 个工具）；权限表按 URL 分组；`reviewed` 首次确认门禁；`present` 标记；上传用 stdlib multipart。

## License

MIT
