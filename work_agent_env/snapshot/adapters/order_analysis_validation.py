"""Bounded workbook/Calendar supplement for the pinned public order-analysis task.

The caller must supply complete owned order/product snapshots. This module
does not attest snapshot ownership/pagination, contact Calendar, run the
original evaluator, or grant a task reward. Numeric ROUND follows the
original PostgreSQL numeric expression (ties away from zero); lexical ordering
matches the current owned UTF-8/C database locale, without locale guessing.
"""
from collections import Counter
from datetime import datetime, timezone
from decimal import Context, Decimal, DecimalException, ROUND_HALF_UP, localcontext
from io import BytesIO
from pathlib import Path
import re
from zipfile import BadZipFile, ZipFile

from adapters.meal_plan_validation import InvalidPlan, normalized
from workenv.core import ResourceError, no_links

TASK_ID = "wc-order-analysis-gcal"
SOURCE_COMMIT = "ed735ba0af71d82688a55f952a6ea11479ae7206"
INSTRUCTION_BLOB = "44ca55d6edf474b5d8498ec5f66e51581a16e05f"
INSTRUCTION_SHA256 = "43ed3a8a02d1f395c815b7f479ba31d4318a24dd3765932da5f014abdfd5499a"
EVALUATION_BLOB = "bbdbefa17ccf7e10934bcca4763d6f5165082a05"
EVALUATION_SHA256 = "44b2a607f7a35a566ea58a0233b9cb877ef2fb298eddbe483d022f206c56a735"
HEADERS = {
    "Status_Summary": ("Status", "Order_Count", "Total_Revenue", "Avg_Order_Value"),
    "Low_Stock_Products": ("Product_Name", "SKU", "Stock_Quantity", "Total_Sales"),
}
MAX_SOURCE_ROWS = 100000
CENT = Decimal("0.01")
WORKBOOK_NAME = "Order_Analysis.xlsx"
MEETING_TITLE = "Restock Planning Meeting"
MEETING_START = datetime(2026, 3, 10, 10, tzinfo=timezone.utc)
MEETING_END = datetime(2026, 3, 10, 11, tzinfo=timezone.utc)
MAX_WORKBOOK_BYTES = 1024 * 1024


class InvalidRows(ValueError):
    """A supplied measurable workbook artifact fails the public row contract."""


def contract_identity():
    return {"source": "toolathlon_gym", "commit": SOURCE_COMMIT, "task_id": TASK_ID,
        "instruction_git_blob": INSTRUCTION_BLOB, "instruction_sha256": INSTRUCTION_SHA256,
        "evaluation_git_blob": EVALUATION_BLOB, "evaluation_sha256": EVALUATION_SHA256}


def _number(value, error, field):
    if isinstance(value, bool) or not isinstance(value, (str, int, float, Decimal)):
        raise error("invalid_numeric_" + field)
    if isinstance(value, int) and value.bit_length() > 256 or isinstance(value, Decimal) and len(value.as_tuple().digits) > 128:
        raise error("numeric_budget_" + field)
    text = str(value).strip()
    if len(text) > 128 or not re.fullmatch(r"[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?", text):
        raise error("invalid_numeric_" + field)
    try:
        number = Decimal(text)
        if not number.is_finite() or abs(number.adjusted()) > 24 or abs(number.as_tuple().exponent) > 24:
            raise error("numeric_budget_" + field)
        return number
    except DecimalException as exc:
        raise error("invalid_numeric_" + field) from exc


def _text(value, error, field, *, blank=False):
    if blank and value is None:
        return None
    if not isinstance(value, str) or len(value) > 4096 or "\0" in value or not blank and not value:
        raise error("invalid_text_" + field)
    try:
        value.encode("utf-8")
    except UnicodeError as exc:
        raise error("invalid_text_" + field) from exc
    return None if blank and value == "" else value


def _required(row, keys, error):
    if not isinstance(row, dict) or not set(keys) <= set(row):
        raise error("missing_or_invalid_row_fields")


def _products(products):
    if not isinstance(products, list) or len(products) > MAX_SOURCE_ROWS:
        raise ResourceError("order_analysis_product_snapshot_missing_or_unbounded")
    result = []
    for product in products:
        _required(product, ("stock_quantity",), ResourceError)
        if product["stock_quantity"] is None:
            continue
        stock = _number(product["stock_quantity"], ResourceError, "stock_quantity")
        if stock >= 5:
            continue
        _required(product, ("name", "sku", "total_sales"), ResourceError)
        result.append((_text(product["name"], ResourceError, "product_name"),
            _text(product["sku"], ResourceError, "sku", blank=True), stock,
            _number(product["total_sales"], ResourceError, "total_sales")))
    return result


def expected_rows(orders, products, *, source_binding):
    """Compute expectations; invalid/unavailable source data raises ResourceError.

    No ID, currency, date, or customer field is required. All supplied orders
    contribute, including cancelled/refunded orders. Duplicate/blank SKUs never
    collapse products. NULL stock is excluded by the original SQL predicate.
    """
    if source_binding != contract_identity():
        raise ResourceError("order_analysis_public_source_binding_differs")
    if any(not isinstance(rows, list) or len(rows) > MAX_SOURCE_ROWS for rows in (orders, products)):
        raise ResourceError("order_analysis_source_snapshot_missing_or_unbounded")
    with localcontext(Context(prec=80)):
        groups = {}
        for order in orders:
            _required(order, ("status", "total"), ResourceError)
            status = _text(order["status"], ResourceError, "order_status")
            total = _number(order["total"], ResourceError, "order_total")
            count, revenue = groups.get(status, (0, Decimal(0)))
            groups[status] = (count + 1, revenue + total)
        statuses = [{"Status": status, "Order_Count": count,
            "Total_Revenue": revenue.quantize(CENT, rounding=ROUND_HALF_UP),
            "Avg_Order_Value": (revenue / count).quantize(CENT, rounding=ROUND_HALF_UP)}
            for status, (count, revenue) in sorted(groups.items())]
        low_stock = [{"Product_Name": name[:80], "SKU": sku, "Stock_Quantity": stock, "Total_Sales": sales}
                     for name, sku, stock, sales in _products(products)]
        low_stock.sort(key=lambda row: (row["Stock_Quantity"], row["Product_Name"]))
        return statuses, low_stock


def _artifact_rows(rows, headers):
    if not isinstance(rows, list) or len(rows) > MAX_SOURCE_ROWS:
        raise InvalidRows("invalid_workbook_rows")
    result = []
    for row in rows:
        _required(row, headers, InvalidRows)
        if headers == HEADERS["Status_Summary"]:
            status = _text(row["Status"], InvalidRows, "status")
            count = _number(row["Order_Count"], InvalidRows, "order_count")
            revenue = _number(row["Total_Revenue"], InvalidRows, "total_revenue")
            average = _number(row["Avg_Order_Value"], InvalidRows, "average_order_value")
            if count < 0 or count != count.to_integral_value():
                raise InvalidRows("order_count_not_nonnegative_integer")
            if any(value != value.quantize(CENT, rounding=ROUND_HALF_UP) for value in (revenue, average)):
                raise InvalidRows("money_values_not_rounded_to_two_decimals")
            result.append((status, count, revenue, average))
        else:
            result.append((_text(row["Product_Name"], InvalidRows, "product_name"),
                _text(row["SKU"], InvalidRows, "sku", blank=True),
                _number(row["Stock_Quantity"], InvalidRows, "stock_quantity"),
                _number(row["Total_Sales"], InvalidRows, "total_sales")))
    return result


def measure_rows(orders, products, status_rows, low_stock_rows, *, source_binding):
    """Classify source faults as unknown and artifact defects as completed failures."""
    passed, unknown, errors, measurements = None, False, [], {}
    try:
        # Source validation takes precedence even when both artifacts are invalid.
        statuses, products_expected = expected_rows(orders, products, source_binding=source_binding)
        with localcontext(Context(prec=80)):
            actual_status = _artifact_rows(status_rows, HEADERS["Status_Summary"])
            actual_products = _artifact_rows(low_stock_rows, HEADERS["Low_Stock_Products"])
        expected_status = [tuple(row[key] for key in HEADERS["Status_Summary"]) for row in statuses]
        expected_products = [tuple(row[key] for key in HEADERS["Low_Stock_Products"]) for row in products_expected]
        if actual_status != expected_status:
            raise InvalidRows("status_groups_counts_money_or_order_differ")
        if Counter(actual_products) != Counter(expected_products):
            raise InvalidRows("complete_low_stock_product_multiset_differs")
        sort_keys = [(row[2], row[0]) for row in actual_products]
        if sort_keys != sorted(sort_keys):
            raise InvalidRows("low_stock_sort_order_differs")
        passed = True
        measurements = {"orders": len(orders), "status_groups": len(statuses), "products": len(products),
            "low_stock_products": len(products_expected)}
    except InvalidRows as exc:
        passed, errors = False, [str(exc)]
    except (ResourceError, DecimalException) as exc:
        unknown, errors = True, [str(exc)]
    return {"schema_version": "order-analysis-row-contract-v1", "contract_source": contract_identity(),
        "evaluation_status": "error" if unknown else "completed", "rows_passed": None if unknown else passed,
        "supplemental_passed": None if unknown else passed, "task_passed": None,
        "error_category": "verifier_source_unavailable" if unknown else None, "errors": errors,
        "measurements": measurements, "reward": None, "score": None, "calendar_checked": False,
        "snapshot_completeness_verified": False, "original_evaluator_executed": False,
        "nemo_task_executed": False, "training_eligible": False}


def read_workbook(workspace):
    """Apply the existing meal reader's bounded ZIP/openpyxl protection policy.

    Filename, sheet/column contract and row capacity are task-specific. Shared
    guard extraction is deferred to avoid changing the verified meal runtime.
    Both ZIP inspection and openpyxl consume the same bounded immutable bytes.
    """
    try:
        from openpyxl import load_workbook
        from openpyxl.utils.exceptions import InvalidFileException
    except ImportError as exc:
        raise ResourceError("openpyxl_unavailable") from exc
    workspace = Path(workspace).absolute()
    path = workspace / WORKBOOK_NAME
    try:
        no_links(path, workspace)
    except ValueError as exc:
        raise ResourceError("workbook_resource_path_untrusted") from exc
    if not path.exists():
        raise InvalidRows("workbook_missing")
    if not path.is_file() or path.stat().st_size > MAX_WORKBOOK_BYTES:
        raise InvalidRows("workbook_file_budget_exceeded")
    with path.open("rb") as stream:
        raw = stream.read(MAX_WORKBOOK_BYTES + 1)
    if len(raw) > MAX_WORKBOOK_BYTES:
        raise InvalidRows("workbook_file_budget_exceeded")
    try:
        with ZipFile(BytesIO(raw)) as archive:
            infos = archive.infolist()
            if len(infos) > 128 or sum(i.file_size for i in infos) > 8 * 1024 * 1024:
                raise InvalidRows("workbook_uncompressed_budget_exceeded")
            if len({i.filename for i in infos}) != len(infos):
                raise InvalidRows("workbook_duplicate_archive_member")
            for info in infos:
                if (info.flag_bits & 1 or info.filename.startswith(("/", "\\"))
                    or ".." in info.filename.replace("\\", "/").split("/")
                    or "externallinks/" in info.filename.casefold() or "vbaproject" in info.filename.casefold()):
                    raise InvalidRows("workbook_external_or_unsupported_member")
                if info.filename.casefold().endswith((".xml", ".rels")):
                    xml = archive.read(info).replace(b"\0", b"").upper()
                    if b"<!DOCTYPE" in xml or b"<!ENTITY" in xml:
                        raise InvalidRows("workbook_xml_entities_forbidden")
        workbook = load_workbook(BytesIO(raw), read_only=True, data_only=False, keep_links=False)
    except (BadZipFile, InvalidFileException, ValueError, KeyError, SyntaxError, NotImplementedError) as exc:
        if isinstance(exc, InvalidRows):
            raise
        raise InvalidRows("workbook_content_invalid") from exc
    try:
        mapping = {normalized(name): name for name in workbook.sheetnames}
        if len(mapping) != 2 or set(mapping) != {normalized(name) for name in HEADERS}:
            raise InvalidRows("workbook_requires_both_task_sheets")
        result = []
        for name, required in HEADERS.items():
            sheet = workbook[mapping[normalized(name)]]
            if (sheet.max_row is None or sheet.max_column is None or sheet.max_row > MAX_SOURCE_ROWS + 1
                or sheet.max_column > 16):
                raise InvalidRows("workbook_sheet_budget_exceeded")
            # The producer-controlled dimension must not hide later rows/cells.
            sheet.reset_dimensions()
            rows = sheet.iter_rows()
            first = next(rows)
            if len(first) > 16:
                raise InvalidRows("workbook_sheet_budget_exceeded")
            if any(cell.data_type == "f" for cell in first):
                raise InvalidRows("workbook_formulas_forbidden")
            header = [re.sub(r"[\s_]+", "_", normalized(c.value)) if c.value is not None else "" for c in first]
            positions = {}
            for key in required:
                canonical = normalized(key)
                if header.count(canonical) != 1:
                    raise InvalidRows("workbook_required_header_missing_or_duplicate")
                positions[key] = header.index(canonical)
            values = []
            for row_number, row in enumerate(rows, 2):
                if row_number > MAX_SOURCE_ROWS + 1 or len(row) > 16:
                    raise InvalidRows("workbook_sheet_budget_exceeded")
                if any(cell.data_type == "f" for cell in row):
                    raise InvalidRows("workbook_formulas_forbidden")
                if all(cell.value is None for cell in row):
                    continue
                values.append({key: row[index].value if index < len(row) else None for key, index in positions.items()})
            result.append(values)
        return tuple(result)
    except (InvalidPlan, ValueError, KeyError, SyntaxError, StopIteration) as exc:
        if isinstance(exc, InvalidRows):
            raise
        raise InvalidRows("workbook_content_invalid") from exc
    finally:
        workbook.close()


def _instant(event, key):
    if key in event:
        raw = event[key]
        if not isinstance(raw, dict):
            raise ResourceError("calendar_time_snapshot_incomplete")
        if "date" in raw and "dateTime" not in raw:
            raise InvalidRows("calendar_requires_timed_event")
        _required(raw, ("dateTime",), ResourceError)
        value = raw["dateTime"]
    else:
        _required(event, (key + "_datetime",), ResourceError)
        value = event[key + "_datetime"]
    try:
        instant = datetime.fromisoformat(value.replace("Z", "+00:00")) if isinstance(value, str) else value
        if not isinstance(instant, datetime) or instant.utcoffset() is None:
            raise InvalidRows("calendar_requires_explicit_offset")
        return instant.astimezone(timezone.utc)
    except (ValueError, OverflowError) as exc:
        if isinstance(exc, InvalidRows):
            raise
        raise InvalidRows("calendar_invalid_timestamp") from exc


def _description(description, products):
    expected = Counter((name, stock) for name, _, stock, _ in products)
    if not expected:
        return 0
    if description is None or description == "":
        raise InvalidRows("calendar_product_description_missing")
    if not isinstance(description, str) or len(description) > 256 * 1024:
        raise ResourceError("calendar_description_snapshot_invalid_or_unbounded")
    names = {name for name, _ in expected}
    if len({name.casefold() for name in names}) != len(names):
        raise ResourceError("calendar_case_ambiguous_product_names")
    candidates = []
    for name in names:
        pattern = (r"(?<!\w)" if name[0].isalnum() else "") + re.escape(name) + (r"(?!\w)" if name[-1].isalnum() else "")
        candidates.extend((match.start(), match.end(), name) for match in re.finditer(pattern, description, re.IGNORECASE))
    matches, end = [], -1
    for start, stop, name in sorted(candidates, key=lambda item: (item[0], -item[1])):
        if start >= end:
            matches.append((start, stop, name))
            end = stop
    masked = list(description)
    for start, stop, _ in matches:
        masked[start:stop] = " " * (stop - start)
    masked = "".join(masked)
    separators = list(re.finditer(r"[;\n|,\u3001]|\band\b", masked, re.IGNORECASE))
    boundaries = [(0, separators[0].start())] if separators else [(0, len(masked))]
    if separators:
        boundaries += [(sep.end(), separators[i + 1].start() if i + 1 < len(separators) else len(masked)) for i, sep in enumerate(separators)]
    observed = Counter()
    numeric = r"[+-]?[0-9]+(?:\.[0-9]+)?"
    for start, stop in boundaries:
        present = [name for left, right, name in matches if start <= left and right <= stop]
        if not present:
            continue
        if len(present) != 1:
            raise ResourceError("calendar_description_product_quantity_association_ambiguous")
        fragment = masked[start:stop]
        labels = re.findall(r"(?:stock(?:[_ ]quantity| level)?|quantity|qty|inventory|\u5e93\u5b58|\u6570\u91cf)\s*(?::|=|is|\u4e3a)?\s*(" + numeric + ")", fragment, re.IGNORECASE)
        numbers = labels or re.findall(r"(?<![\w.])(" + numeric + r")(?![\w.])", fragment)
        if len(numbers) != 1:
            raise ResourceError("calendar_description_quantity_ambiguous")
        observed[(present[0], _number(numbers[0], ResourceError, "description_stock"))] += 1
    for name in names:
        claimed = {stock for (item, stock), count in observed.items() if item == name and count}
        wanted = {stock for item, stock in expected if item == name}
        if not claimed:
            if len(name) > 80 and name[:80].casefold() in description.casefold():
                raise ResourceError("calendar_truncated_product_name_ambiguous")
            raise InvalidRows("calendar_full_product_name_missing")
        if len(claimed) > 1 and claimed != wanted:
            raise ResourceError("calendar_conflicting_stock_description")
        if claimed != wanted:
            raise InvalidRows("calendar_product_stock_differs")
    if any(observed[item] < count for item, count in expected.items()):
        raise ResourceError("calendar_duplicate_product_multiplicity_ambiguous")
    return sum(expected.values())


def measure_calendar(products, events, *, source_binding):
    """Measure supplied calendar state; ambiguous free-text associations stay unknown.

    The prompt truncates names only in the spreadsheet. Calendar coverage uses
    full names. Unrelated events and additional valid meetings are not forbidden
    by the task. A complete qualifying meeting proves the existence requirement.
    """
    passed, unknown, errors, measurements = None, False, [], {}
    try:
        if source_binding != contract_identity():
            raise ResourceError("order_analysis_public_source_binding_differs")
        products = _products(products)
        if not isinstance(events, list) or len(events) > 1024:
            raise ResourceError("calendar_snapshot_missing_or_unbounded")
        for event in events:
            _required(event, ("summary",), ResourceError)
            if event["summary"] is not None and not isinstance(event["summary"], str):
                raise ResourceError("calendar_summary_snapshot_invalid")
        candidates = [event for event in events if event["summary"] == MEETING_TITLE]
        if not candidates:
            raise InvalidRows("calendar_exact_meeting_title_missing")
        failures, uncertain = [], []
        for event in candidates:
            try:
                _required(event, ("description",), ResourceError)
                if _instant(event, "start") != MEETING_START or _instant(event, "end") != MEETING_END:
                    raise InvalidRows("calendar_fixed_utc_interval_differs")
                coverage = _description(event["description"], products)
                measurements = {"calendar_events": len(events), "exact_title_events": len(candidates), "described_products": coverage}
                passed = True
                break
            except InvalidRows as exc:
                failures.append(str(exc))
            except ResourceError as exc:
                uncertain.append(str(exc))
        if not passed:
            if uncertain:
                raise ResourceError(uncertain[0])
            raise InvalidRows(failures[0])
    except InvalidRows as exc:
        passed, errors = False, [str(exc)]
    except ResourceError as exc:
        unknown, errors = True, [str(exc)]
    return {"schema_version": "order-analysis-calendar-contract-v1", "contract_source": contract_identity(),
        "evaluation_status": "error" if unknown else "completed", "calendar_passed": None if unknown else passed,
        "supplemental_passed": None if unknown else passed, "task_passed": None, "reward": None, "score": None,
        "errors": errors, "measurements": measurements, "calendar_checked": True,
        "error_category": "verifier_source_unavailable" if unknown else None,
        "snapshot_completeness_verified": False, "original_evaluator_executed": False,
        "nemo_task_executed": False, "training_eligible": False}


def measure_workbook(orders, products, events, workspace, *, source_binding):
    """Combine bounded real XLSX rows and supplied Calendar state; no task reward."""
    try:
        expected_rows(orders, products, source_binding=source_binding)
        calendar = measure_calendar(products, events, source_binding=source_binding)
        if calendar["evaluation_status"] != "completed":
            return {**calendar, "schema_version": "order-analysis-supplement-v1", "rows_passed": None, "workbook_read": False}
        statuses, stocks = read_workbook(workspace)
        rows = measure_rows(orders, products, statuses, stocks, source_binding=source_binding)
        passed = rows["rows_passed"] and calendar["calendar_passed"]
        return {**rows, "schema_version": "order-analysis-supplement-v1", "supplemental_passed": passed,
            "calendar_checked": True, "calendar_passed": calendar["calendar_passed"], "workbook_read": True,
            "errors": rows["errors"] + calendar["errors"], "measurements": {**rows["measurements"], **calendar["measurements"]}}
    except (InvalidRows, ResourceError, OSError) as exc:
        unknown = not isinstance(exc, InvalidRows)
        return {"schema_version": "order-analysis-supplement-v1", "contract_source": contract_identity(),
            "evaluation_status": "error" if unknown else "completed", "rows_passed": None if unknown else False,
            "supplemental_passed": None if unknown else False, "task_passed": None, "reward": None, "score": None,
            "calendar_checked": False, "calendar_passed": None, "workbook_read": False,
            "error_category": "verifier_source_unavailable" if unknown else None, "errors": [str(exc)], "measurements": {},
            "snapshot_completeness_verified": False, "original_evaluator_executed": False,
            "nemo_task_executed": False, "training_eligible": False}
