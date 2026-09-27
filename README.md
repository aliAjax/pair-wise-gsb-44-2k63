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
- `POST /api/jurisdictions`：配置处理时限、延期上限、未成年人和代理规则，以及 `escalation_threshold_days` 超期升级阈值（逾期多少天后进入升级队列，默认 0）
- `POST /api/subjects`：保存不含明文联系方式的索引
- `POST /api/requests`：创建权利请求，支持幂等键和30天重复请求识别
- `POST /api/requests/verify`、`POST /api/requests/assign`
- `POST /api/locations`、`POST /api/locations/classify`：多系统定位和第三方/保留分类
- `POST /api/requests/extend`、`POST /api/requests/prepare`
- `POST /api/requests/fulfill`、`POST /api/requests/reject`
- `GET /api/escalations`：超期升级台，返回未关闭升级队列（含逾期天数）和地区汇总
- `POST /api/escalations/submit`：被指派的处理人提交处置说明、责任人和计划完成日，缺一不可
- `POST /api/escalations/close`：主管填写复核意见后关闭，升级单移出队列

## 超期升级台

未结束的请求超过地区响应期限并达到升级阈值后，自动进入升级队列（读取队列或状态时扫描开立）。同一请求最多存在一张未关闭升级单；处理人提交处置后由主管复核关闭才移出队列。关闭后请求仍未办结且超过计划完成日的，会重新开立升级单。升级开立、处置提交、复核关闭均写入请求时间线，页面展示地区汇总（待处置/待复核/已关闭/最大逾期天数）与每张升级单的逾期天数。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

测试覆盖完整查阅请求、第三方遮蔽、未成年人/代理限制、重复与幂等、延期上限、删除法律保留、权限拒绝和版本冲突，以及超期升级阈值、处置提交校验、重复提交拦截、复核关闭、重新升级和地区汇总。

## 局限

身份依赖请求头，联系方式只存哈希；请求正文、证据文件和实际回复文件未实现加密存储；地区规则是可配置模板，不构成法律意见；删除是流程判定，不会自动调用外部业务系统执行清除。
