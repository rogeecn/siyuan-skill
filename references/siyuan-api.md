# SiYuan HTTP API 参考（实测 3.8.2）

目标：`$SIYUAN_API_URL` 指向的实例。
认证：`Authorization: Token $SIYUAN_TOKEN`（设置 → 鉴权 → API token）。
**公开 API 全部是 POST**，JSON body，响应包络：

```json
{ "code": 0, "msg": "", "data": null }
```

- `code != 0` 即失败，`msg` 是错误文案；HTTP 401 = token 无效。
- `code:0` 只表示「该接口没报错」，**不代表**索引/缓存/WebSocket/同步已更新。
- 官方权威文档：仓库 `docs/API.zh-CN.md`（本文档为其 3.8.x 实测摘要 + 本项目踩坑记录）。

---

## 1. 端点总览

### 1.1 笔记本

| 端点 | 请求体 | 说明 |
| --- | --- | --- |
| `/api/notebook/lsNotebooks` | `{}` | `{notebooks:[{id,name,icon,sort,closed}]}` |
| `/api/notebook/openNotebook` | `{notebook}` | 打开（加密库需额外解锁） |
| `/api/notebook/closeNotebook` | `{notebook}` | 关闭（关闭后不可读写） |
| `/api/notebook/createNotebook` | `{name}` | 返回新库 id |
| `/api/notebook/removeNotebook` | `{notebook}` | 删除（不可恢复，先快照） |
| `/api/notebook/renameNotebook` | `{notebook,name}` | |
| `/api/notebook/getNotebookConf` | `{notebook}` | `conf`：`name/sort/icon/closed/docCreateSavePath/dailyNoteSavePath/dailyNoteTemplatePath/sortMode/encrypted` |
| `/api/notebook/setNotebookConf` | `{notebook,conf}` | 整对象覆盖，先 get 再改 |
| `/api/notebook/reorder` | `{notebooks:[id...]}` | 笔记本排序 |

### 1.2 文档（filetree）

| 端点 | 请求体 | 返回 |
| --- | --- | --- |
| `/api/filetree/createDocWithMd` | `{notebook,path,markdown}` | `docId`；同 path 幂等；父级自动建 |
| `/api/filetree/createDocByParent` | `{notebook,parentID,id,title:{format:"plain",text},markdown,...}` | 新文档；标题可含 `/`（**3.8.2 无此端点**，官方 master 文档新增） |
| `/api/filetree/resolveDocPath` | `{notebook,path}` | 创建计划/版本信息（3.8.2 无） |
| `/api/filetree/renameDocByID` | `{id,title}` | |
| `/api/filetree/removeDocByID` | `{id}` | 删文档（进数据历史） |
| `/api/filetree/moveDocs` | `{fromPaths,toNotebook,toPath}` | 见 §5 |
| `/api/filetree/moveDocsByID` | `{fromIDs,toID}` | `toID`=文档/笔记本 id |
| `/api/filetree/reorderDocs` | `{id,previousID}` | 同级排序（3.8.2 无） |
| `/api/filetree/setSort` | `{notebook,pathSort}` | |
| `/api/filetree/setDocSortMode` | `{notebook,sortMode}` | 子文档排序方式 |
| `/api/filetree/getHPathByID` | `{id}` | `"/父/子"` |
| `/api/filetree/getPathByID` | `{id}` | `{notebook,path}`（`path` 含 `.sy`） |
| `/api/filetree/getIDsByHPath` | `{notebook,path}` | `[ids]` |
| `/api/filetree/listDocsByPath` | `{notebook,path}` | `{files:[{id,name,path,...}]}` （内部端点，CLI 用于 tree） |

### 1.3 块

| 端点 | 请求体 | 说明 |
| --- | --- | --- |
| `/api/block/insertBlock` | `{dataType,data,nextID?,previousID?,parentID?}` | 锚点至少一个，优先级 next > prev > parent |
| `/api/block/prependBlock` | `{dataType,data,parentID}` | 父块开头插入 |
| `/api/block/appendBlock` | `{dataType,data,parentID}` | 父块末尾追加 |
| `/api/block/updateBlock` | `{id,dataType,data,lockType?}` | 整块替换 |
| `/api/block/deleteBlock` | `{id}` | |
| `/api/block/moveBlock` | `{id,previousID?,parentID?}` | 块级移动 |
| `/api/block/foldBlock` / `unfoldBlock` | `{id}` | |
| `/api/block/getBlockKramdown` | `{id}` | `{id,kramdown}`，保留 IAL |
| `/api/block/getBlockDOM` | `{id}` | `{id,dom}`（内部端点，排查渲染用） |
| `/api/block/getChildBlocks` | `{id}` | 直接子块 |
| `/api/block/transferBlockRef` | `{fromID,toID,refIDs?}` | 转移引用 |

### 1.4 属性 / SQL / 模板 / 文件

| 端点 | 请求体 | 说明 |
| --- | --- | --- |
| `/api/attr/setBlockAttrs` | `{id,attrs}` | `custom-*` 自定义属性；空串=删除 |
| `/api/attr/getBlockAttrs` | `{id}` | `{name,alias,memo,bookmark,custom-*,...}` |
| `/api/query/sql` | `{stmt}` | `[{row}]`；写后索引有延迟 |
| `/api/sqlite/flushTransaction` | `{}` | 强制落 SQL 索引后立刻可查 |
| `/api/template/render` | `{id,path,mode?}` | `preview`/`editorInsert`；返回 DOM 与 `docTreePlan` |
| `/api/template/renderSprig` | `{template}` | 纯 Sprig 字符串渲染（日记路径/标题） |
| `/api/template/docSaveAsTemplate` | `{id,name}` | 文档存为模板 |
| `/api/file/getFile` | `{path}` | 工作空间内文件（如 `/data/templates/a.md`） |
| `/api/file/putFile` | `{path,isDir,file}` | 写文件 |
| `/api/file/readDir` | `{path}` | 列目录 |
| `/api/file/removeFile` / `renameFile` | `{path}` / `{src,dest}` | |

### 1.5 资源 / 导出 / 转换 / 系统

| 端点 | 请求体 | 说明 |
| --- | --- | --- |
| `/api/asset/upload` | multipart，字段名 **`file[]`** | 见 §4 |
| `/api/export/exportMdContent` | `{id,headingNumber?,preserveTaskMarkers?}` | `{hPath,content}`，有损 |
| `/api/export/exportResources` | `{paths,name?,merge?}` | 服务端打包导出 |
| `/api/convert/pandoc` | `{dir,args}` | 需内核自带 pandoc |
| `/api/system/version` | `{}` | `"3.8.2"` |
| `/api/system/bootProgress` | `{}` | 启动进度（容器刚起时轮询） |
| `/api/system/currentTime` | `{}` | 服务器毫秒时间戳 |
| `/api/notification/pushMsg` | `{msg,timeout}` | 桌面提示 |
| `/api/network/forwardProxy` | `{url,method,payload,headers}` | 内核代发请求（响应正文上限 32MiB） |

### 1.6 数据库（Attribute View）

`/api/av/renderAttributeView`、`getAttributeView`、`getAttributeViewPrimaryKeyValues`、`searchAttributeView`、
`setAttributeViewBlockAttr`、`addAttributeViewBlocks`、`removeAttributeViewBlocks`、`changeAttrViewLayout`、
`setAttrViewGroup`、`getAttributeViewFilterSort`、`setAttrViewFilters`、`setAttrViewSorts`、
`addAttributeViewKey`、`removeAttributeViewKey`、`sortAttributeViewKey`、`sortAttributeViewViewKey`、
`setAttrViewContextFilter`、`getCurrentAttrViewImages`。
用法与字段类型见 `references/siyuan-operations.md#9-数据库attribute-view`。

### 1.7 搜索条件组（保存的搜索）

`/api/storage/getCriteria`、`/api/storage/setCriterion`、`/api/storage/removeCriterion`。

### 1.8 本项目使用但官方未列入公开 API 的端点（可能随版本变化）

`/api/filetree/listDocsByPath`、`/api/search/searchTag`、`/api/tag/renameTag`、
`/api/search/fulltextSearchBlock`、`/api/ref/getBacklink`、`/api/ref/getGlobalBacklinks`、
`/api/ref/getGlobalBacklinkContexts`、`/api/block/getBlockDOM`、`/api/repo/*`、`/api/history/*`。
调用前先用 `/api/system/version` 记录版本，升级后回归一次。

---

## 2. 块类型速查（`blocks.type` / `subtype`）

| type | 含义 | subtype |
| --- | --- | --- |
| `d` | 文档 | |
| `p` | 段落 | |
| `h` | 标题 | `h1`..`h6` |
| `l` | 列表 | `u` 无序 / `o` 有序 / `t` 任务 |
| `i` | 列表项 | `u` / `o` / `t` |
| `b` | 引述 | |
| `callout` | 提示块 | `NOTE`/`TIP`/`IMPORTANT`/`WARNING`/`CAUTION` |
| `s` | 超级块 | (`sb_layout`=row/col) |
| `c` | 代码块 | 语言在 ial/DOM |
| `m` | 公式块 | |
| `t` | 表格 | |
| `tb` | 分隔线 | |
| `iframe` / `video` / `audio` | 媒体 | |
| `query_embed` | 嵌入块 | |
| `av` | 数据库 | |

---

## 3. SQL：schema 与常用查询

工作空间 SQLite（`temp/siyuan.db`）核心表：

```sql
blocks(id, parent_id, root_id, hash, box, path, hpath, name, alias, memo, tag,
       content, fcontent, markdown, length, type, subtype, ial, sort, created, updated)
attributes(id, name, value, type, block_id, root_id, box, path)
spans(id, block_id, root_id, box, path, content, markdown, type, ial)
assets(id, block_id, root_id, box, docpath, path, name, title, hash)
refs(id, def_block_id, def_block_parent_id, def_block_root_id, def_block_path,
     block_id, root_id, box, path, content, markdown, type)
stat(key, value)
blocks_fts(...)          -- FTS5，支持 match 语法
```

常用查询：

```sql
-- docId/blockId → 所属笔记本（权限审计关键一步）
select distinct box from blocks where id='<id>' or root_id='<id>' limit 1;

-- 文档结构（排查解析结果）
select id,type,subtype,sort,substr(content,1,40) from blocks
where root_id='<docId>' order by sort;

-- 内容搜索（纯子串，无分词）
select id, box, root_id, content, type from blocks
where content like '%<kw>%' limit 20;

-- FTS 分词搜索 + 列过滤
select id, root_id, content from blocks_fts where blocks_fts match '语法' limit 20;
select id from blocks_fts where blocks_fts match '{content}: 语法 AND {name}: 周报';

-- 未完成任务
select id, content from blocks
where type='i' and subtype='t' and markdown like '%[ ]%' and box='<nb>';

-- 按自定义属性筛选
select b.id, b.content, a.value from blocks b
join attributes a on a.block_id=b.id
where a.name='custom-progress' and a.value='30';

-- 反链
select block_id, root_id from refs where def_block_id='<id>';

-- 今天/最近
select id, box, content, updated from blocks where type='d'
order by updated desc limit 10;
```

- 单引号按 SQL 规则转义为两个单引号（脚本 `esc()`）。
- `fcontent` 是去掉标记的纯文本，模糊匹配比 `content` 更干净。
- 写入后立刻查可能查不到：`sleep 2` 或先 `POST /api/sqlite/flushTransaction {}`。

---

## 4. 上传契约（`/api/asset/upload`）

- multipart 字段名必须是 **`file[]`**（内核 `form.File["file[]"]`）。
  用 `files` / `files[]` 会得到 `code:0` + **空 `succMap`**，文件没落库 —— 静默失败。
- 不要手工设置 `Content-Type: multipart/form-data`（缺 boundary）；自行拼 boundary。
- 响应：

```json
{
  "errFiles": [],
  "succFiles": [{"index": 0, "name": "a.png", "path": "assets/a-xxxx.png"}],
  "succMap": {"a.png": "assets/a-xxxx.png"}
}
```

- `code:0` + 空 `succMap` + 空 `errFiles` 一律视为失败（CLI 会抛错）。
- 返回的 `assets/...` 直接写进 Markdown：`![alt](assets/a-xxxx.png)`。

---

## 5. 移动文档（3.8 实测）

```json
{"fromPaths": ["/20261117164312-l5bhqvc.sy"], "toNotebook": "<目标库 id>", "toPath": "/"}
```

- `fromPaths` 用 SQL `blocks.path`（存储路径，**已含 `.sy`**），不是 `hpath`。
- `toPath`：移到目标库根用 `"/"`；移到某父文档下用父文档的 `path`（含 `.sy`）。
- **不要传 `fromIDs`**：3.8.2 会触发内核 `PANIC RECOVERED: interface conversion: interface {} is nil, not []interface {}`，且静默不移动。
- `/api/filetree/moveDocsByID {"fromIDs":[...],"toID":"<笔记本 id>"}` 可跨库移动；`toID:""` 报 `invalid ID argument`。
- 移动后 SQL 索引有数秒延迟，`box` 可能仍是旧值，等 2–3 秒再判定。

---

## 6. 快照：repo key 前置

新工作区 `repo.key` 为空时：

```
Failed to create data snapshot: Please initialize the data repo key first in
[Settings - Account & Sync - Local Data Repo - Data repo key]
```

先 `POST /api/repo/initRepoKey {}` → `{"key":"..."}`（只显示一次，交给用户保存），之后：

- `POST /api/repo/createSnapshot {memo}`
- `POST /api/repo/getRepoSnapshots {page}` → `{snapshots,pageCount,...}`
- `POST /api/repo/checkoutRepo {id}`（整工作区回滚）

`/api/repo/listSnapshots` 在部分反代下响应被吞，用 `getRepoSnapshots`。

---

## 7. 反代兼容（重要）

部分部署前面有反向代理，会**吞掉某些端点的响应体**（HTTP 200 + 空 body），
看起来成功但 `data` 丢失。本 skill 全部改用等价可用端点：

| 被吞的端点 | 替代方案 |
| --- | --- |
| `/api/query/query` | `/api/query/sql` |
| `/api/search/fulltextSearchBlock` | 3.8.2 无此端点（空响应）；用 SQL `content like '%kw%'` 或 `blocks_fts match`（权限表/正则过滤自行在 SQL 里拼） |
| `/api/repo/listSnapshots` | `/api/repo/getRepoSnapshots` |
| `/api/tag/replaceTag` | `/api/tag/renameTag`（不支持删除标签） |
| `/api/dailynote/*` | `createDocWithMd` 到渲染后的日记路径 + `setBlockAttrs` 打 `custom-dailynote-yyyymmdd` |

客户端对「空响应」显式报错：`SiYuan 端点 <path> 返回空响应（可能被反向代理吞掉）`。

---

## 8. 行为语义与易错点

- `createDocWithMd` 同 `path` 幂等（返回已有 id）；`/a/b` 会顺带创建父文档 `a`。
- `updateBlock` 传**文档 ID** 只会替换首块，不是整篇替换；整篇请按块操作。
- `lsNotebooks` 包含已关闭库（`closed:true`）；权限审计同样覆盖。
- 内容搜索命中块的 `id` 是块 id，**文档 id 在 `root_id`**；对文档操作必须用 `root_id`。
- 写块接口可能在块树事务已提交、SQL 索引未完成时返回 `code:0`。
- 只有文档明确说明的接口才保证幂等，其余失败不要盲目重试（先读状态）。
- 加密笔记本需先解锁；关闭的库读写会失败，需 `openNotebook`。

---

## 9. Docker 联调环境

```bash
mkdir -p /tmp/siyuan-e2e/workspace/conf
echo '{"api":{"token":"e2e-token-123"}}' > /tmp/siyuan-e2e/workspace/conf/conf.json
docker run -d --name siyuan-e2e -p 16806:6806 \
  -v /tmp/siyuan-e2e/workspace:/siyuan/workspace \
  b3log/siyuan serve --workspace=/siyuan/workspace/ --accessAuthCode=e2e-pass
export SIYUAN_API_URL=http://127.0.0.1:16806 SIYUAN_TOKEN=e2e-token-123
```

镜像 entrypoint 会 chown 工作区并执行 `/opt/siyuan/kernel`，因此必须显式给 `serve` 子命令；
容器内无 python，测试脚本在宿主运行；空工作区首启约 3~5 秒，用 `/api/system/bootProgress` 等待。
镜像内自带官方用户指南：`/opt/siyuan/guide`（四语言 `.sy`），可 `docker cp` 出来做语法参考。

## 10. 本轮（1.2.0）实测新增/修正

| 端点 | 契约 | 备注 |
| --- | --- | --- |
| `/api/file/putFile` | multipart：`path`（相对工作区）、`isDir=false`、`file` | 写工作区文件；CLI 手写 multipart，不依赖 requests |
| `/api/file/readDir` | `{"path":"data/templates"}` | 返回 `[{name,isDir,isSymlink,updated}]` |
| `/api/file/getFile` | 返回**裸文件内容**（非 JSON 包络） | 失败时才是 `{code,msg}`，CLI 已兼容两种 |
| `/api/asset/statAsset` | `{"path":"assets/x.png"}`（**不带前导 `/`**，带 `/` 会 code=1） | 返回 `{size,hSize,created,hCreated,updated,hUpdated}` |
| `/api/export/exportMd` | `{"id"}` → `{"name","zip":"/export/x.md.zip"}` | **落文件**到工作区 `/export/`，不是内容 |
| `/api/export/exportMdContent` | `{"id"}` → `{"content"}` | 要 Markdown 文本用这个 |
| `/api/export/exportHTML` | `{"id","pdf":false}`，`pdf` 必填 | 缺 `pdf` 报 `Field [pdf] is required` |
| `/api/outline/getDocOutline` | `{"id":docId}` | 返回标题树（`nodeType/type/subType/blocks`） |
| `/api/ref/getBacklink` / `getBacklinkDoc` | `{"id","keyword?","sort?"}` | 无引用时 `data=null`（不是 `[]`） |
| `/api/block/getBlockDOM|getBlockKramdown|getChildBlocks|getBlockBreadcrumb|getBlockInfo` | `{"id"}` | 块的读接口；DOM 里能取到 `data-av-id` |
| `/api/attr/batchGetBlockAttrs` | `{"ids":"id1,id2"}`（**逗号字符串**） | 不是数组 |
| `/api/template/docSaveAsTemplate` | `{"id","name","overwrite"}` | 存到 `data/templates/<name>.md`，返回 `data=null` |
| `/api/template/renderSprig` | `{"template": "<内容>"}` | 内容里的字面 `{{ }}` 会被当 Go action 解析 → 必须先转义 |
| `/api/notebook/open|close|rename|setNotebookIcon` | `{"notebook"}`、`{"notebook","name"}`、`{"notebook","icon"}` | 成功 `data=null` |
| `/api/sync/performSync` | `{}` | 同步未启用时也返回 code 0，需看 `getSyncInfo` 的 `stat` |
| `/api/bookmark/getBookmark` | `{}` → `[{name,blocks:[...]}]` | 无书签返回 `[]` |
| `/api/repo/initRepoKey` | `{}` → `{"key": "..."}` | 快照前置；密钥务必备份 |
| `/api/import/importStdMd` | `{"notebook","localPath","toPath"}` | `localPath` 是**服务器**可见路径 |

**av（数据库）不要在 HTTP 层硬拼**：`/api/av/getAttributeView` 只读、`addAttributeViewKey` 返回 code 0 但不生效；
创建数据库 HTTP 根本无端点。CLI 的 `av *` 全部走 `/mcp` 的 `database` 工具；`avID ≠ blockID`，
需从 `getBlockDOM` 的 `data-av-id` 解析（CLI 自动做）。

## 11. 内置 MCP（`/mcp`）与 HTTP 的缺口

- 端点固定 `/mcp`，Streamable HTTP，`Authorization: Token <token>`，仅管理员；只读模式下拒绝 `POST/DELETE` 类工具。
- 握手：`initialize`（响应用头 `Mcp-Session-Id` 返回会话 id）→ `notifications/initialized` → `tools/call`。
  响应可能是 `application/json`，也可能是 SSE（`event: message` + `data: {...}`），两者都要解析。
- 3.8.2 共 **29 个工具**：`asset attr block bookmark dailynote database document export file history
  http_request image import inbox notebook outline ref repo search skill sql sync system tag template
  unzip web_fetch web_search workspace`。
- HTTP 没有、只能走 MCP 的能力：`database.create`、`skill*`（技能管理）、`web_fetch`/`web_search`、
  `http_request`、`unzip`、`workspace`、`inbox`、`image`（生图）、`history` 的细粒度回滚。
- CLI 的统一入口：`siyuan.py mcp <tool> --action <act> --arg k=v`（权限按动作名推断 R/W/D，可用 `--perm` 覆盖）。

## 12. `call` 白名单（`siyuan.py call`）

不在白名单的端点直接拒绝（提示看本节），避免 agent 手拼任意端点绕过权限语义。

| 权限 | 端点（节选） |
| --- | --- |
| `R` | `system/currentTime|version|getWorkspaces|getConf|bootProgress`、`outline/getDocOutline`、`ref/getBacklink(|Doc)`、`block/getBlockKramdown|getBlockDOM|getChildBlocks|getBlockBreadcrumb|getBlockIndex|getDocsInfo`、`attr/getBlockAttrs`、`export/exportMdContent|exportHTML`、`filetree/getDoc|getPathByID|getHPathByID|listDocTree`、`search/searchAsset|searchRefBlock`、`template/renderSprig`、`history/getHistoryItems|getHistoryDirs`、`repo/getRepoSnapshots|diffRepoSnapshots|openRepoSnapshotFile`、`riff/getRiffDecks|getRiffCards`、`av/getAttributeViewPrimaryKeyValues`、`notebook/lsNotebooks` |
| `W` | `block/insertBlock|prependBlock|appendBlock|updateBlock|foldBlock`、`attr/setBlockAttrs`、`filetree/createDocWithMd|renameDoc|moveDocsByID`、`file/putFile|renameFile|copyFile`、`export/exportMd|exportSY|exportDocx`、`template/docSaveAsTemplate`、`notebook/createNotebook|renameNotebook|setNotebookIcon`、`tag/renameTag`、`av/addAttributeViewKey|addAttributeViewBlocks`、`asset/uploadCloud`、`riff/createRiffDeck|addRiffCards` |
| `D`（需 `--yes`） | `block/deleteBlock`、`filetree/removeDoc(s)`、`file/removeFile`、`notebook/removeNotebook`、`tag/removeTag`、`asset/removeUnusedAsset`、`av/removeAttributeViewKey|removeAttributeViewBlocks`、`repo/purgeRepo`、`riff/removeRiffCards`、`history/rollbackDocByCreated` |

需要新端点：先在本机实例实测（空响应 = 3.8.2 没有），再把「端点 → 权限」加进 `CALL_TABLE`。
