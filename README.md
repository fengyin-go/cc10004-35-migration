# 数据洞察分析工作台

面向多源数据接入、交互式查询分析、可视化看板搭建、分析报告生成与数据服务编排的数据洞察分析工作台。

这是一个前后端分离的管理平台：前端 Vue 3 + Vite + TypeScript，后端 FastAPI（Python）。
两边各自独立启动，前端 dev server 已关掉自动打开页面，启动后按终端打印的地址手工打开。

## 目录结构

```text
.
├── frontend/                 Vue 3 + Vite + TypeScript 前端
│   ├── src/views/            每个业务模块一个页面
│   ├── src/api/              统一请求封装
│   ├── src/stores/           会话与筛选状态
│   └── vite.config.ts        dev server 配置（open: false）
├── backend/                  FastAPI（Python） 后端
│   ├── app/routers/          每个业务模块一组接口
│   ├── app/services/         业务规则与状态流转
│   ├── app/db.py             SQLite 持久层（模板、制品元数据、领取与投递记录）
│   ├── app/services/artifacts.py  制品落盘、提交顺序与重启恢复
│   └── app/store.py          其余模块的内存数据仓库与示例数据
├── .gitignore
└── docker-compose.yml
```

## 启动

### 后端

```bash
cd backend
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
./run.sh
```

健康检查：`curl http://127.0.0.1:8000/api/health`

SQLite 与制品默认落在 `backend/data/`（可用 `SQLITE_PATH` /
`ARTIFACT_STORAGE_DIR` 覆盖）。启动时会自动建表、迁移存量模板并收敛上次中断的制品写入。

### 前端

```bash
cd frontend
npm install
npm run dev
```

前端默认监听 `http://127.0.0.1:5173/`，dev server 不会自动打开浏览器，
需要自己访问。`/api` 由 vite 代理到后端 `http://127.0.0.1:8000`。

## 业务模块

| 模块 | 目录 | 业务对象 | 主要字段 |
| --- | --- | --- | --- |
| 交互查询 | `query` | 查询语句 | 语句编号、查询表达式、目标数据集 |
| 可视化图表 | `chart` | 图表配置 | 图表编号、图表名称、图表类型 |
| 数据源接入 | `source` | 接入配置 | 配置编号、连接类型、接入地址 |
| 聚合计算 | `aggregate` | 聚合方案 | 方案编号、聚合函数、目标字段 |
| 数据筛选 | `filter` | 筛选规则 | 规则编号、目标数据集、筛选字段 |
| 报告生成 | `reportgen` | 报告模板 | 模板编号、模板名称、包含图表 |
| 异常检测 | `anomaly` | 检测规则 | 规则编号、监控指标、基线均值 |
| 多维下钻 | `drill` | 下钻路径 | 路径编号、起点维度、下钻层数 |
| 对比分析 | `compare` | 对比方案 | 方案编号、基准数据集、对照数据集 |
| 分析流水线 | `pipeline` | 流水线节点 | 节点编号、节点类型、输入源 |
| 数据集管理 | `datastore` | 数据集定义 | 数据集编号、数据集名称、来源表名 |
| 趋势预测 | `forecast` | 预测模型 | 模型编号、模型类型、训练数据集 |
| 数据分片 | `segment` | 分片规则 | 规则编号、目标数据集、分片字段 |
| 调度管理 | `scheduler` | 调度配置 | 配置编号、关联任务、调度表达式 |
| 通知分发 | `notify` | 通知规则 | 规则编号、触发条件、通知渠道 |
| 查询缓存 | `cache` | 缓存策略 | 策略编号、目标查询、缓存键规则 |
| 数据标签 | `tag` | 标签定义 | 标签编号、标签名称、计算表达式 |
| 数据分享 | `share` | 分享链接 | 链接编号、目标数据集、访问密码 |

## 约定

- 每个模块的前端页面在 `frontend/src/views/<模块>/index.vue`，后端接口在
  `backend/app/routers/<模块>.py`，业务规则在 `backend/app/services/<模块>.py`。
- 列表接口统一返回 `{ items, total, page, size }`，动作接口统一返回 `{ ok, message }`。
- 状态流转只允许在 `app/services` 里改，路由层不做业务判断。

## 报告制品：持久化与恢复

报告模板定义、制品元数据、领取记录与通知投递记录都在 SQLite；制品正文在可配置的
持久卷（`ARTIFACT_STORAGE_DIR`）。两者分开存放，备份时一起带走即可。

接口（模板接口路径与返回结构保持不变）：

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/api/reportgen/{模板id}/artifacts` | 生成并确认制品，必须带 `idempotency_key` |
| GET | `/api/reportgen/artifacts` | 制品列表，缺文件/损坏的会标记为不可投递 |
| GET | `/api/reportgen/artifacts/{artifact_id}` | 单条制品元数据 |
| GET | `/api/reportgen/artifacts/{artifact_id}/download` | 下载已确认制品 |
| POST | `/api/reportgen/artifacts/{artifact_id}/claim` | 领取已确认制品 |
| POST | `/api/reportgen/artifacts/reconcile` | 手动触发恢复判定与孤立文件隔离 |
| POST | `/api/notify/deliveries` | 按制品身份投递通知 |
| GET | `/api/notify/deliveries` | 查询投递记录 |

一致性策略：

- 提交顺序是「预留元数据 → 写临时文件并 fsync → 标记 staged → 原子改名 → 校验后标记 ready」。
  只有 `ready` 且摘要/大小都校验通过的制品才出现在 `download_url` 里。
- 进程在写文件与写元数据之间退出：重启时按元数据里的 sha256/size 校验临时文件，
  完整则补完提交，不完整则删除并回滚为 `failed`，不会留下可下载的半成品。
- `ready` 但文件缺失 → `missing`；摘要不符 → 文件移入 `_quarantine` 并转 `corrupt`。
  两者都不会显示为可投递，下载、领取、通知投递一律拒绝。
- 没有元数据引用的文件只会被移入隔离区，绝不会自动生成制品记录或公开下载。
- 同一模板 + 同一确认请求号只受理一次；重放返回第一次的制品身份（`replayed: true`）。
- 存量模板迁移保留原 id 与模板编号，只迁移模板本身，不伪造任何生成历史。
- 通知投递的下载地址只使用 `/api/reportgen/artifacts/{artifact_id}/download`，
  模板名称/编号只作为文案，绝不拼进路径。

容器部署：

```bash
docker compose up --build
```

`report-data` 命名卷同时挂载 SQLite 与制品目录；重建容器或重启进程后，已确认的
制品仍然可以按 `artifact_id` 领取。后端健康检查会返回存储状态与各状态制品数量。
如果同一卷挂到多个后端副本，启动恢复判定用文件锁串行，后启动的副本会跳过协调。
可用 `RECONCILE_ON_STARTUP=0` 暂时关闭启动时的自动恢复判定，再通过
`POST /api/reportgen/artifacts/reconcile` 手动触发。

## 测试

```bash
cd backend
.venv/bin/pip install pytest httpx
.venv/bin/python -m pytest tests -q
```

测试覆盖迁移、接口兼容、幂等、重启恢复、半成品回滚、缺文件降级、孤立文件隔离、
按制品身份投递与路径穿越拒绝。

领域：数据工程
子方向：数据分析与数据应用
