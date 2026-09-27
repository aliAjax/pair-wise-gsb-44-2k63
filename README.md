# 个人数据权利请求处理系统

标准库实现的跨地区数据访问、更正、删除、撤回同意和限制处理请求后台，使用 SQLite 保存案件、数据位置、时限和审计时间线。

## 运行

要求 Python 3.11+（当前 Python 3.9 环境亦可）。

```bash
python3 app.py --init --seed
python3 app.py
```

默认地址 `http://127.0.0.1:8210`，数据库默认 `privacy_requests.db`。可用 `--db`、`--host`、`--port` 修改。

## 主要接口

使用 `X-User`、`X-Role` 请求头。角色有 `intake`、`privacy_officer`、`supervisor`、`auditor`。

- `GET /health`、`GET /api/state`、`GET /api/queue`、`GET /api/requests/{id}`
- `POST /api/jurisdictions`：配置处理时限、延期上限、未成年人和代理规则
- `POST /api/subjects`：保存不含明文联系方式的索引
- `POST /api/requests`：创建权利请求，支持幂等键和30天重复请求识别
- `POST /api/requests/verify`、`POST /api/requests/assign`
- `POST /api/locations`、`POST /api/locations/classify`：多系统定位和第三方/保留分类
- `POST /api/requests/extend`、`POST /api/requests/prepare`
- `POST /api/requests/fulfill`、`POST /api/requests/reject`

## 超期升级台

主管在页面（或接口）按地区配置**升级阈值** `escalation_threshold_days`（响应期限届满后的宽限天数，0 表示超期即升级）。打开升级台时系统幂等扫描：未结束且逾期天数超过地区阈值的请求自动生成升级单进入队列。

- `GET /api/escalations`：升级队列（自动执行超期扫描；主管/审计加 `?include_closed=1` 可看历史单）。返回当前逾期天数与状态标签。
- `GET /api/escalations/summary`：地区汇总（阈值、超期未结请求数、待处置/待复核/退回/活跃单数、最大逾期天数）。
- `GET /api/escalations/{id}`：升级单详情，含升级事件流与所属请求完整时间线。
- `POST /api/escalations/open`：手动对超阈值请求提交升级单（同一请求有未关闭单时 409，防重复）。
- `POST /api/escalations/disposition`：被指派处理人提交处置，必须同时提供 `disposition_note`、`responsible_party`、`planned_completion_date`（YYYY-MM-DD），缺一项即 400。
- `POST /api/escalations/review`：主管复核，`action=close|return` 且 `review_note` 必填；退回后处理人可重新提交，关闭后才移出活跃队列。提交与复核均带 `expected_version` 乐观锁。

升级单状态：`open`（待处置）→ `awaiting_review`（待复核）→ `returned`（已退回，可再提交）/ `closed`（已关闭）。升级开启、处置提交、退回、关闭同时写入 `escalation_events` 与请求审计时间线，全程可追溯。处理人只能看到指派给自己的升级单；关闭后的单不会因自动扫描重新开启，复发需手动提交新单。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

测试覆盖完整查阅请求、第三方遮蔽、未成年人/代理限制、重复与幂等、延期上限、删除法律保留、权限拒绝和版本冲突；`test_escalation.py` 覆盖升级阈值入队、防重复开单、处置必填与权限、退回/关闭生命周期、地区汇总、角色可见性、时间线追溯和旧库迁移。

## 局限

身份依赖请求头，联系方式只存哈希；请求正文、证据文件和实际回复文件未实现加密存储；地区规则是可配置模板，不构成法律意见；删除是流程判定，不会自动调用外部业务系统执行清除。
