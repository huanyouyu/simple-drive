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

## Linux 命令部署

先安装 Python 3.9 和 venv，使用有权读写用户数据目录的 Linux 账号执行以下命令：

```bash
git clone git@github.com:huanyouyu/simple-drive.git
cd simple-drive
python3.9 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt

# 首次执行，交互设置超级管理员用户名和密码。
bash run.sh init-admin

# 启动；按 Ctrl+C 停止。
bash run.sh start
```

访问 `http://服务器IP:8000`，服务器防火墙需要允许访问此端口。使用管理员登录后新增普通用户；创建用户前，先在项目目录之外准备真实数据目录，并确保运行账号具有读写和访问权限。例如用当前 Linux 账号执行 `mkdir -p "$HOME/drive-data/alice"`，再在网页中填写该目录的绝对路径。

`run.sh` 首次执行自动生成 `ops/drive.env`，保存随机密钥；以后启动自动读取同一份配置，无需手动导出环境变量。初始化管理员仅执行一次。保持配置和密钥不变，重新启动后原账号及文件继续可用。

## 后台启动、停止和日志

后台运行时退出 SSH 会话后程序继续运行：

```bash
# 后台启动。
bash run.sh background

# 查看最近 100 行日志并持续跟踪；Ctrl+C 只退出日志查看。
bash run.sh logs

# 停止前台或后台启动的本项目进程。
bash run.sh stop
```

日志保存在 `instance/gunicorn.log`，PID 保存在 `instance/gunicorn.pid`。前台启动时日志直接显示在终端。停止命令会核对进程属于本项目后发送停止信号；等待进程退出、PID 文件消失，再重新启动。后台运行不会自动处理机器重启或进程崩溃，届时重新执行启动命令即可。

## 可选：Nginx 与 HTTPS

可直接通过服务器 IP 使用，也可以参考 `ops/nginx.conf.example` 配置域名和已有 HTTPS 证书。使用配套 Nginx 时，在 `ops/drive.env` 中设置：

```bash
DRIVE_BIND=127.0.0.1:8000
DRIVE_COOKIE_SECURE=1
DRIVE_TRUST_PROXY=1
```

随后重新启动网盘。仅通过 HTTP 访问时保持 `DRIVE_COOKIE_SECURE=0`，否则浏览器不会发送登录 Cookie。

## 配置与日常维护

- `DRIVE_SECRET_KEY`：必填，至少 32 字符的随机密钥。
- `DRIVE_STATE_DIR`：SQLite 和写操作锁文件目录，默认项目下 `instance`。
- `DRIVE_BIND`：启动地址，默认 `0.0.0.0:8000`；使用本机 Nginx 代理时改为 `127.0.0.1:8000`。
- `DRIVE_MAX_FILE_MB`：单文件上限，默认 `1024`；修改时同步调整 Nginx `client_max_body_size`，为 multipart 留出至少 1MB 余量。
- `DRIVE_COOKIE_SECURE`：HTTPS 设置 `1`，本机 HTTP 设置 `0`。
- `DRIVE_TRUST_PROXY`：默认 `0`；使用配套 Nginx、且 Gunicorn 仅监听本机时设 `1`，信任一层代理提供的来源地址和协议。

配置保存在 `ops/drive.env`；修改后执行 `bash run.sh stop`，等待原进程退出，再执行 `bash run.sh start` 或 `bash run.sh background`。用户管理网页保存的账号修改立即生效。管理员可以通过“编辑 / 重置密码”修改普通用户密码；密码留空表示保留原密码。

登录有效期 8 小时，修改用户资料、密码、根目录或启用状态均使该用户旧登录失效。已开始执行的文件请求可能完成，后续请求会拒绝旧登录。

忘记超级管理员密码时，在项目目录执行：

```bash
bash run.sh reset-admin-password
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

登录失败达到 10 次时对同一来源限制 5 分钟。直接访问时不信任转发头；自带 Nginx 配置配合 Gunicorn 仅监听本机及 `DRIVE_TRUST_PROXY=1` 使用，信任一层代理。

## 验证

```bash
.venv/bin/python check.py
```

检查使用临时目录和临时 SQLite，不接触生产账号或用户数据，验证登录、权限、账号隔离、中文文件名、上传覆盖、目录冲突、超限、路径穿越、符号链接、删除保护和旧会话失效。

全部源码为 UTF-8 无 BOM，方法提供说明，处理流程使用 Step 注释；不使用 SQL 文件，内嵌 SQL 均有说明注释。
