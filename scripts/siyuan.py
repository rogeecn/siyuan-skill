#!/usr/bin/env python3
"""siyuan.py — SiYuan 笔记远程操作 CLI（纯 Python stdlib）。

连接信息只从环境变量读取：
  SIYUAN_API_URL   实例地址，如 http://127.0.0.1:6806
  SIYUAN_TOKEN     API token（SiYuan → 设置 → 关于 → API token）

权限表默认存放于 ~/.config/.siyuan.json，**按实例地址分组**：
  {
    "version": 1,
    "servers": {
      "http://127.0.0.1:6806": {
        "token": "",                 # 可选：未设 SIYUAN_TOKEN 时的兜底
        "reviewed": false,           # 首次同步后必须人工确认
        "reviewedAt": null,
        "notebooks": [
          {"id": "...", "name": "...", "permissions": ["R"], "present": true}
        ]
      }
    }
  }

权限模型：R（读）/ W（写）/ D（破坏性、不可逆），可自由组合；
["NONE"] 表示禁止一切操作，不计入任何 "任一 R/W/D" 集合，也永不满足 "所有" 类要求。

所有笔记本数据操作前都经过 GUARD 审计；首次同步后 reviewed=false，
除 ping/notebooks/perms/selftest 外一律拒绝，直到 `perms confirm`。

子命令：
  ping / notebooks / perms {show,set,confirm} / selftest
  search / read / create / append / update / move / tree / daily / recent / sql
  snapshot {list,create,rollback} / tags / retag / upload

S=scripts/siyuan.py; python3 $S <cmd> -h
"""

from __future__ import annotations

import argparse
import json
import mimetypes
import os
import re
import sys
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timezone
from pathlib import Path

DEFAULT_CONFIG = Path(
    os.environ.get("SIYUAN_CONFIG", "~/.config/.siyuan.json")
).expanduser()
VALID_PERMS = ("R", "W", "D")
MAX_UPLOAD_FILES = 50
PROG = sys.argv[0] or "siyuan.py"


# ── 错误 ─────────────────────────────────────────────────────────────────────


class SiYuanError(RuntimeError):
    """HTTP / API / 配置类错误。"""


class PermissionDenied(Exception):
    """GUARD 拒绝（含权限不足、未知笔记本、块反查失败、权限表未确认）。"""


def fail(msg: str, code: int = 1):
    print(f"❌ {msg}", file=sys.stderr)
    sys.exit(code)


def warn(msg: str):
    print(f"提示: {msg}", file=sys.stderr)


def print_json(obj):
    print(json.dumps(obj, ensure_ascii=False, indent=2))


# ── 连接配置 ─────────────────────────────────────────────────────────────────


def normalize_url(url: str) -> str:
    return url.strip().rstrip("/")


class Connection:
    def __init__(self, url: str, token: str):
        self.url = normalize_url(url)
        self.token = token.strip()
        self.timeout = float(os.environ.get("SIYUAN_TIMEOUT", "120") or 120)

    def validate(self) -> "Connection":
        if not self.url:
            raise SiYuanError(
                "缺少环境变量 SIYUAN_API_URL（实例地址）。\n"
                "  在 ~/.zshenv 或 shell profile 中设置：\n"
                "    export SIYUAN_API_URL=https://<你的实例地址>\n"
                "    export SIYUAN_TOKEN=<API token>   # SiYuan → 设置 → 关于 → API token"
            )
        if not self.token:
            raise SiYuanError(
                "缺少 API token：设置环境变量 SIYUAN_TOKEN，"
                "或在配置文件的 servers.<url>.token 中填写兜底值。"
            )
        return self


def load_store(path: Path = DEFAULT_CONFIG) -> dict:
    """读取权限表；不存在/损坏则返回空骨架（不落盘）。"""
    if path.exists():
        try:
            data = json.loads(path.read_text("utf-8"))
            if isinstance(data, dict) and isinstance(data.get("servers"), dict):
                data.setdefault("version", 1)
                return data
            warn(f"配置文件结构异常，按空表处理: {path}")
        except (OSError, json.JSONDecodeError) as e:
            warn(f"配置文件读取失败（{e}），按空表处理: {path}")
    return {"version": 1, "servers": {}}


def save_store(store: dict, path: Path = DEFAULT_CONFIG):
    if not path.parent.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(path.parent, 0o700)
        except OSError:
            pass
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(store, ensure_ascii=False, indent=2) + "\n", "utf-8")
    try:
        os.chmod(tmp, 0o600)
    except OSError:
        pass
    os.replace(tmp, path)


def server_entry(store: dict, url: str) -> dict:
    """取（或创建）某实例的权限记录。新建即视为未确认。"""
    entry = store["servers"].get(url)
    if entry is None:
        entry = {
            "token": "",
            "reviewed": False,
            "reviewedAt": None,
            "notebooks": [],
        }
        store["servers"][url] = entry
    entry.setdefault("token", "")
    entry.setdefault("reviewed", False)
    entry.setdefault("reviewedAt", None)
    entry.setdefault("notebooks", [])
    return entry


def resolve(args, store: dict) -> tuple[Connection, dict]:
    url = normalize_url(
        args.url or os.environ.get("SIYUAN_API_URL", "") or ""
    )
    if not url:
        raise SiYuanError(
            "缺少环境变量 SIYUAN_API_URL（实例地址）。\n"
            "  export SIYUAN_API_URL=https://<你的实例地址>\n"
            "  export SIYUAN_TOKEN=<API token>"
        )
    entry = server_entry(store, url)
    key = (args.key or os.environ.get("SIYUAN_TOKEN", "") or entry.get("token") or "").strip()
    if not key:
        raise SiYuanError(
            f"缺少 API token：设置环境变量 SIYUAN_TOKEN，"
            f"或在 {args.config} 的 servers[\"{url}\"].token 中填写兜底值。"
        )
    return Connection(url, key), entry


# ── 权限矩阵（纯函数） ───────────────────────────────────────────────────────


def perms_of(nb: dict) -> set[str]:
    raw = nb.get("permissions") or []
    if "NONE" in raw:
        return set()
    return {p for p in raw if p in VALID_PERMS}


def active_notebooks(entry: dict) -> list[dict]:
    """只审计当前仍存在的笔记本（present 缺省视为存在）。"""
    return [nb for nb in entry.get("notebooks", []) if nb.get("present", True)]


def audit_notebook(
    entry: dict, notebook_id: str, required: list[str]
) -> tuple[bool, dict | None, list[str]]:
    nb = next((n for n in entry.get("notebooks", []) if n.get("id") == notebook_id), None)
    if nb is None:
        return False, None, list(required)
    perms = perms_of(nb)
    missing = [p for p in required if p not in perms]
    return not missing, nb, missing


def audit_any(entry: dict, perm: str) -> bool:
    return any(perm in perms_of(nb) for nb in active_notebooks(entry))


def audit_all(entry: dict, perm: str) -> bool:
    nbs = active_notebooks(entry)
    return bool(nbs) and all(perm in perms_of(nb) for nb in nbs)


def deny_message(
    nb: dict | None,
    required: list[str],
    missing: list[str],
    operation: str,
    notebook_id: str = "",
) -> str:
    if nb is None:
        target = f"笔记本 {notebook_id}（未记录在权限表中）" if notebook_id else "未知笔记本"
    else:
        target = f"笔记本 {nb['id']}（{nb.get('name', '')}）"
    return (
        f"权限拒绝：{target} 执行「{operation}」需要 "
        f"{'/'.join(required) or '（无）'}，缺失 {'/'.join(missing) or '（无）'}"
    )


# ── 权限表同步 ───────────────────────────────────────────────────────────────


def sync_notebooks(entry: dict, api_notebooks: list[dict]) -> list[str]:
    """把 API 返回的笔记本并入权限表：新笔记本补 ["R"]；name 变化则更新；标记 present。"""
    added = []
    seen = set()
    for nb in api_notebooks:
        nid, name = nb.get("id", ""), nb.get("name", "")
        if not nid:
            continue
        seen.add(nid)
        existing = next((n for n in entry["notebooks"] if n.get("id") == nid), None)
        if existing is None:
            entry["notebooks"].append(
                {"id": nid, "name": name, "permissions": ["R"], "present": True}
            )
            added.append(nid)
        else:
            if existing.get("name") != name:
                existing["name"] = name
            existing["present"] = True
    for nb in entry["notebooks"]:
        if nb.get("id") not in seen:
            nb["present"] = False
    return added


def _notebook_banner(entry: dict) -> str:
    lines = []
    for nb in entry.get("notebooks", []):
        perms = "NONE" if "NONE" in (nb.get("permissions") or []) else "".join(
            p for p in VALID_PERMS if p in (nb.get("permissions") or [])
        )
        flag = "" if nb.get("present", True) else "  (已不存在)"
        lines.append(f"  {nb['id']} | {nb.get('name', '')} | {perms or '-'}{flag}")
    return "\n".join(lines) if lines else "  (空)"


def require_reviewed(entry: dict, config_path: str):
    if entry.get("reviewed"):
        return
    raise PermissionDenied(
        "权限表尚未确认（首次同步已完成，所有新笔记本默认只有 R）。\n"
        f"当前权限表（{config_path}）：\n{_notebook_banner(entry)}\n"
        "请先按需配置权限（R/W/D 自由组合，或 NONE 禁止一切操作）：\n"
        f"  python3 {PROG} perms set <notebookId> R,W\n"
        f"  python3 {PROG} perms set <notebookId> NONE\n"
        f"  python3 {PROG} perms show\n"
        "确认无误后执行：\n"
        f"  python3 {PROG} perms confirm\n"
        "然后才能执行搜索/读写/快照等操作。"
    )


def review_hint(entry: dict, config_path: str):
    if not entry.get("reviewed"):
        warn(
            "首次同步已写入权限表（默认 R），但尚未确认。"
            f"请先 `python3 {PROG} perms show` 检查、`perms set` 调整，"
            "然后 `perms confirm` 后才能执行数据操作。"
        )


# ── HTTP 客户端 ──────────────────────────────────────────────────────────────


class Client:
    def __init__(self, conn: Connection):
        self.conn = conn

    # -- 底层 --

    def _raw(self, path: str, body: bytes | None, headers: dict) -> bytes:
        req = urllib.request.Request(
            self.conn.url + path, data=body, headers=headers, method="POST"
        )
        try:
            with urllib.request.urlopen(req, timeout=self.conn.timeout) as resp:
                return resp.read()
        except urllib.error.HTTPError as e:
            if e.code == 401:
                raise SiYuanError("SiYuan 认证失败（401）：检查 SIYUAN_TOKEN") from e
            detail = ""
            try:
                detail = e.read().decode("utf-8", "replace")[:500]
            except Exception:
                pass
            raise SiYuanError(f"HTTP {e.code} POST {path}: {detail or e.reason}") from e
        except urllib.error.URLError as e:
            raise SiYuanError(f"连接 SiYuan 失败（{self.conn.url}）: {e.reason}") from e
        except TimeoutError as e:
            raise SiYuanError(f"请求超时（{self.conn.timeout}s）: POST {path}") from e

    def call(self, path: str, payload: dict | None = None):
        text = self._raw(
            path,
            json.dumps(payload or {}, ensure_ascii=False).encode("utf-8"),
            {
                "Authorization": f"Token {self.conn.token}",
                "Content-Type": "application/json",
            },
        )
        return self._decode(path, text)

    def _decode(self, path: str, raw: bytes):
        text = raw.decode("utf-8", "replace").strip()
        if not text:
            raise SiYuanError(
                f"SiYuan 端点 {path} 返回空响应（可能被反向代理吞掉）"
            )
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            raise SiYuanError(
                f"SiYuan 端点 {path} 返回非 JSON: {text[:200]}"
            )
        if data.get("code") != 0:
            raise SiYuanError(
                data.get("msg") or f"SiYuan 错误 code={data.get('code')}"
            )
        return data.get("data")

    # -- 基础 --

    def version(self) -> str:
        return self.call("/api/system/version") or ""

    def ls_notebooks(self) -> list[dict]:
        data = self.call("/api/notebook/lsNotebooks")
        return (data or {}).get("notebooks", [])

    def sql(self, stmt: str) -> list[dict]:
        return self.call("/api/query/sql", {"stmt": stmt}) or []

    def box_of(self, block_id: str) -> str | None:
        """docId/blockId → 所属笔记本（blocks.box）。查不到返回 None。"""
        meta = self.doc_meta(block_id)
        return meta.get("box") if meta else None

    def doc_meta(self, doc_id: str) -> dict | None:
        """docId → {box, path, hpath}。path 是存储路径（含 .sy），moveDocs 要用。"""
        rows = self.sql(
            "select box, path, hpath from blocks where id='%s' limit 1"
            % esc(doc_id)
        )
        return rows[0] if rows else None

    # -- 文档 --

    def create_doc(self, notebook: str, path: str, markdown: str) -> str:
        return self.call(
            "/api/filetree/createDocWithMd",
            {"notebook": notebook, "path": path, "markdown": markdown},
        )

    def get_doc_content(self, doc_id: str) -> str:
        data = self.call("/api/export/exportMdContent", {"id": doc_id})
        return (data or {}).get("content", "")

    def append_block(self, parent_id: str, markdown: str):
        return self.call(
            "/api/block/appendBlock",
            {"dataType": "markdown", "data": markdown, "parentID": parent_id},
        )

    def update_block(self, block_id: str, markdown: str):
        return self.call(
            "/api/block/updateBlock",
            {"dataType": "markdown", "data": markdown, "id": block_id},
        )

    def move_docs(self, from_paths: list[str], to_notebook: str, to_path: str):
        """SiYuan 3.8 的 moveDocs 只认 fromPaths（存储路径含 .sy）；传 fromIDs 会内核 panic。"""
        return self.call(
            "/api/filetree/moveDocs",
            {"fromPaths": from_paths, "toNotebook": to_notebook, "toPath": to_path},
        )

    def list_docs(self, notebook: str, path: str = "/") -> list[dict]:
        data = self.call(
            "/api/filetree/listDocsByPath", {"notebook": notebook, "path": path}
        )
        return (data or {}).get("files", [])

    def notebook_conf(self, notebook: str) -> dict:
        data = self.call("/api/notebook/getNotebookConf", {"notebook": notebook})
        if not isinstance(data, dict):
            return {}
        conf = data.get("conf")
        return conf if isinstance(conf, dict) else data

    def render_sprig(self, template: str) -> str:
        data = self.call("/api/template/renderSprig", {"template": template})
        return data if isinstance(data, str) else ""

    def set_block_attrs(self, block_id: str, attrs: dict):
        return self.call("/api/attr/setBlockAttrs", {"id": block_id, "attrs": attrs})

    def read_workspace_file(self, path: str) -> str:
        """/api/file/getFile 返回裸文件内容，不是 JSON 包络。"""
        raw = self._raw(
            "/api/file/getFile",
            json.dumps({"path": path}).encode("utf-8"),
            {
                "Authorization": f"Token {self.conn.token}",
                "Content-Type": "application/json",
            },
        )
        text = raw.decode("utf-8", "replace")
        if text.lstrip().startswith("{"):
            try:
                data = json.loads(text)
            except json.JSONDecodeError:
                return text
            if isinstance(data, dict) and "code" in data:
                raise SiYuanError(
                    "读取文件失败 %s: %s" % (path, data.get("msg") or data.get("code"))
                )
        return text

    def daily_note(self, notebook: str, markdown: str) -> tuple[str, bool]:
        """按笔记本 dailyNoteSavePath（Sprig 模板）解析路径，打 custom-dailynote-yyyymmdd 标记。

        该部署 /api/dailynote/* 返回空 body，所以退化为 createDocWithMd + appendBlock。
        """
        today = datetime.now().astimezone()
        date_str = today.strftime("%Y-%m-%d")
        compact = today.strftime("%Y%m%d")
        conf: dict = {}
        path = f"/{date_str}"
        try:
            conf = self.notebook_conf(notebook)
            tpl = str(conf.get("dailyNoteSavePath") or "").strip()
            if tpl:
                rendered = self.render_sprig(tpl).strip()
                if rendered:
                    path = rendered if rendered.startswith("/") else "/" + rendered
        except SiYuanError:
            conf = {}
        rows = self.sql(
            "select id from blocks where type='d' and box='%s' and hpath='%s' limit 1"
            % (esc(notebook), esc(path))
        )
        if rows:
            doc_id = rows[0]["id"]
            self.append_block(doc_id, markdown)
            return doc_id, False
        body = ""
        tpl_file = str(conf.get("dailyNoteTemplatePath") or "").strip()
        if tpl_file:
            try:
                body = self.read_workspace_file(tpl_file).strip()
            except SiYuanError:
                body = ""
        head = body or f"# {date_str}"
        doc_id = self.create_doc(notebook, path, head)
        attrs = {f"custom-dailynote-{compact}": "1"}
        try:
            self.set_block_attrs(doc_id, attrs)
        except SiYuanError:
            pass
        if markdown.strip():
            self.append_block(doc_id, markdown)
        return doc_id, True

    # -- 搜索 / 标签 / 快照 --

    def search_content(self, query: str, limit: int) -> list[dict]:
        return self.sql(
            "select id, box, root_id, content, type from blocks "
            "where content like '%%%s%%' limit %d" % (esc(query), limit)
        )

    def search_doc_titles(self, query: str, limit: int) -> list[dict]:
        return self.sql(
            "select id, box, content from blocks where type='d' "
            "and content like '%%%s%%' limit %d" % (esc(query), limit)
        )

    def search_tags(self, k: str = "") -> list[str]:
        data = self.call("/api/search/searchTag", {"k": k})
        return [re.sub(r"</?mark>", "", t) for t in (data or {}).get("tags", [])]

    def rename_tag(self, old: str, new: str):
        # /api/tag/replaceTag 被部分反代吞掉；renameTag 可用
        return self.call("/api/tag/renameTag", {"oldLabel": old, "newLabel": new})

    def create_snapshot(self, memo: str):
        return self.call("/api/repo/createSnapshot", {"memo": memo})

    def list_snapshots(self, page: int) -> dict:
        # /api/repo/listSnapshots 被吞；getRepoSnapshots 可用
        return self.call("/api/repo/getRepoSnapshots", {"page": page}) or {}

    def rollback(self, snapshot_id: str):
        return self.call("/api/repo/checkoutRepo", {"id": snapshot_id})

    # -- 附件（multipart 字段名必须是 file[]） --

    def upload_assets(self, files: list[tuple[str, bytes, str]]) -> dict:
        if not files:
            raise SiYuanError("上传文件列表为空")
        boundary = "----SiYuanSkill" + uuid.uuid4().hex
        body = bytearray()
        for name, data, mime in files:
            safe_name = name.replace('"', "_").replace("\r", "").replace("\n", "")
            body += f"--{boundary}\r\n".encode()
            body += (
                'Content-Disposition: form-data; name="file[]"; '
                f'filename="{safe_name}"\r\n'
            ).encode()
            body += f"Content-Type: {mime}\r\n\r\n".encode()
            body += data + b"\r\n"
        body += f"--{boundary}--\r\n".encode()

        raw = self._raw(
            "/api/asset/upload",
            bytes(body),
            {
                "Authorization": f"Token {self.conn.token}",
                "Content-Type": f"multipart/form-data; boundary={boundary}",
            },
        )
        data = self._decode("/api/asset/upload", raw) or {}
        err_files = data.get("errFiles") or []
        succ_map = data.get("succMap") or {}
        # code=0 + 空 succMap = 字段名回归 / 反代吞 body 的静默失败
        if not succ_map and not err_files:
            raise SiYuanError(
                "上传返回空 succMap（文件未落库）：疑似 multipart 字段名回归或反代吞掉 body，"
                "当前字段名 file[]"
            )
        return {"errFiles": err_files, "succMap": succ_map}


def esc(s: str) -> str:
    return str(s).replace("'", "''")


# ── GUARD（组合权限判定 + 块反查） ──────────────────────────────────────────


class Guard:
    def __init__(self, client: Client, entry: dict, operation: str):
        self.client = client
        self.entry = entry
        self.operation = operation
        self._synced = False

    def _ensure_synced(self):
        """按需同步：只在权限表缺该库、或需要「全库」判定时拉一次列表。"""
        if self._synced:
            return
        self._synced = True
        resync = getattr(self.client, "resync", None)
        if resync:
            resync()

    def by_block(self, block_id: str, required: list[str]):
        box = self.client.box_of(block_id)
        if not box:
            raise PermissionDenied(
                f"权限拒绝：块 {block_id} 无法反查所属笔记本（ID 无效或不可见），"
                f"执行「{self.operation}」需要 {'/'.join(required)}"
            )
        self.by_notebook(box, required)

    def by_notebook(self, notebook_id: str, required: list[str]):
        ok, nb, missing = audit_notebook(self.entry, notebook_id, required)
        if nb is None:
            self._ensure_synced()  # 可能是新建的库：同步后重判一次
            ok, nb, missing = audit_notebook(self.entry, notebook_id, required)
        if not ok:
            raise PermissionDenied(
                deny_message(nb, required, missing, self.operation, notebook_id)
            )

    def any_perm(self, perm: str):
        if not audit_any(self.entry, perm):
            raise PermissionDenied(
                f"权限拒绝：执行「{self.operation}」需要至少一个笔记本具备 {perm} 权限"
                f"（NONE 不计入）"
            )

    def all_perm(self, perm: str):
        self._ensure_synced()  # 「所有库」必须看到最新库集合，避免新库绕过
        if not audit_all(self.entry, perm):
            bad = [
                nb
                for nb in active_notebooks(self.entry)
                if perm not in perms_of(nb)
            ]
            detail = "、".join(
                f"{nb['id']}（{nb.get('name', '')}）" for nb in bad
            ) or "权限表中没有可用笔记本"
            raise PermissionDenied(
                f"权限拒绝：执行「{self.operation}」需要所有笔记本都有 {perm} 权限；"
                f"不满足：{detail}"
            )

    def move(self, from_box: str, to_notebook: str):
        self.by_notebook(from_box, ["D"])
        self.by_notebook(to_notebook, ["W"])


# ── 命令公共流程 ─────────────────────────────────────────────────────────────


def prepare(args, operation: str) -> tuple[Client, dict, dict]:
    """加载连接 → reviewed 门禁。权限表按需同步（见 Guard._ensure_synced）。

    返回 (client, entry, store)；client.resync 供 Guard 在缺库/全库判定时调用。
    """
    cfg = Path(args.config)
    store = load_store(cfg)
    conn, entry = resolve(args, store)
    conn.validate()
    client = Client(conn)

    def resync():
        try:
            api_notebooks = client.ls_notebooks()
        except SiYuanError as e:
            raise SiYuanError(
                f"同步笔记本失败，拒绝执行（权限表可能过期）：{e}"
            ) from e
        added = sync_notebooks(entry, api_notebooks)
        save_store(store, cfg)
        if added:
            warn(
                f"发现 {len(added)} 个新笔记本，已按默认 R 写入权限表："
                f"{', '.join(added)}"
                f"（需要写/删请 `python3 {PROG} perms set <id> R,W`）"
            )
        return added

    client.resync = resync  # type: ignore[attr-defined]
    if not entry.get("notebooks"):
        resync()  # 首次使用/表为空：先填充，引导信息才有内容
    require_reviewed(entry, str(cfg))
    return client, entry, store


def find_nb(entry: dict, notebook_id: str) -> dict | None:
    return next(
        (n for n in entry.get("notebooks", []) if n.get("id") == notebook_id), None
    )


def nb_label(entry: dict, notebook_id: str) -> str:
    nb = find_nb(entry, notebook_id)
    return f"{notebook_id}（{nb.get('name', '')}）" if nb else notebook_id


# ── 命令：连接 / 权限表 ─────────────────────────────────────────────────────


def cmd_ping(args):
    store = load_store(Path(args.config))
    conn, entry = resolve(args, store)
    conn.validate()
    client = Client(conn)
    version = client.version()
    notebooks = client.ls_notebooks()
    added = sync_notebooks(entry, notebooks)
    save_store(store, Path(args.config))
    if args.json:
        print_json(
            {
                "ok": True,
                "url": conn.url,
                "version": version,
                "notebooks": len(notebooks),
                "added": added,
                "reviewed": bool(entry.get("reviewed")),
                "config": str(args.config),
            }
        )
    else:
        print(f"✅ SiYuan {version} 连接正常（{conn.url}）")
        print(f"   笔记本 {len(notebooks)} 个；权限表: {args.config}")
        if added:
            print(f"   新增 {len(added)} 个笔记本，已按默认 R 记录")
    review_hint(entry, args.config)


def cmd_notebooks(args):
    store = load_store(Path(args.config))
    conn, entry = resolve(args, store)
    conn.validate()
    client = Client(conn)
    notebooks = client.ls_notebooks()
    added = sync_notebooks(entry, notebooks)
    save_store(store, Path(args.config))
    rows = []
    for nb in notebooks:
        conf = find_nb(entry, nb.get("id", ""))
        perms = "NONE" if conf and "NONE" in (conf.get("permissions") or []) else "".join(
            p for p in VALID_PERMS if conf and p in (conf.get("permissions") or [])
        )
        rows.append(
            {
                "id": nb.get("id"),
                "name": nb.get("name"),
                "permissions": perms or "-",
                "closed": bool(nb.get("closed")),
            }
        )
    if args.json:
        print_json(
            {
                "url": conn.url,
                "count": len(rows),
                "added": added,
                "reviewed": bool(entry.get("reviewed")),
                "notebooks": rows,
            }
        )
    else:
        print(f"共 {len(rows)} 个笔记本（{conn.url}）：")
        print("  id | name | 权限 | 状态")
        for r in rows:
            print(
                f"  {r['id']} | {r['name']} | {r['permissions']}"
                f"{' | 已关闭' if r['closed'] else ''}"
            )
        if added:
            print(f"新增 {len(added)} 个笔记本，已按默认 R 记录")
    review_hint(entry, args.config)


def cmd_perms(args):
    store = load_store(Path(args.config))
    url = normalize_url(args.url or os.environ.get("SIYUAN_API_URL", "") or "")
    if not url:
        raise SiYuanError("缺少 SIYUAN_API_URL，无法定位权限表分组。")
    entry = server_entry(store, url)

    if args.action == "show":
        if args.json:
            print_json(
                {
                    "url": url,
                    "config": str(args.config),
                    "reviewed": bool(entry.get("reviewed")),
                    "reviewedAt": entry.get("reviewedAt"),
                    "notebooks": entry.get("notebooks", []),
                    "rules": {
                        "global_read": "任一 R",
                        "batch_replace_tag": "所有 W",
                        "create_snapshot": "任一 R",
                        "rollback_to_snapshot": "所有 D",
                        "move_documents": "源 D + 目标 W",
                        "exempt": ["ping", "notebooks", "perms"],
                    },
                }
            )
            return
        print(f"实例: {url}")
        print(f"权限表: {args.config}")
        print(f"状态: {'已确认' if entry.get('reviewed') else '未确认（数据操作被 GUARD 拒绝）'}")
        if entry.get("reviewedAt"):
            print(f"确认时间: {entry['reviewedAt']}")
        print("笔记本:")
        print(_notebook_banner(entry))
        print("规则: 读类需 R；写类需 W；move 源 D + 目标 W；")
        print("      rollback 所有 D；batch_replace_tag 所有 W；全局读/快照创建 任一 R；")
        print("      NONE = 禁止一切操作（不计入任一，也永不满足所有）。")
        if not entry.get("reviewed"):
            if entry.get("notebooks"):
                print("下一步:")
                print(f"  python3 {PROG} perms set <notebookId> R,W")
                print(f"  python3 {PROG} perms confirm")
            else:
                print("权限表还没有笔记本：先运行 "
                      f"`python3 {PROG} notebooks` 同步，再设置权限。")
        return

    if args.action == "set":
        nb_id = args.notebook_id
        nb = find_nb(entry, nb_id)
        if nb is None:
            ids = ", ".join(n["id"] for n in entry.get("notebooks", [])) or "(空)"
            raise SiYuanError(
                f"权限表中没有笔记本 {nb_id}。先运行 `python3 {PROG} notebooks` 同步。\n"
                f"已知 id: {ids}"
            )
        perms = parse_permissions(args.permissions)
        nb["permissions"] = perms
        save_store(store, Path(args.config))
        print(f"✅ 已设置 {nb['id']}（{nb.get('name', '')}）权限: {'+'.join(perms)}")
        if not entry.get("reviewed"):
            print("提醒: 权限表尚未确认，配置完成后运行 `perms confirm`。")
        else:
            print("提醒: 权限表已确认，改动即时生效。")
        return

    if args.action == "confirm":
        if not entry.get("notebooks"):
            raise SiYuanError(
                f"权限表为空。先运行 `python3 {PROG} notebooks` 同步笔记本。"
            )
        entry["reviewed"] = True
        entry["reviewedAt"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
        save_store(store, Path(args.config))
        print("✅ 权限表已确认，后续数据操作将按以下权限审计：")
        print(_notebook_banner(entry))
        return

    raise SiYuanError(f"未知 perms 动作: {args.action}")


def parse_permissions(raw: str) -> list[str]:
    tokens = [t.upper() for t in re.split(r"[\s,+/|]+", (raw or "").strip()) if t]
    if not tokens:
        raise SiYuanError("权限不能为空：用 R / W / D 的组合，或 NONE")
    if "NONE" in tokens:
        if len(tokens) > 1:
            raise SiYuanError("NONE 不能与其他权限组合（NONE 已表示禁止一切操作）")
        return ["NONE"]
    invalid = [t for t in tokens if t not in VALID_PERMS]
    if invalid:
        raise SiYuanError(f"非法权限: {', '.join(invalid)}（只允许 R / W / D / NONE）")
    return [p for p in VALID_PERMS if p in tokens]


def cmd_selftest(args):
    """纯函数权限矩阵 + 权限表同步自检（无网络）。"""
    nb = lambda i, p, n=None: {"id": i, "name": n or i, "permissions": p, "present": True}
    # permsOf
    assert perms_of(nb("a", ["R", "W"])) == {"R", "W"}
    assert perms_of(nb("a", ["NONE"])) == set()
    assert perms_of(nb("a", ["NONE", "R"])) == set(), "NONE 混入仍为空"
    assert perms_of(nb("a", ["R", "X", "W"])) == {"R", "W"}, "非法值过滤"
    # audit_notebook
    e = {"notebooks": [nb("a", ["R", "W"]), nb("b", ["NONE"], "敏感")]}
    assert audit_notebook(e, "a", ["R"])[0] is True
    assert audit_notebook(e, "a", ["D"])[0] is False
    assert audit_notebook(e, "a", ["W", "D"])[2] == ["D"]
    assert audit_notebook(e, "b", ["R"])[0] is False, "NONE 不满足 R"
    assert audit_notebook(e, "unknown", ["R"])[0] is False, "未知笔记本拒绝"
    # any / all
    assert audit_any(e, "R") is True, "任一 R"
    assert audit_any({"notebooks": [nb("b", ["NONE"])]}, "R") is False, "NONE 不算 R"
    assert audit_any({"notebooks": []}, "R") is False, "空表不放行"
    assert audit_all({"notebooks": [nb("a", ["W"]), nb("b", ["R", "W"])]}, "W") is True
    assert audit_all({"notebooks": [nb("a", ["W"]), nb("b", ["NONE"])]}, "W") is False
    assert audit_all({"notebooks": [nb("a", ["W"]), nb("b", ["R", "D"])]}, "W") is False
    assert audit_all({"notebooks": []}, "W") is False, "空表不满足所有"
    # 已不存在的笔记本不参与全局判定
    stale = {"notebooks": [nb("a", ["W"]), {**nb("z", ["NONE"]), "present": False}]}
    assert audit_all(stale, "W") is True
    # deny 消息要素
    msg = deny_message(
        nb("20260804180250-4kuh8xy", ["NONE"], "05-Vault"),
        ["R"], ["R"], "get_document_content",
    )
    for part in ("20260804180250-4kuh8xy", "05-Vault", "get_document_content", "需要 R", "缺失 R"):
        assert part in msg, part
    # 权限解析
    assert parse_permissions("w,r") == ["R", "W"]
    assert parse_permissions("R/W/D") == ["R", "W", "D"]
    assert parse_permissions("NONE") == ["NONE"]
    for bad in ("", "NONE,R", "X"):
        try:
            parse_permissions(bad)
        except SiYuanError:
            pass
        else:
            raise AssertionError(f"应拒绝权限 {bad!r}")
    # 权限表同步（匿名临时路径）
    entry = {"notebooks": [nb("a", ["R", "W"], "旧名")]}
    added = sync_notebooks(
        entry, [{"id": "a", "name": "新名"}, {"id": "new1", "name": "新笔记本"}]
    )
    assert "new1" in added and "a" not in added
    assert entry["notebooks"][0]["name"] == "新名"
    new = next(x for x in entry["notebooks"] if x["id"] == "new1")
    assert new["permissions"] == ["R"] and new["present"] is True
    assert all(x["present"] for x in entry["notebooks"])
    # 消失的笔记本标记 present=False，权限保留
    sync_notebooks(entry, [{"id": "a", "name": "新名"}])
    assert next(x for x in entry["notebooks"] if x["id"] == "new1")["present"] is False
    assert next(x for x in entry["notebooks"] if x["id"] == "new1")["permissions"] == ["R"]
    print("selftest: all assertions passed ✅")


# ── 命令：搜索 / 文档 ───────────────────────────────────────────────────────


def _doc_key(row: dict) -> str:
    """内容搜索命中的是块，读文档要用 root_id；标题搜索命中本身就是文档。"""
    return row.get("root_id") or row.get("id") or ""


def cmd_search(args):
    client, entry, _ = prepare(args, "unified_search")
    Guard(client, entry, "unified_search").any_perm("R")
    if not args.query and not args.title:
        raise SiYuanError("至少提供 query（内容）或 --title（标题）之一")
    limit = max(1, min(args.limit, 50))
    results: list[dict] = []
    seen_docs: set[str] = set()
    if args.title:
        results = client.search_doc_titles(args.title, limit)
        seen_docs = {_doc_key(r) for r in results}
    if args.query:
        for r in client.search_content(args.query, limit):
            key = _doc_key(r)
            if key in seen_docs:
                continue
            seen_docs.add(key)
            results.append(r)
    results = results[:limit]
    if args.json:
        print_json(
            {
                "count": len(results),
                "results": [
                    {
                        "docId": _doc_key(r),
                        "blockId": r.get("id"),
                        "notebook": r.get("box"),
                        "notebookName": (find_nb(entry, r.get("box", "")) or {}).get("name", ""),
                        "content": r.get("content", ""),
                    }
                    for r in results
                ],
            }
        )
        return
    if not results:
        print("（没有匹配结果）")
        return
    print(f"搜索结果 {len(results)} 条（第二列为可 read 的 docId）：")
    for r in results:
        snippet = str(r.get("content", "")).replace("\n", " ")[:100]
        print(f"{r.get('box')} | {_doc_key(r)} | {snippet}")


def cmd_read(args):
    client, entry, _ = prepare(args, "get_document_content")
    Guard(client, entry, "get_document_content").by_block(args.doc_id, ["R"])
    content = client.get_doc_content(args.doc_id)
    if args.json:
        print_json({"docId": args.doc_id, "content": content})
    else:
        print(content)


def cmd_create(args):
    client, entry, _ = prepare(args, "create_document")
    Guard(client, entry, "create_document").by_notebook(args.notebook, ["W"])
    path = args.path if args.path.startswith("/") else "/" + args.path
    markdown = read_content(args)
    doc_id = client.create_doc(args.notebook, path, markdown)
    if args.json:
        print_json({"docId": doc_id, "notebook": args.notebook, "path": path})
    else:
        print(f"✅ 已创建文档 {doc_id}（{nb_label(entry, args.notebook)}{path}）")


def cmd_append(args):
    client, entry, _ = prepare(args, "append_to_document")
    Guard(client, entry, "append_to_document").by_block(args.doc_id, ["W"])
    client.append_block(args.doc_id, read_content(args))
    print(f"✅ 已追加内容到文档 {args.doc_id}")


def cmd_update(args):
    client, entry, _ = prepare(args, "update_document")
    Guard(client, entry, "update_document").by_block(args.doc_id, ["W"])
    client.update_block(args.doc_id, read_content(args))
    print(f"✅ 已更新文档 {args.doc_id} 首块")


def cmd_move(args):
    client, entry, _ = prepare(args, "move_documents")
    guard = Guard(client, entry, "move_documents")
    sources = []
    for doc_id in args.doc_ids:
        meta = client.doc_meta(doc_id)
        if not meta:
            raise PermissionDenied(
                f"权限拒绝：块 {doc_id} 无法反查所属笔记本，执行「move_documents」需要 D"
            )
        guard.move(meta.get("box", ""), args.to_notebook)
        if not meta.get("path"):
            raise SiYuanError(f"文档 {doc_id} 缺少存储路径，无法移动")
        sources.append(meta)

    to_path = "/"
    target = nb_label(entry, args.to_notebook)
    if args.to_doc:
        parent = client.doc_meta(args.to_doc)
        if not parent:
            raise SiYuanError(f"目标父文档 {args.to_doc} 不存在")
        if parent.get("box") != args.to_notebook:
            raise SiYuanError("目标父文档不在目标笔记本内")
        to_path = parent.get("path") or ""
        if not to_path:
            raise SiYuanError(f"目标父文档 {args.to_doc} 缺少存储路径")
        target += parent.get("hpath") or "/"

    client.move_docs([m["path"] for m in sources], args.to_notebook, to_path)
    print(f"✅ 已移动 {len(sources)} 个文档到 {target}")


def cmd_tree(args):
    client, entry, _ = prepare(args, "get_document_tree")
    guard = Guard(client, entry, "get_document_tree")
    if args.notebook_root:
        notebook = args.notebook_root.split("/")[0]
        guard.by_notebook(notebook, ["R"])
        files = client.list_docs(notebook, "/")
        if args.json:
            print_json({"notebook": notebook, "files": files})
            return
        print(f"# {nb_label(entry, notebook)}（{len(files)} 个顶层文档）")
        for f in files[:100]:
            sub = f.get("subFileCount") or 0
            extra = f" | 子文档 {sub}" if sub else ""
            print(f"  {f.get('name')} | {f.get('id')}{extra}")
        return
    guard.any_perm("R")
    notebooks = client.ls_notebooks()
    out = []
    data = {}
    for nb in notebooks[:20]:
        if nb.get("closed"):
            continue
        try:
            files = client.list_docs(nb.get("id", ""), "/")
        except SiYuanError as e:
            out.append(f"# {nb.get('name')}（读取失败: {e}）")
            continue
        out.append(f"# {nb.get('name')}（{len(files)} 个顶层文档）")
        out.extend(
            f"  {f.get('name')} | {f.get('id')}"
            + (f" | 子文档 {f.get('subFileCount')}" if f.get("subFileCount") else "")
            for f in files[:50]
        )
        data[nb.get("id")] = files
    if args.json:
        print_json(data)
    else:
        print("\n".join(out))


def cmd_daily(args):
    client, entry, _ = prepare(args, "append_to_daily_note")
    Guard(client, entry, "append_to_daily_note").by_notebook(args.notebook, ["W"])
    doc_id, created = client.daily_note(args.notebook, read_content(args))
    if args.json:
        print_json({"docId": doc_id, "created": created, "notebook": args.notebook})
    else:
        print(f"✅ {'已创建' if created else '已追加到'}日记 {doc_id}")


def cmd_sql(args):
    client, entry, _ = prepare(args, "sql_query")
    Guard(client, entry, "sql_query").any_perm("R")
    stmt = args.stmt.strip().rstrip(";")
    if not stmt.lstrip().lower().startswith(("select", "with")):
        raise SiYuanError(
            "sql 只接受 SELECT / WITH 查询；写操作请用 create/append/update/move"
        )
    rows = client.sql(stmt)
    if args.json:
        print_json(rows)
        return
    if not rows:
        print("（无结果）")
        return
    cols = list(rows[0].keys())
    print(" | ".join(cols))
    for r in rows[: args.limit]:
        print(
            " | ".join(
                "" if r.get(c) is None else str(r.get(c)).replace("\n", "⏎") for c in cols
            )
        )
    if len(rows) > args.limit:
        print(f"…… 共 {len(rows)} 行，仅显示前 {args.limit} 行")


def cmd_recent(args):
    client, entry, _ = prepare(args, "get_recently_updated_documents")
    Guard(client, entry, "get_recently_updated_documents").any_perm("R")
    limit = max(1, min(args.limit, 50))
    rows = client.sql(
        "select id, box, content, updated from blocks where type='d' "
        "order by updated desc limit %d" % limit
    )
    if args.json:
        print_json({"count": len(rows), "documents": rows})
        return
    if not rows:
        print("（无）")
        return
    for r in rows:
        print(f"{r.get('updated')} | {r.get('box')} | {r.get('id')} | {r.get('content')}")


# ── 命令：快照 / 标签 / 附件 ────────────────────────────────────────────────


def cmd_snapshot(args):
    client, entry, _ = prepare(args, f"snapshot_{args.action}")
    if args.action == "list":
        Guard(client, entry, "list_snapshots").any_perm("R")
        data = client.list_snapshots(args.page)
        snaps = data.get("snapshots") or []
        if args.json:
            print_json({"page": args.page, "pageCount": data.get("pageCount"), "snapshots": snaps})
            return
        print(f"第 {args.page} 页（共 {data.get('pageCount', '?')} 页）：")
        for s in snaps:
            print(f"  {s.get('id')} | {s.get('hCreated')} | {s.get('memo')} | {s.get('hSize')}")
        return
    if args.action == "create":
        Guard(client, entry, "create_snapshot").any_perm("R")
        client.create_snapshot(args.memo or "siyuan-skill snapshot")
        print("✅ 快照已创建")
        return
    if args.action == "rollback":
        Guard(client, entry, "rollback_to_snapshot").all_perm("D")
        if not args.yes:
            print(f"将回滚全库到快照 {args.id}（不可逆，覆盖所有笔记本）。")
            print("确认请加 --yes 重新执行。")
            return
        client.rollback(args.id)
        print(f"✅ 已回滚到快照 {args.id}")
        return
    raise SiYuanError(f"未知 snapshot 动作: {args.action}")


def cmd_tags(args):
    client, entry, _ = prepare(args, "list_all_tags")
    Guard(client, entry, "list_all_tags").any_perm("R")
    tags = client.search_tags("")
    if args.json:
        print_json({"count": len(tags), "tags": tags})
    else:
        print(", ".join(tags) if tags else "（无标签）")


def cmd_retag(args):
    client, entry, _ = prepare(args, "batch_replace_tag")
    Guard(client, entry, "batch_replace_tag").all_perm("W")
    if not args.new.strip():
        raise SiYuanError(
            "new_tag 不能为空（该部署暂不支持删除标签，只支持重命名）"
        )
    client.rename_tag(args.old, args.new)
    print(f"✅ 已将标签「{args.old}」替换为「{args.new}」")


def cmd_upload(args):
    client, entry, _ = prepare(args, "upload_asset")
    Guard(client, entry, "upload_asset").by_notebook(args.notebook, ["W"])
    if not args.paths:
        raise SiYuanError("paths 不能为空")
    if len(args.paths) > MAX_UPLOAD_FILES:
        raise SiYuanError(f"一次最多上传 {MAX_UPLOAD_FILES} 个文件")
    files = []
    for p in args.paths:
        try:
            data = Path(p).read_bytes()
        except OSError as e:
            raise SiYuanError(f"读取文件失败：{p}（{e.strerror}）") from e
        mime = mimetypes.guess_type(p)[0] or "application/octet-stream"
        files.append((Path(p).name, data, mime))
    r = client.upload_assets(files)
    succ, errs = r["succMap"], r["errFiles"]
    if args.json:
        print_json(
            {
                "ok": True,
                "uploaded": len(succ),
                "files": len(files),
                "succMap": succ,
                "errFiles": errs,
            }
        )
        return
    print(
        f"✅ 上传成功 {len(succ)}/{len(files)}"
        + (f"，失败 {len(errs)}" if errs else "")
        + "："
    )
    for name, path in succ.items():
        print(f"  {name} → {path}")
    if errs:
        print(f"  失败：{', '.join(errs)}")
    if succ:
        print("在 create/append 的 Markdown 中直接用 ![alt](assets/...) 引用。")


# ── 内容读取 ─────────────────────────────────────────────────────────────────


def read_content(args) -> str:
    if args.content:
        return args.content
    if args.file:
        if args.file == "-":
            return sys.stdin.read()
        try:
            return Path(args.file).read_text("utf-8")
        except OSError as e:
            raise SiYuanError(f"读取文件失败：{args.file}（{e.strerror}）") from e
    if not sys.stdin.isatty():
        return sys.stdin.read()
    raise SiYuanError("缺少内容：用 -c/--content、-f/--file（- 表示 stdin），或通过管道输入")


# ── argparse ─────────────────────────────────────────────────────────────────


def add_common(p: argparse.ArgumentParser):
    # SUPPRESS：让子命令解析器不覆盖主解析器已取到的值
    p.add_argument("--url", default=argparse.SUPPRESS, help="覆盖 SIYUAN_API_URL")
    p.add_argument("--key", default=argparse.SUPPRESS, help="覆盖 SIYUAN_TOKEN")
    p.add_argument(
        "--config",
        default=argparse.SUPPRESS,
        help=f"权限表路径（默认 {DEFAULT_CONFIG}）",
    )
    p.add_argument("--json", action="store_true", default=argparse.SUPPRESS, help="机器可读输出")


def add_content(p: argparse.ArgumentParser):
    p.add_argument("--content", "-c", default="", help="正文（Markdown）")
    p.add_argument(
        "--file", "-f", default="", help="从文件读取正文，- 表示 stdin"
    )


def build_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    add_common(common)

    p = argparse.ArgumentParser(
        prog="siyuan.py",
        description="SiYuan 笔记远程 CLI（依赖 SIYUAN_API_URL + SIYUAN_TOKEN；所有数据操作先过权限 GUARD）",
    )
    add_common(p)
    sub = p.add_subparsers(dest="cmd", required=True)

    def cmd(name: str, help_: str, func):
        sp = sub.add_parser(name, help=help_, parents=[common])
        sp.set_defaults(func=func)
        return sp

    cmd("ping", "校验连通性/令牌，并同步权限表（审计豁免）", cmd_ping)
    cmd("notebooks", "列出笔记本并同步权限表（审计豁免）", cmd_notebooks)
    cmd("selftest", "权限矩阵纯函数自检（无网络）", cmd_selftest)

    sp = cmd("perms", "查看/设置/确认权限表（审计豁免）", cmd_perms)
    psub = sp.add_subparsers(dest="action", required=True)
    show = psub.add_parser("show", help="显示当前实例的权限表", parents=[common])
    show.set_defaults(func=cmd_perms, action="show")
    st = psub.add_parser(
        "set", help="设置某笔记本权限，如 R,W 或 NONE", parents=[common]
    )
    st.add_argument("notebook_id")
    st.add_argument("permissions", help="R/W/D 组合（逗号或斜杠分隔），或 NONE")
    st.set_defaults(func=cmd_perms, action="set")
    cf = psub.add_parser(
        "confirm", help="确认权限表，放行后续数据操作", parents=[common]
    )
    cf.set_defaults(func=cmd_perms, action="confirm")

    sp = cmd("search", "统一搜索（内容/标题，需任一 R）", cmd_search)
    sp.add_argument("query", nargs="?", default="", help="内容关键词")
    sp.add_argument("--title", "-t", default="", help="文档标题关键词")
    sp.add_argument("--limit", "-n", type=int, default=10)

    sp = cmd("read", "读取文档 Markdown（需所属笔记本 R）", cmd_read)
    sp.add_argument("doc_id")

    sp = cmd("create", "创建文档（需目标笔记本 W）", cmd_create)
    sp.add_argument("notebook")
    sp.add_argument("path", help="文档路径，如 /notes/new-doc")
    add_content(sp)

    sp = cmd("append", "向文档末尾追加内容（需所属笔记本 W）", cmd_append)
    sp.add_argument("doc_id")
    add_content(sp)

    sp = cmd("update", "覆盖文档首块（需所属笔记本 W）", cmd_update)
    sp.add_argument("doc_id")
    add_content(sp)

    sp = cmd("move", "移动文档（源 D + 目标 W）", cmd_move)
    sp.add_argument("to_notebook", help="目标笔记本 ID")
    sp.add_argument("doc_ids", nargs="+")
    sp.add_argument("--to-doc", default="", help="目标父文档 ID（缺省移到笔记本根）")

    sp = cmd("tree", "文档树（指定笔记本需 R；不带参数需任一 R）", cmd_tree)
    sp.add_argument("notebook_root", nargs="?", default="")

    sp = cmd("daily", "追加到今天的日记（需该笔记本 W）", cmd_daily)
    sp.add_argument("notebook")
    add_content(sp)

    sp = cmd("recent", "最近更新的文档（需任一 R）", cmd_recent)
    sp.add_argument("--limit", "-n", type=int, default=10)

    sp = cmd("sql", "只读 SQL 查询（SELECT/WITH，需任一 R）", cmd_sql)
    sp.add_argument("stmt")
    sp.add_argument("--limit", "-n", type=int, default=50)

    sp = cmd("snapshot", "快照：list（任一 R）/ create（任一 R）/ rollback（所有 D，需 --yes）", cmd_snapshot)
    ssub = sp.add_subparsers(dest="action", required=True)
    sl = ssub.add_parser("list", help="列出快照", parents=[common])
    sl.add_argument("--page", type=int, default=1)
    sl.set_defaults(func=cmd_snapshot, action="list")
    sc = ssub.add_parser("create", help="创建全库快照", parents=[common])
    sc.add_argument("--memo", default="")
    sc.set_defaults(func=cmd_snapshot, action="create")
    sr = ssub.add_parser("rollback", help="回滚到快照（不可逆）", parents=[common])
    sr.add_argument("id")
    sr.add_argument("--yes", "-y", action="store_true")
    sr.set_defaults(func=cmd_snapshot, action="rollback")

    cmd("tags", "列出全库标签（需任一 R）", cmd_tags)

    sp = cmd("retag", "批量替换标签（需所有 W）", cmd_retag)
    sp.add_argument("old")
    sp.add_argument("new")

    sp = cmd("upload", "上传图片/附件到 assets（需目标笔记本 W）", cmd_upload)
    sp.add_argument("notebook")
    sp.add_argument("paths", nargs="+")

    return p


def main(argv=None):
    args = build_parser().parse_args(argv)
    # SUPPRESS 的公共参数缺省值在这里补齐
    for name, default in (
        ("url", ""),
        ("key", ""),
        ("config", str(DEFAULT_CONFIG)),
        ("json", False),
    ):
        if not hasattr(args, name):
            setattr(args, name, default)
    try:
        args.func(args)
    except PermissionDenied as e:
        fail(str(e), 2)
    except SiYuanError as e:
        fail(str(e), 1)
    except KeyboardInterrupt:
        sys.exit(130)


if __name__ == "__main__":
    main()
