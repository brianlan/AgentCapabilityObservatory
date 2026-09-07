# Harbor 环境与客观评分复用调查

研究日期：2026-09-07。对应[研究：Harbor 与客观评分框架能复用哪些环境和验证能力](https://github.com/brianlan/AgentCapabilityObservatory/issues/3)。本报告提供事实与候选路线，不替代后续产品决策。

边界：用户手动打开选好模型的全新 Codex / OpenCode / Pi interactive session，由该会话通过 API 领题、作答、交卷。初期为单用户可信执行机器；平台独立评分。不能以 Harbor 自动启动另一个 agent 替代这个要求。

## 结论

Harbor 的任务结构和独立评分流程值得复用评估，但“必须整体采用 Harbor”没有得到证据支持。默认隔离与原聊天记录不符；已启动 interactive session 接入不是本次查到的现成 CLI 能力。可以先保留 ACO 的领题、交卷边界，再比较 Harbor 评分适配和直接运行评分容器的成本。

还有实际兼容限制：本次 Harbor commit 的 `requires-python = ">=3.12"`，不能直接安装在用户指定的 `/home/rlan/anaconda3/envs/mykik_py311` 中。若采用，可将 Harbor 作为独立进程/容器运行，ACO 继续使用 py311；这只是候选部署边界，本次没有安装或升级环境。[pyproject.toml](https://github.com/harbor-framework/harbor/blob/71c39eafbd134d43ae3f489b5e6488b2a157de65/pyproject.toml#L9)

## 已核实的执行边界

| 问题 | 事实及其含义 |
| --- | --- |
| 默认 verifier 是否独立？ | 默认 `shared`，在 agent 同一容器执行。设置 `environment_mode = "separate"` 或提供 `[verifier.environment]` 才启用独立环境。不能把 Harbor 的存在视作独立评分保证。 |
| separate 如何收卷？ | 将 `/logs/artifacts/` 与显式声明 artifacts 传到 verifier 的对应路径；不是自动复制整个工作区。评分镜像须自带 `/tests/test.sh`，独立模式不会再上传 tests。代码修复题必须先定义如何交付新增文件、删除和二进制等结果。 |
| runner 是否看不到隐藏测试？ | 独立容器隔开的是 agent 环境和评分环境；运行 Harbor 的主机仍持有 task、tests、镜像。不能推导“整个 runner 不接触 verifier”。ACO 若要求本地解题机器拿不到评分材料，需要把评分部署在另一个权限边界。 |

前两项见[官方 Task Structure](https://www.harborframework.com/docs/tasks#verifier-environment-shared-vs-separate)及固定源码 [`Trial._run_separate_verifier`](https://github.com/harbor-framework/harbor/blob/71c39eafbd134d43ae3f489b5e6488b2a157de65/src/harbor/trial/trial.py#L674)。第三项是由该调用流程与本地主机持有任务目录推导的信任边界判断。可信机器也不意味着其上运行的 agent 应获得隐藏测试。

**开发命令不能直接用作考生入口。** `harbor tasks start-env` 的 `all` 参数默认 `True`，`-a/--all` 意为添加 solution 和 tests；函数会把二者上传到环境。它随后 attach shell，退出后停止并删除环境。不能未经配置和隔离验证就让被测会话使用其默认结果。本次未运行 CLI，因此不宣称某个反向布尔标志已验证可用；禁用上传的实际调用方式应在原型中验证。[固定源码 `cli/tasks.py`](https://github.com/harbor-framework/harbor/blob/71c39eafbd134d43ae3f489b5e6488b2a157de65/src/harbor/cli/tasks.py#L216)

## 对现有 interactive session 的适配程度

- `BaseAgent.setup/run` 是 Harbor 主动驱动的生命周期。自定义 agent 可以写桥接逻辑，但把等待外部会话交卷包装成 `run` 会增加 glue；本次未找到现成“接管任意正在运行会话”的接口。负面结论限于所检查的官方 CLI、agent 基类和 trial 流程。[BaseAgent](https://github.com/harbor-framework/harbor/blob/71c39eafbd134d43ae3f489b5e6488b2a157de65/src/harbor/agents/base.py#L320)
- `trials handoff` 是将已经完成的 Harbor trial 恢复到本地 agent CLI，方向与本项目的手动启动→领题不同。
- `trials regrade` 是实在存在的独立重评入口：已有 trial 目录或 hub UUID，加上新 verifier task；只支持单步、separate verifier。它创建新 trial，不改源记录。它并非通用 HTTP 交卷入口。[上述两条的 CLI 源码](https://github.com/harbor-framework/harbor/blob/71c39eafbd134d43ae3f489b5e6488b2a157de65/src/harbor/cli/trials.py#L867)
- regrade 检查 `artifacts/manifest.json` 和声明产物覆盖，复用时需要生成符合 Harbor 要求的记录布局，或直接接其 Python 内部环境/Verifier API。后者带来版本耦合；前者也需兼容性验证，不能说只要上传 patch 就能调用。[regrade 源码](https://github.com/harbor-framework/harbor/blob/71c39eafbd134d43ae3f489b5e6488b2a157de65/src/harbor/trial/regrade.py#L191)

候选路线：整体 Harbor 桥接的收益主要是环境生命周期复用，但与手动会话流程的配合尚未验证；只复用 task/tests 格式和 regrade 会把耦合集中到交卷转换；直接按固定镜像运行 `test.sh` 则更少依赖，但需自行负责产物恢复、超时和结果解析。当前不足以锁定其中任何一条。

## 客观评分与题目准入可借鉴之处

SWE-bench 将提交 patch 放进可重复的容器化评测流程。其 grading 区分 `FAIL_TO_PASS` 修复能力和 `PASS_TO_PASS` 既有行为保持，完全解决要求两者满足。ACO 可借鉴“正确性＋回归门槛”，不应把测试条数任意加权当普适能力分；导入其任务还要遵循数据与各仓库许可。[评测说明](https://www.swebench.com/SWE-bench/guides/evaluation/)、[固定 grading.py](https://github.com/SWE-bench/SWE-bench/blob/02e7a74ffd0b707aab73d203fe87bdc7c76afc8e/swebench/harness/grading.py#L309)

Terminal-Bench 当前自动化包括 Docker build、Oracle、NOP；agent trials 与 cheat trials 由维护者触发。Oracle 通过、NOP 失败只能排除部分坏题，不能证明 verifier 完整或不可作弊。其 rubric 要求结果导向、可重复、行为验证，LLM judge 只允许少数有充分说明的情形；这些适合作为 ACO 题目准入原则。[自动化说明](https://github.com/harbor-framework/terminal-bench/blob/83c7a6172d629c6575b785ab12c8db787bb2e323/docs/TASK_REVIEW_AUTOMATION.md)、[当前 rubric](https://github.com/harbor-framework/terminal-bench/blob/83c7a6172d629c6575b785ab12c8db787bb2e323/docs/prompts/task-implementation.toml)

原记录引用的 `rubrics/task-implementation.toml` 当前返回 404；实际位置是 `docs/prompts/task-implementation.toml`。不要照搬 Terminal-Bench 对题目高难度的要求，ACO 长期追踪仍可保留易题作为控制；也没有依据把“Oracle/NOP 各 5 次”视为通用官方标准。

## 证据快照与未完成验证

读取 main 的快照：

| 仓库 | commit | 提交时间（仓库返回） |
| --- | --- | --- |
| Harbor | `71c39eafbd134d43ae3f489b5e6488b2a157de65` | 2026-09-06T22:19:17-07:00 |
| Terminal-Bench | `83c7a6172d629c6575b785ab12c8db787bb2e323` | 2026-09-03T02:43:32Z |
| SWE-bench | `02e7a74ffd0b707aab73d203fe87bdc7c76afc8e` | 2026-09-02T01:50:34Z |

本次仅官方文档、源码与静态调用流程核查；没有启动容器、实际适配外部会话、调用模型或测量可靠性。main 快照也不等于所需功能已在某个发布版本提供。下一步若选择 Harbor 原型，应只验证“一个既有会话得到公开工作区→上传一个明确产物→可信侧独立重评”，并验证隐藏文件、镜像层与宿主路径不会进入 agent 可访问范围；不需要先搭全套 runner scheduler。
