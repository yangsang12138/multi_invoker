#!/bin/bash
# 桌面客户端启动器（Linux，模板）
#
# 装上 multi-invoker 后，把本文件复制到 /usr/local/bin/multi-invoker-launch 并 chmod +x，
# 再把 linux/multi-invoker.desktop 复制到 ~/.local/share/applications/ 即可在应用菜单里看到。
#
# 行为与 macOS 版一致：起核心 → 等就绪 → 开无地址栏窗口（有 Chromium 系浏览器时）→ 关闭即停。

set -u

PORT="${MULTI_INVOKER_DESKTOP_PORT:-8765}"
CORE_BIN="${MULTI_INVOKER_DESKTOP_CORE:-multi-invoker}"
LOG_FILE="${HOME}/.multi-invoker/desktop.log"
HEALTH_URL="http://127.0.0.1:${PORT}/api/health"
HOME_URL="http://127.0.0.1:${PORT}/"

mkdir -p "$(dirname "${LOG_FILE}")"

core_ready() {
  curl -s --max-time 1 "${HEALTH_URL}" 2>/dev/null | grep -q '"ok"'
}

SERVER_PID=""
if ! core_ready; then
  "${CORE_BIN}" serve --port "${PORT}" --no-open >>"${LOG_FILE}" 2>&1 &
  SERVER_PID=$!
fi

cleanup() {
  [ -n "${SERVER_PID}" ] && kill "${SERVER_PID}" 2>/dev/null
  [ -n "${PROFILE:-}" ] && rm -rf "${PROFILE}"
}
trap cleanup EXIT

for _ in $(seq 1 80); do core_ready && break; sleep 0.25; done
core_ready || { echo "核心启动失败，日志：${LOG_FILE}" >&2; exit 1; }

BROWSER_BIN=""
for candidate in chromium chromium-browser google-chrome google-chrome-stable brave-browser; do
  if command -v "${candidate}" >/dev/null 2>&1; then BROWSER_BIN="${candidate}"; break; fi
done

if [ -n "${BROWSER_BIN}" ]; then
  PROFILE="$(mktemp -d -t multi-invoker-window-XXXX)"
  "${BROWSER_BIN}" --app="${HOME_URL}" --user-data-dir="${PROFILE}" \
    --no-first-run --no-default-browser-check >>"${LOG_FILE}" 2>&1
else
  xdg-open "${HOME_URL}" >/dev/null 2>&1
  while core_ready; do sleep 2; done
fi
