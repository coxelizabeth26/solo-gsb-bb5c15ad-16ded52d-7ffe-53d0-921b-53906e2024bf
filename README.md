# 直播字幕定稿服务（Live Subtitle Finalizer）

基于 FastAPI + SQLite 的直播字幕定稿服务。客户端按频道提交带**序号**、**修订号**、**文本**的字幕片段；服务仅从序号 1 起按**连续前缀**推进水位线，生成不可再修改的定稿字幕。

## 快速启动

```bash
docker compose up --build
```

单条命令完成构建、初始化数据库并启动服务，访问端口：**8000**。

- 交互式 API 文档：http://localhost:8000/docs
- 健康检查：http://localhost:8000/healthz

## 配置

通过环境变量配置（`docker-compose.yml` 中已给出默认值）：

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `DATABASE_PATH` | `/data/subtitles.db` | SQLite 数据库文件路径。启动时自动建库建表（幂等），无需手动初始化 |

Compose 默认将 `/data` 挂载到命名卷 `subtitles-data`，容器重启数据不丢失。修改宿主机端口映射可编辑 `docker-compose.yml` 中的 `ports`（如 `"8080:8000"`）。

## 核心语义

- **乱序到达**：片段可按任意顺序提交，未进入连续前缀的片段进入暂存区。
- **修订裁决**：同一序号只接受**更高修订号**；低于或等于已暂存修订号的提交不生效。
- **水位线推进**：仅从序号 1 起按连续前缀推进；缺口补齐后，一次性释放全部连续片段定稿。
- **定稿不可变**：序号 ≤ 水位线的片段已定稿，任何后续提交（即使修订号更高）均不生效。
- **并发事务化**：「接收片段 + 推进水位线 + 落库定稿」在同一 `BEGIN IMMEDIATE` 事务中完成，不会重复定稿、不会越过缺口。

## API

### 提交字幕片段

`POST /channels/{channel}/fragments`

```bash
curl -X POST http://localhost:8000/channels/news/fragments \
  -H 'Content-Type: application/json' \
  -d '{"seq": 1, "revision": 1, "text": "各位观众晚上好"}'
```

响应（`accepted` 表示本次提交是否生效，`watermark` 为最新水位线，`released` 为本次触发定稿的连续片段）：

```json
{
  "channel": "news",
  "seq": 1,
  "accepted": true,
  "watermark": 1,
  "released": [{"seq": 1, "revision": 1, "text": "各位观众晚上好"}]
}
```

约束：`seq >= 1`，`revision >= 0`，否则返回 422。

### 查询频道定稿

`GET /channels/{channel}/finalized?from_seq=1&limit=100`

```bash
curl http://localhost:8000/channels/news/finalized
```

```json
{
  "channel": "news",
  "watermark": 1,
  "fragments": [
    {"seq": 1, "revision": 1, "text": "各位观众晚上好", "finalized_at": "2026-09-20T06:20:53.818Z"}
  ]
}
```

`from_seq`（起始序号，默认 1）与 `limit`（最大条数）为可选查询参数。

### 查询频道状态

`GET /channels/{channel}` → `{"channel", "watermark", "pending", "finalized"}`

## 行为示例

```bash
# 乱序：2、3 先到，水位线不动（watermark=0）
curl -X POST .../channels/news/fragments -d '{"seq":2,"revision":1,"text":"b"}'
curl -X POST .../channels/news/fragments -d '{"seq":3,"revision":1,"text":"c"}'
# 补齐缺口：1 到达后一次释放 1-3，watermark=3
curl -X POST .../channels/news/fragments -d '{"seq":1,"revision":1,"text":"a"}'
# 同序号低修订号：accepted=false
curl -X POST .../channels/news/fragments -d '{"seq":5,"revision":1,"text":"旧"}'
curl -X POST .../channels/news/fragments -d '{"seq":5,"revision":1,"text":"旧"}'   # 不生效
curl -X POST .../channels/news/fragments -d '{"seq":5,"revision":2,"text":"新"}'   # 生效
# 已定稿序号不可修改：accepted=false
curl -X POST .../channels/news/fragments -d '{"seq":1,"revision":9,"text":"x"}'
```

## 本地开发与测试

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt -r requirements-dev.txt
pytest tests/ -v          # 含乱序、修订、缺口释放、并发事务测试
DATABASE_PATH=/tmp/sub.db uvicorn app.main:app --port 8000
```

## 设计说明

- **存储**：`pending_fragments`（暂存区，可被更高修订号覆盖）、`finalized_fragments`（定稿，只增不改）、`channels`（水位线）。
- **事务**：提交端点在一个 `BEGIN IMMEDIATE` 事务内完成「 upsert 暂存 → 扫描连续前缀 → 迁移定稿 → 更新水位线」，进程内互斥锁 + SQLite 单写者语义 + `busy_timeout` 保证并发正确。
- **部署**：默认单 worker。多 worker 共享同一 SQLite 文件时同样安全（`BEGIN IMMEDIATE` 串行化写事务），但建议单 worker 以获得稳定吞吐。
