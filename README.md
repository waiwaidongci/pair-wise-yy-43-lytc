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
- `GET /api/items/{id}`
- `POST /api/items/{id}/records`
- `POST /api/items/{id}/transition`，必须提交`expected_version`
- `GET /api/audit`

允许角色：observer, response_commander, operations, viewer。估算油量、海况和未完成任务数影响响应等级；关闭前必须完成回收和岸线监测记录。

## 舱级转驳（沉船抽油，舱级计量）

解决"只有总回油量、不知道哪艘船哪个舱抽走多少、岸罐收到多少"的问题。实体层级：沉船`ships`→沉船舱`wreck_tanks`（带舱容计划版本）；岸罐`shore_tanks`；转驳作业`transfers`。

- **一舱一未结作业**：一个沉船舱同时只能有一项未结作业，由部分唯一索引在数据库层兜底；已确认（未作废）才算结项。
- **开工前三检**（`POST .../transfers/{id}/precheck`）：核对岸罐剩余舱容（≥计划量）、接地电阻（≤100Ω）、风速（≤10.8m/s）。任一不符，作业转`held`停待处理，响应逐项列出`failed_items`（remaining_capacity/grounding_resistance/wind_speed）及中文说明，不产生错误状态码。
- **计量与2%差异**（`POST .../measure`）：登记船端交油量、岸端接收量和双方计量单；相差超过2%时作业自动转`reconciling`待核对，须补齐核对说明/计量单。
- **另一人确认**（`POST .../confirm`）：确认人不得是计量记录人；缺计量单、未核对完成或缺核对说明均以冲突拒绝。
- **舱容计划修订**（`POST .../tanks/{id}/plan`）：版本号+1，同舱历史确认全部标记`confirmation_voided`作废；同舱存在在途未结作业时修订整体回滚。
- **围控→回收闸门**：存在未结/差异未结的舱级作业时，`containing→recovering`返回`409 Conflict`，响应体`gaps`逐项列出缺口（作业标识、缺口代码、差异比例和说明）。
- **进度**：作业详情、`GET /api/items/{id}/progress`、`/fleet`以及全部转驳审计事件均带出按船/舱/岸罐汇总的进度；转入回收的审计事件同样附进度快照。

转驳接口（均挂在`/api/items/{id}`下）：

- `POST/GET /ships`、`POST /ships/{ship_id}/tanks`
- `POST/GET /shore-tanks`
- `POST/GET /transfers`、`GET /transfers/{transfer_id}`
- `POST /transfers/{transfer_id}/precheck|measure|confirm`
- `POST /tanks/{tank_id}/plan`、`GET /progress`、`GET /fleet`

转驳操作要求事件处于`containing`或`recovering`。角色：建实体/开工/计量为`operations`（沉船/舱登记也允许 observer、response_commander），修订舱容与确认为`response_commander`。

## 测试

```bash
python3 -m unittest discover -s tests -v
```
