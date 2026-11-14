"""Bounded XLSX writer for this seed's fixed output artifact, without code execution."""
import io
import math

SHEETS = {"By State", "Overall"}
MAX_CELLS = 4096
PARAMETERS = {"type": "object", "properties": {"sheets": {"type": "array", "minItems": 2, "maxItems": 2,
    "items": {"type": "object", "properties": {"name": {"type": "string", "enum": sorted(SHEETS)},
              "rows": {"type": "array", "minItems": 1, "maxItems": 512, "items": {"type": "array", "maxItems": 16,
                       "items": {"type": ["string", "number", "boolean", "null"]}}}},
              "required": ["name", "rows"], "additionalProperties": False}}}, "required": ["sheets"], "additionalProperties": False}


def validate_workbook(value):
    if not isinstance(value, dict) or set(value) != {"sheets"}:
        raise ValueError("Workbook accepts only sheets")
    sheets = value["sheets"]
    if not isinstance(sheets, list) or len(sheets) != 2:
        raise ValueError("This task needs exactly two sheets")
    seen, count = set(), 0
    for sheet in sheets:
        if not isinstance(sheet, dict) or set(sheet) != {"name", "rows"} or sheet["name"] not in SHEETS or sheet["name"] in seen:
            raise ValueError("Invalid or duplicate fixed task sheet")
        seen.add(sheet["name"])
        rows = sheet["rows"]
        if not isinstance(rows, list) or not 1 <= len(rows) <= 512:
            raise ValueError("Sheet row budget exceeded")
        for row in rows:
            if not isinstance(row, list) or not 1 <= len(row) <= 16:
                raise ValueError("Sheet column budget exceeded")
            count += len(row)
            if count > MAX_CELLS:
                raise ValueError("Workbook cell budget exceeded")
            for value in row:
                if value is None or type(value) is bool: continue
                if type(value) in (int, float) and abs(value) <= 10**15 and math.isfinite(value): continue
                if isinstance(value, str) and len(value) <= 2000 and not value.startswith("=") and all(ord(c) >= 32 or c in "\t\r\n" for c in value): continue
                raise ValueError("Cell must be a bounded literal value")
    return value


def render_workbook(value):
    validate_workbook(value)
    from openpyxl import Workbook
    workbook = Workbook()
    workbook.remove(workbook.active)
    for entry in value["sheets"]:
        sheet = workbook.create_sheet(entry["name"])
        for row in entry["rows"]: sheet.append(row)
    output = io.BytesIO()
    workbook.save(output)
    workbook.close()
    raw = output.getvalue()
    if len(raw) > 2 * 1024 * 1024:
        raise ValueError("Rendered workbook exceeds artifact budget")
    return raw
