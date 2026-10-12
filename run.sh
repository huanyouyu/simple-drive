#!/usr/bin/env bash
# 说明：在项目目录直接初始化管理员、前台/后台启动、停止及查看日志。
set -euo pipefail
umask 077

# Step1：定位项目，检查已经安装依赖的虚拟环境。
cd -P -- "$(dirname -- "${BASH_SOURCE[0]}")"
if [[ ! -x .venv/bin/python || ! -x .venv/bin/gunicorn ]]; then
    printf '%s\n' '请先安装依赖：python3.9 -m venv .venv && .venv/bin/python -m pip install -r requirements.txt' >&2
    exit 1
fi

# Step2：首次运行生成固定密钥和本地配置，后续运行始终复用。
if [[ ! -f ops/drive.env ]]; then
    .venv/bin/python -c 'from pathlib import Path; import secrets; source = Path("ops/drive.env.example").read_text(encoding="utf-8"); source = source.replace("replace-with-a-random-secret-of-at-least-32-characters", secrets.token_hex(32)); Path("ops/drive.env").write_text(source, encoding="utf-8")'
    printf '%s\n' '已生成 ops/drive.env，密钥将固定保存，请勿提交此文件。'
fi
set -a
# shellcheck disable=SC1091
source ops/drive.env
set +a
mkdir -p instance
pid_file="$PWD/instance/gunicorn.pid"
log_file="$PWD/instance/gunicorn.log"

# Step3：通过直接命令处理管理员初始化及进程操作。
case "${1:-start}" in
    init-admin|reset-admin-password)
        exec .venv/bin/python -m flask --app app:create_app "$@"
        ;;
    start)
        printf '启动地址：%s；按 Ctrl+C 停止。\n' "${DRIVE_BIND:-0.0.0.0:8000}"
        exec .venv/bin/gunicorn --bind "${DRIVE_BIND:-0.0.0.0:8000}" --workers 2 --threads 2 --timeout 3600 --pid "$pid_file" --access-logfile - --error-logfile - 'app:create_app()'
        ;;
    background)
        .venv/bin/gunicorn --bind "${DRIVE_BIND:-0.0.0.0:8000}" --workers 2 --threads 2 --timeout 3600 --pid "$pid_file" --daemon --access-logfile "$log_file" --error-logfile "$log_file" 'app:create_app()'
        printf '已提交后台启动命令，日志：%s\n' "$log_file"
        ;;
    stop)
        if [[ ! -f "$pid_file" ]]; then
            printf '%s\n' '没有运行中的进程 PID 文件。'
            exit 0
        fi
        task_pid=$(cat -- "$pid_file")
        if [[ ! "$task_pid" =~ ^[1-9][0-9]*$ ]]; then
            printf '%s\n' 'PID 文件无效，请检查 instance/gunicorn.pid。' >&2
            exit 1
        fi
        if ! kill -0 "$task_pid" 2>/dev/null; then
            rm -- "$pid_file"
            printf '%s\n' '进程已停止，已清理过期 PID 文件。'
            exit 0
        fi
        # Step4：核对进程所属项目和命令，避免过期 PID 误伤其他程序。
        task_cwd=$(readlink -- "/proc/$task_pid/cwd")
        task_command=$(tr '\0' ' ' < "/proc/$task_pid/cmdline")
        if [[ "$task_cwd" != "$PWD" || "$task_command" != *gunicorn* || "$task_command" != *app:create_app* ]]; then
            printf '%s\n' 'PID 不属于本项目 Gunicorn，拒绝停止；请检查 PID 文件。' >&2
            exit 1
        fi
        kill -TERM "$task_pid"
        printf '%s\n' '已发送停止信号，Gunicorn 会等待正在处理的请求结束。'
        ;;
    logs)
        exec tail -n 100 -f "$log_file"
        ;;
    *)
        printf '%s\n' '用法：bash run.sh [init-admin|start|background|stop|logs|reset-admin-password]' >&2
        exit 1
        ;;
esac
