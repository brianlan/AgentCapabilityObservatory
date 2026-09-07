# 单命令启动下 Harbor 与原生 CLI 的接入成本

研究日期：2026-09-07。对应[研究：单命令启动下 Harbor 与原生 CLI 的接入成本](https://github.com/brianlan/AgentCapabilityObservatory/issues/9)。这是选型依据和原型建议，不是用户已经批准的后端决定。

用户已允许启动端用一个命令创建容器并开启全新 CLI 会话，仍由 agent 通过 API 领取绑定题目、交卷；会话外监督者负责截止、收卷，可信侧独立评分。此前“必须接入已经手动打开的会话”的约束已移除。ACO 继续使用指定 Python 3.11 环境。

## 结论

Harbor 重新成为可行候选：现有适配器本来就调用原生非交互入口。建议先验证 **Harbor 负责环境与启动，关闭其自动评分，ACO 作为唯一正式收卷与评分裁决者**。这能实测复用收益，也避免立即重写环境管理。选择依据应是首题试验中的生命周期衔接成本，不能仅凭 CLI 命令短就判原生更省。

主要未决点是 **API 交卷或到期时能否先可靠冻结作答，再采集唯一答案**。Harbor 有超时、产物、网络策略和独立 verifier，但固定源码的默认采集顺序不足以证明上述冻结保证。原生 CLI 也不提供整个 ACO 所需的容器监督、网络隔离、可信快照及异常恢复。

## 源码快照

复用[前次 Harbor 研究](https://github.com/brianlan/AgentCapabilityObservatory/issues/3)的本地官方源码，重新检查相关调用。Harbor commit 为 `71c39eafbd134d43ae3f489b5e6488b2a157de65`，提交时间 `2026-09-06T22:19:17-07:00`；Pi README 固定于 `e687434a60174db1a9c961d973881a7a851a0597`，提交时间 `2026-09-07T08:32:10Z`（前次记录）。OpenAI/OpenCode 是当日官方滚动文档，未绑定发行版。固定 main 源码不意味着功能已发布。

## 启动和实验条件

| 方面 | Harbor 的现成能力与限制 | 直接驱动原生 CLI |
| --- | --- | --- |
| 非交互入口 | Codex 调 `codex exec --json`；OpenCode 调 `opencode ... run --format=json`；Pi 调 `pi --print --mode json`。不再需要已有会话桥接。[H-C][H-O][H-P] | 官方分别提供这三类入口；明确不使用 resume/continue，并隔离历史目录。[N-C][N-O][N-P] |
| Prompt | 三个适配器共用可选 template；未提供 template 时 instruction 原样返回。不能说 Harbor 默认必然追加一段 benchmark prompt；也不能忽略 CLI 内建指令及项目指令。[H-B] | 启动端控制任务提示词，但仍须记录 CLI 内建行为及所有自动发现来源。 |
| 默认行为变化 | Codex 使用 bypass approvals/sandbox、skip git check、enable unified_exec；OpenCode skip permissions、设置 fake VCS、改变日志目录；Pi 过滤 message_update 流事件。这些是 wrapper 条件，不是无影响透明转发。[H-C][H-O][H-P] | 可显式选择必要参数，减少 wrapper 的隐含条件；对应版本的非交互权限仍须验证，不能复制旧参数假定兼容。 |
| Provider/config | Codex 支持 native config 基础上应用运行参数和 endpoint；OpenCode 配置 overlay/custom provider；Pi custom endpoint 要求 model_api 与 API key 环境变量引用。[H-C][H-O][H-P] | 沿用各 harness 的配置机制即可，不需统一实现模型协议；组合兼容性和订阅登录仍待实测。 |
| Skills | Harbor 可解析并注入 agent.skills；三个 adapter 会将指定目录复制到各自发现目录。这不等于清除镜像预装、用户或项目 skills，复制还使用容错命令。[H-T][H-C][H-O][H-P] | Pi 明确有 --no-skills、--skill，且支持关闭发现后显式加载。其他 CLI 的无 skills baseline 本次未证实通用单一开关；需要干净配置/镜像、发现源审计和实测。[N-P] |
| 安装与联网 | BaseInstalledAgent.setup 会调用 install；Codex 已装匹配版本可跳过安装，Pi/OpenCode 此快照仍执行 npm 安装，未 pin 默认 latest。Harbor 区分环境 baseline 与 agent 阶段网络 allowlist，因而可把安装与作答网络分开。[H-B][H-C][H-O][H-P][H-N] | 可把 CLI、任务依赖预装进固定镜像，但 ACO 负责构建、版本检查和实际 egress；运行时目录刷新/认证端点也须验证。 |
| Python | Harbor requires-python >=3.12，不能直接装进指定 py311；可以独立进程/容器运行。[H-Y] | ACO 可保持 py311；三种 CLI 是外部程序，不要求为其改 ACO Python。 |

应同时保存启动端请求配置与运行证据。Harbor 配置版本未指定时的探测是 best-effort；指定版本本身不能代替实际二进制核验。[H-B] provider/model 标签、日志和模型自报不能验证服务内部真实模型。默认无需把这些身份写进任务 prompt，agent 的 API 权限预先绑定本次作答即可。

## 收卷、截止和评分的真实边界

Harbor 单步流程捕获 AgentTimeoutError 和 NonZeroAgentExitCodeError 后，仍尝试同步输出、收集产物、运行 verifier。separate 模式在收集后停止作答环境再评分；因此“超时就一定不评分”不成立。[H-S]

但 `_run_agent_phase` 用 asyncio.wait_for 包裹 agent.run。Docker 后端部分异常处理终止的是宿主 compose exec 子进程；静态代码不足以证明容器内所有后代同步死亡。主服务的 collect hooks 和产物下载发生在主服务停止之前；separate 下先停主服务保护的是随后收集的 sidecar 证据。不能把这个顺序当作截止瞬间的一致快照。[H-T][H-D]

存在扩展点，但尚未证实可零修改满足 ACO：

- `Trial.add_hook(AGENT_END, ...)` 可在主采集前运行 Python callback；事件含 config/result/lock，不含直接环境控制句柄。BaseEnvironment 中本次未找到通用 pause/freeze 方法。是否可在外部监督者暂停后继续 Harbor 日志及采集，仍未知。[H-K][H-T]
- `verifier.collect` 能在服务内执行采集前命令，但设计为 best-effort，失败仅记警告；它不是冻结成功的硬门槛。暂停整个容器也可能让随后必须在容器内执行的采集步骤阻塞，需要用原型检查。[H-T]
- **公共 `--disable-verification` 确实存在**，配置为 verifier.disable。可由 Harbor 只跑 agent、ACO 评分；不是必须双重评分。关闭评分不会自动关闭 Harbor 的收集和清理生命周期。[H-J][H-S]

API submit 到达时，Harbor agent.run 可能仍在执行。建议试验把 submit 作为“请求结束”，由唯一监督者冻结并确认最终产物，agent 不自行生成官方分数；重复请求只返回同一结果。必须记录 submit_received_at 和实际 frozen_at，防止用 API 到达时间冒充已经停止写入。这个协议建议来自以上时序分析，并非 Harbor 现成功能。

## 最小验证路线与条件性选型

先用一个无需模型的假 CLI/受控写文件进程验证监督边界，再在授权的实际 harness/provider 上跑一题。无需先建 dashboard、分布式 lease 或完整 schema。

1. **Harbor 路线先测**：固定镜像和版本，禁用自动评分，公开题包仅含作答输入；agent 工具调用 ACO 领题/提交。检查领取、进程和日志是否绑定同一新会话，无隐式 resume。
2. **冻结竞态**：父进程和后台子进程持续更新文件；正常交卷、超时、异常退出分别触发冻结。必须证明正式产物在 frozen_at 后不再变，且临界并发 submit 只得到一个答案。API 响应丢失也不能重新取一份后续答案。
3. **异常收卷**：截止前未交卷、监督者重启、容器异常和上传失败都留下记录；区别“有可评分产物”和“无法回收”，不捏造零分/成功。验证恢复不会重复启动模型。
4. **隔离与联网**：禁止访问 hidden tests、solution、宿主 Docker socket、其他试题和管理 API；模型服务及受限 ACO 可达，任意其他目的地不可达。安装在计时前结束，作答阶段不能借自动安装/模型目录刷新开放公网。
5. **配置与 skills**：实际版本/配置不符即标错；baseline 不发现额外 skills，显式 skill 能加载且内容有摘要；依然保留内建 harness 行为。未知观测字段必须为空/未知，不让模型补填。
6. **独立恢复评分**：用冻结产物在干净 verifier 中恢复新增、删除、二进制或题目要求的文件，并验证公开/隐藏测试边界；Harbor 评分禁用后不会产生第二个正式成绩。

若 Harbor 的公开配置加一个清晰监督衔接就通过，优先保留环境、网络和日志复用。若必须修改其内部收集/清理顺序、复制其 Trial 调度或维护多处版本补丁，再以同一组检查比较原生 CLI 容器监督路线。后者可能减少 Harbor 耦合，但必须补齐上面同样的保障，不能用几行 subprocess 代替。可独立借鉴 Harbor task/artifact 思路，不意味着必须复用整个运行引擎。

本次未启动容器、调用模型、安装依赖或读取凭证；不存在工期、代码量或可靠性实测结论。

[H-B]: https://github.com/harbor-framework/harbor/blob/71c39eafbd134d43ae3f489b5e6488b2a157de65/src/harbor/agents/installed/base.py#L1024
[H-C]: https://github.com/harbor-framework/harbor/blob/71c39eafbd134d43ae3f489b5e6488b2a157de65/src/harbor/agents/installed/codex.py
[H-O]: https://github.com/harbor-framework/harbor/blob/71c39eafbd134d43ae3f489b5e6488b2a157de65/src/harbor/agents/installed/opencode.py
[H-P]: https://github.com/harbor-framework/harbor/blob/71c39eafbd134d43ae3f489b5e6488b2a157de65/src/harbor/agents/installed/pi.py
[H-T]: https://github.com/harbor-framework/harbor/blob/71c39eafbd134d43ae3f489b5e6488b2a157de65/src/harbor/trial/trial.py
[H-S]: https://github.com/harbor-framework/harbor/blob/71c39eafbd134d43ae3f489b5e6488b2a157de65/src/harbor/trial/single_step.py
[H-D]: https://github.com/harbor-framework/harbor/blob/71c39eafbd134d43ae3f489b5e6488b2a157de65/src/harbor/environments/docker/docker.py#L647
[H-K]: https://github.com/harbor-framework/harbor/blob/71c39eafbd134d43ae3f489b5e6488b2a157de65/src/harbor/trial/hooks.py
[H-J]: https://github.com/harbor-framework/harbor/blob/71c39eafbd134d43ae3f489b5e6488b2a157de65/src/harbor/cli/jobs.py#L1017
[H-N]: https://github.com/harbor-framework/harbor/blob/71c39eafbd134d43ae3f489b5e6488b2a157de65/src/harbor/trial/network_policy.py#L82
[H-Y]: https://github.com/harbor-framework/harbor/blob/71c39eafbd134d43ae3f489b5e6488b2a157de65/pyproject.toml#L9
[N-C]: https://learn.chatgpt.com/docs/non-interactive-mode
[N-O]: https://opencode.ai/docs/cli/#run
[N-P]: https://github.com/earendil-works/pi/blob/e687434a60174db1a9c961d973881a7a851a0597/packages/coding-agent/README.md
