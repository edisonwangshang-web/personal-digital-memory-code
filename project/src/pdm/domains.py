"""Versioned classification policy and conservative legacy display aliases."""

from .contracts import CaptureError


DOMAIN_LABELS = {"health": "健康&运动", "career": "工作事业", "learning": "学习&能力提升"}
DOMAIN_POLICY_VERSION = "three-domains-v1"
_LEGACY_ALIASES = {
    "健康": "health", "事业": "career", "学习": "learning",
}


def domain_policy():
    """Return a fresh JSON-only policy; classification is part of normal Review."""
    definitions = {
        "health": "健康状态、运动、体能、睡眠及其监控与管理；佳明监控资料属于本领域。",
        "career": "工作、职业、事业发展与实际项目中的职责、决策和执行。",
        "learning": "学习活动、知识掌握、技能训练与能力提升；阅读收藏不等于掌握。",
    }
    return {"version": DOMAIN_POLICY_VERSION,
            "domains": [{"id": key, "label": label, "definition": definitions[key]}
                        for key, label in DOMAIN_LABELS.items()],
            "rules": ["新建或实质更新的记录只使用三个稳定id，可零个或多个，不重复。",
                      "同一事实只保存一份，跨领域共享记录，不为归类复制事实。",
                      "无关或证据不足时允许空列表，不强制归类，不补造用户目标或决定。",
                      "精确复用旧受审对象时逐字保留原字段、正文和旧标签，不为分类改写旧对象。",
                      "领域属于受审内容，现有提取与独立Review核对归属，不追加分类模型调用。"],
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
