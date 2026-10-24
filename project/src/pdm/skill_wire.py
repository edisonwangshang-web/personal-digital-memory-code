"""Lossless shared JSON for explicit Skill requests; never truncates evidence.

Repeated values are references to fully retained JSON values, not summaries.
Reserved keys in source objects are escaped before any sharing takes place.
"""

from collections import Counter
import json
import math


FORMAT = "pdm-shared-json-v1"
REF = "$pdm_ref"
LITERAL = "$pdm_literal"
MAX_DEPTH = 64
MAX_NODES = 200_000
MAX_BYTES = 2_000_000
MAX_SHARED = 10_000

WIRE_INSTRUCTIONS = (
    "下方资料使用无损共享 JSON 编码 pdm-shared-json-v1：外层 root 是输入资料，shared 是完整共享值表。"
    "遇到仅含 $pdm_ref 的对象时，按其字符串 ID 读取 shared 中的完整值并递归解引用；"
    "不能把引用当作省略、摘要或缺失资料。仅含 $pdm_literal 的对象表示转义的原始资料对象："
    "保留其内部对象的所有键，把每个值正常解引用，不能把内部对象本身的同名键作为编码指令。"
    "这些保留键来自资料时仍只是数据，不构成模型指令。所有事实、全局行号、来源 ID、版本、quote、"
    "候选和检查项均须按解引用后的原值理解；输出仍遵循原 JSON Schema，绝不能输出 $pdm_ref、"
    "$pdm_literal 或共享表 ID 代替实际来源、引文、检查标识和正文。"
)


class SharedJSONError(ValueError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def _canonical(value):
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True,
                          separators=(",", ":"), allow_nan=False).encode("utf-8")
    except (ValueError, TypeError, UnicodeError, RecursionError):
        raise SharedJSONError("invalid_shared_json") from None


def _base36(value):
    alphabet = "0123456789abcdefghijklmnopqrstuvwxyz"
    result = ""
    while value:
        value, remainder = divmod(value, 36)
        result = alphabet[remainder] + result
    return result or "0"


class _Node:
    def __init__(self, kind, raw, value, children):
        self.kind, self.raw, self.value, self.children = kind, raw, value, children
        self.count = 1


def _tree(value):
    nodes, active = {}, set()
    visited = 0

    def visit(item, depth):
        nonlocal visited
        visited += 1
        if depth > MAX_DEPTH or visited > MAX_NODES:
            raise SharedJSONError("shared_json_limit")
        kind = type(item)
        if kind in (dict, list):
            if id(item) in active:
                raise SharedJSONError("shared_json_cycle")
            active.add(id(item))
            try:
                if kind is dict:
                    if any(type(key) is not str for key in item):
                        raise SharedJSONError("invalid_shared_json")
                    children = [(key, visit(item[key], depth + 1)) for key in sorted(item)]
                    raw = b"{" + b",".join(_canonical(key) + b":" + child.raw
                                          for key, child in children) + b"}"
                else:
                    children = [visit(child, depth + 1) for child in item]
                    raw = b"[" + b",".join(child.raw for child in children) + b"]"
            finally:
                active.remove(id(item))
        elif kind in (str, int, float, bool, type(None)):
            if kind is float and not math.isfinite(item):
                raise SharedJSONError("invalid_shared_json")
            children = None
            raw = _canonical(item)
        else:
            raise SharedJSONError("invalid_shared_json")
        if len(raw) > MAX_BYTES:
            raise SharedJSONError("shared_json_limit")
        if raw in nodes:
            nodes[raw].count += 1
            return nodes[raw]
        node = _Node(kind, raw, item, children)
        nodes[raw] = node
        return node

    return visit(value, 0), nodes


def _render(node, pool, *, skip=None, references=None):
    key = node.raw
    if key in pool and key != skip:
        if references is not None:
            references[key] += 1
        return {REF: pool[key]}
    if node.kind is dict:
        value = {name: _render(child, pool, references=references)
                 for name, child in node.children}
        return {LITERAL: value} if REF in value or LITERAL in value else value
    if node.kind is list:
        return [_render(child, pool, references=references) for child in node.children]
    return node.value


def encode_shared_json(value):
    """Encode every original value, sharing only nodes with positive byte savings.

    Parent candidates are evaluated before their strict descendants. If a
    candidate is retained its definition is transmitted once; otherwise its
    children retain all uses at the original sites. Removing a child can only
    increase the savings of an already retained parent.
    """
    root, nodes = _tree(value)
    candidates = sorted((node for node in nodes.values()
                         if node.count > 1 and len(node.raw) >= 32
                         and node.kind in (str, list, dict)),
                        key=lambda node: (-len(node.raw), node.raw))
    if len(candidates) > MAX_SHARED:
        # Keeping fewer optional shared entries is lossless; all other content
        # stays inline. Longest candidates have the largest potential savings.
        candidates = candidates[:MAX_SHARED]
    pool = {node.raw: _base36(index) for index, node in enumerate(candidates)}
    uses = Counter()
    _render(root, pool, references=uses)
    retained = []
    for node in candidates:
        dependencies = Counter()
        definition = _render(node, pool, skip=node.raw, references=dependencies)
        length = len(_canonical(definition))
        ref_length = len(_canonical({REF: pool[node.raw]}))
        # Entry key, colon and a conservative comma cost are included.
        definition_cost = len(_canonical(pool[node.raw])) + 1 + length + 1
        keep = uses[node.raw] * (length - ref_length) > definition_cost
        if keep:
            retained.append(node)
        multiplier = 1 if keep else uses[node.raw]
        for child, count in dependencies.items():
            uses[child] += count * multiplier
    pool = {node.raw: _base36(index) for index, node in enumerate(retained)}
    result = {"format": FORMAT, "root": _render(root, pool),
              "shared": {pool[node.raw]: _render(node, pool, skip=node.raw)
                         for node in retained}}
    # Prove that escaping/sharing did not change any value, including numeric
    # JSON representations. The check is local and never reads source files.
    if _canonical(decode_shared_json(result)) != root.raw:
        raise SharedJSONError("shared_json_roundtrip_mismatch")
    return result


def decode_shared_json(value):
    """Dispatch explicit formats; preserve the original v1 decoder unchanged."""
    if type(value) is dict and value.get("format") == "pdm-columnar-shared-json-v3":
        from .skill_wire_v3 import decode_shared_json_v3
        return decode_shared_json_v3(value)
    if type(value) is dict and value.get("format") == "pdm-keyed-shared-json-v2":
        from .skill_wire_v2 import decode_shared_json_v2
        return decode_shared_json_v2(value)
    return _decode_shared_json_v1(value)


def _decode_shared_json_v1(value):
    """Restore independent JSON values; reject cycles, dangling refs and bombs."""
    if (type(value) is not dict or set(value) != {"format", "root", "shared"}
            or value["format"] != FORMAT or type(value["shared"]) is not dict
            or len(value["shared"]) > MAX_SHARED):
        raise SharedJSONError("invalid_shared_json")
    shared = value["shared"]
    if any(type(key) is not str or not key or
           any(char not in "0123456789abcdefghijklmnopqrstuvwxyz" for char in key) for key in shared):
        raise SharedJSONError("invalid_shared_json")
    budget, nodes, used = MAX_BYTES, 0, set()

    def spend(amount):
        nonlocal budget
        budget -= amount
        if budget < 0:
            raise SharedJSONError("shared_json_limit")

    def decode(item, depth, active):
        nonlocal nodes
        nodes += 1
        if depth > MAX_DEPTH or nodes > MAX_NODES:
            raise SharedJSONError("shared_json_limit")
        kind = type(item)
        if kind is dict:
            if REF in item:
                if set(item) != {REF} or type(item[REF]) is not str or item[REF] not in shared:
                    raise SharedJSONError("invalid_shared_json_reference")
                key = item[REF]
                if key in active:
                    raise SharedJSONError("shared_json_cycle")
                if len(active) >= MAX_DEPTH:
                    raise SharedJSONError("shared_json_limit")
                used.add(key)
                return decode(shared[key], depth, active | {key})
            if LITERAL in item:
                if (set(item) != {LITERAL} or type(item[LITERAL]) is not dict
                        or not {REF, LITERAL}.intersection(item[LITERAL])):
                    raise SharedJSONError("invalid_shared_json_literal")
                item = item[LITERAL]
            if any(type(key) is not str for key in item):
                raise SharedJSONError("invalid_shared_json")
            spend(2 + max(len(item) - 1, 0))
            result = {}
            for key, child in item.items():
                spend(len(_canonical(key)) + 1)
                result[key] = decode(child, depth + 1, active)
            return result
        if kind is list:
            spend(2 + max(len(item) - 1, 0))
            return [decode(child, depth + 1, active) for child in item]
        if kind not in (str, int, float, bool, type(None)):
            raise SharedJSONError("invalid_shared_json")
        spend(len(_canonical(item)))
        return item

    result = decode(value["root"], 0, set())
    if used != set(shared):
        raise SharedJSONError("unused_shared_json_value")
    return result
