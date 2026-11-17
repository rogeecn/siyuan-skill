# SiYuan HTTP API 实测记录

目标：`$SIYUAN_API_URL` 指向的 SiYuan 实例（实测 3.8.x）。
认证：请求头 `Authorization: Token $SIYUAN_TOKEN`，**所有端点都是 POST**。

响应包络：

```json
{ "code": 0, "msg": "", "data": ... }
```

`code != 0` 时 `msg` 为错误信息；HTTP 401 = token 无效。

## 端点

| 路径 | 请求体 | 返回 `data` | 用途 |
| --- | --- | --- | --- |
| `/api/system/version` | `{}` | `"3.8.2"` | 连通性检查 |
| `/api/notebook/lsNotebooks` | `{}` | `{notebooks:[{id,name,closed}]}` | 笔记本列表 |
| `/api/query/sql` | `{stmt}` | `[{...}]` | SQL 查询（替代被吞的 `/api/query/query`） |
| `/api/filetree/createDocWithMd` | `{notebook,path,markdown}` | `docId` | 创建文档 |
| `/api/export/exportMdContent` | `{id}` | `{content,...}` | 读文档 Markdown |
| `/api/block/appendBlock` | `{dataType:"markdown",data,parentID}` | `{...}` | 追加块 |
| `/api/block/updateBlock` | `{dataType:"markdown",data,id}` | `{...}` | 覆盖块 |
| `/api/filetree/moveDocs` | `{fromPaths:[],toNotebook,toPath}` | `null` | 移动文档（3.8 起只有 fromPaths 生效） |
| `/api/filetree/listDocsByPath` | `{notebook,path}` | `{files:[{id,name,path,...}]}` | 文档树 |
| `/api/search/searchTag` | `{k}` | `{tags:[...]}` | 标签列表（含 `<mark>`） |
| `/api/tag/renameTag` | `{oldLabel,newLabel}` | `null` | 重命名标签 |
| `/api/repo/initRepoKey` | `{}` | `{key}` | 初始化数据仓库密钥（快照前置） |
| `/api/repo/createSnapshot` | `{memo}` | `null` | 创建快照 |
| `/api/repo/getRepoSnapshots` | `{page}` | `{snapshots:[...],pageCount,...}` | 快照列表 |
| `/api/repo/checkoutRepo` | `{id}` | `null` | 回滚快照 |
| `/api/asset/upload` | multipart `file[]` | `{errFiles,succFiles,succMap}` | 上传附件 |

## 反代兼容（重要）

部分部署前面有反向代理，会**吞掉某些端点的响应体**（HTTP 200 + 空 body），
看起来像成功但 `data` 丢失。本 skill 全部改用等价可用端点：

| 被吞的端点 | 替代方案 |
| --- | --- |
| `/api/query/query` | `/api/query/sql`（搜索用 SQL `content LIKE`） |
| `/api/search/fulltextSearchBlock` | SQL `content like '%kw%'` |
| `/api/repo/listSnapshots` | `/api/repo/getRepoSnapshots` |
| `/api/tag/replaceTag` | `/api/tag/renameTag`（暂不支持删除标签） |
| `/api/dailynote/*` | `createDocWithMd` 到 `/YYYY-MM-DD`，已存在则 `appendBlock` |

客户端对「空响应」显式报错：`SiYuan 端点 <path> 返回空响应（可能被反向代理吞掉）`。

## 常用 SQL

```sql
-- docId/blockId → 所属笔记本（权限审计的关键一步）
select distinct box from blocks where id='<id>' or root_id='<id>' limit 1;

-- 内容搜索
select id, box, root_id, content, type from blocks
where content like '%<kw>%' limit 20;

-- 文档标题搜索
select id, box, content from blocks
where type='d' and content like '%<kw>%' limit 20;

-- 最近更新
select id, box, content, updated from blocks
where type='d' order by updated desc limit 10;

-- 今天的日记
select id from blocks
where type='d' and box='<notebook>' and content='YYYY-MM-DD' limit 1;
```

字符串里的单引号按 SQL 规则转义为两个单引号（脚本 `esc()`）。

## 上传契约（`/api/asset/upload`）

- multipart 字段名必须是 **`file[]`**（SiYuan `kernel/model/upload.go`：`form.File["file[]"]`）。
  用 `files` / `files[]` 会得到 `code:0` + **空 `succMap`**，文件没落库 —— 静默失败。
- 不要手工设置 `Content-Type: multipart/form-data`（缺 boundary）；脚本自行拼 boundary 并带上。
- 响应：

```json
{
  "errFiles": [],
  "succFiles": [{"index": 0, "name": "a.png", "path": "assets/a-xxxx.png"}],
  "succMap": {"a.png": "assets/a-xxxx.png"}
}
```

- `code:0` + 空 `succMap` + 空 `errFiles` 一律视为失败（脚本抛错）。
- 返回的 `assets/...` 路径直接用于 Markdown：`![alt](assets/a-xxxx.png)`。

## 备注

- `createDocWithMd` 的 `path` 必须以 `/` 开头；同名路径会新建另一篇文档（非幂等）。
- `updateBlock` 传文档 ID 时只会更新文档首块，不是整篇替换。
- `lsNotebooks` 包含已关闭（`closed:true`）的笔记本；权限审计同样覆盖它们。
- 删除标签在本部署不支持（`renameTag` 需 `newLabel` 非空）。
- 内容搜索 `select id, root_id, content, type from blocks where content like ...`：
  命中块的 `id` 是**块 id**，文档 id 在 `root_id`；对文档操作（read/append/update/move）必须用 `root_id`。
- `createDocWithMd` 的 `/a/b` 会顺带创建父文档 `a`，返回 `b` 的 id。

## 移动文档（3.8 实测）

```json
{"fromPaths": ["/20261117164312-l5bhqvc.sy"], "toNotebook": "<目标库 id>", "toPath": "/"}
```

- `fromPaths` 用 SQL `blocks.path`（存储路径，**已含 `.sy`**），不是 `hpath`。
- `toPath`：移到目标库根用 `"/"`；移到某父文档下用该父文档的 `path`（含 `.sy`）。
- **不要传 `fromIDs`**：3.8.2 会触发内核 `PANIC RECOVERED: interface conversion: interface {} is nil, not []interface {}`，且静默不移动。
- `/api/filetree/moveDocsByID {"fromIDs":[...],"toID":"<笔记本 id>"}` 可跨库移动；`toID:""` 报 `invalid ID argument`。
- `blocks` 的 SQL 索引有数秒延迟：移动后立刻查 `box` 可能仍是旧值，等 2–3 秒再判定。

## 快照前置：repo key

新工作区 `repo.key` 为空时，`createSnapshot` 报
`Failed to create data snapshot: Please initialize the data repo key first in [Settings - Account & Sync - Local Data Repo - Data repo key]`。
先 `POST /api/repo/initRepoKey {}`（返回 `{"key":"..."}`）或由 UI 生成密钥，之后 create/list/rollback 均可用（本项目 3.8.2 实测通过）。

## Docker 联调环境

```bash
mkdir -p /tmp/siyuan-e2e/workspace/conf
echo '{"api":{"token":"e2e-token-123"}}' > /tmp/siyuan-e2e/workspace/conf/conf.json
docker run -d --name siyuan-e2e -p 16806:6806 \
  -v /tmp/siyuan-e2e/workspace:/siyuan/workspace \
  b3log/siyuan serve --workspace=/siyuan/workspace/ --accessAuthCode=e2e-pass
export SIYUAN_API_URL=http://127.0.0.1:16806 SIYUAN_TOKEN=e2e-token-123
```

镜像内 entrypoint 会 chown 工作区并执行 `/opt/siyuan/kernel`，因此必须显式给 `serve` 子命令；容器内无 python，测试脚本在宿主运行。
