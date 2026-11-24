# 学籍更正清单 API

区教育局按批登记补登的转学单，整批生效后按 **批号 / 学校 / 统计日** 查询
各校名单的“补报”与“撤回”。数据存储于 SQLite，接口文档位于 `/docs`。

## 业务语义

| 概念 | 规则 |
| --- | --- |
| 转学单 | 单号、学生号、转入校、生效日；**单号与学生号固定绑定**，生效日与转入校可在提交前更正 |
| 批次 | 先建草稿批次，在批内登记/更正转学单，再整批提交；提交后批次只读 |
| 生效约束 | 整批提交时校验：同一学生同一生效日最多一张转学单 |
| 归属 | 某统计日（含当日）时，学生生效日最晚的一张单所对应的学校；无单视为“未登记” |
| 补报 | 本批后归属变为该校、本批前不是该校 |
| 撤回 | 本批前归属是该校、本批后不再是该校 |
| 归属未变 | 不列入任何名单 |

### 提交失败不产生任何副作用

`POST /api/batches/{batch_no}/commit` 在单个事务内完成取提交序号、校验、
写入学籍与复查依据。任一步失败即整体回滚：学籍不变、不生成依据、批次仍为
草稿，查询该批清单返回 `409 batch_not_committed`。

### 旧批清单稳定

提交成功时，系统为本批中 **归属确实会变化** 的学生冻结一份复查依据：

* 该学生在本批生效前的所有有效单据快照；
* 本批的单据快照。

`GET /api/batches/{batch_no}/corrections` 只读取这份冻结依据与本批的查询数据，
**不回放历史批次**。因此后续补录批次、或服务重启，都不会改变旧批清单的内容。

### 清单查询数据与读取条数上限

提交事务内一并写好两类查询数据（任一失败整体回滚，批次保留草稿、查不到清单）：

* `basis_student_schools`：本批归属会变化的学校索引，用于候选学生计数；
* `correction_segments`：**辅助记录**，按单据生效日边界保存每名学生相对某校
  “补报/撤回成立”的半开区间 `[start_date, end_date)`（末点为空表示一直成立）。
  归属只在生效日处变化，因此 **建数据时不按统计日逐日展开**。

清单查询先用复合索引上的日期条件（`start_date <= 统计日` 且
`end_date > 统计日或为空`）直接定位统计日命中的学生，**不会扫完该校候选再
回内存筛日期**；命中后每名学生只做两次 `LIMIT 1` 的索引取数（本批单、历史单
各至多 1 条）读取复查依据。读取条数有硬上限，超限时返回
`500 read_budget_exceeded`，不静默超量：

| 记录类型 | 读取上限 |
| --- | --- |
| 辅助记录 `correction_segments` | 基础最多 **64** 条；每命中一名学生再放宽 **4** 条 |
| 复查依据 `basis_records` | 每命中一名学生最多 **4** 条（实现上至多 2 条） |

清单响应分别报出两类记录的 **实际读取条数**：

* `auxiliary_records_read`：辅助记录实际读取条数（未命中时日期条件在索引内
  即被排除，通常为 0）；
* `basis_records_read`：复查依据实际读取条数（未命中学生不读取依据）。

`candidate_student_count` 是本批中归属与该校相关的学生数（走索引计数，不读
记录行），`unmatched_student_count` 是其中统计日未列入的人数。

### 旧版 SQLite 卷升级

旧版本卷没有 `correction_segments` 与依据覆盖索引。服务启动时自动、幂等地
按 **各批原先留存的冻结依据** 重建查询数据并补齐索引：不改动任何依据、学籍
与批次数据，旧名单口径不变，旧批同样满足上述读取上限；回填按批提交，中断后
重启可继续。

### 只核对本批学生、不逐日展开

依据只为“本批涉及的学生”建立；学校索引与辅助区间只为“归属会随日期变化”的
学校/学生建立。归属只在单据生效日处变化，因此判定变化时只检查前后两侧生效日
的并集，不按日展开。

## 快速启动

### Docker Compose

```bash
docker compose up --build -d
# 文档： http://127.0.0.1:8000/docs
```

SQLite 文件保存在命名卷 `student-status-data` 中，容器重建后数据仍在。
可通过 `API_PORT` 覆盖宿主端口。

### 本地运行

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements-dev.txt
DATABASE_URL="sqlite:///./data/student_status.db" \
  .venv/bin/uvicorn app.main:app --host 0.0.0.0 --port 8000
```

### 测试与试跑

```bash
.venv/bin/python -m pytest          # 单元/集成测试
./scripts/smoke.sh 8010             # 端到端：日期更正 + 真实重启后查旧批
```

## API 一览

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/api/batches` | 新建草稿批次 `{batch_no}` |
| GET | `/api/batches` | 列出批次（可 `?status=draft\|committed`） |
| GET | `/api/batches/{batch_no}` | 批次详情、明细与回执 |
| POST | `/api/batches/{batch_no}/slips` | 登记或更正转学单（按 `slip_no` upsert） |
| GET | `/api/batches/{batch_no}/slips` | 列出租学单 |
| DELETE | `/api/batches/{batch_no}/slips/{slip_no}` | 草稿内移除转学单 |
| POST | `/api/batches/{batch_no}/commit` | 整批生效，返回回执 |
| GET | `/api/batches/{batch_no}/corrections` | 查询补报/撤回名单 |
| GET | `/api/health` | 健康检查 |
| GET | `/api/stats` | 数据量统计 |

### 典型流程

```bash
BASE=http://127.0.0.1:8000/api

curl -X POST $BASE/batches -H 'content-type: application/json' \
  -d '{"batch_no":"B2026-001"}'

curl -X POST $BASE/batches/B2026-001/slips \
  -H 'content-type: application/json' \
  -d '{"slip_no":"Z-1001","student_no":"S-001",
       "school_id":"SCHOOL-A","effective_date":"2026-09-01"}'

curl -X POST $BASE/batches/B2026-001/commit

curl "$BASE/batches/B2026-001/corrections?school_id=SCHOOL-A&statistics_date=2026-09-30"
```

清单响应：

```json
{
  "batch_no": "B2026-001",
  "school_id": "SCHOOL-A",
  "statistics_date": "2026-09-30",
  "additions": [
    {"student_no": "S-001", "school_id": "SCHOOL-A", "slip_no": "Z-1001",
     "effective_date": "2026-09-01", "previous_school_id": null,
     "previous_slip_no": null}
  ],
  "withdrawals": [],
  "addition_count": 1,
  "withdrawal_count": 0,
  "candidate_student_count": 1,
  "listed_student_count": 1,
  "unmatched_student_count": 0,
  "auxiliary_records_read": 1,
  "basis_records_read": 1
}
```

## 错误格式

所有业务错误返回统一结构：

```json
{"error": {"code": "batch_not_committed", "message": "...", "details": {...}}}
```

| HTTP | code | 场景 |
| --- | --- | --- |
| 404 | `batch_not_found` / `slip_not_found` | 批次或转学单不存在 |
| 409 | `batch_exists` | 批号重复 |
| 409 | `batch_not_editable` | 已生效批次被修改 |
| 409 | `batch_empty` | 空批次提交 |
| 409 | `batch_not_committed` | 提交草稿批次或查询其清单 |
| 409 | `student_day_conflict` | 同一学生同日多单 |
| 409 | `data_conflict` | 其他数据库约束冲突 |
| 422 | `slip_student_immutable` | 试图改变单号绑定的学生 |
| 500 | `read_budget_exceeded` | 清单查询记录读取条数超过预算上限 |
| 503 | `database_busy` | 数据库繁忙 |

## 目录结构

```
app/
  main.py      应用工厂、生命周期、错误处理器
  routes.py    HTTP 接口
  service.py   批次、提交、依据冻结、清单计算
  models.py    SQLAlchemy 模型
  schemas.py   请求/响应模型
  db.py        引擎与会话
  config.py    环境配置
tests/         测试
scripts/
  smoke.sh     端到端试跑脚本
```
