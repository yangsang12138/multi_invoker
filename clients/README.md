# 客户端成形方案（多客户端）

> 参照 worklog 的做法：**一份核心，多个外壳**。
> worklog 把同一份前端分别装进 Tauri 桌面壳和独立站点壳；这里把这套结构
> 落到一个纯标准库的 Python 项目上，不引入任何构建链路。

---

## 1. 结构

```
                    ┌──────────────────────────────┐
                    │  核心 core                   │
                    │  multi_invoker（HTTP API）      │
                    │  config/app.db  ← 唯一数据源   │
                    └──────────────┬───────────────┘
                                   │  HTTP / JSON
        ┌──────────────┬───────────┴────────┬──────────────────┐
        │              │                    │                  │
   ┌────▼────┐   ┌─────▼─────┐      ┌───────▼───────┐   ┌──────▼───────┐
   │ CLI 外壳 │   │ Web 外壳   │      │ Desktop 外壳   │   │ Remote-Web   │
   │ query.py│   │ web/*.html│      │ .app / .desktop│   │ 独立站点+远程核心│
   └─────────┘   └───────────┘      └───────────────┘   └──────────────┘
```

**核心**只做三件事：读写配置、按模板发请求、把结果整理成 JSON。
所有业务规则（URL 模板解析、请求体拼装、并发、取值路径）都在核心里，**只实现一次**。

**外壳**只负责「宿主能力」：怎么被启动、窗口长什么样、用户在哪里点。
外壳之间不共享代码，也不复制业务逻辑 —— 它们共享的是**同一个 HTTP 契约**。

---

## 2. 四种外壳

| 外壳 | 在哪 | 启动方式 | 比别的外壳多了什么 | 边界 |
| --- | --- | --- | --- | --- |
| **CLI** | `query.py` / `multi-invoker` | `multi-invoker query S_XX --tag 正式 --csv out.csv` | 可脚本化、可进 CI、可 `plan` 干跑核对 | 无 UI，结果靠文本表格 |
| **Web** | `web/*.html` | 核心启动后浏览器打开 `/` | 可视化挑条件、实时刷新、导出 | 需要核心同源或用 `?api=` 指过去 |
| **Desktop** | `clients/desktop/` | 双击 `.app` / 菜单项 | 无地址栏窗口、程序坞图标、关窗即退 | 外壳本身不含逻辑 |
| **Remote-Web** | 任意静态服务器 | 见 `clients/remote/` | 网页和核心可以分机器部署 | 需要核心开 `--allow-origin` |

四种外壳可以**同时存在**：同一个人开着桌面窗口，同事用浏览器连他的核心，
CI 再用 CLI 跑一遍回归 —— 它们看到的是同一份 `app.db`。

---

## 3. 外壳与核心的契约

外壳只依赖下面这些接口，不读文件、不 import 核心内部模块（CLI 除外，它和核心同仓）。

| 方法 | 路径 | 用途 |
| --- | --- | --- |
| GET | `/api/health` | 存活探测，外壳启动时轮询它判断核心是否就绪 |
| GET | `/api/meta` | 握手：核心版本、运行形态、数据目录、平台/调用项数量、跨域开关 |
| GET | `/api/config` | 调用页需要的节点 + 调用项 + 模板快照 |
| GET | `/api/services`、`/api/platforms`、`/api/headers` | 各编辑页的数据源 |
| POST | `/api/preview`、`/api/preview/batch` | 只组装请求、不发网络，用于人工核对 |
| POST | `/api/query`、`/api/query/batch` | 执行；带 `stream: true` 时按 NDJSON 边跑边回 |
| POST | `/api/services/save` 等 | 写配置（写完核心自动 reload，不重启） |
| OPTIONS | `/api/*` | 跨域预检，仅在核心开启 `--allow-origin` 时返回 204 |

**版本兼容原则**：新增字段只增不改；外壳用不到的字段忽略即可。
`/api/meta` 里的 `version` 就是给外壳做能力判断用的。

---

## 4. 目录

```
clients/
├── README.md                  # 本文（方案总览）
├── desktop/
│   ├── README.md              # 桌面外壳怎么生成 / 怎么改
│   ├── macos/launcher.sh      # macOS .app 内部的启动器模板
│   ├── windows/*.bat          # Windows 启动器模板
│   └── linux/launch.sh        # Linux 启动器模板 + .desktop
└── remote/
    └── README.md              # 网页与核心分机部署（含 nginx 示例）
tools/
├── build_single_file.py       # 核心 → 一个可执行文件
└── make_desktop_app.py        # 核心 + 启动器 → macOS .app
```

---

## 5. 新增一个外壳时要做什么

只允许做这几件事，做别的说明分层错了：

1. 能启动 / 连上一个核心（本机的，或 `?api=` 指过去的）。
2. 启动时打一次 `/api/meta` 做握手，版本不匹配给出人话提示。
3. 用 `/api/health` 轮询等待就绪，不要用固定 `sleep` 猜。
4. 把用户的动作翻译成上面表里的请求；**不要**在壳里重新实现 URL 模板、请求体拼装、取值路径。
5. 壳自己的状态（窗口大小、上次选的核心地址）自己存，不要写进 `app.db`。

反例：外壳里自己拼 URL、自己判断 `pick` 路径是否合法、自己算并发数 ——
这些一旦分叉，各个壳的行为就会不一致。

---

## 6. 和 worklog 的对应关系

| worklog 的做法 | 本项目的对应 |
| --- | --- |
| `packages/worklog` 一份前端 + `src-tauri` 原生壳 | `web/` 一份前端 + `clients/desktop/` 启动器壳 |
| `packages/site` 独立部署到 GitHub Pages | `clients/remote/` 网页与核心分机部署 |
| 数据层（SQLite）与 UI 解耦，可导出可同步 | `config/app.db` + `/api/*` 契约，外壳不碰文件 |
| 用 Tauri 能力清单约束壳能干什么 | 壳只允许访问 `/api/*`，不读磁盘 |
| `tauri.conf.json` 里窗口/图标/更新集中配置 | `tools/make_desktop_app.py --name --port` 集中配置 |

差异：worklog 用 Tauri 是为了拿到原生窗口、文件和自动更新；
本项目零依赖优先，所以桌面外壳用「系统里已有的浏览器 + `--app` 模式」实现，
代价是没有自动更新和系统托盘 —— 需要这些时再换真正的原生壳，
但**不要因此把业务逻辑搬进壳里**。
