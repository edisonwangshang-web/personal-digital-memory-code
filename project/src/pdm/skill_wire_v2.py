"""Versioned, lossless field-key dictionary around the unchanged v1 wire."""

from collections import Counter
import re

from . import skill_wire as v1


FORMAT = "pdm-keyed-shared-json-v2"
MAX_KEYS = 10_000
MAX_WIRE_BYTES = 2 * v1.MAX_BYTES
MAX_WIRE_DEPTH = 2 * v1.MAX_DEPTH + 4
MAX_WIRE_NODES = 2 * v1.MAX_NODES + 4
_ALIAS = re.compile(r"~[0-9a-z]{1,8}\Z")

WIRE_INSTRUCTIONS = (
    "下方资料外层采用无损键字典 pdm-keyed-shared-json-v2：keys 是编码键到完整原字段名的字典，"
    "value 保存完整的内层 pdm-shared-json-v1 对象。先递归恢复 value 中所有对象的键名："
    "键若以 ~~ 开头，仅去掉第一个 ~；否则若匹配 keys，则替换为其完整原名；其他键保持原样。"
    "只替换对象键，不替换任何字符串值，不改变数组顺序或数值。原 ~ 开头键已通过 ~~ 转义，"
    "字典原名恢复一次后不再次解释。外层 keys/value 与内层 shared 的引用 ID 是不同层次，不能混用。"
    "完成全部键名恢复后，所得对象才按下面的 pdm-shared-json-v1 规则解共享引用。"
    "所有原字段和原文都完整保留；输出必须使用原字段、原值和原引文，不返回编码键。"
)


def _walk_keys(value, transform):
    """Traverse a known-valid v1 encoding without changing any scalar value."""
    if type(value) is dict:
        return {transform(key): _walk_keys(child, transform) for key, child in value.items()}
    if type(value) is list:
        return [_walk_keys(child, transform) for child in value]
    return value


def encode_shared_json_v2(value):
    """Keep the v1 algorithm intact, then share only profitable field names."""
    wire = v1.encode_shared_json(value)
    counts = Counter()

    def count(key):
        counts[key] += 1
        return key

    _walk_keys(wire, count)
    aliases = {}
    for key in sorted(counts, key=lambda item: (-(len(v1._canonical(item)) - 3) * counts[item], item)):
        alias = "~" + v1._base36(len(aliases))
        # Include the complete definition, separator and actual alias spelling.
        benefit = (counts[key] * (len(v1._canonical(key)) - len(v1._canonical(alias)))
                   - len(v1._canonical(alias)) - len(v1._canonical(key)) - 2)
        if benefit > 0 and len(aliases) < MAX_KEYS:
            aliases[key] = alias

    def encode_key(key):
        return aliases.get(key, "~" + key if key.startswith("~") else key)

    result = {"format": FORMAT, "keys": {alias: key for key, alias in aliases.items()},
              "value": _walk_keys(wire, encode_key)}
    if v1._canonical(decode_shared_json_v2(result)) != v1._canonical(value):
        raise v1.SharedJSONError("shared_json_roundtrip_mismatch")
    return result


def decode_shared_json_v2(value):
    """Restore all keys first; only then let the frozen v1 decoder read refs."""
    if (type(value) is not dict or set(value) != {"format", "keys", "value"}
            or value["format"] != FORMAT or type(value["keys"]) is not dict
            or len(value["keys"]) > MAX_KEYS):
        raise v1.SharedJSONError("invalid_shared_json")
    definitions = value["keys"]
    if any(type(alias) is not str or not _ALIAS.fullmatch(alias) or type(key) is not str
           for alias, key in definitions.items()):
        raise v1.SharedJSONError("invalid_shared_json_key_dictionary")
    if len(set(definitions.values())) != len(definitions):
        raise v1.SharedJSONError("duplicate_shared_json_key_definition")
    if len(v1._canonical(value)) > MAX_WIRE_BYTES:
        raise v1.SharedJSONError("shared_json_limit")
    used, active = set(), set()
    nodes, remaining = 0, MAX_WIRE_BYTES

    def spend(amount):
        nonlocal remaining
        remaining -= amount
        if remaining < 0:
            raise v1.SharedJSONError("shared_json_limit")

    def restore(item, depth):
        nonlocal nodes
        nodes += 1
        if depth > MAX_WIRE_DEPTH or nodes > MAX_WIRE_NODES:
            raise v1.SharedJSONError("shared_json_limit")
        kind = type(item)
        if kind in (dict, list):
            if id(item) in active:
                raise v1.SharedJSONError("shared_json_cycle")
            active.add(id(item))
            spend(2 + max(len(item) - 1, 0))
            try:
                if kind is list:
                    return [restore(child, depth + 1) for child in item]
                result = {}
                for key, child in item.items():
                    if type(key) is not str:
                        raise v1.SharedJSONError("invalid_shared_json")
                    if key.startswith("~~"):
                        original = key[1:]
                    elif key.startswith("~"):
                        if key not in definitions:
                            raise v1.SharedJSONError("invalid_shared_json_key_reference")
                        used.add(key)
                        original = definitions[key]
                    else:
                        original = key
                    if original in result:
                        raise v1.SharedJSONError("duplicate_shared_json_restored_key")
                    spend(len(v1._canonical(original)) + 1)
                    result[original] = restore(child, depth + 1)
                return result
            finally:
                active.remove(id(item))
        if kind not in (str, int, float, bool, type(None)):
            raise v1.SharedJSONError("invalid_shared_json")
        spend(len(v1._canonical(item)))
        return item

    restored = restore(value["value"], 0)
    if used != set(definitions):
        raise v1.SharedJSONError("unused_shared_json_key_definition")
    return v1._decode_shared_json_v1(restored)
