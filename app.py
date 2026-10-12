"""Python 3.9/Linux 简易网盘：账号管理、登录及隔离的文件操作。"""

import functools
import fnmatch
import hmac
import os
import secrets
import sqlite3
import stat
from contextlib import contextmanager
from datetime import timedelta
from pathlib import Path

import click
from flask import Flask, g, jsonify, request, send_file, session
from werkzeug.exceptions import HTTPException
from werkzeug.security import check_password_hash, generate_password_hash
from werkzeug.middleware.proxy_fix import ProxyFix


# 默认按路径组件匹配；点文件配置、版本库元数据和密钥均不允许上传。
DEFAULT_UPLOAD_BLOCK_PATTERNS = (
    ".env", ".env.*", "*.env", ".git", ".svn", ".hg", ".ssh", ".aws", ".kube",
    "id_rsa", "id_dsa", "id_ecdsa", "id_ed25519", "id_xmss", "id_ecdsa_sk", "id_ed25519_sk",
    "*.key", "*.pem", "*.p12", "*.pfx", "*.jks", "*.ppk",
)


class DriveError(Exception):
    """携带可显示给用户的错误信息和 HTTP 状态码。"""

    def __init__(self, message, status=400):
        """设置错误信息与状态码。"""
        super().__init__(message)
        self.status = status


def create_app(test_config=None):
    """创建应用，加载环境配置并初始化 SQLite 表结构。"""
    # Step1：读取运行配置，密钥必须由部署环境提供。
    app = Flask(__name__, static_folder="static", static_url_path="/static")
    state_dir = Path(os.environ.get("DRIVE_STATE_DIR", str(Path(__file__).parent / "instance"))).resolve()
    app.config.update(
        SECRET_KEY=os.environ.get("DRIVE_SECRET_KEY"),
        STATE_DIR=str(state_dir),
        MAX_FILE_BYTES=int(os.environ.get("DRIVE_MAX_FILE_MB", "1024")) * 1024 * 1024,
        UPLOAD_BLOCK_PATTERNS=DEFAULT_UPLOAD_BLOCK_PATTERNS + tuple(
            pattern.strip().casefold() for pattern in os.environ.get("DRIVE_UPLOAD_BLOCK_PATTERNS", "").split(",") if pattern.strip()
        ),
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        SESSION_COOKIE_SECURE=os.environ.get("DRIVE_COOKIE_SECURE", "0") == "1",
        PERMANENT_SESSION_LIFETIME=timedelta(hours=8),
        SESSION_REFRESH_EACH_REQUEST=False,
        MAX_FORM_MEMORY_SIZE=1024 * 1024,
        MAX_FORM_PARTS=20,
    )
    if test_config:
        app.config.update(test_config)
    if os.environ.get("DRIVE_TRUST_PROXY", "0") == "1":
        # 仅在 Gunicorn 被本机受信任反向代理访问时启用。
        app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1)
    if not app.config["SECRET_KEY"] or len(app.config["SECRET_KEY"]) < 32:
        raise RuntimeError("请配置至少 32 字符的 DRIVE_SECRET_KEY")
    if app.config["MAX_FILE_BYTES"] <= 0:
        raise RuntimeError("DRIVE_MAX_FILE_MB 必须为正整数")
    app.config["MAX_CONTENT_LENGTH"] = app.config["MAX_FILE_BYTES"] + 1024 * 1024
    # Step2：保护状态目录，并建立应用所需的数据库。
    Path(app.config["STATE_DIR"]).mkdir(mode=0o700, parents=True, exist_ok=True)
    database = str(Path(app.config["STATE_DIR"]) / "drive.sqlite3")

    @contextmanager
    def db(write=False):
        """创建数据库连接；写事务串行化，失败时自动回滚。"""
        # Step1：打开独立连接并启用外键。
        connection = sqlite3.connect(database, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("-- 启用外键约束\nPRAGMA foreign_keys = ON")
        try:
            # Step2：写事务提前取得锁，保证目录检查和账号保存一致。
            if write:
                connection.execute("-- 开始独占写事务\nBEGIN IMMEDIATE")
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    with db() as connection:
        connection.executescript("""
            -- 保存超级管理员与普通用户；version 用于立即使旧会话失效。
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY,
                username TEXT NOT NULL UNIQUE,
                password_hash TEXT NOT NULL,
                role TEXT NOT NULL CHECK (role IN ('admin', 'user')),
                root TEXT,
                enabled INTEGER NOT NULL DEFAULT 1 CHECK (enabled IN (0, 1)),
                version INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                CHECK ((role = 'admin' AND root IS NULL) OR (role = 'user' AND root IS NOT NULL))
            );
            -- 保存登录失败次数，限制单一来源的连续登录尝试。
            CREATE TABLE IF NOT EXISTS login_limits (
                address TEXT PRIMARY KEY,
                failures INTEGER NOT NULL,
                updated_at INTEGER NOT NULL
            );
        """)
    os.chmod(database, 0o600)

    def body():
        """读取 JSON 对象，拒绝其他请求体类型。"""
        value = request.get_json(silent=True)
        if not isinstance(value, dict):
            raise DriveError("请求内容必须为 JSON 对象")
        return value

    def text_field(data, name):
        """读取字符串字段，拒绝缺失字段和错误类型。"""
        value = data.get(name)
        if not isinstance(value, str):
            raise DriveError("字段 %s 必须为字符串" % name)
        return value

    def account_name(value):
        """校验用户名长度与字符，保留中文用户名。"""
        value = value.strip()
        if not 1 <= len(value) <= 64 or any(ord(char) < 32 for char in value):
            raise DriveError("用户名须为 1–64 个字符，且不能含控制字符")
        return value

    def password_hash(value):
        """校验密码并使用 Python 3.9 兼容的 PBKDF2 算法生成哈希。"""
        if not 8 <= len(value) <= 256:
            raise DriveError("密码须为 8–256 个字符")
        return generate_password_hash(value, method="pbkdf2:sha256:600000")

    def public_user(row):
        """返回可公开的账号信息，排除密码哈希和会话版本。"""
        return {key: row[key] for key in ("id", "username", "role", "root", "enabled", "created_at")}

    def require_role(role=None):
        """生成登录及角色检查装饰器。"""
        def decorate(function):
            """包装路由函数并保留原函数元信息。"""
            @functools.wraps(function)
            def wrapped(*args, **kwargs):
                """校验账号状态、会话版本与路由权限后执行请求。"""
                # Step1：每次从数据库读取最新账号，避免停用后继续使用旧会话。
                with db() as connection:
                    user = connection.execute(
                        "-- 根据会话用户 ID 查询最新账号\nSELECT * FROM users WHERE id = ?",
                        (session.get("user_id"),),
                    ).fetchone()
                if not user or not user["enabled"] or user["version"] != session.get("version"):
                    session.clear()
                    raise DriveError("登录已失效，请重新登录", 401)
                # Step2：检查角色并传递当前账号。
                if role and user["role"] != role:
                    raise DriveError("没有操作权限", 403)
                g.user = user
                return function(*args, **kwargs)
            return wrapped
        return decorate

    @app.before_request
    def verify_csrf():
        """所有写请求必须携带会话绑定的 CSRF 令牌，包括登录请求。"""
        if request.method in ("POST", "PUT", "PATCH", "DELETE"):
            expected = session.get("csrf", "")
            actual = request.headers.get("X-CSRF-Token", "")
            if not expected or not hmac.compare_digest(expected.encode("utf-8"), actual.encode("utf-8")):
                raise DriveError("页面已过期，请刷新后重试", 403)

    @app.after_request
    def response_headers(response):
        """添加浏览器安全策略并禁止缓存登录及文件管理接口。"""
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "same-origin"
        response.headers["Content-Security-Policy"] = "default-src 'self'; script-src 'self'; style-src 'self'; object-src 'none'; base-uri 'self'; frame-ancestors 'none'"
        if request.path.startswith("/api/"):
            response.headers["Cache-Control"] = "no-store"
        return response

    @app.errorhandler(Exception)
    def handle_error(error):
        """将预期错误转换为中文提示，服务器内部错误只写入日志。"""
        # Step1：按错误类型选择可安全返回的消息。
        if isinstance(error, DriveError):
            message, status_code = str(error), error.status
        elif isinstance(error, sqlite3.IntegrityError):
            message, status_code = "用户名已存在或账号配置不合法", 409
        elif isinstance(error, FileNotFoundError):
            message, status_code = "文件或目录不存在", 404
        elif isinstance(error, PermissionError):
            message, status_code = "服务账号没有读写权限", 403
        elif isinstance(error, OSError):
            message, status_code = "文件操作失败，请检查磁盘空间、目录权限和文件是否仍存在", 400
            app.logger.warning("文件操作失败", exc_info=True)
        elif isinstance(error, HTTPException):
            status_code = error.code
            message = "上传内容超过大小限制" if status_code == 413 else "请求无效或接口不存在"
        else:
            app.logger.exception("请求处理异常")
            message, status_code = "服务器处理失败，请查看服务日志", 500
        # Step2：保持错误接口格式一致。
        return jsonify(error=message), status_code

    @app.get("/")
    def index():
        """返回网盘单页界面。"""
        return app.send_static_file("index.html")

    @app.get("/api/session")
    def get_session():
        """返回当前登录账号和 CSRF 令牌，未登录时返回空账号。"""
        # Step1：为匿名访问也创建 CSRF 令牌。
        if "csrf" not in session:
            session["csrf"] = secrets.token_urlsafe(32)
        with db() as connection:
            user = connection.execute(
                "-- 获取当前会话账号\nSELECT * FROM users WHERE id = ?", (session.get("user_id"),)
            ).fetchone()
        # Step2：检查旧会话是否有效。
        if user and (not user["enabled"] or user["version"] != session.get("version")):
            user = None
        return jsonify(user=public_user(user) if user else None, csrf=session["csrf"], max_file_bytes=app.config["MAX_FILE_BYTES"])

    @app.post("/api/login")
    def login():
        """校验账号密码，限制连续失败次数，并创建新的登录会话。"""
        import time
        # Step1：读取登录数据并检查最近失败次数。
        data = body()
        username = account_name(text_field(data, "username"))
        password = text_field(data, "password")
        if len(password) > 256:
            raise DriveError("用户名或密码错误", 401)
        address = request.remote_addr or "unknown"
        now = int(time.time())
        with db(write=True) as connection:
            limit = connection.execute(
                "-- 查询来源登录失败记录\nSELECT * FROM login_limits WHERE address = ?", (address,)
            ).fetchone()
            if limit and limit["failures"] >= 10 and now - limit["updated_at"] < 300:
                raise DriveError("登录失败次数过多，请 5 分钟后重试", 429)
            user = connection.execute(
                "-- 按用户名读取登录凭据\nSELECT * FROM users WHERE username = ?", (username,)
            ).fetchone()
            valid = user and user["enabled"] and check_password_hash(user["password_hash"], password)
            # Step2：更新失败记录；退出事务后再返回失败，确保计数被提交。
            if not valid:
                failures = limit["failures"] + 1 if limit and now - limit["updated_at"] < 300 else 1
                connection.execute(
                    "-- 保存来源登录失败记录\nINSERT OR REPLACE INTO login_limits VALUES (?, ?, ?)",
                    (address, failures, now),
                )
            else:
                connection.execute("-- 清除成功登录来源的失败记录\nDELETE FROM login_limits WHERE address = ?", (address,))
        if not valid:
            raise DriveError("用户名或密码错误", 401)
        # Step3：轮换会话和 CSRF 令牌。
        session.clear()
        session.update(user_id=user["id"], version=user["version"], csrf=secrets.token_urlsafe(32))
        session.permanent = True
        return jsonify(user=public_user(user), csrf=session["csrf"])

    @app.post("/api/logout")
    def logout():
        """清除登录会话，并创建供下一次登录使用的 CSRF 令牌。"""
        session.clear()
        session["csrf"] = secrets.token_urlsafe(32)
        return jsonify(csrf=session["csrf"])

    def overlaps(first, second):
        """判断两个规范目录是否相同或具有父子关系。"""
        return first == second or first in second.parents or second in first.parents

    def validated_root(value, connection, user_id=None):
        """检查根目录真实路径、权限、项目隔离及其他账号目录冲突。"""
        # Step1：要求绝对路径并验证可安全打开所有目录组件。
        path = Path(value)
        if not path.is_absolute():
            raise DriveError("根目录必须为 Linux 绝对路径")
        with root_fd(str(path)):
            pass
        path = path.resolve(strict=True)
        if not os.access(str(path), os.R_OK | os.W_OK | os.X_OK):
            raise DriveError("根目录必须存在，且服务账号具有读写和访问权限")
        # Step2：禁止将项目源码、数据库和其他账号数据包含在用户目录中。
        for protected in (Path(__file__).parent.resolve(), Path(app.config["STATE_DIR"]).resolve()):
            if overlaps(path, protected):
                raise DriveError("用户根目录不能与项目或状态目录重叠")
        rows = connection.execute(
            "-- 查询其他普通用户根目录，停用用户仍保留目录归属\nSELECT id, root FROM users WHERE role = 'user'"
        ).fetchall()
        for row in rows:
            if row["id"] != user_id and overlaps(path, Path(row["root"])):
                raise DriveError("根目录不能与其他用户相同或互相嵌套")
        return str(path)

    @app.get("/api/users")
    @require_role("admin")
    def list_users():
        """为超级管理员列出全部普通用户。"""
        with db() as connection:
            rows = connection.execute("-- 列出普通用户配置\nSELECT * FROM users WHERE role = 'user' ORDER BY id").fetchall()
        return jsonify(users=[public_user(row) for row in rows])

    @app.post("/api/users")
    @require_role("admin")
    def create_user():
        """新增普通用户，校验根目录后以事务保存。"""
        # Step1：校验输入和密码。
        data = body()
        username = account_name(text_field(data, "username"))
        hashed = password_hash(text_field(data, "password"))
        enabled = data.get("enabled", True)
        if not isinstance(enabled, bool):
            raise DriveError("启用状态必须为布尔值")
        # Step2：串行检查目录冲突并新增账号。
        with db(write=True) as connection:
            root = validated_root(text_field(data, "root"), connection)
            connection.execute(
                "-- 新增普通用户配置\nINSERT INTO users (username, password_hash, role, root, enabled) VALUES (?, ?, 'user', ?, ?)",
                (username, hashed, root, int(enabled)),
            )
        return jsonify(ok=True), 201

    @app.put("/api/users/<int:user_id>")
    @require_role("admin")
    def update_user(user_id):
        """更新普通用户资料，可选重置密码，并使已有会话失效。"""
        # Step1：校验提交字段，空密码表示不修改原密码。
        data = body()
        username = account_name(text_field(data, "username"))
        password = text_field(data, "password")
        hashed = password_hash(password) if password else None
        enabled = data.get("enabled")
        if not isinstance(enabled, bool):
            raise DriveError("启用状态必须为布尔值")
        # Step2：检查目标为普通用户并原子更新版本。
        with db(write=True) as connection:
            row = connection.execute(
                "-- 查询可修改的普通用户\nSELECT * FROM users WHERE id = ? AND role = 'user'", (user_id,)
            ).fetchone()
            if not row:
                raise DriveError("普通用户不存在", 404)
            requested_root = text_field(data, "root")
            # 未修改目录时保留原配置，即使目录暂时不可访问也允许停用账号。
            root = row["root"] if requested_root == row["root"] else validated_root(requested_root, connection, user_id)
            connection.execute(
                "-- 更新普通用户资料并使旧会话失效\nUPDATE users SET username = ?, password_hash = ?, root = ?, enabled = ?, version = version + 1 WHERE id = ? AND role = 'user'",
                (username, hashed or row["password_hash"], root, int(enabled), user_id),
            )
        return jsonify(ok=True)

    @app.delete("/api/users/<int:user_id>")
    @require_role("admin")
    def delete_user(user_id):
        """仅删除普通用户账号，保留用户根目录及其中的文件。"""
        with db(write=True) as connection:
            cursor = connection.execute(
                "-- 仅删除普通用户，保护超级管理员\nDELETE FROM users WHERE id = ? AND role = 'user'", (user_id,)
            )
            if not cursor.rowcount:
                raise DriveError("普通用户不存在", 404)
        return jsonify(ok=True)

    @contextmanager
    def root_fd(root):
        """从 Linux 文件系统根逐级打开目录，禁止跟随任意符号链接。"""
        # Step1：通过目录描述符定位，避免路径校验后目录被符号链接替换。
        if os.name != "posix":
            raise RuntimeError("文件管理功能要求 Linux/POSIX 环境")
        descriptor = os.open("/", os.O_RDONLY | os.O_DIRECTORY)
        try:
            for part in Path(root).parts[1:]:
                child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptor)
                os.close(descriptor)
                descriptor = child
            # Step2：交给调用方，操作结束后关闭描述符。
            yield descriptor
        finally:
            os.close(descriptor)

    def parts(relative, allow_empty=True):
        """校验来自浏览器的相对路径，禁止穿越、控制字符及临时名称。"""
        if not isinstance(relative, str) or len(relative) > 4096:
            raise DriveError("文件路径无效")
        if relative == "" and allow_empty:
            return []
        values = relative.split("/")
        if any(value in ("", ".", "..") or "\\" in value or any(ord(c) < 32 for c in value) or value.startswith(".drive-upload-") for value in values):
            raise DriveError("文件路径无效，禁止目录穿越和保留名称")
        return values

    @contextmanager
    def directory(root, components, create=False):
        """在根描述符内逐级打开目录；上传时可按需创建父目录。"""
        # Step1：复制根描述符，使调用方的根描述符始终有效。
        descriptor = os.dup(root)
        try:
            for component in components:
                if create:
                    try:
                        os.mkdir(component, mode=0o750, dir_fd=descriptor)
                    except FileExistsError:
                        pass
                child = os.open(component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptor)
                os.close(descriptor)
                descriptor = child
            # Step2：返回已打开的目录并在退出时清理。
            yield descriptor
        finally:
            os.close(descriptor)

    @contextmanager
    def storage_lock():
        """串行执行文件写操作，避免同名上传、删除与重命名互相竞争。"""
        # ponytail: 全局写锁适合小规模使用；写入吞吐量成为瓶颈时改为每用户锁。
        import fcntl
        with open(str(Path(app.config["STATE_DIR"]) / "storage.lock"), "a+b") as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)

    def entry_info(parent, name):
        """读取条目状态，只接受普通文件或真实目录。"""
        info = os.stat(name, dir_fd=parent, follow_symlinks=False)
        if not (stat.S_ISREG(info.st_mode) or stat.S_ISDIR(info.st_mode)):
            raise DriveError("不支持符号链接或特殊文件", 403)
        return info

    @app.get("/api/files")
    @require_role("user")
    def list_files():
        """返回当前目录内容，隐藏符号链接、特殊文件及上传临时文件。"""
        # Step1：在用户根目录内打开目标文件夹。
        relative = request.args.get("path", "")
        components = parts(relative)
        entries = []
        with root_fd(g.user["root"]) as root, directory(root, components) as folder:
            # Step2：安全读取列表，忽略列举期间已经消失的文件。
            for name in os.listdir(folder):
                if name.startswith(".drive-upload-"):
                    continue
                try:
                    info = entry_info(folder, name)
                except (FileNotFoundError, DriveError):
                    continue
                entries.append(dict(name=name, is_dir=stat.S_ISDIR(info.st_mode), size=info.st_size, modified=info.st_mtime))
        entries.sort(key=lambda item: (not item["is_dir"], item["name"].casefold()))
        return jsonify(path=relative, entries=entries)

    @app.post("/api/folders")
    @require_role("user")
    def create_folder():
        """在当前目录创建一个文件夹，拒绝覆盖现有条目。"""
        # Step1：校验父目录和单层名称。
        data = body()
        parent = parts(text_field(data, "path"))
        name = parts(text_field(data, "name"), False)
        if len(name) != 1:
            raise DriveError("文件夹名称不能包含路径")
        # Step2：串行创建目录。
        with storage_lock(), root_fd(g.user["root"]) as root, directory(root, parent) as folder:
            try:
                os.mkdir(name[0], mode=0o750, dir_fd=folder)
            except FileExistsError:
                raise DriveError("同名文件或文件夹已存在", 409)
        return jsonify(ok=True), 201

    @app.post("/api/rename")
    @require_role("user")
    def rename_entry():
        """在同一目录重命名条目，拒绝覆盖及移动根目录。"""
        # Step1：校验来源路径和新名称。
        data = body()
        source = parts(text_field(data, "path"), False)
        target = parts(text_field(data, "name"), False)
        if len(target) != 1:
            raise DriveError("新名称不能包含路径")
        # Step2：检查目标不存在并重命名。
        with storage_lock(), root_fd(g.user["root"]) as root, directory(root, source[:-1]) as folder:
            entry_info(folder, source[-1])
            if source[-1] != target[0]:
                try:
                    os.stat(target[0], dir_fd=folder, follow_symlinks=False)
                except FileNotFoundError:
                    pass
                else:
                    raise DriveError("同名文件或文件夹已存在", 409)
                os.rename(source[-1], target[0], src_dir_fd=folder, dst_dir_fd=folder)
        return jsonify(ok=True)

    def remove_entry(parent, name):
        """基于描述符递归删除条目，任何阶段都不跟随符号链接。"""
        # Step1：读取条目类型；子树中的链接仅删除链接本身。
        info = os.stat(name, dir_fd=parent, follow_symlinks=False)
        if stat.S_ISDIR(info.st_mode):
            # Step2：打开真实目录并递归清理，再移除空目录。
            child = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
            try:
                for child_name in os.listdir(child):
                    remove_entry(child, child_name)
            finally:
                os.close(child)
            os.rmdir(name, dir_fd=parent)
        else:
            # Step3：移除文件、符号链接或特殊条目，不打开其内容。
            os.unlink(name, dir_fd=parent)

    @app.delete("/api/files")
    @require_role("user")
    def delete_entry():
        """永久删除文件或目录，根目录不能作为删除目标。"""
        # Step1：要求明确确认并检查目标路径。
        data = body()
        if data.get("confirmed") is not True:
            raise DriveError("请确认永久删除")
        source = parts(text_field(data, "path"), False)
        # Step2：限制在根目录内进行递归删除。
        with storage_lock(), root_fd(g.user["root"]) as root, directory(root, source[:-1]) as folder:
            entry_info(folder, source[-1])
            remove_entry(folder, source[-1])
        return jsonify(ok=True)

    @app.get("/api/download")
    @require_role("user")
    def download():
        """打开普通文件并以附件返回，下载过程由文件描述符保持定位。"""
        # Step1：定位到用户目录内的普通文件，拒绝链接和特殊设备。
        source = parts(request.args.get("path", ""), False)
        with root_fd(g.user["root"]) as root, directory(root, source[:-1]) as folder:
            descriptor = os.open(source[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=folder)
        handle = os.fdopen(descriptor, "rb")
        try:
            info = os.fstat(handle.fileno())
            if not stat.S_ISREG(info.st_mode):
                raise DriveError("只能下载普通文件")
            # Step2：流式返回附件，关闭响应时释放文件句柄。
            response = send_file(handle, as_attachment=True, download_name=source[-1], mimetype="application/octet-stream", conditional=False)
            response.content_length = info.st_size
            response.call_on_close(handle.close)
            return response
        except Exception:
            handle.close()
            raise

    @app.post("/api/upload")
    @require_role("user")
    def upload():
        """逐文件接收上传，在目标目录使用临时文件并原子替换。"""
        # Step1：读取上传路径，客户端明确确认后才允许覆盖。
        source = parts(request.form.get("path", ""), False)
        incoming = request.files.get("file")
        if incoming is None:
            raise DriveError("请选择上传文件")
        # 在创建目录或临时文件前过滤目标路径及 multipart 原始文件名。
        original_parts = (incoming.filename or "").replace("\\", "/").split("/")
        blocked = blocked_upload_component(source + original_parts)
        if blocked:
            return jsonify(error="敏感文件或目录禁止上传：" + blocked, blocked=True), 422
        overwrite = request.form.get("overwrite") == "1"
        # Step2：创建父目录并检查目标类型。
        with storage_lock(), root_fd(g.user["root"]) as root, directory(root, source[:-1], create=True) as folder:
            try:
                info = entry_info(folder, source[-1])
            except FileNotFoundError:
                info = None
            if info and stat.S_ISDIR(info.st_mode):
                raise DriveError("目标是文件夹，不能覆盖为文件", 409)
            if info and not overwrite:
                return jsonify(error="同名文件已存在", conflict=True), 409
            temporary = ".drive-upload-" + secrets.token_hex(16)
            descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=folder)
            try:
                # Step3：按块写入并执行大小检查，完成后替换目标。
                total = 0
                with os.fdopen(descriptor, "wb") as output:
                    while True:
                        chunk = incoming.stream.read(1024 * 1024)
                        if not chunk:
                            break
                        total += len(chunk)
                        if total > app.config["MAX_FILE_BYTES"]:
                            raise DriveError("文件超过上传大小限制", 413)
                        output.write(chunk)
                    output.flush()
                    # 显式设置最终文件权限，保证不受启动进程 umask 077 影响。
                    os.fchmod(output.fileno(), 0o644)
                    os.fsync(output.fileno())
                if overwrite:
                    os.replace(temporary, source[-1], src_dir_fd=folder, dst_dir_fd=folder)
                else:
                    # 原子创建目标硬链接，避免外部程序同时新建文件时被覆盖。
                    try:
                        os.link(temporary, source[-1], src_dir_fd=folder, dst_dir_fd=folder, follow_symlinks=False)
                    except FileExistsError:
                        return jsonify(error="同名文件已存在", conflict=True), 409
            finally:
                # Step4：成功或失败均清除临时文件。
                try:
                    os.unlink(temporary, dir_fd=folder)
                except FileNotFoundError:
                    # 原子覆盖成功时，临时名称已经被替换操作移走。
                    pass
        return jsonify(ok=True, size=total), 201

    def blocked_upload_component(components):
        """按名称忽略大小写匹配敏感文件及目录，返回首个被拦截的组件。"""
        # Step1：对每层目录和文件名称分别检查，覆盖文件夹内嵌的敏感目录。
        for component in components:
            normalized = component.casefold()
            # Step2：同时应用默认规则与配置追加的通配符规则。
            if any(fnmatch.fnmatchcase(normalized, pattern.casefold()) for pattern in app.config["UPLOAD_BLOCK_PATTERNS"]):
                return component
        return None

    @app.cli.command("init-admin")
    @click.option("--username", prompt="超级管理员用户名")
    @click.password_option(confirmation_prompt=True)
    def init_admin(username, password):
        """通过服务器命令初始化唯一超级管理员，无默认密码。"""
        # Step1：校验凭据并生成密码哈希。
        username = account_name(username)
        hashed = password_hash(password)
        # Step2：在事务内确保只能创建一个超级管理员。
        with db(write=True) as connection:
            exists = connection.execute("-- 检查超级管理员是否已初始化\nSELECT id FROM users WHERE role = 'admin'").fetchone()
            if exists:
                raise click.ClickException("超级管理员已存在")
            connection.execute(
                "-- 初始化唯一超级管理员\nINSERT INTO users (username, password_hash, role) VALUES (?, ?, 'admin')",
                (username, hashed),
            )
        click.echo("超级管理员创建成功")

    @app.cli.command("reset-admin-password")
    @click.password_option(confirmation_prompt=True)
    def reset_admin_password(password):
        """通过服务器命令重置超级管理员密码，并使旧会话失效。"""
        hashed = password_hash(password)
        with db(write=True) as connection:
            cursor = connection.execute(
                "-- 重置超级管理员密码并轮换会话版本\nUPDATE users SET password_hash = ?, version = version + 1 WHERE role = 'admin'", (hashed,)
            )
            if not cursor.rowcount:
                raise click.ClickException("请先初始化超级管理员")
        click.echo("超级管理员密码已重置")

    return app
