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

    def validate(self) -> Connection:
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

    # -- MCP 桥（内核进程内能力，HTTP API 未开放的少量操作）--

    def _mcp_post(self, payload: dict, session: str = "") -> tuple[str, str]:
        """MCP 走 Streamable HTTP：会话 id 在响应头 Mcp-Session-Id，必须自己读头。"""
        headers = {
            "Authorization": f"Token {self.conn.token}",
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        }
        if session:
            headers["Mcp-Session-Id"] = session
        req = urllib.request.Request(
            self.conn.url + "/mcp", data=json.dumps(payload).encode("utf-8"), headers=headers
        )
        try:
            with urllib.request.urlopen(req, timeout=self.conn.timeout) as resp:
                session = resp.headers.get("Mcp-Session-Id") or session
                text = resp.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            if e.code == 401:
                raise SiYuanError("SiYuan 认证失败（401）：检查 SIYUAN_TOKEN") from e
            detail = ""
            try:
                detail = e.read().decode("utf-8", "replace")[:500]
            except Exception:
                pass
            raise SiYuanError(f"HTTP {e.code} POST /mcp: {detail or e.reason}") from e
        except urllib.error.URLError as e:
            raise SiYuanError(f"连接 SiYuan 失败（{self.conn.url}）: {e.reason}") from e
        if text.lstrip().startswith(("event:", "data:")):
            parts = [
                line[5:].strip() for line in text.splitlines() if line.startswith("data:")
            ]
            text = parts[-1] if parts else ""
        return session, text

    def mcp_call(self, name: str, arguments: dict):
        """调内核内置 MCP 工具；只需一次 initialize 握手（每次进程一次）。"""
        session, _ = self._mcp_post(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-06-18",
                    "capabilities": {},
                    "clientInfo": {"name": "siyuan-skill", "version": "1.2.0"},
                },
            }
        )
        # 响应的 Mcp-Session-Id 在头部，_raw 不返回头部，故会话复用依赖内核容忍无 session
        self._mcp_post({"jsonrpc": "2.0", "method": "notifications/initialized"}, session)
        _, text = self._mcp_post(
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {"name": name, "arguments": arguments},
            },
            session,
        )
        if not text:
            raise SiYuanError(f"MCP 工具 {name} 返回空响应")
        data = json.loads(text)
        if "error" in data:
            raise SiYuanError(f"MCP 错误: {data['error']}")
        result = data.get("result") or {}
        if result.get("isError"):
            msgs = [c.get("text", "") for c in result.get("content", [])]
            raise SiYuanError("MCP 调用失败: " + "; ".join(msgs))
        if result.get("structuredContent") is not None:
            return result["structuredContent"]
        for c in result.get("content", []):
            txt = c.get("text", "")
            try:
                return json.loads(txt)
            except json.JSONDecodeError:
                continue
        return result

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

    def write_workspace_file(self, path: str, data: bytes):
        """multipart: path + file（isDir=false），契约同 /api/file/putFile"""
        boundary = "----SiYuanSkill" + uuid.uuid4().hex
        body = bytearray()
        body += f"--{boundary}\r\n".encode()
        body += b'Content-Disposition: form-data; name="path"\r\n\r\n'
        body += f"{path}\r\n".encode()
        body += f"--{boundary}\r\n".encode()
        body += b'Content-Disposition: form-data; name="isDir"\r\n\r\n'
        body += b"false\r\n"
        body += f"--{boundary}\r\n".encode()
        body += (
            'Content-Disposition: form-data; name="file"; '
            f'filename="{Path(path).name}"\r\n'
        ).encode()
        body += b"Content-Type: application/octet-stream\r\n\r\n"
        body += data + b"\r\n"
        body += f"--{boundary}--\r\n".encode()
        self._decode(
            "/api/file/putFile",
            self._raw(
                "/api/file/putFile",
                bytes(body),
                {
                    "Authorization": f"Token {self.conn.token}",
                    "Content-Type": f"multipart/form-data; boundary={boundary}",
                },
            ),
        )

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


# ── 命令：数据层通用能力（对齐内置 MCP 工具面） ─────────────────────────────
# 约定：一个命令 = MCP 工具的一个 action；权限仍按笔记本 R/W/D 判定，
# 无法归属到笔记本的（工作区文件/同步/资源清理）要求「任一笔记本」具备该权限。


def _need_yes(args, what: str):
    if not getattr(args, "yes", False):
        raise SiYuanError(f"{what} 不可逆：确认后加 --yes")


def _str_of(data, key: str) -> str:
    return data.get(key, "") if isinstance(data, dict) else ""


def cmd_outline(args):
    client, entry, _ = prepare(args, "outline")
    Guard(client, entry, "outline").by_block(args.id, ["R"])
    print_json(client.call("/api/outline/getDocOutline", {"id": args.id}))


def cmd_backlinks(args):
    client, entry, _ = prepare(args, "backlinks")
    Guard(client, entry, "backlinks").by_block(args.id, ["R"])
    ep = "/api/ref/getBacklinkDoc" if args.by_doc else "/api/ref/getBacklink"
    payload = {"id": args.id}
    if args.keyword:
        payload["keyword"] = args.keyword
    if args.sort:
        payload["sort"] = args.sort
    print_json(client.call(ep, payload))


def cmd_attr(args):
    op = f"attr_{args.action}"
    client, entry, _ = prepare(args, op)
    guard = Guard(client, entry, op)
    if args.action == "get":
        guard.by_block(args.id, ["R"])
        print_json(client.call("/api/attr/getBlockAttrs", {"id": args.id}))
    elif args.action == "batch":
        guard.any_perm("R")
        print_json(client.call("/api/attr/batchGetBlockAttrs", {"ids": args.ids}))
    else:
        guard.by_block(args.id, ["W"])
        attrs = {}
        for kv in args.pairs:
            k, sep, v = kv.partition("=")
            if not sep:
                raise SiYuanError(f"属性写法应为 key=value：{kv}")
            attrs[k.strip()] = v
        if not attrs:
            raise SiYuanError("至少给一个 key=value")
        client.set_block_attrs(args.id, attrs)
        print_json({"id": args.id, "attrs": attrs, "ok": True})


def cmd_block(args):
    op = f"block_{args.action}"
    client, entry, _ = prepare(args, op)
    guard = Guard(client, entry, op)
    a = args.action
    if a in ("children", "dom", "breadcrumb", "info", "kramdown"):
        guard.by_block(args.id, ["R"])
        if a == "kramdown":
            data = client.call("/api/block/getBlockKramdown", {"id": args.id})
            if args.json:
                print_json(data)
            else:
                print(_str_of(data, "kramdown"))
            return
        endpoint = {
            "children": "/api/block/getChildBlocks",
            "dom": "/api/block/getBlockDOM",
            "breadcrumb": "/api/block/getBlockBreadcrumb",
            "info": "/api/block/getBlockInfo",
        }[a]
        print_json(client.call(endpoint, {"id": args.id}))
    elif a == "delete":
        guard.by_block(args.id, ["D"])
        _need_yes(args, "删除块")
        client.call("/api/block/deleteBlock", {"id": args.id})
        print_json({"id": args.id, "deleted": True})
    else:  # insert
        body = read_content(args)
        if args.parent:
            guard.by_block(args.parent, ["W"])
        else:
            guard.any_perm("W")
        payload = {"dataType": args.data_type, "data": body}
        for key in ("parent", "previous_id", "next_id"):
            val = getattr(args, key)
            if val:
                payload[{"parent": "parentID", "previous_id": "previousID", "next_id": "nextID"}[key]] = val
        print_json(client.call("/api/block/insertBlock", payload))


def _escape_go_template(text: str) -> str:
    """把字面量 {{ }} 转义，避免 Sprig 把嵌入块/模板语法当 Go action 解析。"""

    def rep(m):
        return '{{"{{"}}' if m.group() == "{{" else '{{"}}"}}'

    return re.sub(r"\{\{|\}\}", rep, text)


def cmd_export(args):
    client, entry, _ = prepare(args, "export_" + args.format)
    guard = Guard(client, entry, "export_" + args.format)
    guard.by_block(args.doc_id, ["R"])
    if args.format == "md":
        data = client.call("/api/export/exportMdContent", {"id": args.doc_id})
        if args.json:
            print_json(data)
        else:
            print(_str_of(data, "content"))
    elif args.format == "html":
        print_json(
            client.call("/api/export/exportHTML", {"id": args.doc_id, "pdf": args.pdf})
        )
    else:
        guard.by_block(args.doc_id, ["W"])  # 会在工作区写文件
        print_json(
            client.call(
                "/api/export/exportDocx", {"id": args.doc_id, "output": args.output}
            )
        )


def cmd_template(args):
    op = f"template_{args.action}"
    client, entry, _ = prepare(args, op)
    guard = Guard(client, entry, op)
    rel = "data/templates/" + args.path.lstrip("/") if getattr(args, "path", "") else ""
    if args.action == "ls":
        guard.any_perm("R")
        print_json(client.call("/api/file/readDir", {"path": "data/templates"}))
    elif args.action == "get":
        guard.any_perm("R")
        print(client.read_workspace_file(rel))
    elif args.action == "render":
        guard.any_perm("R")
        if args.id:
            guard.by_block(args.id, ["R"])
        text = _escape_go_template(client.read_workspace_file(rel))
        print_json(client.render_sprig(text))
    elif args.action == "save-as":
        guard.by_block(args.id, ["W"])
        print_json(
            client.call(
                "/api/template/docSaveAsTemplate",
                {"id": args.id, "name": args.name, "overwrite": args.overwrite},
            )
        )
    else:  # rm
        guard.any_perm("D")
        _need_yes(args, "删除模板")
        client.call("/api/file/removeFile", {"path": rel})
        print_json({"removed": rel})


def cmd_file(args):
    op = f"file_{args.action}"
    client, entry, _ = prepare(args, op)
    guard = Guard(client, entry, op)
    perm = {"ls": "R", "read": "R", "write": "W", "mv": "W", "cp": "W", "rm": "D"}
    guard.any_perm(perm[args.action])
    a = args.action
    if a == "ls":
        print_json(client.call("/api/file/readDir", {"path": args.path}))
    elif a == "read":
        print(client.read_workspace_file(args.path))
    elif a == "write":
        data = read_content(args).encode("utf-8")
        client.write_workspace_file(args.path, data)
        print_json({"path": args.path, "bytes": len(data), "written": True})
    elif a == "rm":
        _need_yes(args, "删除工作区文件")
        client.call("/api/file/removeFile", {"path": args.path})
        print_json({"path": args.path, "removed": True})
    elif a == "mv":
        client.call("/api/file/renameFile", {"path": args.path, "newPath": args.dest})
        print_json({"from": args.path, "to": args.dest})
    else:
        client.call("/api/file/copyFile", {"src": args.path, "dest": args.dest})
        print_json({"from": args.path, "to": args.dest})


def cmd_notebook(args):
    op = f"notebook_{args.action}"
    client, entry, _ = prepare(args, op)
    guard = Guard(client, entry, op)
    a = args.action
    if a in ("open", "close", "rename", "icon"):
        guard.by_notebook(args.id, ["W"])
    else:
        guard.any_perm("W")
    endpoint = {
        "open": "/api/notebook/openNotebook",
        "close": "/api/notebook/closeNotebook",
        "create": "/api/notebook/createNotebook",
        "rename": "/api/notebook/renameNotebook",
        "icon": "/api/notebook/setNotebookIcon",
    }[a]
    payload = {}
    if a == "create":
        payload["name"] = args.name
    elif a in ("open", "close"):
        payload["notebook"] = args.id
    elif a == "rename":
        payload["notebook"] = args.id
        payload["name"] = args.name
    else:  # icon
        payload["notebook"] = args.id
        payload["icon"] = args.icon
    print_json(client.call(endpoint, payload))
    # 新建/重命名/开关会影响权限表里的库集合与状态
    resync = getattr(client, "resync", None)
    if resync:
        resync()


def cmd_asset(args):
    op = f"asset_{args.action}"
    client, entry, _ = prepare(args, op)
    guard = Guard(client, entry, op)
    a = args.action
    if a == "unused":
        guard.any_perm("R")
        print_json(client.call("/api/asset/getUnusedAssets"))
    elif a == "stat":
        guard.any_perm("R")
        # 实测：statAsset 要工作区相对路径（assets/x.png），带前导 / 会 code=1
        print_json(client.call("/api/asset/statAsset", {"path": args.path.lstrip("/")}))
    else:
        guard.any_perm("D")
        _need_yes(args, "清理未引用资源")
        assets = client.call("/api/asset/getUnusedAssets") or []
        removed = []
        for item in assets:
            path = item.get("path") if isinstance(item, dict) else str(item)
            if not path:
                continue
            client.call("/api/asset/removeUnusedAsset", {"path": path})
            removed.append(path)
        print_json({"removed": removed, "count": len(removed)})


def cmd_sync(args):
    op = f"sync_{args.action}"
    client, entry, _ = prepare(args, op)
    guard = Guard(client, entry, op)
    if args.action == "status":
        guard.any_perm("R")
        print_json(client.call("/api/sync/getSyncInfo"))
        return
    guard.any_perm("D")
    _need_yes(args, "触发数据同步")
    print_json(client.call("/api/sync/performSync"))


def cmd_bookmark(args):
    op = f"bookmark_{args.action}"
    client, entry, _ = prepare(args, op)
    guard = Guard(client, entry, op)
    a = args.action
    if a == "ls":
        guard.any_perm("R")
        print_json(client.call("/api/bookmark/getBookmark"))
    elif a == "rename":
        guard.any_perm("W")
        print_json(
            client.call("/api/bookmark/renameBookmark", {"oldLabel": args.old, "newLabel": args.new})
        )
    else:
        guard.any_perm("D")
        _need_yes(args, "删除书签")
        print_json(client.call("/api/bookmark/removeBookmark", {"label": args.label}))


def cmd_system(args):
    client, entry, _ = prepare(args, "system")
    Guard(client, entry, "system").any_perm("R")
    print_json(
        {
            "version": client.version(),
            "workspace": client.call("/api/system/getWorkspaceInfo"),
            "time": client.call("/api/system/currentTime"),
        }
    )


def _resolve_av_id(client, block_id: str) -> str:
    """数据库块的块 id 与 av id 不同：从 block DOM 的 data-av-id 里取真正的 avID。"""
    if client.call("/api/av/getAttributeView", {"id": block_id}).get("av"):
        return block_id
    dom = _str_of(client.call("/api/block/getBlockDOM", {"id": block_id}), "dom")
    m = re.search(r'data-av-id="([^"]+)"', dom)
    return m.group(1) if m else block_id


def cmd_av(args):
    """数据库（属性视图）全部动作走内核 MCP：HTTP API 只读且参数口径不一。"""
    op = f"av_{args.action}"
    client, entry, _ = prepare(args, op)
    guard = Guard(client, entry, op)
    a = args.action
    if a == "create":
        guard.by_block(args.parent, ["W"])
        payload = {"action": "create", "parentID": args.parent, "name": args.name}
        if args.layout:
            payload["layout"] = args.layout
        if args.keys:
            try:
                payload["keys"] = json.loads(args.keys)
            except json.JSONDecodeError as e:
                raise SiYuanError(f"--keys 需为 JSON 数组：[{{\"name\":\"状态\",\"type\":\"text\"}}]：{e}") from e
        res = client.mcp_call("database", payload)
        data = (res or {}).get("data", res) or {}
        if args.json:
            print_json(data)
        else:
            print(f"✅ 已创建数据库 {data.get('avID')}（块 {data.get('blockID')}）")
        return
    if a == "search":
        guard.any_perm("R")
        print_json(client.mcp_call("database", {"action": "search", "keyword": args.keyword}))
        return
    perm = {
        "view": "R", "keys": "R",
        "add-item": "W", "add-key": "W",
        "rm-item": "D", "rm-key": "D",
    }[a]
    guard.by_block(args.id, [perm])
    av_id = _resolve_av_id(client, args.id)
    if a in ("view", "keys"):
        payload = {"action": "get" if a == "view" else "keys", "id": av_id}
        print_json(client.mcp_call("database", payload))
    elif a == "add-key":
        print_json(
            client.mcp_call(
                "database", {"action": "key_add", "id": av_id, "name": args.name, "icon": args.icon or ""}
            )
        )
    elif a == "rm-key":
        _need_yes(args, "删除数据库字段")
        print_json(client.mcp_call("database", {"action": "key_remove", "id": av_id, "keyID": args.key_id}))
    elif a == "add-item":
        payload = {"action": "item_add", "id": av_id}
        if args.block:
            payload["blockID"] = args.block
        if args.content:
            payload["content"] = args.content
        if args.detached:
            payload["detached"] = True
        print_json(client.mcp_call("database", payload))
    else:  # rm-item
        _need_yes(args, "删除数据库条目")
        print_json(
            client.mcp_call("database", {"action": "item_remove", "id": av_id, "itemIDs": args.item_ids})
        )


MCP_TOOL_PERM = {
    "asset": "W", "attr": "W", "block": "W", "bookmark": "W", "dailynote": "W",
    "database": "W", "document": "W", "export": "R", "file": "W", "history": "R",
    "http_request": "W", "image": "W", "import": "W", "inbox": "W", "notebook": "W",
    "outline": "R", "ref": "R", "repo": "D", "search": "R", "skill": "R", "sql": "R",
    "sync": "D", "system": "R", "tag": "W", "template": "W", "unzip": "W",
    "web_fetch": "R", "web_search": "R", "workspace": "W",
}


def _mcp_perm(tool: str, action: str, explicit: str) -> str:
    if explicit:
        perm = explicit.upper()
        if perm not in VALID_PERMS:
            raise SiYuanError(f"--perm 只允许 {'/'.join(VALID_PERMS)}")
        return perm
    act = (action or "").lower()
    if any(k in act for k in ("delete", "remove", "purge", "clear", "rollback", "checkout")):
        return "D"
    if any(k in act for k in ("create", "add", "append", "update", "set", "rename", "move", "write", "import", "upload", "insert", "prepend", "replace", "sort", "copy")):
        return "W"
    return MCP_TOOL_PERM.get(tool, "R")


def cmd_mcp(args):
    """通用 MCP 工具调用：补齐 HTTP API 未开放的少量内核能力。"""
    tool = args.tool
    payload = {}
    if args.body:
        try:
            payload = json.loads(args.body)
        except json.JSONDecodeError as e:
            raise SiYuanError(f"--body 需为 JSON 对象: {e}") from e
    for kv in args.arg or []:
        k, sep, v = kv.partition("=")
        if not sep:
            raise SiYuanError(f"--arg 写法应为 key=value：{kv}")
        try:
            payload[k.strip()] = json.loads(v)
        except json.JSONDecodeError:
            payload[k.strip()] = v
    op = f"mcp:{tool}:{payload.get('action', '')}"
    client, entry, _ = prepare(args, op)
    guard = Guard(client, entry, op)
    perm = _mcp_perm(tool, str(payload.get("action", "")), args.perm)
    if perm == "D":
        _need_yes(args, f"MCP {tool}")
    if args.notebook:
        guard.by_notebook(args.notebook, [perm])
    else:
        guard.any_perm(perm)
    print_json(client.mcp_call(tool, payload))


def cmd_import(args):
    client, entry, _ = prepare(args, "import_md")
    Guard(client, entry, "import_md").by_notebook(args.notebook, ["W"])
    payload = {"notebook": args.notebook, "localPath": args.path}
    if args.target:
        payload["toPath"] = args.target
    print_json(client.call("/api/import/importStdMd", payload))


# endpoint → 所需权限；只收录已在 3.8.2 实测存在的端点
CALL_TABLE = {
    # 只读
    "/api/search/searchAsset": "R",
    "/api/av/getAttributeViewPrimaryKeyValues": "R",
    "/api/system/currentTime": "R",
    "/api/system/version": "R",
    "/api/system/getWorkspaceInfo": "R",
    "/api/outline/getDocOutline": "R",
    "/api/ref/getBacklink": "R",
    "/api/ref/getBacklinkDoc": "R",
    "/api/block/getBlockKramdown": "R",
    "/api/block/getBlockDOM": "R",
    "/api/block/getChildBlocks": "R",
    "/api/block/getBlockBreadcrumb": "R",
    "/api/export/exportMdContent": "R",
    "/api/export/exportDocx": "W",
    "/api/history/getHistoryItems": "R",
    "/api/history/getHistoryDirs": "R",
    "/api/repo/getRepoSnapshots": "R",
    "/api/repo/diffRepoSnapshots": "R",
    "/api/repo/openRepoSnapshotFile": "R",
    "/api/export/exportMd": "W",
    "/api/export/exportHTML": "R",
    "/api/export/exportSY": "W",
    "/api/export/exportResources": "R",
    "/api/search/searchRefBlock": "R",
    "/api/search/searchWidget": "R",
    "/api/block/getBlockIndex": "R",
    "/api/block/getDocsInfo": "R",
    "/api/filetree/listDocTree": "R",
    "/api/filetree/getDoc": "R",
    "/api/filetree/getPathByID": "R",
    "/api/filetree/getHPathByID": "R",
    "/api/attr/getBlockAttrs": "R",
    "/api/notebook/lsNotebooks": "R",
    "/api/system/bootProgress": "R",
    "/api/system/getConf": "R",
    "/api/system/getWorkspaces": "R",
    # 写入
    "/api/block/insertBlock": "W",
    "/api/block/prependBlock": "W",
    "/api/block/appendBlock": "W",
    "/api/block/updateBlock": "W",
    "/api/block/foldBlock": "W",
    "/api/attr/setBlockAttrs": "W",
    "/api/filetree/createDocWithMd": "W",
    "/api/filetree/renameDoc": "W",
    "/api/filetree/moveDocsByID": "W",
    "/api/file/putFile": "W",
    "/api/file/renameFile": "W",
    "/api/file/copyFile": "W",
    "/api/template/renderSprig": "R",
    "/api/template/docSaveAsTemplate": "W",
    "/api/notebook/createNotebook": "W",
    "/api/notebook/renameNotebook": "W",
    "/api/notebook/setNotebookIcon": "W",
    "/api/tag/renameTag": "W",
    "/api/av/addAttributeViewKey": "W",
    "/api/av/addAttributeViewBlocks": "W",
    "/api/asset/uploadCloud": "W",
    "/api/history/rollbackDocByCreated": "D",
    # 删除 / 破坏性
    "/api/block/deleteBlock": "D",
    "/api/filetree/removeDoc": "D",
    "/api/filetree/removeDocs": "D",
    "/api/file/removeFile": "D",
    "/api/notebook/removeNotebook": "D",
    "/api/tag/removeTag": "D",
    "/api/asset/removeUnusedAsset": "D",
    "/api/av/removeAttributeViewKey": "D",
    "/api/av/removeAttributeViewBlocks": "D",
    "/api/repo/purgeRepo": "D",
    "/api/riff/removeRiffCards": "D",
    # 只读补充
    "/api/riff/getRiffDecks": "R",
    "/api/riff/getRiffCards": "R",
    "/api/riff/createRiffDeck": "W",
    "/api/riff/addRiffCards": "W",
}


def cmd_call(args):
    endpoint = args.endpoint.strip()
    if endpoint not in CALL_TABLE:
        raise SiYuanError(
            f"端点 {endpoint} 未在白名单内（有风险或未实测）。可用端点见 "
            "references/siyuan-api.md 的「call 白名单」小节"
        )
    perm = CALL_TABLE[endpoint]
    client, entry, _ = prepare(args, f"call:{endpoint}")
    guard = Guard(client, entry, f"call:{endpoint}")
    if args.notebook:
        guard.by_notebook(args.notebook, [perm])
    else:
        guard.any_perm(perm)
    if perm == "D" and not args.notebook:
        _need_yes(args, f"调用 {endpoint}")
    payload: dict = {}
    if args.body:
        try:
            payload = json.loads(args.body)
        except json.JSONDecodeError as e:
            raise SiYuanError(f"--body 不是合法 JSON：{e}") from e
    for kv in args.arg or []:
        k, sep, v = kv.partition("=")
        if not sep:
            raise SiYuanError(f"--arg 写法应为 key=value：{kv}")
        try:
            payload[k] = json.loads(v)
        except json.JSONDecodeError:
            payload[k] = v
    print_json(client.call(endpoint, payload))


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

    # ── 数据层通用能力（对齐内置 MCP 工具面）──
    sp = cmd("outline", "文档大纲层级（需 R）", cmd_outline)
    sp.add_argument("id")

    sp = cmd("backlinks", "反链查询（需 R）", cmd_backlinks)
    sp.add_argument("id")
    sp.add_argument("--by-doc", action="store_true", help="按文档聚合反链")
    sp.add_argument("--keyword", default="")
    sp.add_argument("--sort", type=int, default=0)

    sp = cmd("attr", "块属性：get（R）/ set（W）/ batch（R）", cmd_attr)
    asub = sp.add_subparsers(dest="action", required=True)
    ag = asub.add_parser("get", parents=[common])
    ag.add_argument("id")
    ag.set_defaults(func=cmd_attr, action="get")
    ase = asub.add_parser("set", parents=[common])
    ase.add_argument("id")
    ase.add_argument("pairs", nargs="+", help="key=value …，如 name=标题 custom-progress=30")
    ase.set_defaults(func=cmd_attr, action="set")
    ab = asub.add_parser("batch", parents=[common])
    ab.add_argument("ids", help="逗号分隔的块 id")
    ab.set_defaults(func=cmd_attr, action="batch")

    sp = cmd("block", "块操作：kramdown/children/dom/breadcrumb/info（R）、insert（W）、delete（D）", cmd_block)
    bsub = sp.add_subparsers(dest="action", required=True)
    for act, helptext in (
        ("kramdown", "块源码（含 IAL）"),
        ("children", "子块列表"),
        ("dom", "渲染后 DOM 摘要"),
        ("breadcrumb", "面包屑路径"),
        ("info", "块元信息"),
    ):
        bp = bsub.add_parser(act, help=helptext, parents=[common])
        bp.add_argument("id")
        bp.set_defaults(func=cmd_block, action=act)
    bd = bsub.add_parser("delete", help="删除块（D，需 --yes）", parents=[common])
    bd.add_argument("id")
    bd.add_argument("--yes", "-y", action="store_true")
    bd.set_defaults(func=cmd_block, action="delete")
    bi = bsub.add_parser("insert", help="插入块（W）", parents=[common])
    bi.add_argument("--parent", default="", help="父块/文档 id")
    bi.add_argument("--previous-id", default="", help="插到该块之后")
    bi.add_argument("--next-id", default="", help="插到该块之前")
    bi.add_argument("--data-type", default="markdown", choices=["markdown", "dom"])
    add_content(bi)
    bi.set_defaults(func=cmd_block, action="insert")

    sp = cmd("export", "导出文档：md/html（R）/ docx（W，写工作区文件）", cmd_export)
    sp.add_argument("format", choices=["md", "html", "docx"])
    sp.add_argument("doc_id")
    sp.add_argument("--output", "-o", default="", help="docx 输出路径（工作区相对）")
    sp.add_argument("--pdf", action="store_true", help="html 导出按 PDF 版式处理")

    sp = cmd("template", "模板：ls/get/render/save-as/rm", cmd_template)
    tsub = sp.add_subparsers(dest="action", required=True)
    for act, helptext in (("ls", "列出模板"), ("get", "读取模板源码")):
        tp = tsub.add_parser(act, parents=[common])
        tp.set_defaults(func=cmd_template, action=act)
    tr = tsub.add_parser("render", parents=[common])
    tr.add_argument("path")
    tr.add_argument("--id", default="", help="渲染上下文的块 id")
    tr.set_defaults(func=cmd_template, action="render")
    tsa = tsub.add_parser("save-as", help="文档存为模板（W）", parents=[common])
    tsa.add_argument("id")
    tsa.add_argument("name")
    tsa.add_argument("--overwrite", action="store_true")
    tsa.set_defaults(func=cmd_template, action="save-as")
    trm = tsub.add_parser("rm", help="删除模板（D，需 --yes）", parents=[common])
    trm.add_argument("path")
    trm.add_argument("--yes", "-y", action="store_true")
    trm.set_defaults(func=cmd_template, action="rm")

    sp = cmd("file", "工作区文件：ls/read（R）、write/mv/cp（W）、rm（D）", cmd_file)
    fsub = sp.add_subparsers(dest="action", required=True)
    for act in ("ls", "read"):
        fp = fsub.add_parser(act, parents=[common])
        fp.add_argument("path")
        fp.set_defaults(func=cmd_file, action=act)
    fw = fsub.add_parser("write", parents=[common])
    fw.add_argument("path")
    add_content(fw)
    fw.set_defaults(func=cmd_file, action="write")
    for act in ("mv", "cp"):
        fp = fsub.add_parser(act, parents=[common])
        fp.add_argument("path")
        fp.add_argument("dest")
        fp.set_defaults(func=cmd_file, action=act)
    fr = fsub.add_parser("rm", parents=[common])
    fr.add_argument("path")
    fr.add_argument("--yes", "-y", action="store_true")
    fr.set_defaults(func=cmd_file, action="rm")

    sp = cmd("notebook", "笔记本：open/close/create/rename/icon（W）", cmd_notebook)
    nsub = sp.add_subparsers(dest="action", required=True)
    for act in ("open", "close", "rename"):
        np_ = nsub.add_parser(act, parents=[common])
        np_.add_argument("id")
        if act == "rename":
            np_.add_argument("name")
        np_.set_defaults(func=cmd_notebook, action=act)
    nc = nsub.add_parser("create", parents=[common])
    nc.add_argument("name")
    nc.set_defaults(func=cmd_notebook, action="create")
    ni = nsub.add_parser("icon", parents=[common])
    ni.add_argument("id")
    ni.add_argument("icon")
    ni.set_defaults(func=cmd_notebook, action="icon")

    sp = cmd("asset", "资源：unused/stat（R）、clean（D）", cmd_asset)
    asub2 = sp.add_subparsers(dest="action", required=True)
    au = asub2.add_parser("unused", parents=[common])
    au.set_defaults(func=cmd_asset, action="unused")
    ast = asub2.add_parser("stat", parents=[common])
    ast.add_argument("path")
    ast.set_defaults(func=cmd_asset, action="stat")
    ac = asub2.add_parser("clean", parents=[common])
    ac.add_argument("--yes", "-y", action="store_true")
    ac.set_defaults(func=cmd_asset, action="clean")

    sp = cmd("sync", "数据同步：status（R）/ perform（D，需 --yes）", cmd_sync)
    ssub2 = sp.add_subparsers(dest="action", required=True)
    ss = ssub2.add_parser("status", parents=[common])
    ss.set_defaults(func=cmd_sync, action="status")
    spf = ssub2.add_parser("perform", parents=[common])
    spf.add_argument("--yes", "-y", action="store_true")
    spf.set_defaults(func=cmd_sync, action="perform")

    sp = cmd("bookmark", "书签：ls（R）/ rename（W）/ rm（D）", cmd_bookmark)
    ksub = sp.add_subparsers(dest="action", required=True)
    kl = ksub.add_parser("ls", parents=[common])
    kl.set_defaults(func=cmd_bookmark, action="ls")
    kr = ksub.add_parser("rename", parents=[common])
    kr.add_argument("old")
    kr.add_argument("new")
    kr.set_defaults(func=cmd_bookmark, action="rename")
    krm = ksub.add_parser("rm", parents=[common])
    krm.add_argument("label")
    krm.add_argument("--yes", "-y", action="store_true")
    krm.set_defaults(func=cmd_bookmark, action="rm")

    cmd("system", "版本/工作区/当前时间（需 R）", cmd_system)

    sp = cmd("av", "数据库（属性视图，走内置 MCP）：create/view/keys/add-item/add-key/rm-item/rm-key", cmd_av)
    vsub = sp.add_subparsers(dest="action", required=True)
    vc = vsub.add_parser("create", help="创建数据库（W）", parents=[common])
    vc.add_argument("parent", help="父文档/块 id")
    vc.add_argument("name")
    vc.add_argument("--layout", default="", choices=["", "table", "gallery", "kanban"])
    vc.add_argument("--keys", default="", help='JSON：[{"name":"状态","type":"text"}]')
    vc.set_defaults(func=cmd_av, action="create")
    for act in ("view", "keys"):
        vp = vsub.add_parser(act, parents=[common])
        vp.add_argument("id")
        vp.set_defaults(func=cmd_av, action=act)
    vs = vsub.add_parser("search", help="按名称搜索数据库（R）", parents=[common])
    vs.add_argument("keyword")
    vs.set_defaults(func=cmd_av, action="search")
    va = vsub.add_parser("add-item", help="新增条目（W）", parents=[common])
    va.add_argument("id")
    va.add_argument("--content", default="", help="主键列文本")
    va.add_argument("--block", default="", help="绑定已有块 id")
    va.add_argument("--detached", action="store_true", help="创建游离条目")
    va.set_defaults(func=cmd_av, action="add-item")
    vr = vsub.add_parser("rm-item", help="删除条目（D，需 --yes）", parents=[common])
    vr.add_argument("id")
    vr.add_argument("item_ids", nargs="+")
    vr.add_argument("--yes", "-y", action="store_true")
    vr.set_defaults(func=cmd_av, action="rm-item")
    vak = vsub.add_parser("add-key", help="新增字段（W）", parents=[common])
    vak.add_argument("id")
    vak.add_argument("name")
    vak.add_argument("--icon", default="")
    vak.set_defaults(func=cmd_av, action="add-key")
    vrk = vsub.add_parser("rm-key", help="删除字段（D，需 --yes）", parents=[common])
    vrk.add_argument("id")
    vrk.add_argument("key_id")
    vrk.add_argument("--yes", "-y", action="store_true")
    vrk.set_defaults(func=cmd_av, action="rm-key")

    sp = cmd("mcp", "直接调用内核内置 MCP 工具（补齐 HTTP API 缺口）", cmd_mcp)
    sp.add_argument("tool", help="MCP 工具名，如 database / skill / web_search")
    sp.add_argument("--action", default="", help="动作名（用于权限推断）")
    sp.add_argument("--body", default="", help="JSON body")
    sp.add_argument("--arg", action="append", metavar="K=V", help="拼 body（值按 JSON 解析）")
    sp.add_argument("--notebook", default="", help="按该笔记本校验权限（缺省=任一同级权限）")
    sp.add_argument("--perm", default="", help="显式指定 R/W/D（默认按动作推断）")
    sp.add_argument("--yes", "-y", action="store_true", help="D 类操作必须确认")

    sp = cmd("import", "导入本地 Markdown 目录/文件（需目标笔记本 W）", cmd_import)
    sp.add_argument("notebook")
    sp.add_argument("path", help="服务器本地绝对路径")
    sp.add_argument("--target", default="", help="导入到该 hpath 下")

    sp = cmd("call", "通用端点调用（白名单 + 权限审计）", cmd_call)
    sp.add_argument("endpoint", help="如 /api/riff/getRiffDecks")
    sp.add_argument("--body", default="", help="JSON body")
    sp.add_argument("--arg", action="append", metavar="K=V", help="拼 body（值按 JSON 解析）")
    sp.add_argument("--notebook", default="", help="按该笔记本校验权限（缺省=任一同级权限）")
    sp.add_argument("--yes", "-y", action="store_true", help="D 类端点必须确认")

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
