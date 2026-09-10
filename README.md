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
# 两个 ASGI 监听（ADR 0001）：管理面需要服务端校验的 bearer 凭证，绝不暴露给被测容器
ACO_MANAGEMENT_TOKEN=<管理凭证> /ssd4/envs/aco_py312/bin/python -m uvicorn aco.app:management_app --port 8000
/ssd4/envs/aco_py312/bin/python -m uvicorn aco.app:session_app --port 8001
# 打开 http://127.0.0.1:8000/openapi.json 查看运行时生成的 OpenAPI
```

- 数据根目录：默认 `./data`，可用环境变量 `ACO_DATA_ROOT` 覆盖；SQLite 数据库位于 `<data-root>/aco.db`，是唯一元数据权威。
- Migration：`src/aco/migrations/` 内有序 SQL 文件 + `schema_version` 表，重复执行是 no-op。
- 端点：`POST /v1/versions`（登记不可变版本，内容寻址 digest，重复登记幂等，同标识不同内容返回 `409`）、`POST /v1/experiments`（返回 `202` 与原子展开的 Trial 计划）、`GET /v1/experiments/{id}`、`GET /v1/trials/{id}`。管理面所有路由（含 dashboard）都要求 `Authorization: Bearer $ACO_MANAGEMENT_TOKEN`，缺失或错误返回 `401`；未设置该环境变量时管理面拒绝启动。
- Experiment 创建幂等（#35，服务端权威）：`POST /v1/experiments` 支持携带 `Idempotency-Key` 头。同一管理主体（bearer token 的 sha256 指纹，明文永不入库）用相同 key 与相同规范请求体重试，返回原 Experiment（`200`）；相同 key 但请求体不同返回 `409 idempotency_conflict`。幂等键与计划在同一 SQLite 事务中登记，失败不残留 key 或半个计划。不携带该头时每次调用都创建新 Experiment——API 不会把无 key 请求伪装成幂等。
- 凭证只允许逻辑引用（config 的 `credentials` 名称列表），任何凭证值都不会入库；Trial 的 `runtime_observation` 在未观测前保持 `null`。

## 受限 Session API（#12）

被测会话只能到达 Session 监听（网络部署上管理监听绝不暴露给被测容器）。启动端为每个可运行 Trial 调 `POST /v1/trials/{trial_id}/session-token`（管理面）换取短期（默认 24h）bearer token，明文只返回一次，服务端只存 sha256 摘要；已取消或已有封存/异常答案的 Trial 拒绝铸造（`409 trial_not_runnable`）。

会话端点（均需 `Authorization: Bearer <token>`，令牌绑定单个 Trial）：

- `GET /v1/session/task`：领取绑定 Trial 的公开任务（只含 task 引用、`prompt` 指令与状态；不含隐藏测试/答案）。首次成功领取原子记录 `opened_at` 计时起点，重复领取不重置。
- `POST /v1/session/submit`：提交唯一结束意图，请求体只含 `idempotency_key`（携带 `answer` 等额外字段被 schema 拒绝 `422`——正式答案是监督进程封存的 workspace 快照，永远不由会话提交）。返回 `202` 与稳定 `receipt_id`（仅代表意图已接受，未封存未评分）。幂等规则持久化在 SQLite：相同 key 重试返回相同 receipt（`200`）；换 key 不能产生第二次交卷（`409 already_submitted`，数据库约束一 Trial 一意图）。
- `GET /v1/session/submission`：查询结束意图状态（`accepted` / `sealing` / `error`）。

Trial 结束（取消或答案封存/异常）即会话能力失效：旧令牌的一切会话请求返回 `401 session_expired`，不能产生迟到交卷；封存结果只能经管理面观测。

日志与错误响应不包含令牌明文、答案内容或隐藏测试信息。

## 执行：Harbor + 独立监督进程（#13）

FastAPI 进程只保存计划与状态；长运行由独立进程承担：

- **执行管理器**（本机单例，默认单槽）：`/ssd4/envs/aco_py312/bin/python -m aco.execution --data-root data --api-url http://127.0.0.1:8000 --session-api-url http://127.0.0.1:8001`（管理凭证取环境变量 `ACO_MANAGEMENT_TOKEN`，或用 `--api-token` 传入）。从 SQLite 原子领取 `planned` Trial，经管理面铸造 session token，先持久化启动意图（`trial_runs` 表），再 spawn 监督子进程并只下发 Session 监听地址； supervisor 死亡会留下启动意图与 `supervisor_lost` 诊断，并按运行标签清理容器。要求两个监听均已启动。
- **监督进程**：`/ssd4/envs/aco_py312/bin/python -m aco.supervisor --run-id <id> --data-root data`（通常由管理器拉起，不建议手动运行）。用固定版本 Harbor 0.22.0（源 commit `71c39eafbd134d43ae3f489b5e6488b2a157de65`）运行 Trial：只使用公开接入点（`Trial.create`、`add_hook`、`import_path` agent、关闭 verifier、额外 compose 文件）；Harbor 自动评分永久禁用，其原始退出/日志只作诊断，ACO 不读取 Harbor reward 作为正式分数。
- **运行观测**：`GET /v1/trials/{trial_id}/runs` 返回启动意图（`requested_profile` 冻结不覆盖）、阶段事件、容器关联（含运行时安全摘要：非 privileged、无 docker socket、`network_mode: none`）、原始退出（`exit_kind`/`exit_detail`）与日志目录引用；未观测字段保持 `null`。
- **假 target**：V1 只执行 `harness: "fake"`（config 版本内容 `{"harness": "fake", "model": "none"}`）。`aco.fake_agent:FakeAgent` 仅供测试（领题 → 写普通文件 → 按指令场景提交/前台退出/后台写入），不代表真实 harness 接入；其他 harness 或模型/provider/skills/credentials 组合显式失败，不静默回退。

e2e 测试（需要本机 Docker）：`/ssd4/envs/aco_py312/bin/python -m pytest tests/e2e`。

## API 驱动评测 CLI（#17）

`aco` 命令是统一管理 API 的薄客户端（stdlib `argparse` + `urllib`，无 CLI 框架；不直接访问数据库或执行引擎）。所有输出来自 API；关闭或 Ctrl-C CLI 绝不取消服务端运行，只有显式 `aco cancel` 会取消。

```bash
# 安装后使用（console script）
/ssd4/envs/aco_py312/bin/python -m pip install -e . && aco run --api-url http://127.0.0.1:8000 --help
# 免安装运行
/ssd4/envs/aco_py312/bin/python -m aco.cli --help   # 或 PYTHONPATH=src python -m aco.cli

# 登记版本（内容为 JSON 文件）
aco register task demo-task v1 task.json
aco register config demo-cfg v1 config.json

# 创建批次：立即返回 Experiment ID，服务端异步执行
aco run --task demo-task@v1 --target demo-cfg@v1 --repetitions 3
aco run --suite my-suite@v1 --target demo-cfg@v1 --idempotency-key run-2026-09-10

# 查看进度（Ctrl-C --wait 只停止本地等待，批次继续运行）
aco status <experiment-id>
aco run --task demo-task@v1 --target demo-cfg@v1 --wait

# 显式生命周期（#16 语义：取消幂等；已取消批次 resume 返回明确错误）
aco cancel <experiment-id>
aco resume <experiment-id>
```

- 连接配置：`--api-url` / 环境变量 `ACO_API_URL`（默认 `http://127.0.0.1:8000`）；`--token` / 环境变量 `ACO_MANAGEMENT_TOKEN` 以 bearer 头发送（服务端校验，缺失返回 `401`），任何输出与错误信息都不包含凭证。
- 脚本使用：任意命令加 `--json` 得到稳定 JSON（stdout 仅含 JSON，创建前估算输出走 stderr）；API/HTTP 错误返回非零退出码（Ctrl-C 中断 `--wait` 返回 `130`）。
- 幂等键：`--idempotency-key` 同时发送给服务端与本地 ledger（`ACO_CLI_STATE`，默认 `~/.config/aco/cli.json`）。服务端是幂等权威（#35）：相同 key 重试返回既有批次（即使本地 ledger 丢失）；相同 key 但参数不同返回 `409`。本地 ledger 只是便利缓存，同键重复 `run` 命中缓存时直接查询既有批次。

## 结果查询、趋势与题目 × 配置矩阵（#19）

`GET /v1/results`（可选过滤：`task_set`、`config`、`scorer`、`batch`，均 `name@version`；`view=raw|unified`）在服务端完成过滤与聚合，dashboard `/dashboard/results` 只渲染其返回值，统计公式不进前端：

- **分母来自实验计划**（trials 表的计划重复数），成功评分行从不充当分母；异常封存、取消、待完成样本保留在分母中并分别计数（`counts.anomaly` / `cancelled` / `pending` / `score_error`）。
- **主分按题等权**：每题先算预定重复的通过率，再对题目等权平均——不是按 Trial 总数的简单合并平均。
- **缺失界限**：任一计划样本无有效判定时不标主分，输出固定权重下界（确认通过/计划）与"未知全通过"上界；界限是缺失界限，不是置信区间。
- **分线**：题组版本、target 配置、评分口径（scorer 版本）任一不同即不同序列，允许叠加、不自动混合；同题跨序列比较需显式过滤。
- **raw 与 unified**：raw 视图按实际产生判定的 scorer 版本分线（重评过的试验在两个 grader 下各出现一次）；unified 视图必须显式指定 `scorer`，只统计该重评口径。判定解析取每 (trial, scorer 版本) 的最新追加记录（`created_at` + 插入顺序）：重评/重试取代旧记录，旧记录绝不影响结果——不自动挑选"最高分"，而是按时间取当前判定。
- **时间轴**：每批次点以作答批次创建时间为横轴并给出实际起止范围；部分批次明确标"否（部分结果）"。
- **矩阵与下钻**：单 grader 口径下给出题目 × 配置矩阵（跨批次合并计数 + 缺失界限），趋势点/矩阵格/计数表链接到批次与试验详情页。
- **延迟/令牌/费用**：仅当 verifier submetrics 上报时按名称展示均值与样本数（来源：verifier submetrics），未上报显示缺失——不填零、不混入能力分。

测试（含手算分母、覆盖率与上下界的 fixture）：`/ssd4/envs/aco_py312/bin/python -m pytest tests/results tests/web`。

## 独立评分与封存（#14、#15）

正式答案是 ACO 自己经 pause/copy 冻结的 workspace 快照（Harbor 的事后收集仅作诊断）；评分由独立 verifier 容器执行（`--network none --read-only`，封存答案与 verifier bundle 均只读挂载，唯一可写是全新输出目录），每次执行追加一条 `verifications` 记录，错误分类记录、绝不写成 `pass=false`。verifier bundle 以 digest 固定（登记时声明，执行前校验）。

**任务声明的 ArtifactContract（#14 reopen）**：TaskVersion 的不可变内容必须携带 `contract`（`required_outputs`，可选 `allowed_paths`/`max_total_bytes`，未知字段拒绝）及其 `contract_digest`。监督进程在 agent 启动前解析一次并校验 digest——缺失、digest 不符或 schema 无效直接 `contract_invalid` 失败，绝不静默使用默认值；同一 contract 实例贯穿 baseline、正式采集、校验、manifest 与重启恢复（恢复时校验不过按执行条件异常处理，不发布）。必产出物缺失时封存失败并留下异常状态。

## 任务准入（#20）

题目 bundle 在登记为稳定候选前必须通过准入工具全部门槛（复用正式封存与评分入口，非模拟）：

```bash
/ssd4/envs/aco_py312/bin/python -m aco.admission admit <bundle-dir> --data-root data
```

bundle 布局（固定单一布局）：

```
<bundle>/
  task.toml                       # schema/name/version/image/verifier/contract/provenance
  public/environment/workspace/   # agent 可见的初始 workspace
  private/verifier/               # 可信 verifier bundle（run.py + config.json）
  private/reference/workspace/    # 参考答案（workspace 覆盖层）
  private/wrong_answers/<case>/workspace/   # 声明的错误答案（作弊用例）
```

门槛：静态检查（manifest 完整性、provenance、无符号链接/特殊文件）、泄漏扫描（agent 可见 bundle 与镜像层均不得含隐藏资产内容）、Oracle（参考答案新环境连续 3 次通过且封存内容一致——三次是起步检查，不是确定性证明）、NOP（未修改初始 workspace 必须 0/3）、错误答案全部不得通过、同一封存答案重复评分一致。任何门槛失败即退出码非零，且不登记稳定候选版本。

报告（JSON + Markdown，含每项证据与 task/verifier/environment/contract digest 及 provenance）写入 `<data-root>/admission/`。全部通过后，工具把可信 verifier bundle 导入 `<data-root>/verifiers/<scorer-id>/`（即正式评分路径 `verification.runner` 的加载位置，导入后校验 digest），并经正常 registry 登记 task + scorer 版本（数据库只保存版本、digest、provenance 与报告引用，未新增表）；导入或登记失败不会留下"看似可用"的候选。晋级 Core 必须人工审阅：

```bash
/ssd4/envs/aco_py312/bin/python -m aco.admission promote <report.json> --to core --reviewed-by <人工审阅人>
```

公开仓库只包含 `tests/fixtures/tasks/synthetic-add` 合成 fixture（仅用于验证准入工具本身）；私有题目内容（说明、隐藏测试、参考答案）一律放在私有存储，通过本工具在本地准入，不在公开仓库出现。
