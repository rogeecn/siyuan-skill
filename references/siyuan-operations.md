# 思源操作配方（任务导向）

前提：已经通过 `scripts/siyuan.py`（或直接 curl）连上实例，且**权限表 reviewed 且对应笔记本有权限**。
所有端点都是 `POST`，请求头 `Authorization: Token $SIYUAN_TOKEN`，包络 `{code,msg,data}`，`code != 0` 即失败。

> 语法细节（块引用、IAL、超级块、表格……）见 `references/siyuan-markdown.md`；
> 端点全量与错误语义见 `references/siyuan-api.md`。

---

## 1. 先定位：笔记本 / 文档 / 块

```bash
# 笔记本（关闭的库也会列出来，closed=true）
curl -s -X POST "$SIYUAN_API_URL/api/notebook/lsNotebooks" -H "Authorization: Token $SIYUAN_TOKEN"

# 文档树（注意：顶层用 path="/"，子级用返回的 files[].path）
curl -s -X POST "$SIYUAN_API_URL/api/filetree/listDocsByPath" \
  -H "Authorization: Token $SIYUAN_TOKEN" -H 'Content-Type: application/json' \
  -d '{"notebook":"<nb>","path":"/<父id>.sy"}'
```

| 想知道 | 端点 |
| --- | --- |
| 文档的可读路径 | `/api/filetree/getHPathByID {id}` → `"/父/子"` |
| 文档的存储路径 | `/api/filetree/getPathByID {id}` → `{notebook, path}` |
| hpath → 文档 ID | `/api/filetree/getIDsByHPath {notebook, path}` |
| 块属于哪个笔记本 | SQL：`select distinct box from blocks where id='<id>' or root_id='<id>'` |

`blockId`、`docId`、`notebookId` 长得一样（`20261117171302-884qsee`），靠 `blocks.type` 区分：`d`=文档、`p`=段落、`h`=标题、`l/i`=列表/列表项、`b`=引述、`s`=超级块、`c`=代码、`t`=表格、`m`=公式、`av`=数据库、`tb`=分隔线、`query_embed`=嵌入块。

---

## 2. 新建文档

### 2.1 按路径建（最常用）

```bash
curl -s -X POST "$SIYUAN_API_URL/api/filetree/createDocWithMd" \
  -H "Authorization: Token $SIYUAN_TOKEN" -H 'Content-Type: application/json' \
  -d '{"notebook":"<nb>","path":"/项目/周报/2026-11-17","markdown":"# 内容\n\n正文"}'
```

- `path` 中的父文档不存在会**自动创建**（`/项目/周报/2026-11-17` 会连带建 `项目`、`周报`）。
- 同 `path` 重复调用不会覆盖，返回已存在文档的 ID（幂等，可安全重试）。
- 标题里要 `/` 时必须换成全角 `／`，否则会被当层级。

### 2.2 精确父级 / 标题含 `/`

3.8.2 只有 `createDocWithMd`，标题里的 `/` 会被当层级，需先替换成全角 `／`：

```json
{"notebook":"<nb>","path":"/项目/2026/11/11 复盘","markdown":"..."}
```

父文档不存在会被自动建（不想要的父级要先建好再用完整 path）。
官方 master 文档里的 `createDocByParent`（`{notebook,parentID,id,title:{format:"plain",text},markdown,tags,...}`，
`parentID:null`=根，标题可含 `/`）在 **3.8.2 不存在**，调用会返回空响应；升级内核后再用，并用 `/api/system/version` 先确认。

---

## 3. 写内容：四种写法怎么选

| 需求 | 端点 | 说明 |
| --- | --- | --- |
| 文档末尾追加 | `/api/block/appendBlock {dataType:"markdown", data, parentID:<docId>}` | 最安全，只加不改 |
| 文档开头插入 | `/api/block/prependBlock {..., parentID}` | 同上 |
| 在某个块前后插入 | `/api/block/insertBlock {data, previousID 或 nextID 或 parentID}` | 三个锚点至少一个；优先级 `nextID > previousID > parentID` |
| 整块替换 | `/api/block/updateBlock {id, dataType:"markdown", data}` | 会把该块（含子块）整体替换；`lockType:true` 时类型变化会被拒绝 |
| 删除块 | `/api/block/deleteBlock {id}` | 删文档用 `/api/filetree/removeDocByID {id}`，删库用 `/api/notebook/removeNotebook {notebook}` |
| 块内移动 | `/api/block/moveBlock {id, previousID 或 parentID}` | 跨文档移动用它 |
| 文档整体移动 | `/api/filetree/moveDocs` | 见第 4 节 |

要点：

- `dataType` 只支持 `"markdown"` / `"dom"`；**统一用 markdown**，DOM 需要 `data-node-id`，容易写坏。
- 一次请求写多个块，返回的 `data[0].doOperations[0].id` 只给首个顶层块；要逐个拿 ID 就一次写一个块。
- 写入成功后 SQL 索引可能滞后数秒（`code:0` 只代表块树已提交）。需要立刻读就 `getBlockKramdown`，不要立刻 `query/sql`。
- 想清空文档：`updateBlock` 传空字符串不行，改为列出子块逐个 `deleteBlock`。

---

## 4. 移动 / 重组

### 4.1 移动文档（跨库、换父级）

**3.8 起只认 `fromPaths`**：

```json
{
  "fromPaths": ["/20261117164311-rytxylt/20261117164311-nvtj1y6.sy"],
  "toNotebook": "20261117164305-88e17bt",
  "toPath": "/"
}
```

- `fromPaths`：`blocks.path`（以 `/` 开头、以 `.sy` 结尾）—— 先 SQL 查出来，不要拼 hpath。
- `toPath`：`"/"` 表示目标库根目录；放到某父文档下则传父文档的 `path`（同样带 `.sy`）。
- 传 `fromIDs` 会触发内核 panic（`interface conversion: interface {} is nil, not []interface {}`）且**静默不移动**。
- 等价端点：`/api/filetree/moveDocsByID {fromIDs, toID}`，`toID` 必须是**文档或笔记本 ID**，空字符串报 `invalid ID argument`。
- 移动后立刻查 `blocks.box` 可能是旧值，等 2~3 秒再校验。

### 4.2 排序

`/api/filetree/reorderDocs {id, previousID}`、`/api/filetree/setSort {notebook, pathSort}`、
`/api/filetree/setDocSortMode {notebook, sortMode}`（`15`=按更新时间等，见 `getNotebookConf.conf.sortMode`）。

---

## 5. 属性与标签

```bash
# 设置（自定义属性必须带 custom- 前缀；值统一用字符串）
curl -s -X POST "$SIYUAN_API_URL/api/attr/setBlockAttrs" \
  -H "Authorization: Token $SIYUAN_TOKEN" -H 'Content-Type: application/json' \
  -d '{"id":"<blockId>","attrs":{"custom-status":"doing","custom-due":"2026-12-01","bookmark":"P1"}}'

# 读取
curl -s -X POST "$SIYUAN_API_URL/api/attr/getBlockAttrs" -d '{"id":"<blockId>"}'
```

- 传 `{"custom-x": ""}` 是**删除**该属性。
- 内置可写键：`name`、`alias`、`memo`、`bookmark`、`title`（文档标题）、`icon`、`style`、`fold`。
- 标签是行内文本，不是属性：`renameTag {oldLabel,newLabel}` 重命名，`searchTag {k}` 列举（返回里带 `<mark>` 高亮片段）。
- 想「按属性筛选」用 SQL join `attributes`（见第 8 节）。

---

## 6. 日记（Daily Note）

思源没有公开的「创建日记」API，但它约定了配置与属性，照做就能和 UI 对齐：

1. 读笔记本配置：`/api/notebook/getNotebookConf {notebook}` → `conf.dailyNoteSavePath`、`conf.dailyNoteTemplatePath`
   - 路径本身是 Sprig 模板，例如 `"/daily note/{{now | date \"2006/01\"}}/{{now | date \"2006-01-02\"}}"`。
2. 渲染路径：`/api/template/renderSprig {template}`（纯字符串渲染，不需要文档上下文）。
3. 渲染模板正文：`/api/template/render {id:<目标文档id>, path:<模板文件绝对路径>}`，
   或用 `/api/file/getFile {path:"/data/templates/xxx.md"}` 拿原文再 `/api/template/renderSprig`。
4. `createDocWithMd {notebook, path:<渲染后的路径>, markdown:<模板正文>}`。
5. 打日记标记：`/api/attr/setBlockAttrs {id:<新docId>, attrs:{"custom-dailynote-YYYYMMDD":"1"}}`。
   - 属性名格式固定 `custom-dailynote-yyyymmdd`（如 `custom-dailynote-20261117`），UI 靠它识别日记。
6. 已存在就追加：SQL `select id from blocks where type='d' and box='<nb>' and hpath='<渲染路径>'`，命中则 `appendBlock`。

本仓库 CLI：`scripts/siyuan.py daily <notebook>`（已按上述流程实现路径模板与属性标记）。

---

## 7. 模板片段

- 存放目录：工作空间 `data/templates/*.md`（可用 `/api/file/readDir {path:"/data/templates"}` 列举，`/api/file/getFile` 读取）。
- 语法：Go `text/template` + Sprig，避免与思源 `{{...}}` 冲突，动作统一写成 `.action{ ... }`。
- 可用变量：`.title`、`.id`、`.name`、`.alias`；函数：
  `getHPathByID`、`queryBlocks`、`getBlock`、`querySpans`、`querySQL`、`statBlock`（`RuneCount`/`WordCount`）、
  `createDocTree`（生成子文档树计划）。
- 日期格式化用 Go 的参考时间：`{{now | date "2006-01-02 15:04:05"}}`。
- 渲染：`POST /api/template/render {id, path, mode:"preview"|"editorInsert"}`；
  预览只出计划不落盘，插入模式返回一次性计划令牌（30 秒内有效）。
- 保存现有文档为模板：`POST /api/template/docSaveAsTemplate {id, name}`。
- 注意：模板里 `httpGet` 等外部请求需要用户交互确认，后台/发布页面下不可用。

---

## 8. 搜索与 SQL

### 8.1 三种搜索

| 方式 | 用法 | 适用 |
| --- | --- | --- |
| 标签 | `/api/search/searchTag {k}` | 标签树/重命名 |
| SQL LIKE | `where content like '%kw%'` | 精确、可组合、但全表扫 |
| FTS5 | `where blocks_fts match 'kw'` | 分词检索，支持 `AND/OR/NOT`、`{content name memo}:` 列过滤、短语 `+` |

```sql
-- 全文（FTS）
select id, root_id, content from blocks_fts where blocks_fts match '语法' limit 20;
select id from blocks_fts where blocks_fts match '{content}: 语法 AND {name}: 周报';

-- 未完成任务（任务项 markdown 里是 [ ]）
select id, content from blocks
where type='i' and subtype='t' and markdown like '%[ ]%' and box='<nb>' limit 20;

-- 按自定义属性筛选
select b.id, b.content, a.value
from blocks b join attributes a on a.block_id=b.id
where a.name='custom-progress' and a.value='30';

-- 最近改动
select id, hpath, updated from blocks where type='d' and box='<nb>'
order by updated desc limit 20;

-- 今天建的文档
select id, hpath from blocks where type='d'
and updated > strftime('%Y%m%d%H%M%S','now','-1 day');

-- 某文档的块结构（排查解析结果）
select id, type, subtype, sort, substr(content,1,40) from blocks
where root_id='<docId>' order by sort;
```

写入后立刻查询可能查不到：等 2~3 秒，或先 `POST /api/sqlite/flushTransaction {}` 再查。

### 8.2 引用/反链

`/api/ref/getBacklink {id}`（反链列表）、`/api/ref/getGlobalBacklinkContexts`、
`/api/ref/getGlobalBacklinks`；SQL 侧可查 `refs` 表（`def_block_id`=被引用块，`block_id`=引用来源）。

---

## 9. 数据库（Attribute View）

模型：一个 `av` 块 = 一组数据（key 列表里是字段），多个视图（table/kanban/card）共享同一份数据、各自保存过滤排序。

| 操作 | 端点 |
| --- | --- |
| 读整库 | `/api/av/getAttributeView {id}`（`id` 为 av 块 ID） |
| 渲染 | `/api/av/renderAttributeView {id}` |
| 加条目 | `/api/av/addAttributeViewBlocks {avID, blockIDs, previousID, ignoreDefaultFill}` |
| 删条目 | `/api/av/removeAttributeViewBlocks {avID, blockIDs}` |
| 改单元格 | `/api/av/setAttributeViewBlockAttr {avID, keyID, rowID, value}` |
| 加/删字段 | `/api/av/addAttributeViewKey`、`/api/av/removeAttributeViewKey` |
| 视图布局 | `/api/av/changeAttrViewLayout {avID, layoutType:"table|kanban|card"}` |
| 分组/过滤/排序 | `/api/av/setAttrViewGroup`、`setAttrViewFilters`、`setAttrViewSorts` |
| 主键值/搜索 | `/api/av/getAttributeViewPrimaryKeyValues`、`/api/av/searchAttributeView` |

注意：

- 公开 API **没有「创建数据库块」** 的端点；一般在 UI/模板里建，或用 `/api/block/insertBlock` 插入已有的 av 块 ID（不推荐）。
- 条目分「绑定块」与「独立条目」，绑定块删除会影响条目；`blockID` 与 `avID` 语义不同，别混。
- 字段类型（select/multi-select/date/number/relation/rollup/template…）细节见官方 `docs/DATABASE-SUBITEMS.zh-CN.md`。

---

## 10. 资源文件

```bash
# 上传（multipart 字段名必须是 file[]，支持多个）
curl -s -X POST "$SIYUAN_API_URL/api/asset/upload" \
  -H "Authorization: Token $SIYUAN_TOKEN" \
  -F "assetsDirPath=/assets/" -F "file[]=@./pic.png"
# → {errFiles:[], succFiles:[{name,path}], succMap:{"pic.png":"assets/pic-20261117-xxxx.png"}}
```

- 返回的 `succMap` 值就是可写进 Markdown 的 `assets/...` 相对路径。
- 其它文件端点：`/api/file/getFile {path}`、`putFile`、`removeFile`、`renameFile`、`readDir`；
  导出资源：`/api/export/exportResources {paths}`。

---

## 11. 快照与数据历史

```bash
# 新工作区必须先初始化仓库密钥，否则 createSnapshot 报
# "Please initialize the data repo key first in [Settings - Account & Sync - Local Data Repo - Data repo key]"
curl -s -X POST "$SIYUAN_API_URL/api/repo/initRepoKey" -d '{}'      # → {key}
curl -s -X POST "$SIYUAN_API_URL/api/repo/createSnapshot" -d '{"memo":"before refactor"}'
curl -s -X POST "$SIYUAN_API_URL/api/repo/getRepoSnapshots" -d '{"page":1}'
curl -s -X POST "$SIYUAN_API_URL/api/repo/checkoutRepo" -d '{"id":"<snapshotId>"}'
```

- `initRepoKey` 返回的 key **只显示一次**，要交给用户保存；已初始化的工作区重复调用会报错或返回已有密钥。
- 回滚是整工作区覆盖，写操作前先建快照；批量改动前务必 `createSnapshot`。
- 数据历史（单文档历史版本）在 `data/history/`，UI 入口「数据历史」，公开 API 未提供。

---

## 12. 导出 / 转换

| 目的 | 端点 |
| --- | --- |
| 文档 → Markdown 文本 | `/api/export/exportMdContent {id, preserveTaskMarkers?}` → `{hPath, content}` |
| 文档/资源 → 文件（服务端打包） | `/api/export/exportResources {paths, name?, merge?}` |
| Pandoc 转换 | `/api/convert/pandoc {dir, args}`（需内核带 pandoc，参数为命令行数组） |

- `exportMdContent` 有损（见 markdown 文档第 7 节），做程序化编辑请用 `getBlockKramdown` + `updateBlock`。
- 导出整库不要循环几百次 `exportMdContent`，会拖慢内核；优先直接读 `data/<box>/` 下的 `.sy`（只读解析）。

---

## 13. 系统与可观测

| 端点 | 用途 |
| --- | --- |
| `/api/system/version` | 连通性/版本 |
| `/api/system/bootProgress` | 启动进度（刚起容器时轮询） |
| `/api/system/currentTime` | 服务器时间（写日记/时间戳对齐） |
| `/api/notification/pushMsg {msg,timeout}` | 桌面向导（用户可见提示） |
| `/api/notification/pushErrMsg` | 错误提示 |
| `/api/network/forwardProxy {url,method,payload,headers}` | 内核代发请求（跨域/内网） |

---

## 14. 并发与一致性（写操作必读）

- `code:0` 只表示该接口没报错，**不保证**索引、缓存、WebSocket 广播完成。
- 写块接口可能在块树事务已提交、SQL 索引异步完成之间返回 → 立刻 `query/sql` 可能读不到。
- 只有文档明确说明的接口才保证幂等：`createDocWithMd` 同 `path` 幂等，其余不要盲目重试。
- 批量写建议：一次一个块 → 失败可定位；每批之间 `sleep 0.1~0.3`；大批量前先 `createSnapshot`。
- 加密笔记本需先解锁；关闭的库 `lsNotebooks` 里 `closed=true`，读写会失败，需 `openNotebook`。

---

## 15. 本仓库 CLI 与配方的对应

| 配方 | CLI |
| --- | --- |
| 列库/权限 | `siyuan.py notebooks` / `perms show` |
| 文档树 | `siyuan.py tree [<nb>]` |
| 读文档 | `siyuan.py read <docId>` |
| 搜索 | `siyuan.py search <kw>` |
| 新建文档 | `siyuan.py create <nb> <path> -c <content>` |
| 追加/替换 | `siyuan.py append <docId> -c ...` / `siyuan.py update <blockId> -c ...` |
| 移动文档 | `siyuan.py move <nb> <docId...>`（3.8 `fromPaths` 契约） |
| 日记 | `siyuan.py daily <nb>` |
| 标签 | `siyuan.py tags <kw>` / `siyuan.py retag <old> <new>` |
| 上传 | `siyuan.py upload <file...>` |
| 快照 | `siyuan.py snapshot <create|list|rollback>` |
| 任意 SQL | `siyuan.py sql "<stmt>"` |
| 大纲 / 反链 | `siyuan.py outline <docId>` / `backlinks <id> [--by-doc]` |
| 块属性 | `siyuan.py attr get|set|batch` |
| 块操作 | `siyuan.py block kramdown|children|dom|breadcrumb|info|insert|delete` |
| 导出 | `siyuan.py export md|html|docx <docId>` |
| 模板 | `siyuan.py template ls|get|render|save-as|rm` |
| 工作区文件 | `siyuan.py file ls|read|write|mv|cp|rm` |
| 笔记本管理 | `siyuan.py notebook open|close|create|rename|icon` |
| 资源 | `siyuan.py asset unused|stat|clean` |
| 同步 | `siyuan.py sync status|perform` |
| 书签 | `siyuan.py bookmark ls|rename|rm` |
| 系统 | `siyuan.py system` |
| 数据库 | `siyuan.py av create|view|keys|search|add-item|add-key|rm-item|rm-key` |
| 内置 MCP | `siyuan.py mcp <tool> --action <act> --arg k=v` |
| 白名单端点 | `siyuan.py call <endpoint> --arg k=v [--notebook <id>]` |

---

## 16. P0 实战配方（社区高频场景）

### 16.1 收件箱投递（幂等）

把碎片想法投到「Inbox」笔记本，重复投递不产生重复文档。

```bash
# 1) 先查今天是否已有收件箱文档
python3 scripts/siyuan.py sql "select id,hpath from blocks where type='d' and box='<INBOX_NB>' and hpath like '/%2026-11-17%' limit 1"

# 2) 没有就建（路径幂等，日期即标题）
python3 scripts/siyuan.py create <INBOX_NB> "/2026-11-17 收件箱" -c "## 09:12 想法
- 内容"

# 3) 已有就追加
python3 scripts/siyuan.py append <docId> -c "
- **10:30** 第二条"
```

要点：`createDocWithMd` 对同一路径会复用文档（不传重复），这是幂等的关键；不要用随机标题。

### 16.2 日记任务（任务清单 + 未完成结转）

```bash
python3 scripts/siyuan.py daily <NB> -c "## 今日
- [ ] 写方案
- [ ] 回邮件"
# 昨天的未完成任务（嵌入块也能做到，见 siyuan-markdown.md）
python3 scripts/siyuan.py sql "select id from blocks where type='i' and markdown like '%- [ ]%' and box='<NB>' order by updated desc limit 5"
```

要点：任务列表导出是 `[X]`（大写）；`daily` 已按 `dailyNoteSavePath` + Sprig 生成路径，并写入 `custom-dailynote-YYYYMMDD` 属性。

### 16.3 仪表盘 / 日报聚合（只读）

```bash
# 各库文档数、最近 7 天更新
python3 scripts/siyuan.py sql "select box,count(*) c from blocks where type='d' group by box order by c desc"
python3 scripts/siyuan.py sql "select hpath,updated from blocks where type='d' and updated > strftime('%Y%m%d%H%M%S','now','-7 day') order by updated desc limit 20"
# 产出快照写入指定文档
python3 scripts/siyuan.py update <dashboardDocId> -c "## 周报
（粘贴上面的结果）"
```

要点：`update` 覆盖首块 —— 只对「自己刚读过的仪表盘文档」用；其它场景用 `append`。

### 16.4 体检 SQL（库健康）

```bash
# 无标签 / 无标题 / 很久没动的孤儿文档
python3 scripts/siyuan.py sql "select id,hpath from blocks where type='d' and tag='' "
python3 scripts/siyuan.py sql "select id,hpath from blocks where type='d' and content='' "
python3 scripts/siyuan.py sql "select hpath,max(updated) u from blocks where type='d' group by box having u < 20250101000000"
# 未引用资源（先看清单再清）
python3 scripts/siyuan.py asset unused
```

要点：`tag` 列只对块级标签生效；`updated` 是 `yyyyMMddHHmmss` 字符串，比较比大小即可。

### 16.5 文档查重与命名规范

```bash
python3 scripts/siyuan.py search "同一主题" --title "同一主题"
python3 scripts/siyuan.py sql "select id,hpath from blocks where type='d' and hpath like '/项目/%' and content like '%关键词%'"
```

要点：标题搜索只匹配文档；内容的 `docId` 是 `root_id`（CLI 已输出 `docId`+`blockId`）。命名建议 `YYYY-MM-DD 主题`，便于 `hpath like` 过滤。

### 16.6 大段中文/代码的传输

```bash
python3 scripts/siyuan.py append <docId> -f article.md     # 文件
cat article.md | python3 scripts/siyuan.py create <NB> /notes/x   # stdin
```

要点：**不要 `curl -d '{...中文...}'`**（乱码/截断），一律走 CLI 或 `--data-binary @file`。图片先 `upload` 再用返回的 `assets/xxx` 引用。

### 16.7 结构化数据 → 数据库

```bash
# 建库（走内置 MCP，HTTP 没有该端点）
python3 scripts/siyuan.py av create <docId> 任务表 \
  --keys '[{"name":"状态","type":"text"},{"name":"截止","type":"date"}]'
# 加条目（游离行）
python3 scripts/siyuan.py av add-item <blockId> --content "写文档" --detached
python3 scripts/siyuan.py av keys <blockId>
```

要点：`av` 的 `id` 传数据库块 id（CLI 会自动解析真正的 avID）；条目也可绑定已有块（`--block`）。

### 16.8 模板批量落地

```bash
python3 scripts/siyuan.py template render "会议纪要.md" --id <docId>   # 渲染变量
python3 scripts/siyuan.py create <NB> "/2026-11-17 周会" -f 渲染结果.md
```

要点：模板里的 `{{ }}` 会被 `template render` 自动转义（避免被当 Go action）；模板本身用 `.action{...}` 语法。

---

## 17. 什么时候该停下来问人 / 换工具

- 需要**单文档历史回滚** → `mcp history`；`snapshot` 是整库，代价大。
- 需要**数据库视图/关联/汇总字段** → 内建 MCP 的 `database` 或人工配置。
- 需要**实时协同**或**精细排版** → 人来做。
- 需要**事务性批量写** → 拆成幂等小步 + 快照，别无解。

限制完整清单见 `references/limitations.md`。
