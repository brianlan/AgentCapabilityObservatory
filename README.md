# Agent Capability Observatory

追踪固定 agent 配置随时间的任务完成表现，支持不同 harness、模型和 provider 的比较。

V1 架构规划已完成，尚未进入生产实现。GitHub 的 [ACO V1 架构蓝图：决策地图](https://github.com/brianlan/AgentCapabilityObservatory/issues/1)保存完整决策索引；各票的最终 resolution 是权威决定，早期讨论保留为历史。

实现者从 [最小架构、API 契约与交接顺序](https://github.com/brianlan/AgentCapabilityObservatory/issues/8#issuecomment-5578594458)开始阅读，再按需查看：

- [执行环境与独立评分边界](https://github.com/brianlan/AgentCapabilityObservatory/issues/5#issuecomment-5578394410)
- [长期曲线、分母与历史重评](https://github.com/brianlan/AgentCapabilityObservatory/issues/6#issuecomment-5578488592)
- [首批题目与评分准入](https://github.com/brianlan/AgentCapabilityObservatory/issues/7#issuecomment-5578525077)
- [冻结与收卷原型证据](https://github.com/brianlan/AgentCapabilityObservatory/blob/3124a7b/prototypes/harbor-freeze/README.md)

领域术语见 [CONTEXT.md](CONTEXT.md)。项目 Python 环境为 `/ssd4/envs/aco_py312/bin/python`。

研究和原型分支提供选型证据；真实 harness/provider、skills 加载、网络与评分隔离、完整崩溃恢复仍需实现验收。

## 本地运行（Registry 与评测计划 API，#11）

要求 Python 3.12（项目环境：`/ssd4/envs/aco_py312/bin/python`）。

```bash
# 安装（可选；直接跑测试可免安装，pytest 配置了 pythonpath=src）
/ssd4/envs/aco_py312/bin/python -m pip install -e '.[test]'

# 运行测试
/ssd4/envs/aco_py312/bin/python -m pytest

# 启动 API（首次启动自动从零执行 SQLite migration）
/ssd4/envs/aco_py312/bin/python -m uvicorn aco.app:app
# 打开 http://127.0.0.1:8000/openapi.json 查看运行时生成的 OpenAPI
```

- 数据根目录：默认 `./data`，可用环境变量 `ACO_DATA_ROOT` 覆盖；SQLite 数据库位于 `<data-root>/aco.db`，是唯一元数据权威。
- Migration：`src/aco/migrations/` 内有序 SQL 文件 + `schema_version` 表，重复执行是 no-op。
- 端点：`POST /v1/versions`（登记不可变版本，内容寻址 digest，重复登记幂等，同标识不同内容返回 `409`）、`POST /v1/experiments`（返回 `202` 与原子展开的 Trial 计划）、`GET /v1/experiments/{id}`、`GET /v1/trials/{id}`。
- 凭证只允许逻辑引用（config 的 `credentials` 名称列表），任何凭证值都不会入库；Trial 的 `runtime_observation` 在未观测前保持 `null`。

## 受限 Session API（#12）

被测会话不使用管理 API。启动端为每个 Trial 调 `POST /v1/trials/{trial_id}/session-token` 换取短期（默认 24h）bearer token，明文只返回一次，服务端只存 sha256 摘要。

会话端点（均需 `Authorization: Bearer <token>`，令牌绑定单个 Trial）：

- `GET /v1/session/task`：领取绑定 Trial 的公开任务（只含 task 引用、`prompt` 指令与状态；不含隐藏测试/答案）。首次成功领取原子记录 `opened_at` 计时起点，重复领取不重置。
- `POST /v1/session/submit`：提交唯一结束意图，返回 `202` 与稳定 `receipt_id`（仅代表意图已接受，未封存未评分）。幂等规则持久化在 SQLite：相同 key + 相同内容重试返回相同 receipt（`200`）；相同 key 不同内容 `409 idempotency_conflict`；换 key 不能产生第二次交卷（`409 already_submitted`，数据库约束一 Trial 一意图）。
- `GET /v1/session/submission`：查询结束意图状态（`accepted` / `sealing` / `sealed` / `error`，V1 只有 `accepted`）。

日志与错误响应不包含令牌明文、答案内容或隐藏测试信息。
