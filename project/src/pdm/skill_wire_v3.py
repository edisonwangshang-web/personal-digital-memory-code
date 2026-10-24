"""Lossless homogeneous object-array columns around the unchanged v2 wire."""

from . import skill_wire as v1
from . import skill_wire_v2 as v2


FORMAT = "pdm-columnar-shared-json-v3"
TABLE = "$pdm_table"
LITERAL = "$pdm_table_literal"
MAX_ENCODED_BYTES = 2 * v2.MAX_WIRE_BYTES
MAX_RESTORED_BYTES = v2.MAX_WIRE_BYTES
MAX_DEPTH = v2.MAX_WIRE_DEPTH + 4
MAX_NODES = v2.MAX_WIRE_NODES + v2.MAX_KEYS + 4

WIRE_INSTRUCTIONS = (
    "下方外层为无损列式 pdm-columnar-shared-json-v3，其 value 保存完整 v2 对象。"
    "先还原列式数组：仅含 $pdm_table、keys、rows 且 $pdm_table 为整数 1 的对象表示表；"
    "keys 保存完整列名，按顺序把 rows 每一行的全部值与 keys 配对还原为对象，保持行序。"
    "每个单元格递归还原；空值与缺字段不同，不补值、不丢列。仅含 $pdm_table_literal 的对象"
    "是原资料对象转义：保留内部对象所有键，仅递归还原各值，不把内部同名键再次当标记。"
    "完成后得到完整 pdm-keyed-shared-json-v2，再恢复字段键名，最后按 v1 解共享引用。"
    "表列名仍是 v2 的完整编码字段名，不是最终输出字段；所有原文、候选与反馈均完整保留。"
    "输出使用原 JSON Schema 和原值，不输出列式标记、键别名或共享引用。"
)


def _columnar(value):
    if type(value) is dict:
        result = {key: _columnar(child) for key, child in value.items()}
        return {LITERAL: result} if {TABLE, LITERAL}.intersection(value) else result
    if type(value) is list:
        inline = [_columnar(child) for child in value]
        if len(value) > 1 and all(type(row) is dict for row in value):
            keys = sorted(value[0])
            if all(set(row) == set(keys) for row in value):
                rows = []
                for original, encoded in zip(value, inline):
                    fields = encoded[LITERAL] if {TABLE, LITERAL}.intersection(original) else encoded
                    rows.append([fields[key] for key in keys])
                table = {TABLE: 1, "keys": keys, "rows": rows}
                if len(v1._canonical(table)) < len(v1._canonical(inline)):
                    return table
        return inline
    return value


def encode_shared_json_v3(value):
    """Only factor equal complete key sets; never fill missing fields or rows."""
    wire = v2.encode_shared_json_v2(value)
    result = {"format": FORMAT, "value": _columnar(wire)}
    restored = _restore_v2(result)
    if (v1._canonical(restored) != v1._canonical(wire)
            or v1._canonical(v2.decode_shared_json_v2(restored)) != v1._canonical(value)):
        raise v1.SharedJSONError("shared_json_roundtrip_mismatch")
    return result


def _restore_v2(value):
    if (type(value) is not dict or set(value) != {"format", "value"}
            or value["format"] != FORMAT):
        raise v1.SharedJSONError("invalid_shared_json")
    if len(v1._canonical(value)) > MAX_ENCODED_BYTES:
        raise v1.SharedJSONError("shared_json_limit")
    active = set()
    nodes, remaining = 0, MAX_RESTORED_BYTES

    def count(depth):
        nonlocal nodes
        nodes += 1
        if depth > MAX_DEPTH or nodes > MAX_NODES:
            raise v1.SharedJSONError("shared_json_limit")

    def spend(amount):
        nonlocal remaining
        remaining -= amount
        if remaining < 0:
            raise v1.SharedJSONError("shared_json_limit")

    def object_fields(fields, depth):
        spend(2 + max(len(fields) - 1, 0))
        result = {}
        for key, child in fields.items():
            if type(key) is not str:
                raise v1.SharedJSONError("invalid_shared_json")
            spend(len(v1._canonical(key)) + 1)
            result[key] = restore(child, depth + 1)
        return result

    def restore(item, depth):
        count(depth)
        kind = type(item)
        if kind in (dict, list):
            if id(item) in active:
                raise v1.SharedJSONError("shared_json_cycle")
            active.add(id(item))
            try:
                if kind is list:
                    spend(2 + max(len(item) - 1, 0))
                    return [restore(child, depth + 1) for child in item]
                if TABLE in item:
                    if (set(item) != {TABLE, "keys", "rows"}
                            or type(item[TABLE]) is not int or item[TABLE] != 1
                            or type(item["keys"]) is not list or type(item["rows"]) is not list
                            or any(type(key) is not str for key in item["keys"])
                            or len(set(item["keys"])) != len(item["keys"])):
                        raise v1.SharedJSONError("invalid_shared_json_table")
                    keys, rows = item["keys"], item["rows"]
                    spend(2 + max(len(rows) - 1, 0))
                    result = []
                    for row in rows:
                        if type(row) is not list or len(row) != len(keys):
                            raise v1.SharedJSONError("invalid_shared_json_table_row")
                        count(depth + 1)
                        spend(2 + max(len(keys) - 1, 0))
                        record = {}
                        for key, child in zip(keys, row):
                            spend(len(v1._canonical(key)) + 1)
                            record[key] = restore(child, depth + 2)
                        result.append(record)
                    return result
                if LITERAL in item:
                    if (set(item) != {LITERAL} or type(item[LITERAL]) is not dict
                            or not {TABLE, LITERAL}.intersection(item[LITERAL])):
                        raise v1.SharedJSONError("invalid_shared_json_table_literal")
                    return object_fields(item[LITERAL], depth)
                return object_fields(item, depth)
            finally:
                active.remove(id(item))
        if kind not in (str, int, float, bool, type(None)):
            raise v1.SharedJSONError("invalid_shared_json")
        spend(len(v1._canonical(item)))
        return item

    return restore(value["value"], 0)


def decode_shared_json_v3(value):
    """Fully restore columns, then apply the unchanged v2 and v1 decoders."""
    return v2.decode_shared_json_v2(_restore_v2(value))
