# 轻盘：Python 3.9 简易服务器网盘

Linux 上的文件管理网页，后端 Flask，账号存储 SQLite，前端为普通 HTML/CSS/JavaScript。文件直接保存在各用户配置的目录中，不进入数据库。

## 功能

- 唯一超级管理员：新增、编辑、启用、停用、删除普通用户，设置根目录、重置密码。
- 普通用户：浏览目录、新建文件夹、重命名、下载文件、永久删除文件和非空文件夹。
- 两个独立上传按钮：上传文件（支持多选）、上传文件夹（保留所选文件夹名称及内部层级）。
- 同名文件确认覆盖、全部覆盖、跳过、全部跳过；同名文件夹合并，类型冲突报错。
- 默认单文件 1GB 上限，逐文件上传及进度、失败汇总；不提供分片或断点续传。
- 密码哈希、CSRF 校验、登录失败限制、账号修改后旧会话失效、根目录隔离及符号链接拒绝。

超级管理员只管理普通用户配置，不浏览普通用户文件。普通用户根目录不得相同或互相嵌套，也不能与应用源码或状态目录重叠。停用账号仍占用其目录配置。删除账号保留全部文件。

## 环境

服务器需已安装 Python 3.9 及 venv，使用 Linux 本地文件系统保存 SQLite 数据库。文件操作使用 Linux 目录描述符及 `O_NOFOLLOW`，不可直接在 Windows 运行。文件夹选择以桌面 Chrome、Edge 为支持浏览器，浏览器不会提交空文件夹。

依赖锁定在 `requirements.txt`。Gunicorn 23.0.0 支持 Python 3.9，24 及更新版本要求 Python 3.10，因此不能直接升级到这些版本。

## 本机快速启动（Linux）

以下命令在项目目录运行，先安装依赖，再设置环境并创建管理员。初始化和运行必须使用相同的状态目录及密钥。

```bash
python3.9 -m venv .venv
. .venv/bin/activate
python -m pip install -r requirements.txt

export DRIVE_SECRET_KEY="$(python -c 'import secrets; print(secrets.token_hex(32))')"
export DRIVE_STATE_DIR="$PWD/instance"
export DRIVE_COOKIE_SECURE=0

# 命令交互输入用户名和密码，无默认管理员、无默认密码。
python -m flask --app app:create_app init-admin
gunicorn --bind 127.0.0.1:8000 --workers 2 --threads 2 --timeout 3600 'app:create_app()'
```

访问 `http://127.0.0.1:8000`。使用管理员登录后，新增普通用户。创建用户前，先在项目目录之外准备实际数据目录，例如 `/srv/drive/alice`，确保运行服务的 Linux 账号拥有读、写、执行权限。

密钥须固定保存：每次重新生成会使已有登录失效。登录有效期 8 小时，修改用户资料、密码、根目录或启用状态均使该用户旧登录失效。已开始执行的文件请求可能完成，后续请求会拒绝旧登录。

## Linux systemd 部署

将项目复制到 `/opt/simple-drive`，以下为以 root 执行的部署示例。使用专用系统账号运行服务，实际账号密码通过管理员网页设置。

```bash
useradd --system --home-dir /var/lib/simple-drive --shell /usr/sbin/nologin simple-drive
install -d -m 700 -o simple-drive -g simple-drive /var/lib/simple-drive
install -d -m 750 -o simple-drive -g simple-drive /srv/drive/alice

cd /opt/simple-drive
python3.9 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
cp ops/drive.env.example /etc/simple-drive.env
chmod 600 /etc/simple-drive.env
```

编辑 `/etc/simple-drive.env`，替换 `DRIVE_SECRET_KEY` 为随机密钥；确认项目及虚拟环境可被 `simple-drive` 系统账号读取。HTTPS 部署保留 `DRIVE_COOKIE_SECURE=1`。

初始化超级管理员，命令会交互询问账号和密码：

```bash
set -a
. /etc/simple-drive.env
set +a
sudo --preserve-env=DRIVE_SECRET_KEY,DRIVE_STATE_DIR,DRIVE_COOKIE_SECURE,DRIVE_MAX_FILE_MB \
  -u simple-drive /opt/simple-drive/.venv/bin/python -m flask --app /opt/simple-drive/app:create_app init-admin

cp ops/simple-drive.service /etc/systemd/system/simple-drive.service
systemctl daemon-reload
systemctl enable --now simple-drive
systemctl status simple-drive
journalctl -u simple-drive -f
```

配置 Nginx：参考 `ops/nginx.conf.example`，替换域名和已有证书路径，执行 `nginx -t` 后重载 Nginx。Gunicorn 仅绑定本机地址。HTTP 验证环境须把 `DRIVE_COOKIE_SECURE` 设置为 `0`，否则浏览器不会发送登录 Cookie。

## 配置与日常维护

- `DRIVE_SECRET_KEY`：必填，至少 32 字符的随机密钥。
- `DRIVE_STATE_DIR`：SQLite 和写操作锁文件目录，默认项目下 `instance`。
- `DRIVE_MAX_FILE_MB`：单文件上限，默认 `1024`；修改时同步调整 Nginx `client_max_body_size`，为 multipart 留出至少 1MB 余量。
- `DRIVE_COOKIE_SECURE`：HTTPS 设置 `1`，本机 HTTP 设置 `0`。
- `DRIVE_TRUST_PROXY`：默认 `0`；使用配套 Nginx、且 Gunicorn 仅监听本机时设 `1`，信任一层代理提供的来源地址和协议。

配置修改后执行 `systemctl restart simple-drive`。用户管理网页保存的账号修改立即生效，不需要重启。管理员可以通过“编辑 / 重置密码”修改普通用户密码；密码留空表示保留原密码。

忘记超级管理员密码时，加载同一环境配置后执行：

```bash
sudo --preserve-env=DRIVE_SECRET_KEY,DRIVE_STATE_DIR \
  -u simple-drive /opt/simple-drive/.venv/bin/python -m flask --app /opt/simple-drive/app:create_app reset-admin-password
```

备份时停止服务，分别复制状态目录和所有用户根目录；恢复时保留原来的密钥，并恢复文件归属和目录权限。SQLite 只包含账号配置及登录失败计数，不包含用户文件。

## 接口

所有写请求携带 `/api/session` 返回的 `X-CSRF-Token`，包括登录。JSON 接口错误统一为 `{"error": "中文提示"}`；权限判断由服务端会话完成，不接受客户端指定操作用户。

- `GET /api/session`：当前账号、CSRF 令牌和上传限制。
- `POST /api/login`：`username`、`password`；`POST /api/logout`：退出。
- `GET /api/users`：普通用户列表；`POST /api/users`：新增，字段 `username`、`password`、`root`、`enabled`。
- `PUT /api/users/<id>`：更新同上字段，空密码保留原密码；`DELETE /api/users/<id>`：仅删除账号。
- `GET /api/files?path=相对目录`：列表；`POST /api/folders`：`path`、`name`。
- `POST /api/rename`：`path`、`name`；`DELETE /api/files`：`path`、`confirmed: true`。
- `GET /api/download?path=相对文件`：附件下载。
- `POST /api/upload`：multipart 字段 `file`、`path`、`overwrite`（`0` 或 `1`）。同名文件返回 HTTP 409 和 `conflict: true`，确认后重新上传并传 `overwrite=1`。

`path` 统一相对用户根目录；根目录使用空字符串。绝对路径、`..`、反斜杠、控制字符及 `.drive-upload-` 保留名称被拒绝。符号链接和特殊文件不显示、不可下载和上传覆盖；删除真实文件夹时，其中的符号链接仅移除链接，不访问链接目标。

上传期间通过全局文件写锁串行执行写操作，适合少量用户。文件上传失败时已有文件保留，临时文件清理；进程被强制终止可能遗留隐藏的 `.drive-upload-*` 临时文件，可停服后清理。递归删除不具备事务能力，中途遇到权限或磁盘错误可能已删除部分内容。

登录失败达到 10 次时对同一来源限制 5 分钟。自带 Nginx 配置只接受本机代理，Gunicorn 使用受信任的本机转发来源；自定义代理部署需保持该信任边界。

## 验证

```bash
.venv/bin/python check.py
```

检查使用临时目录和临时 SQLite，不接触生产账号或用户数据，验证登录、权限、账号隔离、中文文件名、上传覆盖、目录冲突、超限、路径穿越、符号链接、删除保护和旧会话失效。

全部源码为 UTF-8 无 BOM，方法提供说明，处理流程使用 Step 注释；不使用 SQL 文件，内嵌 SQL 均有说明注释。
