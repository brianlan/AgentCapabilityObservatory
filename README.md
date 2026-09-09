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

## 执行：Harbor + 独立监督进程（#13）

FastAPI 进程只保存计划与状态；长运行由独立进程承担：

- **执行管理器**（本机单例，默认单槽）：`/ssd4/envs/aco_py312/bin/python -m aco.execution --data-root data --api-url http://127.0.0.1:8000`。从 SQLite 原子领取 `planned` Trial，先持久化启动意图（`trial_runs` 表），再 spawn 监督子进程； supervisor 死亡会留下启动意图与 `supervisor_lost` 诊断，并按运行标签清理容器。要求 API 服务已启动（通过 HTTP 领取 session token）。
- **监督进程**：`/ssd4/envs/aco_py312/bin/python -m aco.supervisor --run-id <id> --data-root data`（通常由管理器拉起，不建议手动运行）。用固定版本 Harbor 0.22.0（源 commit `71c39eafbd134d43ae3f489b5e6488b2a157de65`）运行 Trial：只使用公开接入点（`Trial.create`、`add_hook`、`import_path` agent、关闭 verifier、额外 compose 文件）；Harbor 自动评分永久禁用，其原始退出/日志只作诊断，ACO 不读取 Harbor reward 作为正式分数。
- **运行观测**：`GET /v1/trials/{trial_id}/runs` 返回启动意图（`requested_profile` 冻结不覆盖）、阶段事件、容器关联（含运行时安全摘要：非 privileged、无 docker socket、`network_mode: none`）、原始退出（`exit_kind`/`exit_detail`）与日志目录引用；未观测字段保持 `null`。
- **假 target**：V1 只执行 `harness: "fake"`（config 版本内容 `{"harness": "fake", "model": "none"}`）。`aco.fake_agent:FakeAgent` 仅供测试（领题 → 写普通文件 → 按指令场景提交/前台退出/后台写入），不代表真实 harness 接入；其他 harness 或模型/provider/skills/credentials 组合显式失败，不静默回退。

e2e 测试（需要本机 Docker）：`/ssd4/envs/aco_py312/bin/python -m pytest tests/e2e`。

## 结果查询、趋势与题目 × 配置矩阵（#19）

`GET /v1/results`（可选过滤：`task_set`、`config`、`scorer`、`batch`，均 `name@version`；`view=raw|unified`）在服务端完成过滤与聚合，dashboard `/dashboard/results` 只渲染其返回值，统计公式不进前端：

- **分母来自实验计划**（trials 表的计划重复数），成功评分行从不充当分母；异常封存、取消、待完成样本保留在分母中并分别计数（`counts.anomaly` / `cancelled` / `pending` / `conflict` / `score_error`）。
- **主分按题等权**：每题先算预定重复的通过率，再对题目等权平均——不是按 Trial 总数的简单合并平均。
- **缺失界限**：任一计划样本无有效判定时不标主分，输出固定权重下界（确认通过/计划）与"未知全通过"上界；界限是缺失界限，不是置信区间。
- **分线**：题组版本、target 配置、评分口径（scorer 版本）任一不同即不同序列，允许叠加、不自动混合；同题跨序列比较需显式过滤。
- **raw 与 unified**：raw 视图按实际产生判定的 scorer 版本分线（重评过的试验在两个 grader 下各出现一次）；unified 视图必须显式指定 `scorer`，只统计该重评口径。同版本成功判定互相冲突时该样本保持未知并单独计数，绝不自动挑选"最高分"。
- **时间轴**：每批次点以作答批次创建时间为横轴并给出实际起止范围；部分批次明确标"否（部分结果）"。
- **矩阵与下钻**：单 grader 口径下给出题目 × 配置矩阵（跨批次合并计数 + 缺失界限），趋势点/矩阵格/计数表链接到批次与试验详情页。
- **延迟/令牌/费用**：仅当 verifier submetrics 上报时按名称展示均值与样本数（来源：verifier submetrics），未上报显示缺失——不填零、不混入能力分。

测试（含手算分母、覆盖率与上下界的 fixture）：`/ssd4/envs/aco_py312/bin/python -m pytest tests/results tests/web`。
