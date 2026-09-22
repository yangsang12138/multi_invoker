# 配置示例与首次启动引导

`config/` 是本机的**运行时数据目录**，内容不会进入 git：

| 文件 | 说明 | 是否入库 |
| --- | --- | --- |
| `app.db` | SQLite 主库，节点 / 调用项 / 请求头 / URL 模板都在这里 | ❌ 私有 |
| `app.db-wal`、`app.db-shm` | SQLite WAL 附属文件 | ❌ 私有 |
| `*.json.imported` | 从旧版 JSON 迁移过来的原件备份 | ❌ 私有 |
| `examples/` | 本文档与示例 JSON | ✅ 入库 |

## 新克隆仓库后怎么起步

方式一（推荐）：直接启动服务，在网页上配置。

```bash
python3 query.py serve      # 或 ./dist/multi-invoker serve
```

首次启动配置为空是正常状态，服务会自动放宽校验，可以用「节点列表 / 调用项定义 / URL 模板」
三个页面把数据补上，全部写入 `config/app.db`。

方式二：用示例数据快速看效果。

```bash
cp config/examples/*.json config/
python3 query.py serve
```

启动时会自动把这三个 JSON 导入 `app.db`，并把原文件改名为 `*.json.imported` 留档。
导入只发生一次（`meta.imported_from_json` 标记位控制）。

## JSON 字段速查

- `platforms.json` → `platforms[]`：`code` / `name` / `shortName` / `tags` /
  `intranetIp` / `insecure` / `follow` / `anyScheme` /
  `endpoints[]`（每项 `{ "tags": ["测试"], "baseUrl": "http://..." }`）
- `platforms.json` → `meta.contextProfiles[]`：`name` / `context` / `matchTags` /
  `priority` / `urlTemplate`（模板占位符见 `multi_invoker/store.py` 白名单）
- `services.json` → `services[]`：`code` / `name` / `method` / `defaultModule` /
  `moduleByPlatform` / `bodyFields[]` / `bodyByPlatform` / `pick` / `bodyFormat` /
  `tags` / `headers` / `responseFields[]`
- `headers.json`：`defaultHeaders`（K/V）+ `bodyFormat`（`json` 或 `form`）

> 示例里的域名与编码都是占位值，**不要直接拿去调用**；替换成自己的地址后再用。
