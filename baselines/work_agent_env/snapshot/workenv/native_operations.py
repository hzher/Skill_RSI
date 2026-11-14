"""Negative-state mutations for the original native tax diagnostic."""
from pathlib import Path
from adapters import toolathlon_native as native

MODES = ("known_correct", "empty", "claim_only", "wrong_object", "partial", "wrong_sales",
         "missing_artifact", "corrupt_artifact", "wrong_shared_rate", "extra_shared_tab", "allowed_format", "verifier_error", "tool_error")

def mutate(run: Path, mode: str) -> None:
    artifact = run / "workspace" / "Tax_Summary_Report.xlsx"
    if mode == "missing_artifact":
        artifact.unlink()
    elif mode == "corrupt_artifact":
        artifact.write_bytes(b"not an xlsx file")
    elif mode in ("wrong_object", "partial", "wrong_sales", "allowed_format"):
        from openpyxl import load_workbook
        from openpyxl.styles import Font
        wb = load_workbook(artifact)
        if mode == "wrong_object":
            wb["By State"]["A2"] = "NON_TARGET_STATE"
        elif mode == "partial":
            del wb["Overall"]
        elif mode == "wrong_sales":
            wb["By State"]["C2"] = float(wb["By State"]["C2"].value) + 1000
        else:
            wb["By State"]["A1"].font = Font(bold=True, color="223344")
            wb["By State"].column_dimensions["A"].width = 25
        wb.save(artifact)
        wb.close()
    elif mode == "wrong_shared_rate":
        with native.database() as conn, conn.cursor() as cur:
            cur.execute("UPDATE gsheet.cells SET value='999.0' WHERE row_index=4 AND col_index=1")
            conn.commit()
