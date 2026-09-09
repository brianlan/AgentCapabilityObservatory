"""ACO command-line interface (#17): register, run, status, cancel, resume.

A thin management client over the public API (argparse + aco.client, no CLI
framework). Every output comes from API responses; the CLI computes no
statistics and enforces no scheduling rules. Closing or interrupting the CLI
never cancels server-side work — only an explicit ``aco cancel`` does.
"""

import argparse
import json
import os
import sys
import time
from pathlib import Path

from .client import ApiError, ApiUnavailable, Client

DEFAULT_API_URL = "http://127.0.0.1:8000"
POLL_INTERVAL = 2.0


def state_file() -> Path:
    """Local run ledger for --idempotency-key replays (key -> experiment id)."""
    return Path(os.environ.get("ACO_CLI_STATE", "~/.config/aco/cli.json")).expanduser()


def load_state() -> dict:
    try:
        return json.loads(state_file().read_text())
    except (OSError, ValueError):
        return {}


def save_state(state: dict) -> None:
    path = state_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, sort_keys=True))


def ref(text: str) -> dict:
    name, sep, version = text.partition("@")
    if not name or not sep or not version:
        raise SystemExit(f"error: expected name@version, got {text!r}")
    return {"name": name, "version": version}


def emit(args, payload: dict) -> None:
    """Stable JSON for scripts; nothing credentials-bearing ever reaches output."""
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2))


def progress_line(experiment: dict) -> str:
    p = experiment["progress"]
    return (f"计划 {p['planned']} 运行 {p['attempted']} 封存 {p['sealed']} "
            f"异常 {p['anomaly']} 取消 {p['cancelled']} / 共 {len(experiment['trials'])}")


def finished(experiment: dict) -> bool:
    p = experiment["progress"]
    return p["sealed"] + p["anomaly"] + p["cancelled"] >= len(experiment["trials"])


def cmd_register(args) -> int:
    client = Client(args.api_url, token=args.token)
    content = json.loads(Path(args.file).read_text())
    status, record = client.post("/v1/versions", {
        "kind": args.kind, "name": args.name, "version": args.version,
        "content": content, "assets": [],
    })
    if args.json:
        emit(args, record)
    else:
        note = "已登记" if status == 201 else "已存在（内容一致）"
        print(f"{note} {args.kind} {args.name}@{args.version} 摘要 {record['id'][:12]}")
    return 0


def cmd_run(args) -> int:
    client = Client(args.api_url, token=args.token)

    if args.idempotency_key:
        previous = load_state().get(args.idempotency_key)
        if previous:
            _, experiment = client.get(f"/v1/experiments/{previous}")
            if args.wait:
                return wait_loop(client, experiment, args)
            if args.json:
                emit(args, experiment)
            else:
                print(f"已存在（幂等键 {args.idempotency_key}）：Experiment {previous}")
                print(progress_line(experiment))
            return 0

    targets = [ref(t) for t in args.target]
    if args.task:
        estimate = f"计划样本数: {len(targets) * args.repetitions}"
        plan = {"task": ref(args.task)}
    else:
        estimate = f"计划样本数: ≥ {len(targets) * args.repetitions}（suite 任务展开后为准）"
        plan = {"suite": ref(args.suite)}
    plan.update({"targets": targets, "repetitions": args.repetitions})
    # JSON mode keeps stdout machine-parseable; the pre-create estimate goes to stderr
    print(estimate, file=sys.stderr if args.json else sys.stdout)

    _, experiment = client.post("/v1/experiments", plan)
    if args.idempotency_key:
        state = load_state()
        state[args.idempotency_key] = experiment["id"]
        save_state(state)

    if args.json:
        if args.wait:
            return wait_loop(client, experiment, args)  # emits the single terminal document
        emit(args, experiment)
    else:
        print(f"Experiment {experiment['id']}（{len(experiment['trials'])} 个样本，服务端异步执行）")
        if args.wait:
            return wait_loop(client, experiment, args)
    return 0


def wait_loop(client: Client, experiment: dict, args) -> int:
    """Poll progress until every trial has a terminal answer. Ctrl-C only
    stops the local watcher — it never sends a cancel request.

    JSON contract: stdout carries exactly ONE machine-readable JSON document
    (the last observed experiment state, on normal completion or Ctrl-C);
    progress lines and the interruption notice go to stderr. Human mode
    prints everything to stdout."""
    exp_id = experiment["id"]
    stream = sys.stderr if args.json else sys.stdout
    try:
        while True:
            print(progress_line(experiment), file=stream)
            if finished(experiment):
                break
            time.sleep(POLL_INTERVAL)
            _, experiment = client.get(f"/v1/experiments/{exp_id}")
    except KeyboardInterrupt:
        print(f"\n已停止等待（本地查看已退出）。批次 {exp_id} 仍在服务端运行；"
              f"如需取消请显式执行: aco cancel {exp_id}", file=stream)
        code = 130
    else:
        code = 0
    if args.json:
        emit(args, experiment)  # stdout: a single parseable JSON document
    return code


def cmd_status(args) -> int:
    client = Client(args.api_url, token=args.token)
    _, experiment = client.get(f"/v1/experiments/{args.experiment_id}")
    if args.json:
        emit(args, experiment)
        return 0
    print(f"Experiment {experiment['id']} 状态 {experiment['status']}")
    print(progress_line(experiment))
    for t in experiment["trials"]:
        print(f"  #{t['plan_order']} rep{t['repetition']} {t['task']['name']}@{t['task']['version']}"
              f" → {t['config']['name']}@{t['config']['version']}  {t['status']}")
    return 0


def cmd_cancel(args) -> int:
    client = Client(args.api_url, token=args.token)
    _, summary = client.post(f"/v1/experiments/{args.experiment_id}/cancel")
    if args.json:
        emit(args, summary)
    else:
        print(f"批次 {args.experiment_id} 已取消：{summary}")
    return 0


def cmd_resume(args) -> int:
    client = Client(args.api_url, token=args.token)
    _, body = client.post(f"/v1/experiments/{args.experiment_id}/resume")
    if args.json:
        emit(args, body)
    else:
        print(f"批次 {args.experiment_id} 恢复：{body}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--api-url", default=os.environ.get("ACO_API_URL", DEFAULT_API_URL))
    common.add_argument("--token", default=os.environ.get("ACO_API_TOKEN"))
    common.add_argument("--json", action="store_true", help="输出稳定 JSON（供脚本使用）")

    parser = argparse.ArgumentParser(prog="aco", description="ACO 评测管理 CLI（API 客户端）")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("register", parents=[common],
                       help="登记一个版本（task/suite/config/scorer）")
    p.add_argument("kind", choices=("task", "suite", "config", "scorer"))
    p.add_argument("name")
    p.add_argument("version")
    p.add_argument("file", help="内容 JSON 文件路径")
    p.set_defaults(func=cmd_register)

    p = sub.add_parser("run", parents=[common], help="创建批次并立即返回 Experiment ID")
    group = p.add_mutually_exclusive_group(required=True)
    group.add_argument("--task", help="已登记任务 name@version")
    group.add_argument("--suite", help="已登记题组 name@version")
    p.add_argument("--target", action="append", required=True, help="目标配置 name@version，可重复")
    p.add_argument("--repetitions", type=int, default=1)
    p.add_argument("--idempotency-key", help="同一键重复执行返回既有批次，不重复创建")
    p.add_argument("--wait", action="store_true", help="轮询进度直到全部样本结束；Ctrl-C 只停止等待")
    p.set_defaults(func=cmd_run)

    p = sub.add_parser("status", parents=[common], help="查看批次进度")
    p.add_argument("experiment_id")
    p.set_defaults(func=cmd_status)

    p = sub.add_parser("cancel", parents=[common], help="显式取消批次（幂等）")
    p.add_argument("experiment_id")
    p.set_defaults(func=cmd_cancel)

    p = sub.add_parser("resume", parents=[common], help="恢复重启后暂停的批次计划")
    p.add_argument("experiment_id")
    p.set_defaults(func=cmd_resume)
    return parser


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except ApiError as exc:
        print(f"error: API {exc.status} [{exc.code}] {exc.message}", file=sys.stderr)
        return 1
    except ApiUnavailable as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
