"""Bounded literal XLSX reader for new office task contracts.

Existing accepted task readers remain unchanged. Both ZIP inspection and
openpyxl consume the same immutable byte string; dimensions are untrusted.
"""
from io import BytesIO
from pathlib import Path
from zipfile import BadZipFile, ZipFile

from workenv.core import ResourceError, no_links

MAX_BYTES = 1024 * 1024


class InvalidWorkbook(ValueError):
    pass


def read_tables(workspace, filename, headers, *, max_rows=4096):
    if filename not in ("WC_Customer_Report.xlsx", "Fulfillment_Report.xlsx", "Shipping_Analysis.xlsx"):
        raise ValueError("Unsupported office task workbook")
    if not isinstance(headers, dict) or not 1 <= len(headers) <= 4 or not 1 <= max_rows <= 100000:
        raise ValueError("Unsupported office workbook contract")
    root = Path(workspace).absolute()
    path = root / filename
    try:
        no_links(path, root)
    except ValueError as exc:
        raise ResourceError("Office workbook ownership is untrusted") from exc
    if not path.exists(): raise InvalidWorkbook("workbook_missing")
    if not path.is_file() or path.stat().st_size > MAX_BYTES: raise InvalidWorkbook("workbook_file_budget")
    with path.open("rb") as stream: raw = stream.read(MAX_BYTES + 1)
    if len(raw) > MAX_BYTES: raise InvalidWorkbook("workbook_file_budget")
    return read_bytes(raw, headers, max_rows=max_rows)


def read_bytes(raw, headers, *, max_rows=4096):
    if not isinstance(raw, bytes) or not 0 < len(raw) <= MAX_BYTES:
        raise InvalidWorkbook("workbook_file_budget")
    try:
        from openpyxl import load_workbook
        from openpyxl.utils.exceptions import InvalidFileException
    except ImportError as exc:
        raise ResourceError("openpyxl_unavailable") from exc
    try:
        with ZipFile(BytesIO(raw)) as archive:
            members = archive.infolist()
            if len(members) > 128 or sum(m.file_size for m in members) > 8 * 1024 * 1024:
                raise InvalidWorkbook("workbook_archive_budget")
            if len({m.filename for m in members}) != len(members): raise InvalidWorkbook("duplicate_zip_member")
            for member in members:
                name = member.filename.replace("\\", "/")
                if (member.flag_bits & 1 or name.startswith("/") or ".." in name.split("/")
                    or "externallinks/" in name.casefold() or "vbaproject" in name.casefold()):
                    raise InvalidWorkbook("unsupported_zip_member")
                if name.casefold().endswith((".xml", ".rels")):
                    xml = archive.read(member).replace(b"\0", b"").upper()
                    if b"<!DOCTYPE" in xml or b"<!ENTITY" in xml: raise InvalidWorkbook("xml_entities_forbidden")
        workbook = load_workbook(BytesIO(raw), read_only=True, data_only=False, keep_links=False)
        try:
            if set(workbook.sheetnames) != set(headers) or len(workbook.sheetnames) != len(headers):
                raise InvalidWorkbook("task_sheet_names_differ")
            tables = {}
            for name, required in headers.items():
                sheet = workbook[name]
                # Do not let forged dimensions hide or allocate large fake ranges.
                sheet.reset_dimensions()
                rows = sheet.iter_rows()
                first = next(rows)
                if tuple(cell.value for cell in first) != tuple(required): raise InvalidWorkbook("task_headers_differ")
                if any(cell.data_type == "f" for cell in first): raise InvalidWorkbook("formulas_forbidden")
                values = []
                for index, row in enumerate(rows, 1):
                    if index > max_rows or len(row) > len(required): raise InvalidWorkbook("task_row_column_budget")
                    if any(cell.data_type == "f" for cell in row): raise InvalidWorkbook("formulas_forbidden")
                    if all(cell.value is None for cell in row): continue
                    data = [cell.value for cell in row] + [None] * (len(required) - len(row))
                    values.append(dict(zip(required, data)))
                tables[name] = values
            return tables
        finally:
            workbook.close()
    except (BadZipFile, InvalidFileException, ValueError, KeyError, SyntaxError, StopIteration,
            NotImplementedError, OverflowError) as exc:
        if isinstance(exc, InvalidWorkbook): raise
        raise InvalidWorkbook("workbook_content_invalid") from exc
