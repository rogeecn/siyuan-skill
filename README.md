# siyuan-skill

远程 **SiYuan（思源笔记）** 操作 skill：直接封装 SiYuan HTTP API（`/api/*` + `Token` 认证），
外面包一层**笔记本级 R/W/D 权限 GUARD**。纯 Python stdlib，无第三方依赖。

从 pi extension [pi-siyuan](../pi-siyuan) 迁移而来：工具划分、参数设计、反代兼容策略保持一致，
改为 agent skill（CLI）形态；权限表按**实例地址**分组存放，首次同步后强制人工确认。

```
siyuan-skill/
├── SKILL.md                        # skill 入口：触发词、首次流程、权限模型、命令、语法速查
├── scripts/siyuan.py               # 全部实现：HTTP 客户端 + GUARD + 权限表 + CLI
├── references/
│   ├── siyuan-markdown.md          # 写正文必读：行级/块级语法、超级块、IAL、易错清单
│   ├── siyuan-operations.md        # 任务配方：建文档/移动/属性/日记/模板/搜索/数据库/快照/导出
│   └── siyuan-api.md               # 端点全量、块类型、SQL schema、上传契约、反代坑
├── test/smoke.py                   # mock SiYuan 端到端自检
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
sql | snapshot {list,create,rollback} | tags | retag | upload

# 数据层通用能力（对齐内核内置 MCP 的 29 个工具面）
outline | backlinks | attr {get,set,batch} | block {kramdown,children,dom,breadcrumb,info,insert,delete}
export {md,html,docx} | template {ls,get,render,save-as,rm} | file {ls,read,write,mv,cp,rm}
notebook {open,close,create,rename,icon} | asset {unused,stat,clean} | sync {status,perform}
bookmark {ls,rename,rm} | system | av {create,view,keys,search,add-item,add-key,rm-item,rm-key}
mcp <tool> | import | call <endpoint>
```

- `sql` 仅放行 `SELECT` / `WITH`（只读，避免绕过命令级权限语义）。
- `av *` 与 `mcp *` 走内核内置 MCP（HTTP 未开放建库等端点）；`call` 只放行白名单端点（见 `references/siyuan-api.md`）。
- 破坏性动作（`block delete`、`file rm`、`av rm-*`、`sync perform`、`snapshot rollback`…）需 `--yes` 且要求对应库 `D`。

## 文档

- [`references/siyuan-markdown.md`](references/siyuan-markdown.md)：思源 Markdown/Kramdown 语法权威速查
  （行级标记、提示块、超级块、IAL、块引用、导出差异、12 条实测易错点），内容基于官方用户指南 + 3.8.2 live 验证。
- [`references/siyuan-operations.md`](references/siyuan-operations.md)：任务导向配方（建文档、移动、属性、日记、模板、数据库、搜索、快照、导出）。
- [`references/siyuan-api.md`](references/siyuan-api.md)：端点目录、块类型、SQLite schema、上传/移动/快照的契约与坑、
  `call` 白名单、`/mcp` 桥与 HTTP 缺口。
- [`references/note-practice.md`](references/note-practice.md)：**最佳笔记实践**——案例（卢曼卡片盒、常青笔记、PARA、
  思源写作流、渐进式总结）+ 四层架构 + 原子卡规范 + 维护体检 SQL + 反模式 + 30 天计划。
- [`references/limitations.md`](references/limitations.md)：**能力边界与限制**（硬限制、坑与对策、场景可行性速判、
  反模式清单、部署检查表、与 MCP 的分工）。

官方资料：SiYuan 仓库 `docs/API.zh-CN.md`、`docs/SY-FORMAT.zh-CN.md`、`docs/DOCUMENT-ADDRESSING.zh-CN.md`、
`docs/DATABASE-SUBITEMS.zh-CN.md`、`docs/TEMPLATE-MANAGER.zh-CN.md`；Docker 镜像自带用户指南 `/opt/siyuan/guide`。

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
