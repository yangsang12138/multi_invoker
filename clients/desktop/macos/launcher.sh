#!/bin/bash
# 桌面客户端启动器（macOS .app 外壳内部使用）
#
# 职责只有三件事，业务逻辑一律不放在这里：
#   1. 起核心（单文件可执行程序）—— 已经在跑就直接复用
#   2. 等核心就绪（轮询 /api/health，不用 sleep 猜时间）
#   3. 开一个「没有地址栏」的窗口指向核心首页；窗口关掉就停掉核心
#
# 变量由 tools/make_desktop_app.py 在生成 .app 时按实际路径替换。

set -u

APP_NAME="多服务调用器"
RES_DIR="$(cd "$(dirname "$0")/../Resources" && pwd)"
CORE_BIN="${RES_DIR}/__CORE_BIN__"
# 环境变量可以覆盖：换端口、指定浏览器、强制走默认浏览器
PORT="${MULTI_INVOKER_DESKTOP_PORT:-__PORT__}"
BROWSER_OVERRIDE="${MULTI_INVOKER_DESKTOP_BROWSER:-}"
LOG_DIR="${HOME}/Library/Logs"
LOG_FILE="${LOG_DIR}/multi-invoker.log"
HEALTH_URL="http://127.0.0.1:${PORT}/api/health"
HOME_URL="http://127.0.0.1:${PORT}/"

mkdir -p "${LOG_DIR}"

notify_error() {
  /usr/bin/osascript -e "display alert \"${APP_NAME}\" message \"$1\" as critical" >/dev/null 2>&1
}

# ---------------------------------------------------------------- 1. 核心 --
core_ready() {
  /usr/bin/curl -s --max-time 1 "${HEALTH_URL}" 2>/dev/null | /usr/bin/grep -q '"ok"'
}

SERVER_PID=""
if core_ready; then
  # 已经有一个核心在监听这个端口（比如用户手动起过），直接用它
  :
elif [ -x "${CORE_BIN}" ]; then
  "${CORE_BIN}" serve --port "${PORT}" --no-open >>"${LOG_FILE}" 2>&1 &
  SERVER_PID=$!
elif [ -f "${CORE_BIN}" ]; then
  /usr/bin/python3 "${CORE_BIN}" serve --port "${PORT}" --no-open >>"${LOG_FILE}" 2>&1 &
  SERVER_PID=$!
else
  notify_error "核心程序不存在：${CORE_BIN}
请重新运行 tools/make_desktop_app.py 生成 App。"
  exit 1
fi

cleanup() {
  if [ -n "${SERVER_PID}" ]; then
    kill "${SERVER_PID}" 2>/dev/null
    wait "${SERVER_PID}" 2>/dev/null
  fi
  [ -n "${CHROME_PROFILE}" ] && rm -rf "${CHROME_PROFILE}"
}
trap cleanup EXIT

# ---------------------------------------------------------------- 2. 就绪 --
for _ in $(seq 1 80); do
  core_ready && break
  sleep 0.25
done

if ! core_ready; then
  notify_error "核心启动失败，日志：${LOG_FILE}"
  exit 1
fi

# ------------------------------------------------------- 3. 无地址栏窗口 --
# 优先用 Chromium 系浏览器的 --app 模式：像一个真正的桌面应用，没有标签栏和地址栏。
# 独立的 user-data-dir 让它和用户日常浏览的窗口互不干扰，关掉也不影响其他标签页。
CHROME_PROFILE=""
CHROME_BIN=""
if [ -n "${BROWSER_OVERRIDE}" ]; then
  CHROME_BIN="${BROWSER_OVERRIDE}"
else
  for candidate in \
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" \
    "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge" \
    "/Applications/Brave Browser.app/Contents/MacOS/Brave Browser" \
    "/Applications/Chromium.app/Contents/MacOS/Chromium" \
    "/Applications/Arc.app/Contents/MacOS/Arc"; do
    if [ -x "${candidate}" ]; then CHROME_BIN="${candidate}"; break; fi
  done
fi

if [ -n "${CHROME_BIN}" ] && [ "${MULTI_INVOKER_DESKTOP_OPEN_MODE:-app}" != "default" ]; then
  CHROME_PROFILE="$(/usr/bin/mktemp -d -t multi-invoker-window)"
  "${CHROME_BIN}" \
    --app="${HOME_URL}" \
    --user-data-dir="${CHROME_PROFILE}" \
    --no-first-run \
    --no-default-browser-check \
    >>"${LOG_FILE}" 2>&1
else
  # 没装 Chromium 系浏览器就退回默认浏览器：只是少了「无地址栏」这一层壳
  /usr/bin/open "${HOME_URL}"
  # 默认浏览器无法感知窗口关闭，用一个隐藏的等待把这个 App 挂住，
  # 用户从菜单栏退出或再次双击时会走到 cleanup。
  while core_ready; do sleep 2; done
fi
