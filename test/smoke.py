#!/usr/bin/env python3
"""siyuan-skill 端到端冒烟测试：本地 mock SiYuan + 真实 CLI 子进程。

验证：
  1. 首次同步后 reviewed=false 时数据操作被拒绝并给出引导
  2. 权限表按实例地址分组写入 ~/.config/.siyuan.json（测试用临时路径）
  3. R/W/D/NONE 各类守卫（含 docId 反查、全局 all-W、move 的 D+W）
  4. 上传 multipart 字段名必须是 file[]，空 succMap 必须报错
  5. 新笔记本自动补 R、present 标记

运行：python3 test/smoke.py
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CLI = ROOT / "scripts" / "siyuan.py"
ENV = {**os.environ, "SIYUAN_API_URL": "http://127.0.0.1:18606/", "SIYUAN_TOKEN": "test-token"}

STATE = {
    "notebooks": [{"id": "nb1", "name": "工作"}, {"id": "nb2", "name": "敏感"}],
    "empty_upload": False,
    "moved": None,
    "net_assets": 0,
    "requests": [],
}
BOX = {"doc1": "nb1", "doc2": "nb2", "doc3": "nb1", "doc5": "nb4"}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, data):
        body = json.dumps({"code": 0, "msg": "", "data": data}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(n)
        path = self.path
        STATE["requests"].append((path, self.headers.get("Authorization"), raw))
        assert self.headers.get("Authorization") == "Token test-token", path

        if path == "/api/system/version":
            return self._send("3.8.2")
        if path == "/api/notebook/lsNotebooks":
            return self._send({"notebooks": STATE["notebooks"]})
        if path == "/api/query/sql":
            stmt = json.loads(raw)["stmt"]
            if "from blocks where id=" in stmt:
                for did, box in BOX.items():
                    if f"'{did}'" in stmt:
                        return self._send(
                            [
                                {
                                    "box": box,
                                    "path": f"/{did}.sy",
                                    "hpath": f"/{did}",
                                }
                            ]
                        )
                return self._send([])
            if "type='d'" in stmt and "order by updated" in stmt:
                return self._send(
                    [{"id": "doc1", "box": "nb1", "content": "最近", "updated": "2026"}]
                )
            if "type='d'" in stmt and "content='" in stmt:
                return self._send([])  # daily note 不存在 → 创建
            if "content like" in stmt:
                # 真实 SiYuan 内容命中返回块 id + root_id（文档 id）
                return self._send(
                    [
                        {
                            "id": "blockX",
                            "root_id": "doc1",
                            "box": "nb1",
                            "content": "hello world",
                        }
                    ]
                )
            return self._send([])
        if path == "/api/export/exportMdContent":
            did = json.loads(raw)["id"]
            return self._send({"content": f"# {did}\n\n正文"})
        if path == "/api/filetree/createDocWithMd":
            return self._send("newdoc123")
        if path == "/api/filetree/listDocsByPath":
            return self._send({"files": [{"path": "/a", "name": "a"}]})
        if path == "/api/filetree/moveDocs":
            payload = json.loads(raw)
            assert "fromPaths" in payload, "3.8 的 moveDocs 必须用 fromPaths"
            assert "fromIDs" not in payload, "传 fromIDs 会内核 panic"
            STATE["moved"] = payload
            return self._send(None)
        if path == "/api/block/getBlockKramdown":
            return self._send(
                {"id": json.loads(raw)["id"], "kramdown": "network-asset-" * STATE["net_assets"]}
            )
        if path in ("/api/format/netImg2LocalAssets", "/api/format/netAssets2LocalAssets"):
            assert json.loads(raw)["id"], "netImg2LocalAssets 必须带 id"
            STATE["net_assets"] += 2
            return self._send(None)
        if path == "/api/asset/upload":
            assert b'name="file[]"' in raw, "multipart 字段名必须是 file[]"
            if STATE["empty_upload"]:
                return self._send({"errFiles": [], "succMap": {}})
            return self._send(
                {"errFiles": [], "succMap": {"pic.png": "assets/pic-abc.png"}}
            )
        return self._send(None)


def run(*args, expect=0):
    p = subprocess.run(
        [sys.executable, str(CLI), *args],
        env=ENV,
        capture_output=True,
        text=True,
        cwd=str(ROOT),
        check=False,
    )
    if p.returncode != expect:
        raise AssertionError(
            f"cmd {args} 期望 exit {expect}，实际 {p.returncode}\n"
            f"stdout:\n{p.stdout}\nstderr:\n{p.stderr}"
        )
    return p


def main():
    srv = HTTPServer(("127.0.0.1", 18606), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    tmp = tempfile.mkdtemp()
    cfg = os.path.join(tmp, ".siyuan.json")

    # 1. 首次同步前的数据操作：同步写入权限表，但 reviewed=false → 拒绝
    p = run("search", "hello", "--config", cfg, expect=2)
    assert "权限表尚未确认" in p.stderr and "perms confirm" in p.stderr
    store = json.loads(Path(cfg).read_text())
    entry = store["servers"]["http://127.0.0.1:18606"]  # URL 规范化：去掉尾部 /
    assert entry["reviewed"] is False
    assert {n["id"]: n["permissions"] for n in entry["notebooks"]} == {
        "nb1": ["R"],
        "nb2": ["R"],
    }
    assert all(n["present"] for n in entry["notebooks"])
    assert oct(Path(cfg).stat().st_mode & 0o777) == "0o600", "配置文件应为 600"

    # 2. ping 正常（审计豁免），仍提示未确认
    p = run("ping", "--config", cfg)
    assert "3.8.2" in p.stdout and "尚未确认" in p.stderr

    # 3. perms show / set / confirm
    p = run("perms", "show", "--config", cfg)
    assert "未确认" in p.stdout and "nb1" in p.stdout
    run("perms", "set", "nb1", "R,W", "--config", cfg)
    run("perms", "set", "nb2", "NONE", "--config", cfg)
    run("perms", "confirm", "--config", cfg)
    assert json.loads(Path(cfg).read_text())["servers"]["http://127.0.0.1:18606"]["reviewed"]

    # 4. 全局读：nb1 有 R → 放行
    p = run("search", "hello", "--config", cfg)
    assert "doc1" in p.stdout
    # 全局参数放在子命令之前也生效，--json 可解析
    p = run("--json", "search", "hello", "--config", cfg)
    row = json.loads(p.stdout)["results"][0]
    assert row["docId"] == "doc1" and row["blockId"] == "blockX"

    # 5. 按块审计：nb2=NONE 拒绝，nb1 放行
    p = run("read", "doc2", "--config", cfg, expect=2)
    for part in ("nb2", "敏感", "get_document_content", "需要 R", "缺失 R"):
        assert part in p.stderr, (part, p.stderr)
    assert "# doc1" in run("read", "doc1", "--config", cfg).stdout
    # 普通读不应额外拉笔记本列表（按需同步）
    STATE["requests"].clear()
    run("read", "doc1", "--config", cfg)
    assert all("/api/notebook/lsNotebooks" not in r[0] for r in STATE["requests"]), (
        STATE["requests"]
    )

    # 6. 写类：nb2 拒绝，nb1 放行
    p = run("append", "doc2", "-c", "x", "--config", cfg, expect=2)
    assert "需要 W" in p.stderr
    run("append", "doc1", "-c", "x", "--config", cfg)

    # 7. 所有 W：nb2=NONE → retag 拒绝
    p = run("retag", "old", "new", "--config", cfg, expect=2)
    assert "所有笔记本都有 W" in p.stderr and "nb2" in p.stderr

    # 8. move：nb1 无 D → 拒绝
    p = run("move", "nb2", "doc1", "--config", cfg, expect=2)
    assert "需要 D" in p.stderr

    # 9. upload 正常：file[] + succMap
    img = Path(tmp) / "pic.png"
    img.write_bytes(b"\x89PNG\r\n")
    p = run("upload", "nb1", str(img), "--config", cfg)
    assert "assets/pic-abc.png" in p.stdout
    # 空 succMap 必须报错（静默失败防线）
    STATE["empty_upload"] = True
    p = run("upload", "nb1", str(img), "--config", cfg, expect=1)
    assert "空 succMap" in p.stderr
    STATE["empty_upload"] = False

    # 9b. 网络资源转本地：converted = network-asset-* 数量差
    p = run("asset", "netimg", "doc1", "--config", cfg)
    assert '"converted": 2' in p.stdout, p.stdout
    p = run("asset", "netimg", "doc2", "--config", cfg, expect=2)  # nb2 只读
    assert "需要 W" in p.stderr

    # 10. 标签/快照/日记等其余端点走通
    run("tags", "--config", cfg)
    run("snapshot", "list", "--config", cfg)
    run("snapshot", "create", "--config", cfg)
    p = run("snapshot", "rollback", "s1", "--config", cfg, expect=2)  # 所有 D 不满足
    assert p.returncode == 2 and "所有笔记本都有 D" in p.stderr
    run("daily", "nb1", "-c", "今天", "--config", cfg)
    run("tree", "nb1", "--config", cfg)
    run("recent", "--config", cfg)

    # 11. 新笔记本自动补 R（并提示）
    STATE["notebooks"].append({"id": "nb3", "name": "新库"})
    p = run("notebooks", "--config", cfg)
    assert "nb3" in p.stdout
    entry = json.loads(Path(cfg).read_text())["servers"]["http://127.0.0.1:18606"]
    nb3 = next(n for n in entry["notebooks"] if n["id"] == "nb3")
    assert nb3["permissions"] == ["R"]

    # 11b. 未同步过的新库：首次访问按需同步一次并按默认 R 放行
    STATE["notebooks"].append({"id": "nb4", "name": "后加的库"})
    STATE["requests"].clear()
    p = run("read", "doc5", "--config", cfg)
    assert "正文" in p.stdout, p.stdout
    assert sum("/api/notebook/lsNotebooks" in r[0] for r in STATE["requests"]) == 1
    entry = json.loads(Path(cfg).read_text())["servers"]["http://127.0.0.1:18606"]
    assert {n["id"]: n["permissions"] for n in entry["notebooks"]}["nb4"] == ["R"]

    # 12. 已确认状态下新增笔记本不会重置 reviewed，但 all-W/D 会因它失败
    assert entry["reviewed"] is True
    STATE["requests"].clear()
    p = run("retag", "a", "b", "--config", cfg, expect=2)
    assert "nb3" in p.stderr
    # 全库 W 判定必须强制同步一次，避免新库绕过
    assert sum("/api/notebook/lsNotebooks" in r[0] for r in STATE["requests"]) == 1

    # 13. 未知笔记本 / 块反查失败 → 拒绝
    p = run("read", "no-such-doc", "--config", cfg, expect=2)
    assert "无法反查所属笔记本" in p.stderr

    # 14. move 成功路径：源库 D + 目标库 W → fromPaths + toNotebook + toPath
    run("perms", "set", "nb1", "R,W,D", "--config", cfg)
    run("perms", "set", "nb2", "R,W,D", "--config", cfg)
    run("perms", "set", "nb3", "R,W,D", "--config", cfg)
    p = run("move", "nb2", "doc1", "--config", cfg)
    assert "已移动 1 个文档" in p.stdout
    assert STATE["moved"] == {
        "fromPaths": ["/doc1.sy"],
        "toNotebook": "nb2",
        "toPath": "/",
    }, STATE["moved"]
    # --to-doc：toPath 用父文档存储路径
    p = run("move", "nb1", "doc1", "--to-doc", "doc3", "--config", cfg)
    assert STATE["moved"]["toPath"] == "/doc3.sy", STATE["moved"]

    srv.shutdown()
    print("smoke: all checks passed ✅")


if __name__ == "__main__":
    main()
