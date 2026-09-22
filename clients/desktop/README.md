# 桌面客户端

桌面外壳 = **单文件核心** + **一个启动器脚本** + **一个 .app / .desktop / .bat 壳**。
壳里没有业务代码，只有「起核心、等就绪、开窗口、关窗口停核心」四步。

---

## macOS

```bash
python3 tools/build_single_file.py      # 1. 生成核心（dist/multi-invoker）
python3 tools/make_desktop_app.py       # 2. 装进 .app（不存在会自动先做第 1 步）
open "dist/Multi-Service Invoker.app"        # 3. 双击也行
```

产物结构：

```
dist/Multi-Service Invoker.app/Contents/
├── Info.plist              # 应用名、图标、最低系统版本
├── MacOS/launcher          # clients/desktop/macos/launcher.sh（已替换占位符）
└── Resources/multi-invoker   # 单文件核心，原样复制
```

可调参数：

```bash
python3 tools/make_desktop_app.py --name "我的调用器" --port 9000 --out dist
```

### 启动器做了什么

1. 探测 `http://127.0.0.1:<端口>/api/health`：已经有核心在跑就直接复用，不再起第二个。
2. 没有就 `multi-invoker serve --port <端口> --no-open`，日志写 `~/Library/Logs/multi-invoker.log`。
3. 轮询 `/api/health` 直到就绪（最多 20 秒），**不靠固定 sleep 猜时间**。
4. 优先用 Chromium 系浏览器的 `--app=` 模式开一个**没有地址栏和标签栏**的窗口，
   并用独立的 `--user-data-dir` 隔离，关掉不影响用户日常浏览器。
5. 找不到 Chromium 系浏览器就退回默认浏览器（少一层壳，功能不变）。
6. 窗口关闭 → 结束核心进程并清理临时 profile。

### 环境变量（排障用）

| 变量 | 作用 |
| --- | --- |
| `MULTI_INVOKER_DESKTOP_PORT` | 覆盖端口，适合和别的核心并存 |
| `MULTI_INVOKER_DESKTOP_BROWSER` | 指定浏览器可执行文件，跳过自动探测 |
| `MULTI_INVOKER_DESKTOP_OPEN_MODE=default` | 强制走默认浏览器，不要 `--app` 窗口 |
| `MULTI_INVOKER_HOME` | 覆盖数据目录（默认 `~/.multi-invoker`） |

---

## Windows

`windows/启动Multi-Service Invoker.bat` 是模板：把它和 `dist/multi-invoker.pyz` 放同一目录后双击。

前提是目标机器装了 Python 3.9+ 且 `python` 在 PATH 上。
需要「完全不带 Python」时，用 PyInstaller 把核心打成 `.exe`，再把 bat 里的
`python "%CORE%"` 换成直接调用该 exe 即可；核心代码不用改。

---

## Linux

```bash
sudo cp dist/multi-invoker /usr/local/bin/
sudo cp clients/desktop/linux/launch.sh /usr/local/bin/multi-invoker-launch
sudo chmod +x /usr/local/bin/multi-invoker-launch
cp clients/desktop/linux/multi-invoker.desktop ~/.local/share/applications/
```

之后在应用菜单里搜「Multi-Service Invoker」即可。行为与 macOS 版一致：
有 Chromium 系浏览器就开无地址栏窗口，否则 `xdg-open` 默认浏览器。

---

## 什么时候该换真正的原生壳

现在这个壳的代价很明确：**没有系统托盘、没有自动更新、依赖机器上装了浏览器**。
出现下面任一需求时，再考虑 Tauri / PySide / 原生 Swift：

- 需要开机自启 + 托盘常驻
- 需要自动更新（worklog 用 Tauri updater + 签名产物）
- 需要离线携带浏览器内核

换壳时**只换壳**：核心仍然是那个 HTTP 服务，`/api/*` 契约不动。
这也是这套结构存在的意义 —— 壳可以重写，逻辑不用重写。
