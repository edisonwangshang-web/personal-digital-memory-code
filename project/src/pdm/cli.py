"""Small command surface for explicit capture and offline release preparation."""

import argparse
import json
import os
from pathlib import Path
import re
import stat
import sys

from . import __version__
from .budget import BudgetBlocked
from .doctor import inspect_environment


class _CliError(ValueError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


class _SafeParser(argparse.ArgumentParser):
    def error(self, message):
        # argparse's default error may repeat a submitted question or credential.
        raise _CliError("invalid_arguments")


def _model_budget(args):
    from .budget import BudgetLedger, BudgetedModelCall
    from .model_calls import validate_timeout
    timeout = getattr(args, "model_timeout", None)
    if timeout is None:
        timeout = getattr(args, "model_timeout_default", 120)
    validate_timeout(timeout)
    if not args.budget_ledger:
        raise _CliError("budget_ledger_required")
    if not args.operation_id:
        raise _CliError("budget_operation_required")
    return BudgetedModelCall(BudgetLedger(args.budget_ledger, repo=args.repo), args.operation_id,
                             default_timeout=timeout)


def _budget_arguments(command, *, default_timeout=None, configure_timeout=True):
    command.add_argument("--budget-ledger", type=Path, help="仓库外已创建的持久调用账本；支持历史预算或日常审计模式")
    command.add_argument("--operation-id", help="本次操作的稳定 ID；重试复用，不得自动重置预算")
    if configure_timeout:
        from .model_calls import PROCESSING_TIMEOUT_SECONDS
        command.set_defaults(model_timeout_default=PROCESSING_TIMEOUT_SECONDS if default_timeout is None else default_timeout)
        command.add_argument("--model-timeout", type=int,
                             help="单次模型调用超时秒数，1–1800；新调用默认900，历史恢复默认120；不是整个任务时长")


def _skill_arguments(command):
    from .skill_support import DEFAULT_EXTRACT_SKILL_REF, DEFAULT_REVIEW_SKILL_REF
    command.add_argument("--extract-skill", help="显式提取 Skill 引用；新任务默认 " + DEFAULT_EXTRACT_SKILL_REF)
    command.add_argument("--review-skill", help="显式审核 Skill 引用；新任务默认 " + DEFAULT_REVIEW_SKILL_REF)


def _selected_skills(args, *, default=False):
    extract, review = args.extract_skill, args.review_skill
    if not default and extract is None and review is None:
        return None
    from .skill_support import resolve_skill_refs
    return resolve_skill_refs(extract, review)


def _rebuild_context(args):
    """Read only the receipt reference; the core verifies its Git authority."""
    if args.rebuild_context is None:
        return None
    value = _input_json(args.rebuild_context)
    if (not isinstance(value, dict) or set(value) != {"path", "commit_sha", "sha256"}
            or not isinstance(value["path"], str)
            or re.fullmatch(r"memory/runs/processes/rebuilds/[0-9a-f]{64}\.md", value["path"]) is None
            or not isinstance(value["commit_sha"], str)
            or re.fullmatch(r"[0-9a-f]{40}(?:[0-9a-f]{24})?", value["commit_sha"]) is None
            or not isinstance(value["sha256"], str)
            or re.fullmatch(r"sha256-[0-9a-f]{64}", value["sha256"]) is None):
        raise _CliError("invalid_rebuild_context")
    return value


def _reading_arguments(command):
    command.add_argument("--plan", type=Path, help="固定batch计划JSON；必须同时选择--batch")
    command.add_argument("--batch", action="append", help="明确选择的batch ID，可重复；不扩大--source授权")


def _object_reading_arguments(command):
    command.add_argument("--object", action="append", help="配合--current选择正式记录或主题路径；主题自动带上所引记录")
    command.add_argument("--compact-sources", action="store_true", help="配合--current仅在带行号的lines中提供原文，避免重复传输")


def _object_reading_options(args):
    options = {}
    if args.object is not None:
        options["object_paths"] = args.object
    if args.compact_sources:
        options["compact_sources"] = True
    return options


def _reading_projection(args):
    if (args.plan is None) != (not args.batch):
        raise _CliError("batch_plan_and_selection_required")
    if args.plan is None:
        return {}
    from .batching import project_batch_sources, merge_batch_sources
    plan = _input_json(args.plan)
    if len(args.batch) == 1:
        projected = project_batch_sources(args.repo, plan, args.batch[0], source_paths=args.source)
    else:
        projected = merge_batch_sources(args.repo, plan, args.batch, source_paths=args.source,
                                        max_bytes=getattr(args, "budget_bytes", None) or 192000)
    if not projected["coverage"]["complete"] or not projected["source_views"]:
        raise _CliError("batch_read_projection_incomplete")
    return {"source_views": projected["source_views"], "batch_context": projected["batch_context"]}


def _input_json(path, maximum=2_000_000):
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(descriptor, "rb") as stream:
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
            raise ValueError("input_must_be_regular_file")
        data = stream.read(maximum + 1)
    if len(data) > maximum:
        raise ValueError("input_file_too_large")

    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate_input_field")
            result[key] = value
        return result

    def invalid_constant(value):
        raise ValueError("invalid_json_number")

    return json.loads(data, object_pairs_hook=unique, parse_constant=invalid_constant)


def parser() -> argparse.ArgumentParser:
    root = _SafeParser(prog="pdm-memory")
    root.add_argument("--version", action="version", version=__version__)
    commands = root.add_subparsers(dest="command", required=True)
    doctor = commands.add_parser("doctor", help="只读检查本地环境，不推定模型或云端权限")
    doctor.add_argument("--repo", type=Path, required=True)
    capture = commands.add_parser("capture", help="保存明确提交的来源，暂不加工或发布正式知识")
    capture.add_argument("--repo", type=Path, required=True)
    capture.add_argument("--input", type=Path, required=True, help="符合来源契约的 JSON 文件")
    capture.add_argument("--expected-head")
    capture.add_argument("--remote", help="显式指定后才推送；未指定只确认本地保存")
    rebuild = commands.add_parser("rebuild-prepare", help="按明确意图原子清理指定正式知识并保存重建回执；不调用模型")
    rebuild.add_argument("--repo", type=Path, required=True)
    rebuild.add_argument("--intent-id", required=True, help="本次明确清理重建意图的稳定ID；重复调用不再次删除")
    rebuild.add_argument("--source", action="append", required=True, help="获准从头加工的现有原文路径")
    rebuild.add_argument("--target", action="append", required=True, help="明确清理的现有record/topic路径")
    rebuild.add_argument("--expected-head")
    process = commands.add_parser("process", help="对显式选定的已采集来源联合加工并独立 Review；不自动发布")
    process.add_argument("--repo", type=Path, required=True)
    process.add_argument("--source", required=True, action="append", help="已保存的来源相对路径；重复参数可选最多8份")
    process.add_argument("--expected-head")
    process.add_argument("--incremental", action="store_true", help="启用有版本绑定和输入去重的 M1 加工")
    process.add_argument("--related-run", action="append", help="显式允许读取的旧发布运行；涉及的原始来源仍须在 --source 范围中")
    process.add_argument("--revision-from-run", help="显式按固定 needs_revision 的完整候选及审核反馈返工；保留原范围及批次")
    process.add_argument("--rebuild-context", type=Path, help="rebuild-prepare返回的三键回执引用JSON；本轮不读取旧知识")
    process.add_argument("--review-strategy", choices=("legacy-v1", "auto", "decomposed-v1"),
                         help="普通process沿原legacy-v1；已有batch候选返工可显式选择auto或分解审核")
    _skill_arguments(process)
    _budget_arguments(process)
    revision_resume = commands.add_parser("resume-unstarted-revision", help="历史0.9人工恢复：核原返工预留及零启动；新Skill质量任务须完整原证明，当前拒绝此入口")
    revision_resume.add_argument("--repo", type=Path, required=True)
    revision_resume.add_argument("--run", required=True)
    revision_resume.add_argument("--source", action="append", required=True)
    revision_resume.add_argument("--related-run", action="append")
    revision_resume.add_argument("--expected-head")
    revision_resume.add_argument("--reservation", type=Path, required=True, help="经操作者核对的原预留和预算身份绑定 JSON")
    revision_resume.add_argument("--expected-reservation-sha256", required=True, help="原预留绑定的固定 canonical 摘要")
    revision_resume.add_argument("--review-strategy", choices=("legacy-v1", "auto", "decomposed-v1"), default="decomposed-v1")
    _reading_arguments(revision_resume)
    _budget_arguments(revision_resume, default_timeout=120)
    revalidate = commands.add_parser("revalidate-completed-extraction", help="人工核对已完成提取及原始缓存后独立Review；支持历史0.9和明确10/8恢复合同，禁止重放未知调用")
    revalidate.add_argument("--repo", type=Path, required=True)
    revalidate.add_argument("--run", required=True)
    revalidate.add_argument("--source", action="append", required=True)
    revalidate.add_argument("--related-run", action="append")
    revalidate.add_argument("--expected-head")
    revalidate.add_argument("--attestation", type=Path, required=True)
    revalidate.add_argument("--expected-attestation-sha256", required=True)
    revalidate.add_argument("--original-payload", type=Path, required=True)
    revalidate.add_argument("--response", type=Path, required=True)
    revalidate.add_argument("--original-resume-context", type=Path, required=True)
    revalidate.add_argument("--review-strategy", choices=("legacy-v1", "auto", "decomposed-v1"), default="decomposed-v1")
    _reading_arguments(revalidate)
    _budget_arguments(revalidate, default_timeout=120)
    publish = commands.add_parser("publish", help="只发布指定运行中独立 Review 已通过的固定候选")
    publish.add_argument("--repo", type=Path, required=True)
    publish.add_argument("--run", required=True)
    publish.add_argument("--expected-head")
    verify_publication = commands.add_parser("verify-publication", help="只读核对指定发布的来源、审核和实际字节")
    verify_publication.add_argument("--repo", type=Path, required=True)
    verify_publication.add_argument("--run", required=True)
    verify_publication.add_argument("--historical", action="store_true", help="核验原发布提交，不能据此称旧对象仍为当前内容")
    recover = commands.add_parser("recover", help="核对并恢复一次未完成的 Git 事务物化")
    recover.add_argument("--repo", type=Path, required=True)
    evidence = commands.add_parser("evidence", help="只读输出显式选定的原文及行号，不调用模型")
    evidence.add_argument("--repo", type=Path, required=True)
    evidence.add_argument("--source", required=True, action="append", help="可重复指定，最多8份来源")
    evidence.add_argument("--run", action="append")
    evidence.add_argument("--current", action="store_true", help="检查纠正、删除和未处理增量，返回覆盖范围与缺口")
    evidence.add_argument("--history", action="store_true", help="配合 --current 读取已标明纠正的历史；删除始终排除")
    evidence.add_argument("--budget-bytes", type=int)
    _reading_arguments(evidence)
    _object_reading_arguments(evidence)
    knowledge = commands.add_parser("knowledge", help="只读生成明确选定来源与运行的当前领域目录，不查全库或调用模型")
    knowledge.add_argument("--repo", type=Path, required=True)
    knowledge.add_argument("--source", required=True, action="append", help="本次明确允许读取的来源路径，可重复")
    knowledge.add_argument("--run", action="append", help="明确允许读取的发布运行，可重复；不自动发现运行")
    knowledge.add_argument("--domain", choices=("health", "career", "learning"), help="只筛选显示，保留全部读取范围与缺口")
    knowledge.add_argument("--budget-bytes", type=int)
    knowledge.add_argument("--format", choices=("json", "markdown"), default="json")
    _reading_arguments(knowledge)
    ask = commands.add_parser("ask", help="仅将指定来源发送模型问答，需调用者已取得相应授权")
    ask.add_argument("--repo", type=Path, required=True)
    ask.add_argument("--source", required=True, action="append", help="可重复指定，最多8份来源；调用前确认全部资料授权")
    ask.add_argument("--question", required=True)
    ask.add_argument("--run", action="append")
    ask.add_argument("--current", action="store_true")
    ask.add_argument("--history", action="store_true")
    ask.add_argument("--budget-bytes", type=int)
    _budget_arguments(ask)
    _reading_arguments(ask)
    _object_reading_arguments(ask)
    budget = commands.add_parser("budget", help="创建或只读核对仓库外的共享调用预算，不执行模型")
    budget_actions = budget.add_subparsers(dest="budget_command", required=True)
    for name in ("create", "status"):
        action = budget_actions.add_parser(name)
        action.add_argument("--repo", type=Path, required=True, help="预算账本必须位于此数据仓库之外")
        action.add_argument("--ledger", type=Path, required=True, help="私有目录中的账本文件；创建不覆盖")
        if name == "create":
            action.add_argument("--mode", choices=("budget", "audit"), default="budget")
            action.add_argument("--limit", type=int, help="仅历史budget模式必需；audit模式必须省略")
            action.add_argument("--initial-used", type=int, required=True, help="创建前已消耗的额度，必须显式提供")
            action.add_argument("--authorization-ref", required=True, help="仅存哈希的审计引用；不是授权验证或新增授权")
    control = commands.add_parser("control", help="预览或应用明确范围的纠正、替代和普通删除")
    control_actions = control.add_subparsers(dest="control_command", required=True)
    for name in ("plan", "apply"):
        action = control_actions.add_parser(name)
        action.add_argument("--repo", type=Path, required=True)
        action.add_argument("--input", type=Path, required=True)
        action.add_argument("--expected-head")
    pending = commands.add_parser("pending", help="统一查看待确认、关联已保存回复和实际处理结果")
    pending_actions = pending.add_subparsers(dest="pending_command", required=True)
    for name in ("list", "register", "answer", "resolve", "repair-registration"):
        action = pending_actions.add_parser(name)
        action.add_argument("--repo", type=Path, required=True)
        if name == "list":
            action.add_argument("--status", choices=("open", "answered", "resolved", "withdrawn"))
        else:
            action.add_argument("--expected-head")
        if name == "register":
            action.add_argument("--input", type=Path, required=True)
        elif name in {"answer", "resolve"}:
            action.add_argument("--item", required=True)
            action.add_argument("--source" if name == "answer" else "--run", required=True)
        elif name == "repair-registration":
            action.add_argument("--run", required=True)
            action.add_argument("--source", action="append", required=True,
                                help="明确允许核对的原文路径；只补登记，不调用模型")
    schedule = commands.add_parser("schedule", help="每日增量的配置、执行和状态；实际触发由目标云端 runner 提供")
    schedule_actions = schedule.add_subparsers(dest="schedule_command", required=True)
    for name in ("configure", "plan", "run", "drain", "status", "pause", "resume", "abandon"):
        action = schedule_actions.add_parser(name)
        action.add_argument("--repo", type=Path, required=True)
        if name == "configure":
            action.add_argument("--input", type=Path, required=True)
            _skill_arguments(action)
        else:
            action.add_argument("--id", required=True)
        if name in {"plan", "status"}:
            action.add_argument("--now", help="带时区的核对时间；省略时使用当前时间")
        else:
            action.add_argument("--expected-head")
        if name in {"run", "drain"}:
            action.add_argument("--trigger", type=Path, required=True)
            _budget_arguments(action, configure_timeout=False)
            if name == "drain":
                action.add_argument("--max-slices", type=int, default=8)
                action.add_argument("--window-seconds", type=int, default=300)
                action.add_argument("--continuation", type=Path, help="上一执行窗口返回的continuation JSON；不创建唤醒任务")
        elif name == "pause":
            action.add_argument("--reason", required=True)
        elif name == "resume":
            action.add_argument("--validation-ref", required=True)
        elif name == "abandon":
            action.add_argument("--attempt", required=True)
            action.add_argument("--reason", required=True)
            action.add_argument("--validation-ref", required=True, help="停止执行并核对在途结果后的操作记录")
            action.add_argument("--execution-stopped", action="store_true", help="明确声明旧执行已停止；未知尝试另须暂停、租约过期及远程核对")
        elif name == "status":
            action.add_argument("--platform-observation", type=Path, help="实际平台触发查询结果；不可读时不推断成功")
    environment = commands.add_parser("environment", help="显式建立或只读核对clone本地环境绑定")
    environment_actions = environment.add_subparsers(dest="environment_command", required=True)
    for name in ("bind", "verify"):
        action = environment_actions.add_parser(name)
        action.add_argument("--repo", type=Path, required=True)
        if name == "bind":
            action.add_argument("--input", type=Path, required=True)
        else:
            action.add_argument("--require-binding", action="store_true")
            action.add_argument("--remote")
    runner = commands.add_parser("runner", help="只读核对runner配置与本机前置；不创建或启用云端任务")
    runner_actions = runner.add_subparsers(dest="runner_command", required=True)
    probe = runner_actions.add_parser("probe")
    probe.add_argument("--repo", type=Path, required=True)
    probe.add_argument("--input", type=Path, required=True)
    probe.add_argument("--receipts", type=Path, help="平台回执引用名的JSON数组；引用本身不证明能力通过")
    promotion = commands.add_parser("promotion", help="只读核对拟合并树，保护生产资料与环境状态")
    promotion_actions = promotion.add_subparsers(dest="promotion_command", required=True)
    for name in ("check", "prepare"):
        promotion_action = promotion_actions.add_parser(name)
        promotion_action.add_argument("--repo", type=Path, required=True)
        promotion_action.add_argument("--base", required=True)
        if name == "check":
            promotion_action.add_argument("--candidate", required=True)
        promotion_action.add_argument("--dev-commit", required=name == "prepare")
    context = commands.add_parser("context", help="限定授权内发现旧知识及续接真实待确认答复")
    context_actions = context.add_subparsers(dest="context_command", required=True)
    discover = context_actions.add_parser("discover")
    discover.add_argument("--repo", type=Path, required=True)
    discover.add_argument("--source", action="append", required=True)
    discover.add_argument("--allow-source", action="append", required=True)
    discover.add_argument("--allow-run", action="append", default=[])
    discover.add_argument("--object-id", action="append", default=[])
    discover.add_argument("--topic-id", action="append", default=[])
    discover.add_argument("--max-sources", type=int, default=8)
    discover.add_argument("--max-runs", type=int, default=8)
    continuations = context_actions.add_parser("continuations")
    continuation_actions = continuations.add_subparsers(dest="continuation_command", required=True)
    for name in ("plan", "execute"):
        action = continuation_actions.add_parser(name)
        action.add_argument("--repo", type=Path, required=True)
        if name == "plan":
            action.add_argument("--allow-source", action="append", required=True)
            action.add_argument("--allow-run", action="append", default=[])
            action.add_argument("--max-sources", type=int, default=8)
            action.add_argument("--max-runs", type=int, default=8)
        else:
            action.add_argument("--input", type=Path, required=True, help="固定的单条continuation job JSON")
            _skill_arguments(action)
            _budget_arguments(action)
    batch = commands.add_parser("batch", help="只读规划和投影原文批次；不调用模型、不发布")
    batch_actions = batch.add_subparsers(dest="batch_command", required=True)
    for name in ("plan", "project"):
        action = batch_actions.add_parser(name)
        action.add_argument("--repo", type=Path, required=True)
        action.add_argument("--source", action="append", required=True)
        if name == "plan":
            action.add_argument("--max-input-bytes", type=int, required=True)
            action.add_argument("--overhead-bytes", type=int, default=0)
            action.add_argument("--max-sources", type=int, default=8)
            action.add_argument("--expected-head")
            action.add_argument("--purpose", choices=("current", "history"), default="current")
        else:
            action.add_argument("--plan", type=Path, required=True)
            action.add_argument("--batch-id", required=True)
            action.add_argument("--budget-bytes", type=int)
            action.add_argument("--fragment", action="append")
    production = commands.add_parser("production", help="持久知识队列：规划、分批加工审核及本地一致发布；不创建云任务")
    production_actions = production.add_subparsers(dest="production_command", required=True)
    for name in ("prepare", "status", "run"):
        action = production_actions.add_parser(name)
        action.add_argument("--repo", type=Path, required=True)
        action.add_argument("--id", required=True)
        if name == "prepare":
            _skill_arguments(action)
            action.add_argument("--source", action="append", required=True)
            action.add_argument("--allowed-source", action="append")
            action.add_argument("--allowed-run", action="append", default=[])
            action.add_argument("--rebuild-context", type=Path, help="显式清理回执引用JSON；仅支持无旧运行的单批完整重建")
            action.add_argument("--max-input-bytes", type=int, default=128000)
            action.add_argument("--payload-overhead", type=int, default=16000)
            action.add_argument("--review-strategy", choices=("auto", "decomposed-v1", "legacy-v1"),
                                help="新队列默认auto；decomposed-v1强制完整分解审核证明；旧队列默认沿原策略")
        elif name == "run":
            action.add_argument("--max-steps", type=int, default=8)
            action.add_argument("--local-only", action="store_true", help="显式隔离演练可免clone绑定；不表示远程同步或真实runner通过")
            _budget_arguments(action)
    package = commands.add_parser("reading-package", help="准备相同版本的限定读取包；不调用模型，不代表第二模型验收通过")
    package_actions = package.add_subparsers(dest="package_command", required=True)
    prepare = package_actions.add_parser("prepare")
    prepare.add_argument("--repo", type=Path, required=True)
    prepare.add_argument("--source", action="append", required=True)
    prepare.add_argument("--run", action="append")
    prepare.add_argument("--questions", type=Path, required=True, help="问题字符串的 JSON 数组，不含标准答案")
    prepare.add_argument("--output", type=Path, required=True, help="数据仓库外的新文件，拒绝覆盖")
    _reading_arguments(prepare)
    verify_package = package_actions.add_parser("verify")
    verify_package.add_argument("artifact", type=Path)
    verify_package.add_argument("--expected-sha256")
    release = commands.add_parser("release", help="构建或检查代码版本包，不部署")
    actions = release.add_subparsers(dest="release_command", required=True)
    build = actions.add_parser("build")
    build.add_argument("--repo", type=Path, required=True)
    build.add_argument("--ref", required=True, help="固定提交或可解析的 Git 引用")
    build.add_argument("--output", type=Path, required=True)
    verify = actions.add_parser("verify")
    verify.add_argument("artifact", type=Path)
    verify.add_argument("--expected-commit")
    verify.add_argument("--expected-package-version")
    verify.add_argument("--expected-artifact-sha256")
    verify.add_argument("--expected-rules-version")
    return root


def main(argv: list[str] | None = None) -> int:
    try:
        args = parser().parse_args(argv)
        if args.command == "doctor":
            result = inspect_environment(args.repo)
            exit_code = 0 if result["local_ready"] else 1
        elif args.command == "capture":
            from .capture import capture
            payload = _input_json(args.input)
            result = capture(args.repo, payload, args.expected_head, remote=args.remote)
            exit_code = 3 if result.get("remote_status") == "unconfirmed" else 0
        elif args.command == "rebuild-prepare":
            from .rebuild import prepare_rebuild
            result = prepare_rebuild(args.repo, args.intent_id, args.source, args.target,
                                     expected_head=args.expected_head)
            exit_code = 0
        elif args.command == "process":
            if args.rebuild_context is not None and args.related_run is not None:
                raise _CliError("rebuild_requires_fresh_processing")
            if args.review_strategy in {"auto", "decomposed-v1"} and args.revision_from_run is None:
                raise _CliError("invalid_review_strategy")
            rebuild = _rebuild_context(args)
            model_call = _model_budget(args)
            from .pipeline import process_sources
            options = {"model_call": model_call,
                       "skill_bundle": _selected_skills(args, default=args.revision_from_run is None)}
            if args.incremental or args.related_run is not None or args.revision_from_run is not None:
                options.update(related_runs=args.related_run or [], revision_from_run=args.revision_from_run)
            if rebuild is not None:
                options.update(rebuild_context=rebuild, related_runs=[])
            if args.review_strategy is not None:
                options["review_strategy"] = args.review_strategy
            result = process_sources(args.repo, args.source, args.expected_head, **options)
            exit_code = 0 if result["status"] in {"passed", "already_processed"} else 4
        elif args.command == "resume-unstarted-revision":
            from .pipeline import resume_unstarted_revision
            result = resume_unstarted_revision(args.repo, args.run, allowed_source_paths=args.source,
                model_call=_model_budget(args), related_runs=args.related_run or [], expected_head=args.expected_head,
                reservation=_input_json(args.reservation), expected_reservation_sha256=args.expected_reservation_sha256,
                review_strategy=args.review_strategy, **_reading_projection(args))
            exit_code = 0 if result["status"] in {"passed", "already_processed"} else 4
        elif args.command == "revalidate-completed-extraction":
            from .pipeline import revalidate_completed_extraction
            result = revalidate_completed_extraction(args.repo, args.run, allowed_source_paths=args.source,
                model_call=_model_budget(args), related_runs=args.related_run or [], expected_head=args.expected_head,
                attestation=_input_json(args.attestation), expected_attestation_sha256=args.expected_attestation_sha256,
                original_payload=_input_json(args.original_payload), response=_input_json(args.response),
                original_resume_context=_input_json(args.original_resume_context),
                review_strategy=args.review_strategy, **_reading_projection(args))
            exit_code = 0 if result["status"] in {"passed", "already_processed"} else 4
        elif args.command == "publish":
            from .pipeline import publish
            result = publish(args.repo, args.run, args.expected_head)
            exit_code = 0
        elif args.command == "verify-publication":
            from .pipeline import verify_publication
            result = (verify_publication(args.repo, args.run, historical=True) if args.historical
                      else verify_publication(args.repo, args.run))
            exit_code = 0
        elif args.command == "recover":
            from .transaction import PipelineGit
            git = PipelineGit(args.repo)
            with git.lock():
                result = git.recover()
            exit_code = 0
        elif args.command == "evidence":
            from .evidence import evidence, build_evidence_sources, build_current_evidence
            if args.current:
                options = {"history": args.history, **_reading_projection(args), **_object_reading_options(args)}
                if args.budget_bytes is not None:
                    options["max_bytes"] = args.budget_bytes
                result = build_current_evidence(args.repo, args.source, args.run, **options)
            else:
                if (args.history or args.budget_bytes is not None or len(args.run or []) > 1
                        or args.plan is not None or args.batch or args.object is not None or args.compact_sources):
                    raise ValueError("current_evidence_required")
                run = args.run[0] if args.run else None
                result = (evidence(args.repo, args.source[0], run) if len(args.source) == 1
                          else build_evidence_sources(args.repo, args.source, run))
            exit_code = 0
        elif args.command == "knowledge":
            from .domain_catalog import build_domain_catalog, render_domain_catalog
            options = {"domain": args.domain, **_reading_projection(args)}
            if args.budget_bytes is not None:
                options["max_bytes"] = args.budget_bytes
            result = build_domain_catalog(args.repo, args.source, args.run, **options)
            if args.format == "markdown":
                markdown_options = {"max_bytes": args.budget_bytes} if args.budget_bytes is not None else {}
                print(render_domain_catalog(result, **markdown_options), end="")
                return 0
            exit_code = 0
        elif args.command == "ask":
            model_call = _model_budget(args)
            from .evidence import answer, ask_sources, ask_current
            if args.current:
                options = {"history": args.history, "model_call": model_call,
                           **_reading_projection(args), **_object_reading_options(args)}
                if args.budget_bytes is not None:
                    options["max_bytes"] = args.budget_bytes
                result = ask_current(args.repo, args.source, args.question, args.run, **options)
            else:
                if (args.history or args.budget_bytes is not None or len(args.run or []) > 1
                        or args.plan is not None or args.batch or args.object is not None or args.compact_sources):
                    raise ValueError("current_evidence_required")
                run = args.run[0] if args.run else None
                result = (answer(args.repo, args.source[0], args.question, run, model_call=model_call)
                          if len(args.source) == 1 else ask_sources(
                              args.repo, args.source, args.question, run, model_call=model_call))
            exit_code = 0
        elif args.command == "budget":
            from .budget import BudgetLedger
            if args.budget_command == "create" and args.mode == "budget" and args.limit is None:
                raise _CliError("budget_limit_required")
            ledger = (BudgetLedger.create(args.ledger, args.limit, args.initial_used,
                                          args.authorization_ref, repo=args.repo, mode=args.mode)
                      if args.budget_command == "create" else BudgetLedger(args.ledger, repo=args.repo))
            result = ledger.snapshot()
            exit_code = 0
        elif args.command == "control":
            from .controls import plan_control, record_control
            action = plan_control if args.control_command == "plan" else record_control
            result = action(args.repo, _input_json(args.input), args.expected_head)
            exit_code = 0
        elif args.command == "pending":
            from .pending import list_pending, register_pending, answer_pending, resolve_pending
            if args.pending_command == "list":
                result = list_pending(args.repo, status=args.status)
            elif args.pending_command == "register":
                result = register_pending(args.repo, _input_json(args.input), args.expected_head)
            elif args.pending_command == "answer":
                result = answer_pending(args.repo, args.item, args.source, args.expected_head)
            elif args.pending_command == "repair-registration":
                from .pipeline import resume_pending_registration
                result = resume_pending_registration(args.repo, args.run, allowed_source_paths=args.source,
                                                     expected_head=args.expected_head)
            else:
                result = resolve_pending(args.repo, args.item, args.run, args.expected_head)
            exit_code = 4 if isinstance(result, dict) and result.get("status") == "registration_required" else 0
        elif args.command == "schedule":
            from .scheduler import configure_schedule, plan_daily, run_daily, drain_schedule, schedule_status, pause_schedule, resume_schedule, abandon_attempt
            if args.schedule_command == "configure":
                config = _input_json(args.input)
                bundle = _selected_skills(args)
                if bundle is not None:
                    if "skill_bundle" in config and config["skill_bundle"] != bundle:
                        raise _CliError("conflicting_skill_configuration")
                    config = {**config, "skill_bundle": bundle}
                result = configure_schedule(args.repo, config, args.expected_head, use_default_skills=True)
            elif args.schedule_command == "plan":
                result = plan_daily(args.repo, args.id, now=args.now)
            elif args.schedule_command in {"run", "drain"}:
                model_call = _model_budget(args)
                if args.schedule_command == "drain":
                    continuation = _input_json(args.continuation) if args.continuation else None
                    result = drain_schedule(args.repo, args.id, _input_json(args.trigger), args.expected_head,
                                            model_call=model_call, max_slices=args.max_slices,
                                            window_seconds=args.window_seconds, continuation=continuation)
                else:
                    result = run_daily(args.repo, args.id, _input_json(args.trigger), args.expected_head,
                                       model_call=model_call)
            elif args.schedule_command == "status":
                observation = _input_json(args.platform_observation) if args.platform_observation else None
                result = schedule_status(args.repo, args.id, now=args.now, platform_observation=observation)
            elif args.schedule_command == "pause":
                result = pause_schedule(args.repo, args.id, args.reason, args.expected_head)
            elif args.schedule_command == "resume":
                result = resume_schedule(args.repo, args.id, args.validation_ref, args.expected_head)
            else:
                result = abandon_attempt(args.repo, args.id, args.attempt, args.reason, args.expected_head,
                                         validation_ref=args.validation_ref, execution_stopped=args.execution_stopped)
            exit_code = 4 if result.get("status") in {
                "failed", "incomplete", "budget_exhausted", "budget_blocked", "remote_unconfirmed"} else 0
            if args.schedule_command in {"run", "drain"} and (
                    result.get("status") in {"unknown", "reconciliation_required", "registration_required",
                                             "blocked_capacity", "blocked_context", "attempt_limit", "attention_required"}
                    or result.get("next_action") in {"reconcile", "repair_accounting", "repair_registration", "attention_required"}):
                exit_code = 4
        elif args.command == "environment":
            from .runtime import bind_environment, require_environment
            if args.environment_command == "bind":
                result = bind_environment(args.repo, _input_json(args.input))
            else:
                binding = require_environment(args.repo, require_binding=args.require_binding, remote=args.remote)
                result = {"status": "verified" if binding else "unbound", "binding": binding}
            exit_code = 0
        elif args.command == "runner":
            from .runtime import probe_runner
            refs = _input_json(args.receipts) if args.receipts else None
            result = probe_runner(args.repo, _input_json(args.input), receipt_refs=refs)
            exit_code = 0 if result["status"] == "passed" else 4
        elif args.command == "promotion":
            from .runtime import promotion_check, prepare_promotion
            result = (prepare_promotion(args.repo, args.base, args.dev_commit) if args.promotion_command == "prepare"
                      else promotion_check(args.repo, args.base, args.candidate, dev_commit=args.dev_commit))
            exit_code = 0 if result["status"] in {"passed", "prepared"} else 4
        elif args.command == "context":
            from .context import discover_context, plan_answered_pending, execute_continuation
            if args.context_command == "discover":
                result = discover_context(args.repo, args.source, allowed_source_paths=args.allow_source,
                                          allowed_run_ids=args.allow_run, object_ids=args.object_id,
                                          topic_ids=args.topic_id, max_sources=args.max_sources, max_runs=args.max_runs)
                exit_code = 0 if result["coverage"]["complete"] else 4
            elif args.continuation_command == "plan":
                result = plan_answered_pending(args.repo, allowed_source_paths=args.allow_source,
                                               allowed_run_ids=args.allow_run, max_sources=args.max_sources,
                                               max_runs=args.max_runs)
                exit_code = 0 if result["coverage"]["complete"] else 4
            else:
                from .pipeline import process_sources
                job = _input_json(args.input)
                if not isinstance(job, dict) or not isinstance(job.get("job_id"), str):
                    raise _CliError("invalid_continuation_job")
                if args.operation_id is not None and args.operation_id != job["job_id"]:
                    raise _CliError("continuation_operation_mismatch")
                args.operation_id = job["job_id"]
                model_call = _model_budget(args)
                bundle = _selected_skills(args, default=True)
                def process_reply(repo, fixed_job):
                    return process_sources(repo, fixed_job["source_paths"], fixed_job["head"],
                                           related_runs=fixed_job["related_runs"], model_call=model_call,
                                           skill_bundle=bundle)
                result = execute_continuation(args.repo, job, process_call=process_reply)
                exit_code = 0 if result.get("resolved") else 4
        elif args.command == "batch":
            from .batching import plan_batches, project_batch_sources
            if args.batch_command == "plan":
                result = plan_batches(args.repo, args.source, max_input_bytes=args.max_input_bytes,
                                      payload_overhead_bytes=args.overhead_bytes, max_sources=args.max_sources,
                                      expected_head=args.expected_head, purpose=args.purpose)
                exit_code = 0 if result["status"] == "ready" else 4
            else:
                result = project_batch_sources(args.repo, _input_json(args.plan), args.batch_id,
                                               source_paths=args.source, max_bytes=args.budget_bytes,
                                               fragment_ids=args.fragment)
                exit_code = 0 if result["coverage"]["complete"] else 4
        elif args.command == "production":
            from .production import prepare_production, production_status, run_production
            if args.production_command == "prepare":
                if args.rebuild_context is not None and args.allowed_run:
                    raise _CliError("rebuild_requires_fresh_processing")
                rebuild = _rebuild_context(args)
                result = prepare_production(args.repo, args.id, args.source,
                    allowed_source_paths=args.allowed_source, allowed_run_ids=args.allowed_run,
                    max_input_bytes=args.max_input_bytes, payload_overhead_bytes=args.payload_overhead,
                    skill_bundle=_selected_skills(args), use_default_skills=True,
                    **({"review_strategy": args.review_strategy} if args.review_strategy is not None else {}),
                    **({"rebuild_context": rebuild} if rebuild is not None else {}))
                exit_code = 0 if result["status"] in {"ready", "published"} else 4
            elif args.production_command == "status":
                result = production_status(args.repo, args.id)
                exit_code = 0
            else:
                model_call = _model_budget(args)
                result = run_production(args.repo, args.id, model_call=model_call,
                                        max_steps=args.max_steps, require_binding=not args.local_only)
                exit_code = 0 if result["status"] in {"ready", "published"} else 4
            result = {**result, "delivery": {"scope": "local_only", "remote_status": "not_requested"}}
        elif args.command == "reading-package":
            from .portable import prepare_reading_package, verify_reading_package
            if args.package_command == "prepare":
                result = prepare_reading_package(args.repo, args.source, _input_json(args.questions),
                                                args.run, output=args.output, **_reading_projection(args))
            else:
                result = verify_reading_package(args.artifact, expected_sha256=args.expected_sha256)
            exit_code = 0
        elif args.release_command == "build":
            from .release import build_release
            result = build_release(args.repo, args.ref, args.output)
            exit_code = 0
        else:
            from .release import verify_release
            result = verify_release(
                args.artifact, expected_commit=args.expected_commit,
                expected_package_version=args.expected_package_version,
                expected_artifact_sha256=args.expected_artifact_sha256,
                expected_rules_version=args.expected_rules_version,
            )
            exit_code = 0
    except BudgetBlocked as error:
        result = {"ok": False, "error": error.code, "attempted_calls": error.attempted_calls,
                  "operation_started_calls": error.started_count}
        print(json.dumps(result, ensure_ascii=False), file=sys.stderr)
        return 2
    except Exception as error:
        # Avoid echoing submitted source bodies or remote credential-bearing URLs.
        result = {"ok": False, "error": getattr(error, "code", type(error).__name__)}
        print(json.dumps(result, ensure_ascii=False), file=sys.stderr)
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return exit_code
