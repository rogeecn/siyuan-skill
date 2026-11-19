---
name: siyuan
description: 操作远程 SiYuan（思源笔记）：搜索、读文档、创建/追加/更新、移动、文档树、日记、块属性/块操作、大纲/反链、模板、导出、工作区文件、资源、书签、系统信息、数据库（av，走内置 MCP）、只读 SQL、快照、标签、图片上传。当用户说「操作 SiYuan / 思源笔记 / 在思源里搜一下 / 写进思源 / 记到思源 / 追加到某篇文档 / 上传图片到思源 / 看最近笔记 / 做个快照 / 回滚快照」时使用。凭据只从环境变量读取（SIYUAN_API_URL / SIYUAN_TOKEN），权限表存于 ~/.config/.siyuan.json 并按实例地址分组；所有笔记本数据操作前必须通过 R/W/D 权限 GUARD，首次同步后必须先引导用户确认权限。
version: 1.2.0
---

# SiYuan 远程笔记操作 (siyuan)

直接调用 SiYuan HTTP API（`/api/*`，`Authorization: Token` 头），外面包一层**笔记本级 R/W/D 权限 GUARD**。
纯 Python stdlib，所有命令都走 `scripts/siyuan.py`。

**两条硬规则：**

1. **实例地址与 token 只从环境变量读**，不写进代码/文档/回复，不回显 token。
2. **任何笔记本数据操作前先过 GUARD 审计**。审计拒绝时不得绕过、不得自行提权，把拒绝原因原样告诉用户。

> 用户说「备忘录/笔记」时先判断是不是 SiYuan（思源）；用户明确说 SiYuan / 思源时用本 skill，不要用本地 markdown 文件代替。

## 前置：环境变量

```bash
echo "$SIYUAN_API_URL"   # 如 https://siyuan.example.com（结尾斜杠会被规范化去掉）
echo "$SIYUAN_TOKEN"     # SiYuan → 设置 → 关于 → API token
```

- 两个变量都必须存在；缺失时脚本报错并给出修复提示。
- 可选：`SIYUAN_CONFIG`（权限表路径，默认 `~/.config/.siyuan.json`）、`SIYUAN_TIMEOUT`（默认 120 秒）。
- 临时覆盖：`--url` / `--key` / `--config`。
- 配置文件中 `servers.<url>.token` 可作 token 兜底，但**不要主动把 token 写进配置**。

## 首次使用：先确认权限，再操作（必须遵守）

权限表按实例地址分组，存放在 `~/.config/.siyuan.json`：

```json
{
  "version": 1,
  "servers": {
    "https://siyuan.example.com": {
      "token": "",
      "reviewed": false,
      "reviewedAt": null,
      "notebooks": [
        { "id": "20260101120000-abcdef", "name": "个人笔记", "permissions": ["R"], "present": true }
      ]
    }
  }
}
```

**第一次接触某个实例时，严格按下面顺序执行，不要跳步：**

```bash
S=scripts/siyuan.py

python3 $S ping            # 连通性检查 + 首次同步（审计豁免）；新库一律写入 ["R"]
python3 $S perms show      # 把权限表展示给用户
```

然后**停下来向用户确认**（用 ask_user 或直接提问）：每个笔记本要授予哪些权限？

- 默认是 `["R"]`（只读）。不要为了让任务跑通擅自加 W/D。
- 权限可自由组合 `R,W,D`；`["NONE"]` = 禁止一切操作。
- 根据用户答复执行：

```bash
python3 $S perms set <notebookId> R,W      # 允许读+写
python3 $S perms set <notebookId> R,W,D    # 再允许破坏性操作
python3 $S perms set <notebookId> NONE     # 完全禁止
python3 $S perms confirm                   # 用户明确同意后才执行
```

`perms confirm` 之后数据操作才会放行。**在用户没有明确确认权限表前，绝对不要执行 `perms confirm`。**
若用户暂时不想配置，只做只读操作也仍需 confirm（默认 R 已足够读）。

首次同步后 `reviewed=false`，除 `ping` / `notebooks` / `perms` / `selftest` 外一律被拒绝，拒绝消息里会带完整引导。

## 权限模型

| 权限 | 含义 | 典型操作 |
| --- | --- | --- |
| `R` | 读 | search / read / tree / recent / tags / snapshot list |
| `W` | 写 | create / append / update / daily / upload / retag（全库） |
| `D` | 破坏性、不可逆 | rollback（全库）、move 移出源库 |
| `NONE` | 禁止一切 | 不计入任何「任一 R/W/D」，也永不满足「所有」类要求 |

> `move` 以「源库 D + 目标库 W」逐个 docId 审计；`--to-doc` 指定目标父文档，缺省移到目标库根。

审计规则：

| 操作 | 规则 |
| --- | --- |
| `read` | docId 反查所属笔记本（`blocks.box`），需 `R`；反查失败 = 拒绝 |
| `append` / `update` | docId 反查所属笔记本，需 `W`；反查失败 = 拒绝 |
| `create` / `daily` / `upload` | 目标笔记本 `W` |
| `move` | 源笔记本 `D` + 目标笔记本 `W`（逐个 docId 反查） |
| `retag`（batch_replace_tag） | **所有**笔记本 `W` |
| `rollback` | **所有**笔记本 `D`（回滚覆盖全库） |
| `snapshot create` | 任一笔记本 `R`（只读备份） |
| `search` / `tree`（无目标）/ `recent` / `tags` / `snapshot list` | 任一笔记本 `R` |
| `tree <notebook>` | 该笔记本 `R` |
| `outline` / `backlinks` / `block kramdown|children|dom|breadcrumb|info` / `attr get|batch` / `export md|html` / `av view|keys|search` | 该块所属笔记本 `R` |
| `attr set` / `block insert`（带 `--parent`）/ `template save-as` / `av add-item|add-key` | 该块所属笔记本 `W` |
| `block delete` / `av rm-item|rm-key` | 该块所属笔记本 `D`（`block delete` 需 `--yes`） |
| `file ls|read` / `template ls|get|render` / `asset unused|stat` / `sync status` / `bookmark ls` / `system` | 任一笔记本 `R` |
| `file write|mv|cp` / `notebook *` / `import` / `asset clean` | 任一笔记本 `W`（`notebook` 针对具体库时要求该库 `W`） |
| `asset netimg`（网络资源转本地） | 该块所属笔记本 `W`（改写文档链接） |
| `file rm` / `template rm` / `sync perform` / `bookmark rm` / `asset clean`（实际删除） | 任一笔记本 `D`，且需 `--yes` |
| `mcp <tool>` / `call <endpoint>` | 按工具/端点映射 R/W/D；D 类需 `--yes`，显式 `--perm` 可覆盖 |
| `ping` / `notebooks` / `perms` / `selftest` | 审计豁免（发现新库的入口） |

拒绝消息包含：目标笔记本（id+name）、需要的权限、缺失的权限、操作名。退出码：`2` = 权限/确认拒绝，`1` = 连接/参数错误。

权限表按需同步：常见按库读写不再额外拉笔记本列表；仅当库不在表中（新建库）或执行「全库」判定（retag / rollback）时才同步一次，新库自动按默认 `R` 记入；`ping` / `notebooks` 可随时手动刷新。

## 写笔记时的实践（摘要）

动手前先看一眼 `references/note-practice.md`，核心只有五条：

1. **收集不算学习**——存下来只给「进步的感觉」；写不出「我打算什么时候用它」就别收。
2. **标题写成断言**：`块引用比标签更适合表达关系`，而不是 `关于块引用的思考`；标题即 API，可被 `(( ))` 引用。
3. **一卡一意**：100–500 字、自己的话、有边界条件、文末一行来源。
4. **碎片进日记，不进新文档**；靠反链（`backlinks`）在需要时召回，而不是提前归类。
5. **有状态的东西放数据库**（项目/任务/书单），知识本体放卡 + MOC；标签只当临时脚手架。

给 agent 的边界：只做搬运/查重/体检/格式化；摘要、判断、取舍留给用户；只 `append` 不 `update`；批量改动先建快照并列出清单等确认。

## 能力边界（动手前先看一眼）

| 类别 | 边界 | 你怎么做 |
| --- | --- | --- |
| 权限 | GUARD 是**防误操作**，token 本身是管理员全权 | 不把 CLI 暴露给不可信调用方；敏感库设 `NONE` |
| 一致性 | 无事务、无锁、last-write-wins；索引异步数秒 | 先 `read` 再改；写后不要立刻用 SQL 断言；大改先 `snapshot create` |
| 快照 | 整库级，无法单库/单文档回滚 | 回滚前必须给预览并取得用户同意 |
| 导出 | `export md` 有损（补标题、丢 IAL、压表格） | 要源码用 `block kramdown` |
| 数据库 | HTTP 无建库端点，只能走 MCP | 用 `av *`（已内置 MCP 桥） |
| 传输 | `curl` 内联中文 JSON 会乱码 | 一律走 CLI（`-c`/`-f`/stdin） |
| 在线 | 内核不在线 = 无数据层；只读模式下写请求被拒 | 先 `ping`，别把 403 当权限表问题 |

完整清单、场景可行性速判、反模式、部署检查表：`references/limitations.md`。

### 什么时候不用 CLI

- 需要实时协同编辑、精细排版 → 交给人。
- 需要数据库视图/关联/汇总等高级字段 → 内建 MCP `database` 或人工。
- 需要单文档历史回滚 → `mcp history`（`snapshot` 是整库）。

## 命令速查

```bash
S=scripts/siyuan.py

python3 $S ping
python3 $S notebooks                        # 列出笔记本 + 同步权限表（新库补 R）
python3 $S perms show|set <id> R,W|confirm
python3 $S selftest                         # 权限矩阵纯函数自检（无网络）

python3 $S search "关键词" [-n 10] [--title "标题词"]
python3 $S read <docId>
python3 $S create <notebookId> /notes/new -f article.md
python3 $S append <docId> -c "追加内容"
python3 $S update <docId> -f new.md
python3 $S move <目标笔记本ID> <docId...> [--to-doc <父文档ID>]
python3 $S tree [notebookId]
python3 $S daily <notebookId> -c "今天……"
python3 $S recent [-n 10]
python3 $S sql "select id,hpath from blocks where type='d' order by updated desc limit 5"

python3 $S snapshot list [--page 1]
python3 $S snapshot create [--memo "备份说明"]
python3 $S snapshot rollback <snapshotId> [--yes]   # 不加 --yes 只预览

python3 $S tags
python3 $S retag <旧标签> <新标签>          # 需所有库 W
python3 $S upload <notebookId> a.png b.jpg  # 需目标库 W

# ── 数据层通用能力（对齐内置 MCP 工具面）──
python3 $S outline <docId>                    # 文档大纲（R）
python3 $S backlinks <id> [--by-doc]          # 反链（R）
python3 $S attr get <id>                      # 块属性（R）
python3 $S attr set <id> name=标题 custom-progress=30   # 属性（W）
python3 $S block kramdown <id>                # 精确源码（含 IAL）
python3 $S block children|dom|breadcrumb|info <id>
python3 $S block insert --parent <id> -f part.md        # 插入块（W）
python3 $S block delete <id> --yes                      # 删除块（D）
python3 $S export md <docId> > out.md         # 导出 Markdown（R）
python3 $S export html <docId> | jq -r .content
python3 $S export docx <docId> -o out.docx    # 写工作区文件（W）
python3 $S template ls|get|render|save-as|rm  # data/templates（渲染自动转义 {{ }}）
python3 $S file ls|read|write|mv|cp|rm <path> # 工作区文件（data/ 等）
python3 $S notebook open|close|create|rename|icon
python3 $S asset unused|stat|clean            # 资源统计/清理（clean 需 --yes）
python3 $S asset netimg <docId|blockId> [--all] [--referer URL]   # 网络图片/资源转本地（W）
python3 $S sync status [perform --yes]        # 同步状态（perform 为 D）
python3 $S bookmark ls|rename|rm
python3 $S system                             # 版本/工作区/当前时间
python3 $S av create <parentId> 任务表 --keys '[{"name":"状态","type":"text"}]'
python3 $S av view|keys|search|add-item|add-key|rm-item|rm-key
python3 $S import <notebookId> /abs/path [--target /归档]   # 服务器本地已同步目录
python3 $S mcp <tool> --action <act> --arg k=v              # 内置 MCP 工具直调
python3 $S call <endpoint> --arg k=v [--notebook <id>] [--yes]  # 白名单端点
```

正文输入：`-c/--content`、`-f/--file`（`-` = stdin），或无参数时从管道读：

```bash
cat article.md | python3 $S create 2026... /notes/article
```

`search` / `read` / `notebooks` / `perms show` / `snapshot list` / `upload` / `tree` / `sql` 等支持 `--json`。
所有子命令支持 `-h`。`sql` 只放行 `SELECT` / `WITH`，写操作必须走专门命令（否则权限语义会被绕过）。

## 写正文：语法速查（完整版见 references/siyuan-markdown.md）

| 想要 | 写法 | 易错点 |
| --- | --- | --- |
| 加粗/斜体/删除线 | `**粗**` `*斜*` `~~删~~` | 单个 `~x~` 是**下标** |
| 高亮/上下标/按键 | `==标记==` `<sup>x</sup>` `<sub>x</sub>` `<kbd>Ctrl</kbd>` | |
| 行内代码/公式 | `` `code` `` `$E=mc^2$` | |
| 标签 | `#项目/子标签#` | 层级用 `/`，不是块属性 |
| 颜色/背景 | `**文本**{: style="color: var(--b3-font-color1);"}` | 裸文本 + IAL 不生效，必须先套标记 |
| 提示块 | `> [!NOTE] 标题` + `> 内容` | 类型：NOTE/TIP/IMPORTANT/WARNING/CAUTION |
| 任务 | `- [ ] 待办` / `- [x] 完成` | 导出统一成 `[X]` |
| 表格 | `\| a \| b \|` + `\| :-- \| :-: \|` | 单元格内换行别用 `<br>` |
| 代码/脑图/流程图 | ```` ```js ```` / ```` ```mindmap ```` / ```` ```mermaid ```` | |
| 公式块 | `$$` 单独成行包住 | |
| 超级块横排/竖排 | `{{{row` / `{{{col`，子块间**空行**分隔，`}}}` 收尾 | 用 `---` 分隔会多出分隔线块 |
| 嵌入块（动态查询） | `{{SELECT * FROM blocks WHERE ...}}` 独占一行 | |
| 块引用 | `((块id "锚文本"))`（单引号=动态锚） | `[[` 只搜文档 |
| 块超链接 | `[文字](siyuan://blocks/<id>?focus=1)` | |
| 块属性 | 下一行 `{: name="x" alias="a,b" memo="m" custom-k="v"}` | 文档属性要用 `setBlockAttrs`，IAL 里写 `title` 无效 |

**不支持**：脚注 `[^1]`、定义列表（`术语` + `: 定义`）、行内 HTML 样式（只有块级 HTML 生效）。

**读改写要点**：`read` 走 `exportMdContent`，有损（补 `# 标题`、去 IAL、压表格）；需要精确源码用
`python3 $S sql "..."` 定位块后调 `/api/block/getBlockKramdown`，改写用 `updateBlock` / `appendBlock`，写完回读校验。
写入后 SQL 索引有数秒延迟，别用「立刻查不到」判定失败。

## 常用工作流

### 1. 搜索 → 读 → 追加

```bash
python3 $S search "项目复盘" -n 5
python3 $S read 20260101120000-abcdef
python3 $S append 20260101120000-abcdef -c "## 补充\n\n- 结论……"
```

### 2. 发布图文文章（先传图再建文档）

```bash
python3 $S upload <notebookId> cover.png diagram.svg
# 输出：cover.png → assets/cover-xxxx.png
python3 $S create <notebookId> /articles/my-post -f post.md
# post.md 中直接写 ![cover](assets/cover-xxxx.png)
```

`upload` 一次最多 50 个文件；返回 `code=0` 但空 `succMap` 会被脚本判为失败（反代/字段名回归的静默失败症状）。

### 3. 归档 / 整理标签

```bash
python3 $S tags
python3 $S search "旧主题" --json
python3 $S retag 旧标签 已归档
```

### 4. 备份与回滚

```bash
python3 $S snapshot create --memo "批量整理前"
python3 $S snapshot list
python3 $S snapshot rollback <id>          # 先看预览
python3 $S snapshot rollback <id> --yes    # 用户明确同意后再执行
```

## 遇到权限拒绝时

- 原样转述：目标笔记本、需要的权限、缺失的权限、操作名。
- 给出修复命令（`perms set`），让**用户决定**是否提权；不要自行 set/confirm。
- docId 反查失败（`无法反查所属笔记本`）说明 ID 错误或该块不可见：先 `search`/`tree` 确认 ID，不要猜。
- 全局操作（`retag` / `rollback`）要求所有库都有 W/D，一个 `NONE` 库就会拒绝；这是设计而非 bug。

## 安全与礼貌

- 默认权限只有 R；`W`/`D` 必须由用户显式授予。
- `rollback` 不可逆，执行前必须先给预览并取得用户确认；`retag` 会改动全库标签，先 `tags`/`search` 核对。
- 不要连续刷多条文档：同类小改动合并成一次 `append`。
- 不把 token 写进文档、memo 正文或回复；不把实例地址硬编码进脚本。
- 网络失败先 `ping`；401 = token 失效；空响应多为反代吞 body（见 `references/siyuan-api.md`）。

## 文件

- `scripts/siyuan.py` — 全部实现（HTTP + 权限 GUARD + 权限表）
- `references/siyuan-markdown.md` — **写正文必读**：行级/块级语法、超级块、IAL、调用约定、易错清单
- `references/siyuan-operations.md` — 任务配方：建文档/移动/属性/日记/模板/搜索/数据库/资源/快照/导出
- `references/siyuan-api.md` — 端点全量、块类型、SQL schema、上传契约、反代坑、docker 联调、`call` 白名单
- `references/limitations.md` — **能力边界与限制**：硬限制、坑与对策、场景可行性、反模式、部署检查表
- `references/note-practice.md` — **最佳笔记实践**：5 条底线、案例速览（卢曼卡片盒/常青笔记/PARA/思源写作流/渐进式总结）、四层架构、命名规范、日记流、原子卡规范、双链 vs 标签 vs 数据库、维护体检 SQL、反模式、30 天计划
- `test/smoke.py` — mock 服务端到端自检（`python3 test/smoke.py`）
