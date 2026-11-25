# 天文原图 · 暗场/平场配对与最小下载清单服务

给一批天文原图自动配齐标定文件（暗场 dark / 平场 flat），输出测光组可直接按单下载、重处理的清单。
FastAPI + SQLite 实现，一条 `docker compose up` 即可启动。

---

## 1. 配对与优化规则

对每张原图：

| 需求 | 匹配条件 |
| --- | --- |
| 暗场 ×1 | 同探测器 + **同曝光时长** |
| 平场 ×1 | 同探测器 + **同滤镜** |
| 批次约束 | 选中的暗场与平场必须出自**同一标定批次** `batch_id` |
| 可用性 | 只能选**未停用（在用）**的候选文件 |

每单提交原图列表和 **最多 16 个候选标定文件编号**。

**整单下载量最小**：同一标定文件被多张图复用时，其字节数只计一次。
由于候选文件 ≤ 16，服务端枚举全部 `2^m` 个子集（`m ≤ 16`，最多 65536 个），
找出“每张图都至少有一对合法暗/平组合落在子集内”的字节和最小子集，因此得到的是**全局最优**，
而不是逐图贪心。平局时按“文件数更少 → 编号字典序”稳定决胜。

**缺配对**：没有任何合法暗/平配对的原图会被逐张列出（含原因码），本单保持未发布状态。

**停用与重算**：

* 标定文件可随时停用，停用状态、原因、时间以及完整状态事件都持久化在 SQLite；
* 订单创建时先计算一版清单（草稿 `ready` / `unmatched`）；
* 发布（`POST /api/orders/{id}/publish`）在进程级临界区内重新读取候选文件的在用状态：
  若原选文件已被停用，则用**仍在用的候选重新计算**，再冻结发布；
* 发布后的清单以 JSON 快照形式固化。此后即使文件被停用，
  `GET /api/orders/{id}` 仍返回**当时发出的那份清单**（金额/配对不变）；
* 未发布订单被 GET 查询时，会即时给出按当前在用候选重算的草稿视图（`draft_stale` 标示与旧选择的差异），但不影响发布时的权威重算。

---

## 2. docker compose 启动

```bash
docker compose up -d --build
# 等待 healthy
docker compose ps
curl http://localhost:8000/health
```

* 服务监听容器内 `8000`，默认映射到宿主机 `8000`；可用 `HOST_PORT=8011 docker compose up` 改宿主端口。
* SQLite 数据库位于容器 `/data/calibration.db`，由命名卷 `astro-calib-data` 持久化。
* 交互式 API 文档：<http://localhost:8000/docs>，OpenAPI：<http://localhost:8000/openapi.json>。

停止：

```bash
docker compose down          # 保留数据卷
docker compose down -v       # 同时删除数据卷（清空登记/旧单）
```

### 本地直接运行（不用 docker）

```bash
python3 -m pip install -r requirements.txt
CALIB_DB_PATH=./data/calibration.db python3 -m uvicorn app.main:app --host 0.0.0.0 --port 8000
# 或 python main.py
```

运行测试：

```bash
python3 -m pip install pytest httpx
python3 -m pytest
```

---

## 3. 快速体验（curl）

```bash
BASE=http://localhost:8000

# 3.1 登记两份暗场、两份平场（同一探测器 CCD-A；B1 贵，B2 便宜）
curl -s -X POST $BASE/api/calibration-files -H 'Content-Type: application/json' -d '{
  "kind":"dark","detector":"CCD-A","exposure_seconds":60,
  "batch_id":"B1","size_bytes":100000000}'
curl -s -X POST $BASE/api/calibration-files -H 'Content-Type: application/json' -d '{
  "kind":"flat","detector":"CCD-A","filter":"r",
  "batch_id":"B1","size_bytes":100000000}'
curl -s -X POST $BASE/api/calibration-files -H 'Content-Type: application/json' -d '{
  "kind":"dark","detector":"CCD-A","exposure_seconds":60,
  "batch_id":"B2","size_bytes":4096}'
curl -s -X POST $BASE/api/calibration-files -H 'Content-Type: application/json' -d '{
  "kind":"flat","detector":"CCD-A","filter":"r",
  "batch_id":"B2","size_bytes":5120}'

# 3.2 提交一单（两张相同配置的图，复用 B2 那对 -> 9216 字节）
curl -s -X POST $BASE/api/orders -H 'Content-Type: application/json' -d '{
  "images":[
    {"image_id":"M42-001","detector":"CCD-A","exposure_seconds":60,"filter":"r"},
    {"image_id":"M42-002","detector":"CCD-A","exposure_seconds":60,"filter":"r"}
  ],
  "candidate_file_ids":[1,2,3,4]
}'

# 3.3 发布前停用已选中的 3 号暗场
curl -s -X POST $BASE/api/calibration-files/3/deactivate \
  -H 'Content-Type: application/json' -d '{"reason":"hot columns"}'

# 3.4 发布：自动用仍在用的候选重算，改用 B1 批次
curl -s -X POST $BASE/api/orders/1/publish

# 3.5 按编号查旧单（数字 id 或 ORD-000001 均可），拿到当时冻结的清单
curl -s $BASE/api/orders/1
curl -s $BASE/api/orders/ORD-000001/manifest
```

也可以一步创建并发布（原子地完成 计算→校验→冻结）：

```bash
curl -s -X POST "$BASE/api/orders?publish=true" -H 'Content-Type: application/json' -d @examples/order.json
```

---

## 4. HTTP API 速览

规范前缀 `/api`，同时挂载了无前缀版本；资源名支持 `calibration-files / calibrations / files`，
订单支持 `/api/orders` 与 `/orders` 等常见别名。

| 方法 & 路径 | 说明 |
| --- | --- |
| `GET /health` | 健康检查（compose healthcheck 使用） |
| `POST /api/calibration-files` | 登记一份标定文件；body 也可以是数组或 `{"files":[...]}` 批量 |
| `GET /api/calibration-files?active=true&kind=dark&detector=CCD-A` | 列文件/过滤 |
| `GET /api/calibration-files/{id}` | 取一份文件（数字 id 或自定义编号） |
| `POST /api/calibration-files/{id}/deactivate` | 停用，body 可带 `{"reason":"..."}`（别名 `/disable`、`DELETE`） |
| `POST /api/calibration-files/{id}/activate` | 重新启用（别名 `/enable`） |
| `POST /api/deactivations` | body `{"file_id":3,"reason":"..."}` 的停用入口 |
| `GET /api/deactivations` | 停用/状态事件审计表 |
| `POST /api/orders` | 提交订单并计算草稿；`?publish=true` 可直接发布；`?strict=true` 缺配对时返回 422 |
| `GET /api/orders` | 列旧单 |
| `GET /api/orders/{id}` | 查单：已发布返回**冻结清单**；未发布返回按在用候选刷新的草稿 |
| `POST /api/orders/{id}/publish` | 发布（必要时重算）并冻结清单；重复调用幂等返回原清单 |
| `POST /api/orders/{id}/recompute` | 显式重算草稿（已发布单保持不变） |
| `GET /api/orders/{id}/manifest` | 只取已发布清单；未发布返回 `409` 与缺失列表 |
| `GET /api/orders/{id}/events` | 订单事件（创建/重算/阻断/发布） |

`{id}` 既接受自增整数，也接受业务单号 `ORD-000001`，还接受登记时自定义的字符串编号。

### 清单结构（节选）

```json
{
  "id": 1,
  "order_no": "ORD-000001",
  "status": "published",
  "published": true,
  "total_bytes": 200000000,
  "total_download_bytes": 200000000,
  "files": [ {"id": 1, "kind": "dark", "size_bytes": 100000000, "...": "..."},
             {"id": 2, "kind": "flat", "size_bytes": 100000000, "...": "..."} ],
  "items": [
    {
      "image_id": "M42-001",
      "dark_file_id": 1, "flat_file_id": 2,
      "dark": {"...": "完整文件信息"},
      "flat": {"...": "完整文件信息"},
      "batch_id": "B1"
    }
  ],
  "pairings": "同 items 的别名",
  "missing_image_ids": [],
  "recomputed": true,
  "deactivated_selected_file_ids": [3],
  "manifest": { "..." : "冻结的完整清单快照，GET 旧单原样返回" }
}
```

未发布（缺配对）时：

```json
{
  "status": "unmatched",
  "published": false,
  "publishable": false,
  "missing_images": ["bad-exp"],
  "missing_image_ids": ["bad-exp"],
  "missing_image_details": [
    {"image_id": "bad-exp", "reasons": ["no_dark_with_same_exposure"]}
  ]
}
```

原因码：`no_active_file_for_detector`、`no_dark_for_detector`、
`no_dark_with_same_exposure`、`no_flat_for_detector`、
`no_flat_with_same_filter`、`dark_and_flat_not_same_batch` 等。

### 输入字段别名

服务对常见命名做了容错（并接受中文键），例如：

* 类型：`kind` / `type` / `file_type`，值 `dark`/`flat`/`暗场`/`平场`；
* 探测器：`detector` / `camera` / `sensor` / `ccd` / `探测器`；
* 曝光：`exposure_seconds` / `exposure` / `exptime` / `exp_time` / `曝光时长`；
  `60`、`60.0`、`"60.00"` 视为完全相同；
* 滤镜：`filter` / `filter_name` / `band` / `滤镜`；
* 批次：`batch_id` / `batch` / `batch_no` / `标定批次`；
* 字节：`size_bytes` / `bytes` / `file_size` / `download_bytes` / `大小`；
* 候选：`candidate_file_ids` / `candidate_ids` / `file_ids` / `candidates`；
* 原图列表：`images` / `frames` / `raw_images` / `原图`。

---

## 5. 代码结构

```
app/
  planner.py    # 纯领域逻辑：约束匹配 + 2^16 子集全局最优
  db.py         # SQLite：标定文件、状态事件、订单、订单事件
  service.py    # 输入归一化、订单状态机、清单冻结/序列化
  main.py       # FastAPI 路由与路径别名
tests/          # pytest：算法性质测试(300 随机用例对照朴素最优) + API 全流程
Dockerfile
docker-compose.yml
```

单进程 uvicorn + 进程内可重入锁把“检查停用 → 重算 → 冻结发布”串行化，
SQLite `busy_timeout` 兜底；发布快照保证旧单内容不可变。
