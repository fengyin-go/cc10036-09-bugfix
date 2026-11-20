# 多维分析查询与指标平台

面向数据分析师和业务用户，提供多维聚合查询、指标口径、分群圈选和数据集市的数据分析平台

这是一个前后端分离的管理平台：前端 Vue 3 + Vite + TypeScript，后端 FastAPI（Python）。
两边各自独立启动，前端 dev server 已关掉自动打开页面，启动后按终端打印的地址手工打开。

## 目录结构

```text
.
├── frontend/                 Vue 3 + Vite + TypeScript 前端
│   ├── src/views/WorkbenchView.vue|tsx  全部模块共享工作台
│   ├── src/data/modules.ts              18个业务模块配置
│   ├── src/api/              统一请求封装
│   └── vite.config.ts        dev server 配置（open: false）
├── backend/                  FastAPI（Python） 后端
│   ├── app/routers/resource.py  通用查询/导出/登记/动作接口
│   ├── app/services/<key>.py    每个模块独立领域逻辑
│   ├── app/services/base.py     共享服务骨架
│   └── app/store.py          内存数据仓库与示例数据
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

### 前端

```bash
cd frontend
npm ci
npm run dev
```

前端默认监听 `http://127.0.0.1:5173/`，dev server 不会自动打开浏览器，
需要自己访问。`/api` 由 vite 代理到后端 `http://127.0.0.1:8000`。

## 业务模块

| 模块 | 目录 | 业务对象 | 主要字段 |
| --- | --- | --- | --- |
| 指标定义 | `metric_def` | 指标 | 指标编码、指标名称、业务域 |
| 多维立方 | `cube_builder` | OLAP立方 | 立方名称、事实表、维度列表 |
| 切片查询 | `slice_query` | 查询模板 | 模板名称、数据立方、查询维值 |
| 分群圈选 | `segment_rule` | 分群规则 | 分群名称、人群类型、筛选条件 |
| 看板布局 | `dashboard_layout` | 看板配置 | 看板名称、所属业务域、图表布局 |
| 即席导出 | `adhoc_export` | 导出任务 | 任务名称、查询SQL、导出格式 |
| 报表订阅 | `report_subscription` | 订阅配置 | 订阅名称、报表对象、推送周期 |
| 异常检测 | `anomaly_detect` | 检测规则 | 规则名称、监控指标、检测算法 |
| 下钻路径 | `drill_path` | 下钻配置 | 路径名称、起始维度、下钻层级 |
| 语义层 | `semantic_layer` | 语义定义 | 语义名称、物理表、字段映射 |
| 查询加速 | `query_accelerator` | 加速配置 | 配置名称、目标查询、加速方式 |
| 归因模型 | `attribution_model` | 归因配置 | 模型名称、归因对象、归因窗口 |
| 同期群分析 | `cohort_analysis` | 群组配置 | 群组名称、初始事件、回访事件 |
| 数据血缘 | `data_lineage` | 血缘节点 | 节点名称、节点类型、上游列表 |
| 查询调度 | `query_scheduler` | 调度计划 | 计划名称、查询语句、执行周期 |
| 结果缓存 | `result_cache` | 缓存条目 | 缓存键、查询描述、结果行数 |
| 数据范围 | `olap_scope` | 范围规则 | 规则名称、数据范围、授权对象 |
| 实验分析 | `ab_analysis` | 实验配置 | 实验名称、对照组、实验组 |

## 导出签收与订阅交付

即席导出的产物、队列和订阅交付是三个独立事实，靠 `backend/app/services/export_receipts.py`
里的签收账本对齐；`adhoc_export` 只负责产出与签收，`report_subscription` 只读取已签收的数据。

执行分两段：**开始导出**认领任务并写完产物（任务停在“执行中”、产物进入“文件已写完、待签收”），
**完成导出**才签收并释放队列。文件写在 `EXPORT_ARTIFACT_DIR`（缺省 `~/.cache/cc10036/exports`），
先写临时文件再原子替换，凭证里记录大小和 SHA-256，交付前逐字节校验。

| 状态 | 含义 | 是否排队 | 订阅可读取 |
| --- | --- | --- | --- |
| 待执行 | 已认领，还没有产物 | 执行租约 | 否 |
| 文件已写完 | 产物落盘，尚未签收 | 待签收档 | 否 |
| 已签收 | 状态与队列都已确认 | 否 | 是 |
| 需补跑 | 产物缺失或校验失败 | 否（等重新执行） | 否 |
| 已过期 | 清理过期，不再补跑 | 否 | 否 |

相关接口（`backend/app/routers/workflow.py`）：

- `GET /api/workflow/adhoc-export/{task_id}/detail`：产物、签收次数、待处理项、队列位置；
- `POST /api/workflow/adhoc-export/recover`：中断恢复，核对原签收、补签收、清理残留队列、列出需补跑项；
- `GET /api/workflow/adhoc-export/receipts`：导出签收数据，供订阅工作台读取；
- `GET /api/workflow/report-subscription/{id}/board`：工作台面板，本期签收次数与读取过的文件；
- `POST /api/workflow/report-subscription/deliver`：交付一次，只读已签收产物，同一份不重复消费；
- `POST /api/workflow/report-subscription/month-close`：月结，重复触发幂等。

前端入口：即席导出列表的「详情」进入 `/adhoc_export/:taskId/detail`，
报表订阅列表的「工作台」进入 `/report_subscription/workbench`。

测试：`cd backend && .venv/bin/python -m unittest discover -s tests -v`。

## 约定

- 全部模块共享`frontend/src/views/WorkbenchView.vue|tsx`和一套资源路由。
- 每个模块仍保留独立`backend/app/services/<模块>.py`；纯前端保留
  `frontend/src/domain/<模块>.ts`，用于承载真实业务差异并维持36题位容量。
- 列表接口统一返回 `{ items, total, page, size }`，动作接口统一返回 `{ ok, message }`。
- 状态流转只允许在 `app/services` 里改，路由层不做业务判断。

领域：数据工程
子方向：数据分析与数据应用
