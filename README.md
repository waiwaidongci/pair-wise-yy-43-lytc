# 溢油应急响应与任务追踪

围控、回收、岸线保护和废弃物处置任务，按证据和监测结果闭环。

## 模块结构

- `app.py`：参数解析、依赖组装和HTTP服务启动。
- `src/domain.py`：数据结构、错误、状态和基础校验。
- `src/rules.py`：状态机、角色矩阵、优先级、期限和关闭不变量。
- `src/repository.py`：SQLite建表、事务、版本控制和审计链。
- `src/service.py`：权限检查、用例编排、并发控制和审计。
- `src/http_api.py`：JSON路由和统一错误响应。
- `src/audit.py`：UTC时间和SHA-256审计事件。
- `static/index.html`：最小演示页。
- `tests/`：完整流程、规则和失败测试。

## 初始化与启动

```bash
python3 app.py --db ./data.db --port 8320
```

默认端口为`8320`，首次启动自动建库。使用`X-Actor`和`X-Role`请求头传递身份。

## 主要接口

- `GET /health`
- `GET /api/items`
- `POST /api/items`
- `GET /api/items/{id}`，详情带`lightering`转驳进度
- `POST /api/items/{id}/records`
- `POST /api/items/{id}/transition`，必须提交`expected_version`
- `GET /api/items/{id}/tanks`、`POST /api/items/{id}/tanks`
- `POST /api/items/{id}/tanks/{tank_id}/plan`，修订舱容计划
- `GET /api/items/{id}/transfers`、`POST /api/items/{id}/transfers`
- `POST /api/items/{id}/transfers/{tid}/recheck`，待处理作业重新核对
- `POST /api/items/{id}/transfers/{tid}/quantities`，录入船端/岸端计量
- `POST /api/items/{id}/transfers/{tid}/confirm`，补计量单并由另一人确认
- `GET /api/audit`

允许角色：observer, response_commander, operations, viewer。估算油量、海况和未完成任务数影响响应等级；关闭前必须完成回收和岸线监测记录。

## 舱级转驳

沉船抽油按舱级转驳作业记账，哪艘船从哪个舱抽走多少、岸罐收到多少逐笔对账，事件详情带`lightering`进度（分舱剩余舱容、分船汇总、船岸总量差）。

- 沉船舱同时只接一项未结作业（pending/open/review），冲突返回409并列出缺口。
- 开工前核对剩余舱容、接地电阻（≤10Ω）和风速（≤15m/s），不符则作业停在`pending`（待处理）并在`failed_checks`中说明哪项，整改后走`recheck`。
- 船端交油与岸端接收相差超过2%时作业转`review`（待核对），须补齐计量单并由另一人确认；容差内直接`confirmed`。
- 修订舱容计划后原确认作废（`void`），需按新计划重新确认。
- 存在未结差异（review/void）时事件不能从围控转入回收，接口返回409并在`gaps`中列出缺口明细。
- 转驳相关审计事件（tank_create、transfer_create、transfer_recheck、transfer_quantities、transfer_confirm、tank_plan_revise）均带进度快照。

## 测试

```bash
python3 -m unittest discover -s tests -v
```
