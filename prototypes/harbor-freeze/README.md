# THROWAWAY：Harbor 交卷冻结原型

问题：不修改 Harbor 内部 Trial 生命周期，能否通过公开接入点和外部监督者冻结答案、收集唯一产物，并独立验证？

**本次答案：对固定版本的 Docker 单容器假 agent，存在可行接入路径。尚不能据此认定真实 harness 或生产故障恢复已通过。** 这是供人工决策的实验，不是 ACO 实现。

对应[原型票](https://github.com/brianlan/AgentCapabilityObservatory/issues/10)，供[执行与评分边界决策](https://github.com/brianlan/AgentCapabilityObservatory/issues/5)使用。[实测记录](evidence.json)保存完整时间、内容摘要和异常；[回放](report.html)是这些记录的离线查看器。

## 实测结果

2026-09-08，六个核心场景检查通过；主动交卷补测两次。未调用模型、未接真实 provider、未读取凭证。

| 场景 | 观测 |
| --- | --- |
| 对照：前台正常退出，不冻结 | AGENT_END 时后台子进程仍在写，间隔采样的 child.txt 摘要不同。此观察证明存在窗口，不声称每次默认采集都会出错。 |
| 前台退出后冻结 | AGENT_END hook 调外部进程暂停容器，答案稳定；Harbor 后续采集和清理完成，无 Trial 异常。 |
| 达到 2.5 秒期限 | Harbor 记录 AgentTimeoutError，hook 随后冻结；答案仍通过独立校验。本次实际冻结比 AGENT_START + 2.5 秒晚约 143 ms，不是零延迟硬截止保证。 |
| 主动交卷，两个并发请求 | 以本地文件模拟交卷信号，两个独立收卷进程用文件锁串行封存；重试得到相同 receipt 与答案，Harbor 清理后仍可取回。 |
| 收卷进程崩溃 | 第一个收卷进程暂停容器、写状态后实际 os._exit(77)；新进程从该状态继续，仍得到同一冻结答案。仅测试这个指定故障窗口。 |
| 复制失败 | 故意复制不存在路径，记录失败且不产生成功 receipt；新进程从仍暂停的容器成功收卷。 |

五个非对照场景均在新的、无网络、答案只读挂载的容器中校验 parent.txt、child.txt 和新增二进制文件；Harbor 自动评分始终关闭。

## 两个必须保留的区别

1. **ACO 的封存答案与 Harbor 随后采集的工作区不是同一权威对象。** 首轮主动交卷中，后者多出 parent.tmp 和 child.tmp；正文文件值相同，但文件集不同。两次针对性交卷补测未再出现该差异，不能由此排除竞态。本实验未精确定位临时文件在关闭过程中的产生时点。正式评分只能读取收卷进程已复制并封存的答案，不能再换成后续 Harbor 目录。
2. **主动停止 CLI 与答题失败不是同一回事。** 主动交卷后，监督者复制答案并终止容器，假 agent 的 exec 返回非零，Harbor 记录 NonZeroAgentExitCodeError；答案仍可校验。ACO 需用交卷意图、冻结 receipt 和评分分别表达状态，保留原始异常而不把它直接转为 0 分。

## 实际接入成本

仅使用 `Trial.create`、`Trial.add_hook(AGENT_START/AGENT_END)`、自定义测试 Agent 的公开 `import_path`、关闭 verifier 的配置，以及额外 Compose 文件。未改 Harbor 包、未覆盖其私有方法、未复制 Trial 调度。

外部收卷使用 Docker 原生 `pause` / `cp` / `kill`。容器由我们添加的 Compose label 精确识别，不依赖 Harbor 内部命名规则。退出和超时在 AGENT_END 接入；主动提交由外部 watcher 触发。收卷过程是“暂停 → 复制并比对 → 把答案移到会话不可写的目录 → 终止或交还清理 → 发布 receipt”。Harbor 保留其本来的日志/产物清理流程，但其产物不覆盖 ACO 的正式答案。

这是 Docker 特定的适配，不能称作跨所有 Harbor 环境后端的通用接口。`add_hook` 是固定版本的 Python 接口，未来升级需复核。假 Agent 只作为写入刺激，不代表完成了 Codex/OpenCode/Pi 接入。

## 明确未覆盖的部分

- 用文件信号模拟提交，没有真实 HTTP API、认证、网络传输、数据库或分布式协议。
- 首次环境构建被原型的 45 秒 watchdog 中止，尚未启动 agent；原因是 Harbor 的联网控制 sidecar 未及时构建完成。为聚焦收卷，成功试验使用 Harbor public baseline 加显式 Docker `network_mode: none`，不启动其 sidecar。**没有验证 Harbor allowlist、模型网络或生产联网策略。** 这次准备超时不作为 Harbor 无法满足要求的证据。
- 没有测试真实 CLI 的取消行为、登录、skills 的实际发现、日志完整性或隐藏评分防作弊。极小校验器不能证明任意题目 grader 安全。
- 没有机器断电、文件系统崩溃或完整监督进程重启试验；仅杀死指定收卷子进程。状态写入不是经 fsync 保证的断电事务。答案目录 rename 后、receipt 发布前的崩溃窗口尚不能恢复，代码用 ponytail 注释明确标记；不把这个脚本当生产收卷器。
- 写入者只包含该容器的父子进程，不包含其他容器或宿主写入者。文件测试是平面普通文件，未验证大型目录、删除操作、符号链接、权限与特殊文件。
- 只有有限次数试验；收卷启动本身有延迟。生产方案必须记录实际冻结时间，明确超界处理，并用真实 harness 验证 deadline 监督方式。

## 运行

已验证环境：Python 3.12.14，Docker Engine 29.6.1；Harbor 0.22.0，源码 commit `71c39eafbd134d43ae3f489b5e6488b2a157de65`。已核对安装后的 trial.py 与固定源码逐字节一致，摘要在 evidence.json 中。镜像使用固定 digest，见 probe.py；依赖快照见 requirements.snapshot.txt。

在当前机器：

```sh
cd prototypes/harbor-freeze
/ssd4/envs/aco_py312/bin/python probe.py
```

也可只重跑主动交卷：

```sh
/ssd4/envs/aco_py312/bin/python probe.py submit
```

其他机器需 Docker daemon、Compose 插件及已安装快照依赖的 Python 3.12 环境。按 probe.py 中的 digest 预拉取镜像；执行结果存入带随机后缀的 `PROTOTYPE-results-*` 目录。脚本仅清理本次精确标识的容器，保留结果供复查。

## 给决策票的建议

这次原型没有暴露必须修改 Harbor 内部生命周期的障碍，因此支持继续以 **Harbor 负责环境与启动，ACO 独占正式收卷及评分状态** 为候选边界。是否采纳由用户在决策票确定；本实验不自动关闭人工决策，也不扩展实现第二套执行后端。
