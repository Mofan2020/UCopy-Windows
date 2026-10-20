#!/usr/bin/env bash
# restart-server.sh — 临时恢复工具 (v1.7.4)
#
# 用途: long-running UCopy server 在内存与磁盘 db 脱钩时,
#       这个脚本:
#         1. 检测 server 进程是否健康 (走 HTTP API + 直读 db)
#         2. 不健康 -> 优雅停 (TERM 10s 超时转 KILL)
#         3. 重新用 --detach 起新 server
#         4. 验证新 server 端到端
#
# 这是 v1.7.4 的临时方案; root cause 仍未确诊 (参见 docs/notes.md).
# 期望用途: server 又卡 disk I/O error 时, 一键恢复.
#
# 使用:
#   bash restart-server.sh           # 自动判断要不要重启
#   bash restart-server.sh --check   # 只检查, 不重启
#   bash restart-server.sh --force   # 强制重启 (不管 health)

set -e

BIN="$HOME/Documents/MyProjects/UCopy-windows/dist/UCopy-Server-macos-arm64"
DATA_DIR="$HOME/UCopyServer"
PORT=8443
BIND="127.0.0.1"
LOG_FILE="$DATA_DIR/server.log"
PID_FILE="$DATA_DIR/server.pid"
ADMIN_PW="${UCOPY_ADMIN_PW:-}"

MODE="auto"
for arg in "$@"; do
    case $arg in
        --check) MODE="check" ;;
        --force) MODE="force" ;;
        --help|-h)
            sed -n '2,15p' "$0"
            exit 0
            ;;
        *)
            echo "unknown arg: $arg" >&2
            exit 2
            ;;
    esac
done

if [[ ! -x "$BIN" ]]; then
    echo "FATAL: server binary not found at $BIN" >&2
    exit 2
fi

ts() { date "+%Y-%m-%d %H:%M:%S"; }
say() { echo "[$(ts)] $*" >&2; }

check_server_db_health() {
    # 1. 直读 db 文件: 完整性
    if [[ ! -f "$DATA_DIR/server.db" ]]; then
        echo "no_db"
        return 1
    fi
    if ! sqlite3 "$DATA_DIR/server.db" "PRAGMA integrity_check" 2>/dev/null | grep -q '^ok$'; then
        echo "db_corrupt"
        return 1
    fi
    # 2. API 试探 (用 admin_token 仅做 /ping)
    if ! curl -s -m 5 -o /dev/null -w '%{http_code}' "http://$BIND:$PORT/api/v1/ping" | grep -q '^200$'; then
        echo "no_api"
        return 1
    fi
    # 3. 写入一致性: 走 login + POST /keys 看 server 内存视图跟磁盘 db 是否一致
    #    这一项只有 UCOPY_ADMIN_PW 设置时才检查
    if [[ -n "$ADMIN_PW" ]]; then
        local cookie_jar; cookie_jar=$(mktemp -t ucopy_h.XXXXXX)
        local login_res; login_res=$(curl -s -c "$cookie_jar" -X POST \
            -H 'Content-Type: application/json' \
            -d "{\"username\":\"admin\",\"password\":\"$ADMIN_PW\"}" \
            "http://$BIND:$PORT/api/v1/login" 2>/dev/null)
        if echo "$login_res" | grep -q '"ok": true'; then
            local marker="health_check_$$_$(date +%s%N)"
            local create_res; create_res=$(curl -s -b "$cookie_jar" -X POST \
                -H 'Content-Type: application/json' \
                -d "{\"name\":\"$marker\"}" \
                "http://$BIND:$PORT/api/v1/keys" 2>/dev/null)
            if echo "$create_res" | grep -q '"ok": true'; then
                # 立刻从磁盘 db 直读 (不通过 server)
                sleep 0.5  # 给 server 一点时间写盘
                local disk_count; disk_count=$(sqlite3 "$DATA_DIR/server.db" \
                    "SELECT COUNT(*) FROM api_keys WHERE name = '$marker'" 2>/dev/null || echo "-1")
                # 清理 marker (不论成败)
                local new_id; new_id=$(echo "$create_res" | python3 -c "import sys,json; print(json.load(sys.stdin).get('id',''))" 2>/dev/null || echo "")
                if [[ -n "$new_id" ]]; then
                    curl -s -b "$cookie_jar" -X DELETE "http://$BIND:$PORT/api/v1/keys/$new_id" >/dev/null
                fi
                rm -f "$cookie_jar"
                if [[ "$disk_count" == "1" ]]; then
                    echo "ok"
                    return 0
                else
                    echo "write_not_visible:$disk_count"
                    return 1
                fi
            fi
        fi
        rm -f "$cookie_jar"
    fi
    # UCOPY_ADMIN_PW 没设 或 上面 login 失败: 视为健康 (auto mode 不重启)
    echo "ok"
    return 0
}

read_pid_file() {
    if [[ -f "$PID_FILE" ]]; then
        cat "$PID_FILE" 2>/dev/null | tr -d '[:space:]'
    fi
}

is_alive() {
    local p="$1"
    [[ -n "$p" ]] && kill -0 "$p" 2>/dev/null
}

stop_server() {
    local pid="$1"
    if ! is_alive "$pid"; then
        say "[stop] pid $pid 不在跑, 跳过"
        # 清理残留 PID file
        rm -f "$PID_FILE"
        return 0
    fi
    say "[stop] 给 pid $pid 发 SIGTERM"
    kill -TERM "$pid" 2>/dev/null || true
    local deadline=$((SECONDS + 10))
    while (( SECONDS < deadline )); do
        if ! is_alive "$pid"; then
            say "[stop] 优雅退出成功"
            rm -f "$PID_FILE"
            return 0
        fi
        sleep 1
    done
    say "[stop] 10s 未退, 升级 SIGKILL"
    kill -KILL "$pid" 2>/dev/null || true
    sleep 1
    if is_alive "$pid"; then
        say "FATAL: pid $pid SIGKILL 后仍存活" >&2
        return 1
    fi
    rm -f "$PID_FILE"
    return 0
}

start_server() {
    # 端口先确认空
    if lsof -nP -i ":$PORT" 2>/dev/null | grep -q LISTEN; then
        say "[start] 端口 $PORT 已占用, 试图放空..."
        lsof -nP -i ":$PORT" 2>/dev/null | awk '/LISTEN/{print $2}' | xargs -r kill -9
        sleep 2
    fi
    say "[start] 启动新 detached server"
    "$BIN" \
        --data-dir "$DATA_DIR" \
        --port "$PORT" \
        --bind-host "$BIND" \
        --disable-https \
        --detach
    local new_pid
    new_pid=$(read_pid_file)
    if [[ -z "$new_pid" ]]; then
        say "FATAL: 启动后 server.pid 文件为空" >&2
        return 1
    fi
    say "[start] new_pid=$new_pid"
    # 等 ready
    local deadline=$((SECONDS + 30))
    while (( SECONDS < deadline )); do
        if curl -s -m 3 -o /dev/null -w '%{http_code}' "http://$BIND:$PORT/api/v1/ping" | grep -q '^200$'; then
            say "[start] 服务就绪"
            return 0
        fi
        sleep 1
    done
    say "FATAL: 服务 30s 内未就绪" >&2
    return 1
}

verify_end_to_end() {
    if [[ -z "$ADMIN_PW" ]]; then
        say "[verify] UCOPY_ADMIN_PW 未设, 跳过写盘验证"
        return 0
    fi
    local cookie_jar; cookie_jar=$(mktemp -t ucopy_jar.XXXXXX)
    trap "rm -f '$cookie_jar'" EXIT

    local login_res; login_res=$(curl -s -c "$cookie_jar" -X POST \
        -H 'Content-Type: application/json' \
        -d "{\"username\":\"admin\",\"password\":\"$ADMIN_PW\"}" \
        "http://$BIND:$PORT/api/v1/login" 2>/dev/null)
    if ! echo "$login_res" | grep -q '"ok": true'; then
        say "[verify] login 失败: $login_res"
        return 1
    fi

    local marker="restart_test_$(date +%s)"
    local create_res; create_res=$(curl -s -b "$cookie_jar" -X POST \
        -H 'Content-Type: application/json' \
        -d "{\"name\":\"$marker\"}" \
        "http://$BIND:$PORT/api/v1/keys" 2>/dev/null)
    if ! echo "$create_res" | grep -q '"ok": true'; then
        say "[verify] POST /keys 失败: $create_res"
        return 1
    fi

    # 立刻直读 db 看是否落盘
    local disk_count; disk_count=$(sqlite3 "$DATA_DIR/server.db" \
        "SELECT COUNT(*) FROM api_keys WHERE name = '$marker'" 2>/dev/null || echo "-1")
    if [[ "$disk_count" != "1" ]]; then
        say "[verify] ! 立刻直读 db, 该 key (name=$marker) 落盘数 = $disk_count (期望 1)"
        say "[verify] 这就是 2026-10-20 观察到的事故症状"
        return 1
    fi
    say "[verify] ✓ login + POST /keys + db 落盘 全部 200/1"
    # 清理测试 key
    local new_id; new_id=$(echo "$create_res" | python3 -c "import sys,json; print(json.load(sys.stdin)['id'])")
    curl -s -b "$cookie_jar" -X DELETE "http://$BIND:$PORT/api/v1/keys/$new_id" >/dev/null
    rm -f "$cookie_jar"
    trap - EXIT
    return 0
}

# === main ===

say "==== UCopy server health check / restart ===="
say "binary: $BIN"
say "data-dir: $DATA_DIR"
say "mode: $MODE"

current_pid=$(read_pid_file)
say "server.pid: ${current_pid:-(none)}"

health=$(check_server_db_health) || rc=$?; rc=${rc:-0}
say "health: $health"

case "$MODE" in
    check)
        if [[ "$health" == "ok" ]]; then
            say "✓ server 健康, 无需重启"
            exit 0
        fi
        say "✗ server 不健康 ($health), 建议你跑 restart-server.sh (无 --check)"
        exit 1
        ;;
    auto)
        if [[ "$health" == "ok" ]]; then
            say "✓ server 健康 (health=ok), 无需重启"
            verify_end_to_end || true
            exit 0
        fi
        say "! server 不健康 ($health), 重启"
        ;;
esac

# 重启流程
if [[ -z "$current_pid" ]]; then
    say "无 PID file, 直接 start"
else
    stop_server "$current_pid" || exit 1
fi
start_server || exit 1
verify_end_to_end

say "==== 重启完成 ===="
exit 0
