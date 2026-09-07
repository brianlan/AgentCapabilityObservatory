# 已打开的交互会话能否通过 ACO API 领题交卷

调查日期：2026-09-07。对应决策依据：[研究：Codex、OpenCode 与 Pi 能否让已打开的新会话通过 API 领题交卷](https://github.com/brianlan/AgentCapabilityObservatory/issues/2)。仅文档与源码调查，未调用模型、读取凭证或做端到端试跑。

## 结论

三者都提供交互会话与 shell 工具。因此，在 HTTP 客户端可用、文件与网络权限允许的条件下，**用户打开并选好模型的那个 session 可以自己调用 ACO API，下载题目、修改文件、上传交卷产物**。这是从现有工具能力推得的可行性，尚不是 ACO 的兼容性实测。通用薄 CLI 可以代为处理 HTTP 和产物传输；不必为此启动第二个模型会话。客户端不负责给自己正式评分。

| Harness | 同一会话的接入路径 | 已证实可观察信息 | 尚不能保证 |
|---|---|---|---|
| Codex CLI | 交互会话可运行命令；在预先选定的工作目录通过 shell 调 HTTP；实际可行性受 sandbox、网络及审批配置限制。[CLI](https://learn.chatgpt.com/docs/cli)、[Sandbox](https://learn.chatgpt.com/docs/sandboxing) | `/status` 可检查活动模型、权限、可写根及 token 使用；`/new` 在 CLI 内新建 chat。Provider 可配置，但配置层优先级与运行中选择须分开。[Developer commands](https://learn.chatgpt.com/docs/developer-commands)、[Configuration reference](https://learn.chatgpt.com/docs/config-file/config-reference) | 本次没有证实一个跨版本稳定、供普通 shell 自动读取所有当前会话字段的接口；不能把磁盘默认配置当作有效运行配置。 |
| OpenCode | 默认 CLI 启动 TUI；`opencode [project]` 指定项目，`--model provider/model` 选模型；bash 能执行命令。[CLI](https://opencode.ai/docs/cli/)、[Tools](https://opencode.ai/docs/tools/) | TUI 自带 server，可查 health/version、session/messages、config、path；启动时可指定端口以便连接原 server。另开 `opencode serve` 会创建另一 server，不能据此声称观察了原会话。[Server](https://opencode.ai/docs/server/) | 当前 session ID 与观测客户端的可靠绑定仍需实测；配置默认模型不等于每条实际消息模型。API 枚举的是客户端记录，不能证明供应商内部路由。 |
| Pi coding agent | 默认有 read/write/edit/bash；交互 `/new` 新会话，`/model` 选模型。由当前 bash 调 HTTP 即可。[固定源码 README](https://github.com/earendil-works/pi/blob/e687434a60174db1a9c961d973881a7a851a0597/packages/coding-agent/README.md) | `/session` 展示 session ID、文件、消息和用量；bash 子进程获得 `PI_SESSION_ID`、`PI_SESSION_FILE`、`PI_PROVIDER`、`PI_MODEL`；`pi --version` 可取版本。[同一 README](https://github.com/earendil-works/pi/blob/e687434a60174db1a9c961d973881a7a851a0597/packages/coding-agent/README.md#environment-variables) | 这些字段表示 Pi 的选择与本地记录，不是远端模型身份认证；不同安装版本可能尚无上述环境变量，须按版本验证。 |

## 对后续架构决策的影响（建议，未代替用户决策）

1. **保留用户会话的主体地位。** ACO 的 API 是领取与提交协议；不是必须由 ACO worker 再启动 harness 的命令。`codex exec`、`opencode run`、Pi RPC/print 等另起执行路径不能直接替代此交互需求。
2. **先建考试工作区，再在其中打开 harness。** 领题可以发生在新会话内，将公开题目 materialize 到该工作区。既有进程不会因为下载了一份 Dockerfile 就自动迁入容器；若采用容器约束，应在容器内开原始交互 CLI，或明确支持工具远程执行。只在 shell 中 `cd` 不足以证明其他内建文件工具、指令发现和权限根同步改变。这是进程与工具边界推论，尚待接入试验。
3. **单题单新会话最容易审计。** “全新”只能在规定的范围内说：无此前对话不意味着没有用户全局指令、skills、插件、记忆或项目规则。应记录可观察的 session ID、启动/领取时间、配置来源和缺失项。单用户可信机器允许人工声明，但不能把声明升级为系统验证。
4. **闭卷由可访问性保障。** 题目下载只含公开输入；隐藏 verifier、solution 和其他私题不放在该会话可读的目录或可调用的 API 权限范围。交卷 token 不应具有改成绩或读评分器的权限。宿主“由本人控制”并不自动意味着被测 agent 无法读到宿主私有文件。
5. **硬截止需要独立执行者。** HTTP 服务可拒收过期交卷，但这不等于杀掉本地 agent、其子进程并冻结输出。只有 agent 自己执行的提交步骤，在 agent 崩溃、停止工具调用或用户退出时不会保证发生。若需要超时仍收卷与统一耗时，需由本地辅助进程/容器监督者执行并记录；是否纳入 V1 是下一张决策票。Pi README 明确不提供 background bash；OpenCode 暴露 session abort，但不能由此推定跨 harness 的全部子进程清理保证。
6. **分开请求配置与观测证据。** Provider/模型选择、harness 日志、API 响应标签及模型自报分别存来源和时间；`reported_model` 是报告值，未知底层服务身份保持未知。SSE 只是传输方式，不能将其统一命名为 `verified resolved_model`。本调查未验证 GLM、火山方舟或任何具体模型组合的认证与协议兼容性。

## 证据边界与最小后续验证

OpenAI/OpenCode 使用当日官方滚动文档，未绑定发行版本，不能据此填入用户聊天中的示例版本号。Pi 使用 commit `e687434a60174db1a9c961d973881a7a851a0597`（GitHub committer 时间 `2026-09-07T08:32:10Z`）；原 `badlogic/pi-mono` 链接跳转至 `earendil-works/pi`。环境变量事实来自固定 commit 的原文，而非可能滞后的搜索缓存。

将来实现一个临时 ACO HTTP 服务后，对每个实际安装版本人工开新交互会话：领取一题、生成新文件、上传、服务端确认；保留脱敏日志以核对同一 session、模型切换、目录与权限。另测断网、退出与到期后的收卷行为。这个试验需要真实交互运行，本次未执行，因此结论为“存在接入路径，可靠性边界待验证”。
