# Multi-Service Invoker

一个本地的**调用编排层**：在多个外部系统的多个接口上，按「哪个调用项 × 哪些节点 × 什么环境」组合并发执行，结果实时回流、按节点排序、可导出 CSV。

中文展示名「**多服务调用器**」。所有内部模块、可执行文件、配置文件路径、日志路径、Bundle ID 一律英文。

零第三方依赖，只用 Python 3.9+ 标准库。

> **这里只有一个核心库 `multi_invoker/`** —— 负责业务；所有"客户端"（CLI / 浏览器 / 桌面 .app / 远程）只是用不同方式启动这个核心。

---

## 0. 直接下载（不想自己构建）

到 [**Releases**](https://github.com/yangsang12138/multi_invoker/releases/latest) 页面下载对应平台的包：

| 平台 | 文件 | 用法 |
| --- | --- | --- |
| **macOS** | `Multi-Service-Invoker-<版本>-macos.zip` | 解压后双击 `Multi-Service Invoker.app`，关窗即退 |
| **Windows** | `Multi-Service-Invoker-<版本>-windows.zip` | 解压后双击 `multi-invoker.bat` |
| **Linux** | `Multi-Service-Invoker-<版本>-linux.tar.gz` | `chmod +x multi-invoker && ./multi-invoker serve` |
| **通用** | `multi-invoker-<版本>.pyz` | 任何装有 Python 3.9+ 的系统 |

每个包内附该平台的简短说明；`SHA256SUMS.txt` 可用于校验下载完整性。

> ⚠️ **前提：目标机器需要 Python 3.9+**。本工具零第三方依赖，但打包形态是 Python zipapp，
> **不内嵌解释器**。Windows 安装 Python 时请勾选「Add Python to PATH」。
>
> macOS 首次打开若被 Gatekeeper 拦下，右键点 App → 打开；或执行
> `xattr -cr "Multi-Service Invoker.app"`。

---

## 1. 启动方式速查

| 场景 | 命令 / 操作 | 适合 |
|---|---|---|
| **本机命令行** | `python3 query.py serve` 或 `./dist/multi-invoker serve` | 开发、自测 |
| **本机浏览器访问** | `python3 query.py serve` → 浏览器开 http://127.0.0.1:8765/ | 临时用 |
| **macOS 桌面 App** | `python3 tools/build_single_file.py && python3 tools/make_desktop_app.py` → 双击 `dist/Multi-Service Invoker.app` | 日常使用 |
| **Linux 桌面** | `sudo cp dist/multi-invoker /usr/local/bin/ && cp clients/desktop/linux/multi-invoker.desktop ~/.local/share/applications/` | 日常使用 |
| **Windows 桌面** | 把 `dist/multi-invoker.pyz` 和 `clients/desktop/windows/multi-invoker.bat` 放一起 → 双击 .bat | 日常使用 |
| **远程 / 服务器** | `./multi-invoker serve --host 127.0.0.1 --allow-origin '*'`，前端用 nginx 反代或单独部署网页壳 | 多人协作、CI |
| **CI / 脚本** | `./multi-invoker query S_XX --csv out.csv` | 自动化测试、批处理 |

每种方式**业务逻辑走的是同一份 `multi_invoker/` 代码**，只是外壳不同。

---

## 2. 本机命令行（开发用）

```bash
cd /path/to/repo

# 启动网页界面（默认 http://127.0.0.1:8765/，自动开浏览器）
python3 query.py serve

# 换端口 / 不开浏览器 / 允许跨域
python3 query.py serve --port 9000 --no-open --allow-origin '*'

# 端口被占不想操心时：让系统挑一个空闲端口
python3 query.py serve --port auto
# 输出会明确打印实际端口，例如：
#   端口：auto → 系统分配了 52638
# 等价的写法：--port 0

# 查看配置 / 列出节点 / 调用项
python3 query.py platforms
python3 query.py services
python3 query.py info          # 看版本、运行形态、数据目录

# 直接执行一次查询
python3 query.py query S_XX_XX_00 --tag 正式 --csv out.csv
python3 query.py plan S_XX_XX_00 --full     # 只组装请求不发网络，人工核对
```

数据目录：

| 运行形态 | 配置路径 |
|---|---|
| 源码运行（`python3 query.py ...`） | 仓库下的 `config/` |
| 单文件 / 打包后 | `~/.multi-invoker/config`（可用 `MULTI_INVOKER_HOME` 或 `--config-dir` 覆盖） |

### 2.1 端口冲突时会发生什么

启动时端口被占，会分三种情况处理 —— **不会**把 socketserver 的 traceback 甩给用户：

| 占用者 | 行为 | 退出码 |
|---|---|---|
| **本工具自己的核心**（`/api/health` 返回 `{"ok": true}`） | **自动复用**：提示「端口 X 上已经有一个核心在跑，直接用它」，并按需打开浏览器。不用管它 | 0 |
| **别的程序** | 不打自动降级，明确告诉你三件事：怎么查占用者、怎么换端口、怎么先结束它 | 2 |
| 端口 < 1024 且无权限 | 提示换一个大于 1024 的端口 | 2 |

想要「被占了就换一个」的效果，用 `--port auto`：

```bash
python3 query.py serve --port auto
```

> 为什么默认**不**静默换端口：脚本里写 `--port 8765` 是有意的，
> 悄悄换到 8766 会让自动化连接不上、且难以排查。
> `auto` 是显式选择，且会把实际端口打印出来。
>
> ⚠️ `--port auto` 与**桌面 .app** 不兼容 —— 启动器按固定端口轮询 `/api/health`。
> 桌面 App 要换端口请用 `tools/make_desktop_app.py --port <端口>` 重新生成。

---

## 3. 本机浏览器（临时用）

```bash
# 1. 起核心
python3 query.py serve --no-open &

# 2. 浏览器访问
open http://127.0.0.1:8765/        # macOS
# 或直接在浏览器地址栏输入
```

适用：临时查一下、自己电脑上的快速验证。

---

## 4. macOS 桌面 App（双击启动）

适合日常使用，双击就开，关窗即退。

### 4.1 生成 .app

```bash
cd /path/to/repo

# 1. 把核心打成单文件可执行（zipapp）
python3 tools/build_single_file.py
# 产物：dist/multi-invoker（~100 KB）

# 2. 装进 .app 外壳
python3 tools/make_desktop_app.py
# 产物：dist/Multi-Service Invoker.app
#      Contents/
#        Info.plist              ← Bundle ID = local.multi.invoker，
#                                  CFBundleDisplayName = "多服务调用器"（中文别名）
#        MacOS/launcher          ← 启动器脚本
#        Resources/multi-invoker ← 单文件核心
```

### 4.2 启动

- 在 Finder 里双击 `Multi-Service Invoker.app`
- 或命令行 `open dist/Multi-Service Invoker.app`

行为：探测端口 → 没有核心就起一个 → 轮询 `/api/health` 就绪 → 用 Chromium 系浏览器的 `--app` 模式开**无地址栏窗口** → 窗口关闭即停核心。

### 4.3 调参

```bash
python3 tools/make_desktop_app.py --name "My Invoker" --port 9000
```

> `--port` 是**生成时烧进 launcher 的**：换端口必须重新跑 `make_desktop_app.py`。
> 临时换端口可以用下面的环境变量（无需重生成）。

排障环境变量（在 `launcher.sh` 里生效）：

```
MULTI_INVOKER_DESKTOP_PORT=9000      # 换端口
MULTI_INVOKER_DESKTOP_BROWSER=...    # 强制某浏览器可执行路径
MULTI_INVOKER_DESKTOP_OPEN_MODE=default  # 不强制 --app 模式（用默认浏览器）
```

日志：`~/Library/Logs/multi-invoker.log`

---

## 5. Linux 桌面（应用菜单）

```bash
# 1. 装可执行文件
sudo cp dist/multi-invoker /usr/local/bin/

# 2. 装启动器
sudo cp clients/desktop/linux/launch.sh /usr/local/bin/multi-invoker-launch
sudo chmod +x /usr/local/bin/multi-invoker-launch

# 3. 注册到应用菜单
cp clients/desktop/linux/multi-invoker.desktop ~/.local/share/applications/
```

应用菜单里搜「多服务调用器」→ 点开就行。

---

## 6. Windows 桌面（双击 .bat）

```cmd
REM 把这两个文件放同一目录：
REM   multi-invoker.pyz       (build_single_file.py 产物)
REM   multi-invoker.bat       (clients/desktop/windows/multi-invoker.bat)

REM 双击 multi-invoker.bat 即可。
```

要求：装了 Python 3.9+，且安装时勾了「添加到 PATH」。

---

## 7. 远程 / 服务器模式（多人协作、CI）

适合：核心跑在一台机器，网页壳在另一台机器（或多人共用一个核心）。

```bash
# 在服务器上：核心只监听回环、允许跨域
./multi-invoker serve --host 127.0.0.1 --port 8765 \
    --no-open --allow-origin '*'

# 网页壳：单独部署（任选其一）
#   a) 把 web/ 部署到 nginx / GitHub Pages，运行时把 ?api= 指过去
#   b) 直接用本机 CLI 跑 ./multi-invoker ... query ... --csv ...
```

⚠️ 远程模式默认没有鉴权 —— **谁能连上 `8765` 端口谁就能调你的所有节点**。生产环境务必：
- `--host 127.0.0.1` + nginx 加 Basic auth
- 或 `--allow-origin` 限定到具体来源
- 或前面挂 VPN / WireGuard

详细 nginx 示例见 `clients/remote/`（已简化清理，需要时单独发）。

---

## 8. CI / 自动化脚本

`multi-invoker` **不是 SDK**——它是命令行工具。CI 里直接 exec 它：

```yaml
# GitHub Actions 示例
- name: Run daily health check
  run: |
    ./dist/multi-invoker serve --port 8765 --no-open &
    SERVE_PID=$!
    sleep 2
    ./dist/multi-invoker query S_HEALTH_CHECK --tag 正式 --csv out/health.csv
    test -s out/health.csv
    kill $SERVE_PID
```

常用子命令：`query` / `plan`（干跑）/ `platforms` / `services` / `headers` / `serve` / `info`。

---

## 9. 目录与代码

```
query.py                 命令行 + 网页入口（薄壳）
__main__.py              zipapp 单文件的入口（与 query.py 并列）
multi_invoker/           核心库（所有业务在这里）
  __init__.py            版本号
  cli.py                 argparse + 子命令分发
  server.py              HTTP 服务（ThreadingHTTPServer）
  config.py              Config 聚合（从 SQLite 读 snapshot）
  db.py                  SQLite 持久层
  migrate.py             老 JSON 自动迁移
  runtime.py             路径解析（源码 / 单文件 / PyInstaller）
  client.py              HTTP 客户端（超时、证书、跳转、GBK）
  runner.py              URL 组装、并发执行、结果汇总
  store.py               配置 CRUD（校验 + 写回）
web/                     网页壳（HTML/CSS/JS，原型非产物）
config/                  数据目录（运行时自动创建）
  app.db                 SQLite（gitignored）
tools/
  build_single_file.py   → dist/multi-invoker
  make_desktop_app.py    → dist/Multi-Service Invoker.app
clients/
  README.md              客户端成形方案总览
  desktop/
    README.md            桌面壳怎么生成
    macos/launcher.sh    macOS .app 启动器模板
    windows/multi-invoker.bat   Windows 启动器模板
    linux/launch.sh      Linux 启动器模板
    linux/multi-invoker.desktop Linux 应用菜单项
```

---

## 10. 设计原则

- **不绑协议 / 不绑厂商 / 不绑业务** —— 节点地址、调用项定义、URL 模板完全由用户维护
- **预览即真相** —— 预览接口与真实调用走完全相同的 URL 解析逻辑
- **零第三方依赖** —— 只用 Python 标准库；Python 3.9+ 即可
- **一份核心，多个外壳** —— 改业务只动 `multi_invoker/`，所有客户端同步更新

---

## 11. 关闭与清理

「彻底关闭」包含两件事：**核心进程退出 + 端口 8765 释放**。下面按启动方式给操作步骤。

### 11.1 进程退出（两种办法，**都用 SIGINT，不是 SIGKILL**）

| 启动方式 | 优雅退出 |
|---|---|
| 前台 `python3 query.py serve` | 在终端按 **Ctrl+C** |
| 后台 `python3 query.py serve &`（macOS/Linux） | `fg %1` 把它拉到前台，再 Ctrl+C；或 `kill -INT <PID>` |
| 后台（Windows） | 在启动它的 cmd 窗口按 Ctrl+C |
| 桌面 .app | **关掉那个无地址栏窗口**——`launcher.sh` 的 `trap cleanup EXIT` 会自动停核心 |
| 桌面 .bat | 关闭启动时弹出的那个黑窗口 |
| Linux 应用菜单 | 关闭启动时弹出的窗口 |

> 为什么不是 `kill -9`：SIGKILL 让进程没机会刷写 SQLite WAL，可能丢最近一次保存。SIGINT 走正常清理，SQLite 安全 checkpoint。

### 11.2 端口释放（确认彻底关了）

进程退出后端口应该立即释放。**确认**：

```bash
# macOS / Linux
lsof -nP -iTCP:8765 -sTCP:LISTEN

# 没输出 = 端口空闲 = 关干净了
```

### 11.3 强制清理（关不掉的时候）

如果上面方法关不掉（Ctrl+C 无响应、桌面 App 卡死、orphan 子进程），按这个顺序来：

```bash
# 1. 找进程
lsof -nP -iTCP:8765 -sTCP:LISTEN
# 输出形如：Python 12345 yangsang ... TCP localhost:ultraseek-http (LISTEN)
# PID 就是 12345

# 2. 先 SIGINT（优雅）
kill -INT 12345
sleep 2

# 3. 还活着再 SIGTERM
kill 12345
sleep 2

# 4. 实在不行才 SIGKILL（可能丢最近一次写入的数据）
kill -9 12345

# 5. 验证端口已释放
lsof -nP -iTCP:8765 -sTCP:LISTEN   # 应该没输出
```

Windows 上对应：

```powershell
# 1. 找进程
netstat -ano | findstr :8765
# 末尾的 PID 就是进程号

# 2. 优雅结束
taskkill /pid <PID>
# 还活着就强杀
taskkill /f /pid <PID>

# 或者一键关掉所有 multi-invoker 核心（按窗口标题匹配）
taskkill /f /im python.exe /fi "WINDOWTITLE eq multi-invoker-core*"
```

### 11.4 一键彻底关掉一切

```bash
# macOS / Linux：找出所有 multi-invoker / query.py 进程并优雅结束
pkill -INT -f 'multi-invoker serve'
pkill -INT -f 'query.py serve'

# 等 2 秒看是否都退了
sleep 2
pgrep -fl 'multi-invoker serve|query.py serve' || echo "全清了"
lsof -nP -iTCP:8765 -sTCP:LISTEN   # 端口空闲 = 收工
```

### 11.5 配套清理（**不是必须**，看你要不要）

| 文件 / 目录 | 删了影响什么 |
|---|---|
| `config/app.db` | 所有节点 / 调用项 / 请求头数据；下次启动是全新的空 DB |
| `~/.multi-invoker/` | 单文件运行时所有数据；同上 |
| `~/Library/Logs/multi-invoker.log` | 日志，删了下次重新写 |
| `dist/` | 重新 `python3 tools/build_single_file.py` 还能生成 |
| `~/.local/share/applications/multi-invoker.desktop` | Linux 应用菜单里那个图标 |
| `~/Library/Application Support/multi-invoker-desktop-profile/` | macOS 桌面 App 的临时 user-data-dir |

---

## 12. 故障排查

| 现象 | 查什么 |
|---|---|
| `python3 query.py serve` 起不来 | 看 stderr；多半是端口被占（`lsof -nP -iTCP:8765 -sTCP:LISTEN`） |
| 浏览器打开是红字「Failed to fetch」 | 核心没跑。终端里跑 `python3 query.py serve` |
| 桌面 App 双击没反应 | 看 `~/Library/Logs/multi-invoker.log` |
| 数据写哪里了 | `python3 query.py info` 打印 `home` / `configDir` |
| 想清空所有配置 | 删 `config/app.db`（或 `~/.multi-invoker/config/app.db`）重启 |
| 单文件找不到 Python | `./multi-invoker` 第一行 `#!/usr/bin/env python3`，需要系统 PATH 上有 Python 3.9+ |