"""在临时目录执行网盘关键流程检查，不接触生产数据。"""

import io
import os
import stat
import tempfile
from pathlib import Path
from unittest.mock import patch

from app import create_app


def main():
    """运行账号权限、文件隔离、上传完整性和删除安全检查。"""
    if os.name != "posix":
        raise SystemExit("请在 Linux 上运行：python check.py")
    # Step1：建立临时环境并初始化唯一超级管理员。
    with tempfile.TemporaryDirectory(prefix="simple-drive-check-") as temporary:
        base = Path(temporary)
        root_a = base / "alice"
        root_b = base / "bob"
        root_a.mkdir()
        root_b.mkdir()
        with patch.dict(os.environ, {"DRIVE_UPLOAD_BLOCK_PATTERNS": "Secrets.*, private-config.json"}):
            app = create_app({"TESTING": True, "SECRET_KEY": "check-secret-" * 4, "STATE_DIR": str(base / "state"), "MAX_FILE_BYTES": 32, "SESSION_COOKIE_SECURE": False})
        runner = app.test_cli_runner()
        result = runner.invoke(args=["init-admin", "--username", "admin", "--password", "AdminPass123"])
        assert result.exit_code == 0, result.output
        result = runner.invoke(args=["init-admin", "--username", "admin2", "--password", "AdminPass123"])
        assert result.exit_code != 0

        def client_login(username, password):
            """创建独立客户端，并使用匿名 CSRF 令牌登录。"""
            client = app.test_client()
            csrf = client.get("/api/session").json["csrf"]
            response = client.post("/api/login", json={"username": username, "password": password}, headers={"X-CSRF-Token": csrf})
            assert response.status_code == 200, response.json
            return client, response.json["csrf"]

        def send(client, token, method, url, payload):
            """发送带 CSRF 令牌的 JSON 写请求。"""
            return client.open(url, method=method, json=payload, headers={"X-CSRF-Token": token})

        def push(client, token, path, content, overwrite=False, filename="upload.bin"):
            """使用 multipart 请求上传字节内容。"""
            # Step1：模拟启动脚本的严格 umask，验证显式设置权限有效。
            previous_umask = os.umask(0o077)
            try:
                return client.post("/api/upload", data={"path": path, "overwrite": "1" if overwrite else "0", "file": (io.BytesIO(content), filename)}, headers={"X-CSRF-Token": token})
            finally:
                # Step2：恢复检查进程原有 umask。
                os.umask(previous_umask)

        # Step2：验证未登录、CSRF、防止管理员越权及目录重叠。
        anonymous = app.test_client()
        assert anonymous.get("/api/files").status_code == 401
        assert anonymous.post("/api/login", json={}).status_code == 403
        admin, admin_token = client_login("admin", "AdminPass123")
        assert admin.get("/api/files").status_code == 403
        for name, root in (("alice", root_a), ("bob", root_b)):
            response = send(admin, admin_token, "POST", "/api/users", {"username": name, "password": "UserPass123", "root": str(root), "enabled": True})
            assert response.status_code == 201, response.json
        users = admin.get("/api/users").json["users"]
        assert len(users) == 2 and all("password_hash" not in user for user in users)
        child = root_a / "child"
        child.mkdir()
        response = send(admin, admin_token, "POST", "/api/users", {"username": "overlap", "password": "UserPass123", "root": str(child), "enabled": True})
        assert response.status_code == 400
        assert send(admin, admin_token, "POST", "/api/users", {"username": "duplicate", "password": "UserPass123", "root": str(root_a), "enabled": True}).status_code == 400

        # Step3：验证中文名称、嵌套上传、同名覆盖、超限及目录穿越。
        alice, alice_token = client_login("alice", "UserPass123")
        bob, bob_token = client_login("bob", "UserPass123")
        assert alice.get("/api/users").status_code == 403
        assert push(alice, alice_token, "资料/子目录/中文.txt", b"original").status_code == 201
        assert (root_a / "资料/子目录/中文.txt").read_bytes() == b"original"
        assert stat.S_IMODE((root_a / "资料/子目录/中文.txt").stat().st_mode) == 0o644
        assert not (root_b / "资料").exists()
        assert push(alice, alice_token, "资料/子目录/中文.txt", b"new").json["conflict"] is True
        assert (root_a / "资料/子目录/中文.txt").read_bytes() == b"original"
        assert push(alice, alice_token, "资料/子目录/中文.txt", b"new", True).status_code == 201
        assert (root_a / "资料/子目录/中文.txt").read_bytes() == b"new"
        assert stat.S_IMODE((root_a / "资料/子目录/中文.txt").stat().st_mode) == 0o644
        assert push(alice, alice_token, "资料/子目录/中文.txt", b"x" * 33, True).status_code == 413
        assert (root_a / "资料/子目录/中文.txt").read_bytes() == b"new"
        assert not list(root_a.rglob(".drive-upload-*"))
        assert push(alice, alice_token, "资料", b"invalid", True).status_code == 409
        for path in ("../bob/leak", "/etc/passwd", "资料/../../bob/leak", "bad\\path", "", ".drive-upload-test"):
            assert push(alice, alice_token, path, b"bad").status_code == 400, path
        assert alice.get("/api/files?path=../bob").status_code == 400
        response = alice.get("/api/download", query_string={"path": "资料/子目录/中文.txt"})
        assert response.status_code == 200 and response.data == b"new"
        response.close()
        assert bob.get("/api/download", query_string={"path": "资料/子目录/中文.txt"}).status_code == 404

        # 上传过滤必须覆盖大小写、嵌套目录、自定义规则和原始文件名。
        blocked_paths = (
            ".env", ".ENV.production", "production.env", ".git/config", "项目/.svn/entries", "项目/.hg/config",
            "项目/.ssh/id_rsa", "项目/.aws/credentials", "项目/.kube/config", "id_rsa", "id_ed25519_sk",
            "server.PEM", "private.key", "cert.p12", "cert.pfx", "keystore.jks", "private.ppk",
            "SECRETS.yaml", "new-folder/private-config.json",
        )
        for path in blocked_paths:
            response = push(alice, alice_token, path, b"blocked")
            assert response.status_code == 422 and response.json["blocked"] is True, (path, response.json)
            assert not (root_a / path).exists()
        assert not (root_a / "项目").exists() and not (root_a / "new-folder").exists()
        # multipart 头中的 Windows 反斜杠必须转义，才能还原真实原文件名。
        for filename in (".env", "local/id_rsa", "C:\\\\fakepath\\\\id_rsa"):
            response = push(alice, alice_token, "innocent.txt", b"blocked", filename=filename)
            assert response.status_code == 422 and not (root_a / "innocent.txt").exists(), response.json
        protected = root_a / ".env"
        protected.write_bytes(b"existing-secret")
        protected.chmod(0o600)
        assert push(alice, alice_token, ".env", b"overwrite", True).status_code == 422
        assert protected.read_bytes() == b"existing-secret" and stat.S_IMODE(protected.stat().st_mode) == 0o600
        for path in (".gitignore", "id_rsa.pub", "photo.png", "notes.txt", "normal/hello.py"):
            assert push(alice, alice_token, path, b"ordinary").status_code == 201, path
            assert stat.S_IMODE((root_a / path).stat().st_mode) == 0o644
        assert not list(root_a.rglob(".drive-upload-*"))

        # Step4：验证新建、重命名、符号链接和根目录删除保护。
        assert send(alice, alice_token, "POST", "/api/folders", {"path": "", "name": "新目录"}).status_code == 201
        assert send(alice, alice_token, "POST", "/api/rename", {"path": "新目录", "name": "改名"}).status_code == 200
        assert send(alice, alice_token, "POST", "/api/rename", {"path": "改名", "name": "资料"}).status_code == 409
        secret = root_b / "secret.txt"
        secret.write_text("private", encoding="utf-8")
        (root_a / "link").symlink_to(root_b, target_is_directory=True)
        (root_a / "file-link").symlink_to(secret)
        assert alice.get("/api/files?path=link").status_code != 200
        assert alice.get("/api/download?path=file-link").status_code != 200
        assert push(alice, alice_token, "link/secret.txt", b"hack", True).status_code != 201
        assert push(alice, alice_token, "file-link", b"hack", True).status_code == 403
        names = [entry["name"] for entry in alice.get("/api/files").json["entries"]]
        assert "link" not in names and "file-link" not in names
        assert send(alice, alice_token, "DELETE", "/api/files", {"path": "", "confirmed": True}).status_code == 400
        assert send(alice, alice_token, "DELETE", "/api/files", {"path": "改名"}).status_code == 400
        (root_a / "资料/link").symlink_to(root_b, target_is_directory=True)
        assert send(alice, alice_token, "DELETE", "/api/files", {"path": "资料", "confirmed": True}).status_code == 200
        assert secret.read_text(encoding="utf-8") == "private"

        # Step5：验证密码修改、停用与删除账号后旧登录失效且数据保留。
        alice_id = next(user["id"] for user in users if user["username"] == "alice")
        user_data = {"username": "alice", "root": str(root_a), "enabled": True, "password": "NewPassword123"}
        assert send(admin, admin_token, "PUT", "/api/users/%s" % alice_id, user_data).status_code == 200
        assert alice.get("/api/files").status_code == 401
        alice, alice_token = client_login("alice", "NewPassword123")
        user_data.update(enabled=False, password="")
        # 目录暂时不可访问也必须允许管理员停用已有账号。
        root_a.rename(base / "temporarily-away")
        assert send(admin, admin_token, "PUT", "/api/users/%s" % alice_id, user_data).status_code == 200
        assert alice.get("/api/files").status_code == 401
        (base / "temporarily-away").rename(root_a)
        user_data["enabled"] = True
        assert send(admin, admin_token, "PUT", "/api/users/%s" % alice_id, user_data).status_code == 200
        alice, alice_token = client_login("alice", "NewPassword123")
        assert send(admin, admin_token, "DELETE", "/api/users/%s" % alice_id, {}).status_code == 200
        assert alice.get("/api/files").status_code == 401 and root_a.exists()
        assert send(admin, admin_token, "DELETE", "/api/users/1", {}).status_code == 404
        assert send(bob, bob_token, "POST", "/api/logout", {}).status_code == 200
        assert bob.get("/api/files").status_code == 401
        # Step6：验证登录失败限制及服务器命令重置管理员密码。
        limited = app.test_client()
        token = limited.get("/api/session").json["csrf"]
        for _ in range(10):
            response = limited.post("/api/login", json={"username": "missing", "password": "bad"}, headers={"X-CSRF-Token": token}, environ_overrides={"REMOTE_ADDR": "198.51.100.15"})
            assert response.status_code == 401
        response = limited.post("/api/login", json={"username": "missing", "password": "bad"}, headers={"X-CSRF-Token": token}, environ_overrides={"REMOTE_ADDR": "198.51.100.15"})
        assert response.status_code == 429
        result = runner.invoke(args=["reset-admin-password", "--password", "AdminNewPass123"])
        assert result.exit_code == 0, result.output
        assert admin.get("/api/users").status_code == 401
        admin, admin_token = client_login("admin", "AdminNewPass123")
        assert admin.get("/api/users").status_code == 200
        print("PASS: 账号权限、目录隔离、上传覆盖、0644 权限、敏感文件过滤、超限保留、路径穿越、符号链接、删除保护、会话失效")


if __name__ == "__main__":
    main()
