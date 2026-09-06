import csv
import io
import uuid
from datetime import date as date_type
from datetime import datetime, timezone
from typing import List, Optional

from bson import ObjectId
from fastapi import APIRouter, Depends, File, Query, UploadFile
from fastapi.responses import StreamingResponse

from ..auth_helper import require_capability
from ..database.main_db import (
    customers_collection,
    items_collection,
    categories_collection,
    transactions_collection,
    imports_collection,
)
from ..crypto_helper import encrypt_dict, decrypt_dict, get_search_token
from ..router_utils import serialize, log_audit

router = APIRouter(tags=["Import & Export"])

CUSTOMER_SENSITIVE = ["name", "contact_person", "phone", "email", "address", "notes"]
ITEM_SENSITIVE = ["name"]
CAT_SENSITIVE = ["name", "description"]
TXN_SENSITIVE = ["customer_name", "invoice_number", "items", "notes"]
IMPORT_SENSITIVE = ["file_name", "errors", "notes"]

TEMPLATE_HEADERS = [
    "Date", "Customer Name", "Invoice Number", "Item Name",
    "Qty Received", "Qty Washed", "Qty Delivered", "Qty Rejected", "Qty Damaged",
    "Rate", "Cost", "Notes",
]

COLUMN_KEYS = {
    0: "date", 1: "customer_name", 2: "invoice_number", 3: "item_name",
    4: "qty_received", 5: "qty_washed", 6: "qty_delivered", 7: "qty_rejected", 8: "qty_damaged",
    9: "rate", 10: "cost", 11: "notes",
}


def _num(value) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def _fmt_date(value) -> Optional[str]:
    if isinstance(value, datetime):
        return value.strftime("%Y-%m-%d")
    if isinstance(value, date_type):
        return value.isoformat()
    s = str(value or "").strip()
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%m/%d/%Y", "%Y/%m/%d"):
        try:
            return datetime.strptime(s.split(" ")[0], fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    return s


def _is_iso_date(s: str) -> bool:
    try:
        return bool(datetime.strptime(s, "%Y-%m-%d"))
    except ValueError:
        return False


def _parse_rows_from_file(content: bytes, filename: str) -> List[dict]:
    """Parse xlsx/xls/csv content into raw row dicts keyed by COLUMN_KEYS."""
    rows: List[dict] = []
    name = (filename or "").lower()

    if name.endswith(".csv"):
        text = content.decode("utf-8-sig", errors="replace")
        reader = csv.reader(io.StringIO(text))
        data = list(reader)
        if not data:
            return rows
    else:
        try:
            import openpyxl
            from openpyxl import load_workbook
        except ImportError:
            return rows
        wb = load_workbook(io.BytesIO(content), data_only=True)
        ws = wb.active
        data = list(ws.iter_rows(values_only=True))
        if not data:
            return rows

    header = [str(c or "").strip().lower() for c in data[0]]
    for line in data[1:]:
        if not any(str(c or "").strip() for c in line):
            continue
        row: dict = {}
        for idx, key in COLUMN_KEYS.items():
            cell = line[idx] if idx < len(line) else None
            row[key] = cell
        # Map by header if header looks like template, else use positional
        row["_raw"] = line
        rows.append(row)
    return rows


def _validate_row(row: dict, row_num: int) -> dict:
    errors: List[str] = []
    rec = {
        "row": row_num,
        "date": _fmt_date(row.get("date")) or "",
        "customer_name": str(row.get("customer_name") or "").strip(),
        "invoice_number": str(row.get("invoice_number") or "").strip(),
        "item_name": str(row.get("item_name") or "").strip(),
        "qty_received": _num(row.get("qty_received")),
        "qty_washed": _num(row.get("qty_washed")),
        "qty_delivered": _num(row.get("qty_delivered")),
        "qty_rejected": _num(row.get("qty_rejected")),
        "qty_damaged": _num(row.get("qty_damaged")),
        "rate": _num(row.get("rate")),
        "cost": _num(row.get("cost")),
        "notes": str(row.get("notes") or "").strip(),
    }
    if not rec["date"]:
        errors.append("Date is required")
    if rec["date"] and not _is_iso_date(rec["date"]):
        errors.append("Invalid date. Use YYYY-MM-DD")
    if not rec["customer_name"]:
        errors.append("Customer Name is required")
    if not rec["item_name"]:
        errors.append("Item Name is required")
    if rec["qty_received"] <= 0 and rec["qty_washed"] <= 0:
        errors.append("Quantity (Received or Washed) must be greater than 0")
    rec["valid"] = len(errors) == 0
    rec["errors"] = errors
    return rec


async def _find_or_create_customer(name: str) -> str:
    cleaned = name.strip()
    token = get_search_token(cleaned)
    doc = await customers_collection().find_one({"name_search": token})
    if doc:
        return str(doc["_id"])
    now = datetime.now(timezone.utc)
    doc = {
        "name": cleaned,
        "customer_type": "INDIVIDUAL",
        "billing_method": "PER_ITEM",
        "payment_terms": "CASH",
        "is_active": True,
        "created_at": now,
        "updated_at": now,
    }
    encrypted = encrypt_dict(doc, CUSTOMER_SENSITIVE)
    result = await customers_collection().insert_one(encrypted)
    return str(result.inserted_id)


async def _find_or_create_item(name: str) -> tuple[Optional[str], Optional[str]]:
    cleaned = name.strip()
    token = get_search_token(cleaned)
    doc = await items_collection().find_one({"name_search": token})
    if doc:
        return str(doc["_id"]), doc.get("category_id")
    now = datetime.now(timezone.utc)

    cat_doc = await categories_collection().find_one({"name_search": get_search_token("GENERAL")})
    if not cat_doc:
        cat_doc = {
            "name": "GENERAL",
            "description": "General laundry items",
            "is_active": True,
            "created_at": now,
            "updated_at": now,
        }
        cat_enc = encrypt_dict(cat_doc, CAT_SENSITIVE)
        cat_result = await categories_collection().insert_one(cat_enc)
        cat_id = str(cat_result.inserted_id)
    else:
        cat_id = str(cat_doc["_id"])

    item_doc = {
        "name": cleaned,
        "category_id": cat_id,
        "standard_cost": 0.0,
        "default_rate": 0.0,
        "unit": "PIECE",
        "is_active": True,
        "total_quantity": 0.0,
        "total_revenue": 0.0,
        "total_cost": 0.0,
        "created_at": now,
        "updated_at": now,
    }
    encrypted = encrypt_dict(item_doc, ITEM_SENSITIVE)
    result = await items_collection().insert_one(encrypted)
    return str(result.inserted_id), cat_id


@router.get("/import/template")
async def download_template(
    current_user: dict = Depends(require_capability("import:write")),
):
    from openpyxl import Workbook

    wb = Workbook()
    ws = wb.active
    ws.title = "Laundry Import"
    ws.append(TEMPLATE_HEADERS)
    ws.append(["2026-01-05", "Sunshine Hotel", "INV-1001", "Shirt", 20, 20, 18, 2, 0, 150, 80, "Sample row - delete before uploading"])
    widths = [12, 20, 14, 20, 12, 12, 12, 12, 12, 10, 10, 30]
    for i, w in enumerate(widths, start=1):
        ws.column_dimensions[chr(64 + i)].width = w

    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    return StreamingResponse(
        buf,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": "attachment; filename=laundry_import_template.xlsx"},
    )


@router.post("/import/preview")
async def preview_import(
    file: UploadFile = File(...),
    current_user: dict = Depends(require_capability("import:write")),
):
    content = await file.read()
    raw_rows = _parse_rows_from_file(content, file.filename or "")
    if not raw_rows:
        raise HTTPException(status_code=400, detail="Could not read file. Use the provided template.")

    preview = []
    errors = []
    for i, row in enumerate(raw_rows, start=2):
        rec = _validate_row(row, i)
        preview.append(rec)
        if not rec["valid"]:
            errors.append({"row": i, "errors": rec["errors"]})

    return {
        "total_rows": len(preview),
        "preview": preview,
        "errors": errors,
    }


@router.post("/import/execute")
async def execute_import(
    file: UploadFile = File(...),
    current_user: dict = Depends(require_capability("import:write")),
):
    content = await file.read()
    raw_rows = _parse_rows_from_file(content, file.filename or "")
    if not raw_rows:
        raise HTTPException(status_code=400, detail="Could not read file. Use the provided template.")

    validated = [_validate_row(row, i) for i, row in enumerate(raw_rows, start=2)]
    valid_rows = [r for r in validated if r["valid"]]
    error_rows = [r for r in validated if not r["valid"]]

    # Group into transactions by (date, customer, invoice)
    groups: dict = {}
    order: List[str] = []
    success = 0
    for rec in valid_rows:
        key = (rec["date"], rec["customer_name"], rec["invoice_number"])
        if key not in groups:
            order.append(key)
            groups[key] = {
                "date": rec["date"],
                "customer": rec["customer_name"],
                "invoice": rec["invoice_number"],
                "items": [],
            }
        groups[key]["items"].append(rec)

    batch_id = str(uuid.uuid4())
    created_count = 0
    for key in order:
        grp = groups[key]
        customer_id = await _find_or_create_customer(grp["customer"])
        items = []
        for rec in grp["items"]:
            item_id, cat_id = await _find_or_create_item(rec["item_name"])
            qty = rec["qty_washed"] if rec["qty_washed"] > 0 else rec["qty_received"]
            rate = rec["rate"]
            cost = rec["cost"]
            items.append({
                "item_id": item_id,
                "item_name": rec["item_name"],
                "category_id": cat_id,
                "quantity_received": rec["qty_received"],
                "quantity_washed": rec["qty_washed"],
                "quantity_delivered": rec["qty_delivered"],
                "quantity_rejected": rec["qty_rejected"],
                "quantity_damaged": rec["qty_damaged"],
                "rate": rate,
                "cost": cost,
                "line_total": round(qty * rate, 2),
                "line_cost": round(qty * cost, 2),
                "line_profit": round(qty * (rate - cost), 2),
                "status": "RECEIVED",
                "notes": rec["notes"] or None,
            })
        now = datetime.now(timezone.utc)
        doc = {
            "transaction_date": grp["date"],
            "customer_id": customer_id,
            "customer_name": grp["customer"],
            "invoice_number": grp["invoice"] or None,
            "status": "COMPLETED",
            "source": "IMPORT",
            "import_batch_id": batch_id,
            "items": items,
            "item_ids": [it["item_id"] for it in items if it.get("item_id")],
            "total_quantity": round(sum(_num(i["quantity_washed"]) or _num(i["quantity_received"]) for i in items), 2),
            "total_amount": round(sum(_num(i["line_total"]) for i in items), 2),
            "total_cost": round(sum(_num(i["line_cost"]) for i in items), 2),
            "total_profit": round(sum(_num(i["line_profit"]) for i in items), 2),
            "notes": None,
            "created_at": now,
            "updated_at": now,
            "created_by": str(current_user.get("user_name") or current_user.get("user_id") or ""),
        }
        encrypted = encrypt_dict(doc, TXN_SENSITIVE)
        await transactions_collection().insert_one(encrypted)
        created_count += 1

    # Record import batch
    import_doc = {
        "file_name": file.filename or "upload",
        "batch_id": batch_id,
        "status": "IMPORTED",
        "total_rows": len(validated),
        "success_rows": len(valid_rows),
        "error_rows": len(error_rows),
        "failures": [r["errors"] for r in error_rows],
        "errors": [
            {"row": r["row"], "errors": r["errors"]}
            for r in error_rows
        ],
        "created_at": datetime.now(timezone.utc),
        "created_by": str(current_user.get("user_name") or current_user.get("user_id") or ""),
    }
    batch_enc = encrypt_dict(import_doc, IMPORT_SENSITIVE)
    await imports_collection().insert_one(batch_enc)

    await log_audit(
        str(current_user.get("user_id", "")),
        "import",
        "historical_data",
        batch_id,
        details={"total_rows": len(validated), "success": len(valid_rows), "errors": len(error_rows), "transactions": created_count},
    )

    # Refresh item rollups
    for rec in valid_rows:
        if rec["item_name"]:
            token = get_search_token(rec["item_name"])
            item_doc = await items_collection().find_one({"name_search": token})
            if item_doc:
                from ..routers.transactions import _recompute_item_stats
                try:
                    await _recompute_item_stats(str(item_doc["_id"]))
                except Exception:
                    pass

    return {
        "total_rows": len(validated),
        "success_rows": len(valid_rows),
        "error_rows": len(error_rows),
        "transactions_created": created_count,
        "batch_id": batch_id,
    }


@router.get("/import/history")
async def import_history(
    current_user: dict = Depends(require_capability("import:write")),
):
    cursor = imports_collection().find({}).sort("created_at", -1).limit(50)
    return [serialize(doc, IMPORT_SENSITIVE) async for doc in cursor]