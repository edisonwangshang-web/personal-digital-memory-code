"""Versioned classification policy and conservative legacy display aliases."""

from .contracts import CaptureError


DOMAIN_LABELS = {"health": "健康&运动", "career": "工作事业", "learning": "学习&能力提升"}
DOMAIN_POLICY_VERSION = "three-domains-v2"
DOMAIN_POLICY_VERSIONS = ("three-domains-v1", "three-domains-v2")
_LEGACY_ALIASES = {
    "健康": "health", "事业": "career", "学习": "learning",
}


def domain_policy(version=None):
    """Return the exact registered policy; normal Review checks classification."""
    version = DOMAIN_POLICY_VERSION if version is None else version
    if not isinstance(version, str) or version not in DOMAIN_POLICY_VERSIONS:
        raise CaptureError("invalid_domain_policy", "领域策略版本未注册")
    definitions = {
        "health": "健康状态、运动、体能、睡眠及其监控与管理；佳明监控资料属于本领域。",
        "career": "工作、职业、事业发展与实际项目中的职责、决策和执行。",
        "learning": "学习活动、知识掌握、技能训练与能力提升；阅读收藏不等于掌握。",
    }
    rules = ["新建或实质更新的记录只使用三个稳定id，可零个或多个，不重复。",
             "同一事实只保存一份，跨领域共享记录，不为归类复制事实。",
             "无关或证据不足时允许空列表，不强制归类，不补造用户目标或决定。",
             "精确复用旧受审对象时逐字保留原字段、正文和旧标签，不为分类改写旧对象。",
             "领域属于受审内容，现有提取与独立Review核对归属，不追加分类模型调用。"]
    if version == "three-domains-v2":
        definitions = {
            "health": "健康状态、运动、体能、睡眠及其监控与管理，也包括为健康管理整理的参考资料；不要求用户已经采用。",
            "career": "工作、职业、事业发展、实际项目及为工作整理的专业方案、行业研究和业务方法等参考知识；不要求用户已经实施。",
            "learning": "学习活动、知识掌握、技能训练、能力提升及其参考资料；阅读收藏不等于掌握。",
        }
        rules = [
            rules[0], rules[1], rules[2],
            "结合当前获准原文的材料语义和用户明确的知识组织用途归类，区分资料观点、用户整理用途和用户本人已经发生的事实。",
            "为工作参考整理的专业材料可以归career，不必证明用户本人已执行材料中的方案；为健康管理或学习提升整理的参考资料同样按相应用途和材料语义归类。",
            "阅读、PDF或收藏只是资料形式，不自动决定为learning；材料确有多个领域的用途或语义时允许多个标签。",
            "用户已明确相关资料的归档领域且内容与之相关时，应在资料的实质知识记录中体现这一领域，必要时与其他领域并列；不能只给新增用途或收藏记录打标而让相关内容继续缺少该领域。",
            "当次收藏、归档或放入某知识库的组织要求可以帮助归类，但不能自动加工成用户的长期目标、持续计划、已经掌握或已经实施的事实。",
            "精确复用前检查旧领域标签在当前上下文中是否仍适用；新资料或用户明确用途使旧标签不足时，应按现有实质更新流程提交完整候选并重新独立Review，不能声称精确复用。",
            "上下文仍适用且无需修改的旧受审对象才可精确复用，逐字保留原字段、正文和旧标签；同一事实不为重新归类复制成另一条事实。",
            rules[4],
        ]
    return {"version": version,
            "domains": [{"id": key, "label": label, "definition": definitions[key]}
                        for key, label in DOMAIN_LABELS.items()],
            "rules": rules,
            "legacy_aliases": dict(_LEGACY_ALIASES)}


def domain_ids(labels, allow_legacy=True):
    """Map known labels to ordered IDs; unknown legacy tags remain unclassified.

    This display helper does not validate fresh model output. In particular,
    generic tags such as 数据质量 do not imply a domain without other evidence.
    """
    if (not isinstance(labels, list) or any(not isinstance(label, str) for label in labels)
            or type(allow_legacy) is not bool):
        raise CaptureError("invalid_domains", "领域标签必须是字符串列表")
    selected = {label if label in DOMAIN_LABELS else _LEGACY_ALIASES.get(label) if allow_legacy else None
                for label in labels}
    return [key for key in DOMAIN_LABELS if key in selected]
