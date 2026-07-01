import os
import json
import requests
from datetime import datetime, timedelta, timezone
from dotenv import load_dotenv
from supabase import create_client

load_dotenv()

ODOO_URL = os.getenv("ODOO_URL")
ODOO_DB = os.getenv("ODOO_DB")
ODOO_USERNAME = os.getenv("ODOO_USERNAME")
ODOO_API_KEY = os.getenv("ODOO_API_KEY")

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_SERVICE_ROLE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY")

DAYS_BACK = int(os.getenv("DAYS_BACK", "90"))
CUSTOMER_HISTORY_START = os.getenv("CUSTOMER_HISTORY_START", "2024-01-01")

supabase = create_client(SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY)


def odoo_call(service, method, args):
    payload = {
        "jsonrpc": "2.0",
        "method": "call",
        "params": {"service": service, "method": method, "args": args},
        "id": int(datetime.now().timestamp())
    }

    response = requests.post(f"{ODOO_URL}/jsonrpc", json=payload, timeout=60)
    response.raise_for_status()
    data = response.json()

    if "error" in data:
        raise Exception(json.dumps(data["error"], ensure_ascii=False, indent=2))

    return data["result"]


def odoo_login():
    uid = odoo_call("common", "login", [ODOO_DB, ODOO_USERNAME, ODOO_API_KEY])
    if not uid:
        raise Exception("Odoo login failed. Check DB / username / API key.")
    return uid


def search_read(uid, model, domain, fields, limit=1000, offset=0):
    return odoo_call("object", "execute_kw", [
        ODOO_DB,
        uid,
        ODOO_API_KEY,
        model,
        "search_read",
        [domain],
        {"fields": fields, "limit": limit, "offset": offset}
    ])


def fetch_all(uid, model, domain, fields, batch_size=1000):
    all_rows = []
    offset = 0

    while True:
        rows = search_read(uid, model, domain, fields, batch_size, offset)
        if not rows:
            break

        all_rows.extend(rows)
        offset += batch_size
        print(f"{model}: fetched {len(all_rows)} rows")

    return all_rows


def safe_m2o(value, index):
    if isinstance(value, list) and len(value) > index:
        return value[index]
    return None


def log_sync(sync_type, status, message="", rows_count=0):
    supabase.table("sync_logs").insert({
        "sync_type": sync_type,
        "status": status,
        "message": message,
        "rows_count": rows_count,
        "finished_at": datetime.now(timezone.utc).isoformat()
    }).execute()


def upsert_in_batches(table_name, rows, conflict_column=None, batch_size=500):
    if not rows:
        return 0

    total = 0

    for i in range(0, len(rows), batch_size):
        batch = rows[i:i + batch_size]

        if conflict_column:
            supabase.table(table_name).upsert(
                batch,
                on_conflict=conflict_column
            ).execute()
        else:
            supabase.table(table_name).upsert(batch).execute()

        total += len(batch)

    return total


def sync_customers(uid):
    print("Syncing customers...")

    domain = [
        ["customer_rank", ">", 0]
    ]

    fields = [
        "id",
        "name",
        "phone",
        "mobile",
        "email",
        "street",
        "street2",
        "city",
        "country_id",
        "user_id",
        "customer_rank",
        "supplier_rank",
        "active",
        "create_date",
        "write_date"
    ]

    rows = fetch_all(uid, "res.partner", domain, fields)

    output = []

    for r in rows:
        output.append({
            "customer_id": r.get("id"),
            "name": r.get("name"),
            "phone": r.get("phone"),
            "mobile": r.get("mobile"),
            "email": r.get("email"),
            "street": r.get("street"),
            "street2": r.get("street2"),
            "city": r.get("city"),
            "country": safe_m2o(r.get("country_id"), 1),
            "salesperson": safe_m2o(r.get("user_id"), 1),
            "customer_rank": r.get("customer_rank"),
            "supplier_rank": r.get("supplier_rank"),
            "active": r.get("active"),
            "create_date": r.get("create_date"),
            "write_date": r.get("write_date"),
            "updated_at": datetime.now(timezone.utc).isoformat()
        })

    count = upsert_in_batches("raw_customers", output, conflict_column="customer_id")
    log_sync("customers", "success", "Customers synced", count)
    print(f"Customers synced: {count}")


def sync_products(uid):
    print("Syncing products...")

    domain = [
        ["active", "=", True],
        ["type", "in", ["product", "consu"]]
    ]

    fields = [
        "id",
        "product_tmpl_id",
        "display_name",
        "default_code",
        "categ_id",
        "standard_price",
        "list_price",
        "active",
        "type",
        "barcode"
    ]

    rows = fetch_all(uid, "product.product", domain, fields)

    output = []

    for r in rows:
        output.append({
            "product_id": r.get("id"),
            "product_tmpl_id": safe_m2o(r.get("product_tmpl_id"), 0),
            "product_name": r.get("display_name"),
            "internal_reference": r.get("default_code"),
            "category": safe_m2o(r.get("categ_id"), 1),
            "cost": r.get("standard_price") or 0,
            "sale_price": r.get("list_price") or 0,
            "active": r.get("active", True),
            "product_type": r.get("type"),
            "barcode": r.get("barcode"),
            "updated_at": datetime.now(timezone.utc).isoformat()
        })

    count = upsert_in_batches("raw_products", output, conflict_column="product_id")
    log_sync("products", "success", "Products synced", count)
    print(f"Products synced: {count}")


def sync_sales_lines(uid):
    print("Syncing sales lines...")

    from_date = (
        datetime.now(timezone.utc) - timedelta(days=DAYS_BACK)
    ).strftime("%Y-%m-%d")

    domain = [
        ["order_id.date_order", ">=", from_date],
        ["order_id.state", "in", ["sale", "done"]],
        ["product_id", "!=", False]
    ]

    fields = [
        "id",
        "order_id",
        "order_partner_id",
        "product_id",
        "product_uom_qty",
        "price_subtotal",
        "salesman_id",
        "state"
    ]

    rows = fetch_all(uid, "sale.order.line", domain, fields)

    order_ids = list({
        safe_m2o(r.get("order_id"), 0)
        for r in rows
        if safe_m2o(r.get("order_id"), 0)
    })

    order_map = {}

    if order_ids:
        orders = fetch_all(
            uid,
            "sale.order",
            [["id", "in", order_ids]],
            ["id", "name", "date_order", "partner_id", "user_id", "state"]
        )

        for o in orders:
            order_map[o.get("id")] = o

    output = []

    for r in rows:
        order_id = safe_m2o(r.get("order_id"), 0)
        order = order_map.get(order_id, {})

        output.append({
            "odoo_line_id": r.get("id"),
            "order_id": order_id,
            "order_name": order.get("name") or safe_m2o(r.get("order_id"), 1),
            "order_date": order.get("date_order"),
            "customer_id": safe_m2o(order.get("partner_id"), 0) or safe_m2o(r.get("order_partner_id"), 0),
            "customer_name": safe_m2o(order.get("partner_id"), 1) or safe_m2o(r.get("order_partner_id"), 1),
            "salesperson": safe_m2o(order.get("user_id"), 1) or safe_m2o(r.get("salesman_id"), 1),
            "product_id": safe_m2o(r.get("product_id"), 0),
            "product_name": safe_m2o(r.get("product_id"), 1),
            "qty_sold": r.get("product_uom_qty") or 0,
            "subtotal": r.get("price_subtotal") or 0,
            "state": order.get("state") or r.get("state"),
            "created_at": datetime.now(timezone.utc).isoformat()
        })

    count = upsert_in_batches("raw_sales_lines", output, conflict_column="odoo_line_id")
    log_sync("sales_lines", "success", "Sales lines synced", count)
    print(f"Sales lines synced: {count}")


def sync_customer_product_history(uid):
    print("Syncing customer product history...")

    domain = [
        ["order_id.date_order", ">=", CUSTOMER_HISTORY_START],
        ["order_id.state", "in", ["sale", "done"]],
        ["product_id", "!=", False]
    ]

    fields = [
        "id",
        "order_id",
        "product_id",
        "product_uom_qty",
        "price_unit",
        "discount",
        "price_subtotal",
        "salesman_id",
        "state"
    ]

    rows = fetch_all(uid, "sale.order.line", domain, fields)

    order_ids = list({
        safe_m2o(r.get("order_id"), 0)
        for r in rows
        if safe_m2o(r.get("order_id"), 0)
    })

    order_map = {}

    if order_ids:
        orders = fetch_all(
            uid,
            "sale.order",
            [["id", "in", order_ids]],
            ["id", "name", "date_order", "partner_id", "user_id", "state"]
        )

        for o in orders:
            order_map[o.get("id")] = o

    product_ids = list({
        safe_m2o(r.get("product_id"), 0)
        for r in rows
        if safe_m2o(r.get("product_id"), 0)
    })

    product_map = {}

    if product_ids:
        products = fetch_all(
            uid,
            "product.product",
            [["id", "in", product_ids]],
            ["id", "display_name", "categ_id"]
        )

        for p in products:
            product_map[p.get("id")] = p

    output = []

    for r in rows:
        order_id = safe_m2o(r.get("order_id"), 0)
        order = order_map.get(order_id, {})

        product_id = safe_m2o(r.get("product_id"), 0)
        product = product_map.get(product_id, {})

        output.append({
            "odoo_line_id": r.get("id"),
            "order_id": order_id,
            "order_name": order.get("name") or safe_m2o(r.get("order_id"), 1),
            "order_date": order.get("date_order"),
            "customer_id": safe_m2o(order.get("partner_id"), 0),
            "customer_name": safe_m2o(order.get("partner_id"), 1),
            "salesperson_id": safe_m2o(order.get("user_id"), 0) or safe_m2o(r.get("salesman_id"), 0),
            "salesperson": safe_m2o(order.get("user_id"), 1) or safe_m2o(r.get("salesman_id"), 1),
            "product_id": product_id,
            "product_name": product.get("display_name") or safe_m2o(r.get("product_id"), 1),
            "product_category": safe_m2o(product.get("categ_id"), 1),
            "qty_sold": r.get("product_uom_qty") or 0,
            "unit_price": r.get("price_unit") or 0,
            "discount": r.get("discount") or 0,
            "subtotal": r.get("price_subtotal") or 0,
            "state": order.get("state") or r.get("state"),
            "updated_at": datetime.now(timezone.utc).isoformat()
        })

    count = upsert_in_batches("customer_product_history", output, conflict_column="odoo_line_id")
    log_sync("customer_product_history", "success", "Customer product history synced", count)
    print(f"Customer product history synced: {count}")


def sync_current_stock_by_warehouse(uid):
    print("Syncing current stock by warehouse...")

    domain = [
        ["location_id.usage", "=", "internal"],
        ["product_id", "!=", False]
    ]

    fields = [
        "product_id",
        "location_id",
        "quantity",
        "reserved_quantity",
        "company_id"
    ]

    rows = fetch_all(uid, "stock.quant", domain, fields)

    # Fetch product names
    product_ids = list(set([safe_m2o(r.get("product_id"), 0) for r in rows if safe_m2o(r.get("product_id"), 0)]))
    product_map = {}
    if product_ids:
        products = fetch_all(uid, "product.product", [["id", "in", product_ids]], ["id", "display_name"])
        for p in products:
            product_map[p.get("id")] = p.get("display_name")

    # Fetch location and company names
    location_ids = list(set([safe_m2o(r.get("location_id"), 0) for r in rows if safe_m2o(r.get("location_id"), 0)]))
    location_map = {}
    if location_ids:
        locations = fetch_all(uid, "stock.location", [["id", "in", location_ids]], ["id", "display_name", "warehouse_id"])
        for loc in locations:
            location_map[loc.get("id")] = {"name": loc.get("display_name"), "warehouse_id": safe_m2o(loc.get("warehouse_id"), 0)}

    warehouse_ids = list(set([loc["warehouse_id"] for loc in location_map.values() if loc["warehouse_id"]]))
    warehouse_map = {}
    if warehouse_ids:
        warehouses = fetch_all(uid, "stock.warehouse", [["id", "in", warehouse_ids]], ["id", "name"])
        for wh in warehouses:
            warehouse_map[wh.get("id")] = wh.get("name")

    company_ids = list(set([safe_m2o(r.get("company_id"), 0) for r in rows if safe_m2o(r.get("company_id"), 0)]))
    company_map = {}
    if company_ids:
        companies = fetch_all(uid, "res.company", [["id", "in", company_ids]], ["id", "name"])
        for comp in companies:
            company_map[comp.get("id")] = comp.get("name")

    output = []

    for r in rows:
        product_id = safe_m2o(r.get("product_id"), 0)
        location_id = safe_m2o(r.get("location_id"), 0)
        company_id = safe_m2o(r.get("company_id"), 0)

        quantity = r.get("quantity") or 0
        reserved_quantity = r.get("reserved_quantity") or 0

        location_info = location_map.get(location_id, {})
        warehouse_name = warehouse_map.get(location_info.get("warehouse_id")) if location_info.get("warehouse_id") else None

        output.append({
            "product_id": product_id,
            "product_name": product_map.get(product_id),
            "company_id": company_id,
            "company_name": company_map.get(company_id),
            "location_id": location_id,
            "location_name": location_info.get("name"),
            "warehouse_name": warehouse_name,
            "quantity_on_hand": quantity,
            "reserved_quantity": reserved_quantity,
            "available_quantity": quantity - reserved_quantity,
            "updated_at": datetime.now(timezone.utc).isoformat()
        })

    count = upsert_in_batches(
        "current_stock_by_warehouse",
        output,
        conflict_column="product_id,location_id"
    )

    log_sync("current_stock_by_warehouse", "success", "Current stock by warehouse synced", count)
    print(f"Current stock by warehouse synced: {count}")

def sync_stock_quants(uid):
    print("Syncing stock quants...")

    domain = [
        ["location_id.usage", "=", "internal"],
        ["product_id", "!=", False]
    ]

    fields = ["product_id", "location_id", "quantity"]

    rows = fetch_all(uid, "stock.quant", domain, fields)

    output = []

    for r in rows:
        output.append({
            "product_id": safe_m2o(r.get("product_id"), 0),
            "product_name": safe_m2o(r.get("product_id"), 1),
            "location_id": safe_m2o(r.get("location_id"), 0),
            "location_name": safe_m2o(r.get("location_id"), 1),
            "quantity": r.get("quantity") or 0,
            "updated_at": datetime.now(timezone.utc).isoformat()
        })

    count = upsert_in_batches(
        "raw_stock_quants",
        output,
        conflict_column="product_id,location_id"
    )

    log_sync("stock_quants", "success", "Stock quants synced", count)
    print(f"Stock quants synced: {count}")


def sync_product_sales_from_june1(uid):
    print("Syncing product sales from June 1...")

    from_date = "2026-06-01"

    domain = [
        ["order_id.date_order", ">=", from_date],
        ["order_id.state", "in", ["sale", "done"]],
        ["product_id", "!=", False]
    ]

    fields = [
        "id",
        "order_id",
        "product_id",
        "product_uom_qty",
        "price_subtotal",
        "salesman_id",
        "state",
        "company_id",
        "warehouse_id"
    ]

    rows = fetch_all(uid, "sale.order.line", domain, fields)

    order_ids = list({
        safe_m2o(r.get("order_id"), 0)
        for r in rows
        if safe_m2o(r.get("order_id"), 0)
    })

    order_map = {}

    if order_ids:
        orders = fetch_all(
            uid,
            "sale.order",
            [["id", "in", order_ids]],
            ["id", "name", "date_order", "partner_id", "user_id", "state", "company_id", "warehouse_id"]
        )

        for o in orders:
            order_map[o.get("id")] = o

    product_ids = list({
        safe_m2o(r.get("product_id"), 0)
        for r in rows
        if safe_m2o(r.get("product_id"), 0)
    })

    product_map = {}

    if product_ids:
        products = fetch_all(
            uid,
            "product.product",
            [["id", "in", product_ids]],
            ["id", "display_name", "categ_id"]
        )

        for p in products:
            product_map[p.get("id")] = p

    company_ids = list(set([safe_m2o(r.get("company_id"), 0) for r in rows if safe_m2o(r.get("company_id"), 0)]))
    company_map = {}
    if company_ids:
        companies = fetch_all(uid, "res.company", [["id", "in", company_ids]], ["id", "name"])
        for comp in companies:
            company_map[comp.get("id")] = comp.get("name")

    warehouse_ids = list(set([safe_m2o(r.get("warehouse_id"), 0) for r in rows if safe_m2o(r.get("warehouse_id"), 0)]))
    warehouse_map = {}
    if warehouse_ids:
        warehouses = fetch_all(uid, "stock.warehouse", [["id", "in", warehouse_ids]], ["id", "name"])
        for wh in warehouses:
            warehouse_map[wh.get("id")] = wh.get("name")

    output = []

    for r in rows:
        order_id = safe_m2o(r.get("order_id"), 0)
        order = order_map.get(order_id, {})

        product_id = safe_m2o(r.get("product_id"), 0)
        product = product_map.get(product_id, {})

        company_id = safe_m2o(order.get("company_id"), 0)
        warehouse_id = safe_m2o(order.get("warehouse_id"), 0)

        output.append({
            "odoo_line_id": r.get("id"),
            "order_id": order_id,
            "order_name": order.get("name") or safe_m2o(r.get("order_id"), 1),
            "order_date": order.get("date_order"),
            "customer_id": safe_m2o(order.get("partner_id"), 0),
            "customer_name": safe_m2o(order.get("partner_id"), 1),
            "salesperson": safe_m2o(order.get("user_id"), 1) or safe_m2o(r.get("salesman_id"), 1),
            "product_id": product_id,
            "product_name": product.get("display_name") or safe_m2o(r.get("product_id"), 1),
            "product_category": safe_m2o(product.get("categ_id"), 1),
            "company_id": company_id,
            "company_name": company_map.get(company_id),
            "warehouse_id": warehouse_id,
            "warehouse_name": warehouse_map.get(warehouse_id),
            "qty_sold": r.get("product_uom_qty") or 0,
            "subtotal": r.get("price_subtotal") or 0,
            "state": order.get("state") or r.get("state"),
            "updated_at": datetime.now(timezone.utc).isoformat()
        })

    count = upsert_in_batches("product_sales_from_june1", output, conflict_column="odoo_line_id")
    log_sync("product_sales_from_june1", "success", "Product sales from June 1 synced", count)
    print(f"Product sales from June 1 synced: {count}")

def sync_supplier_settings(uid):
    print("Syncing supplier settings...")

    products_response = supabase.table("raw_products").select(
        "product_id, product_tmpl_id"
    ).execute()

    products = products_response.data or []

    tmpl_to_product = {}
    tmpl_ids = []

    for p in products:
        tmpl_id = p.get("product_tmpl_id")
        product_id = p.get("product_id")

        if tmpl_id and product_id:
            tmpl_to_product.setdefault(tmpl_id, product_id)
            tmpl_ids.append(tmpl_id)

    tmpl_ids = list(set(tmpl_ids))

    if not tmpl_ids:
        print("No product templates found. Supplier sync skipped.")
        log_sync("supplier_settings", "skipped", "No product templates found", 0)
        return

    rows = fetch_all(
        uid,
        "product.supplierinfo",
        [["product_tmpl_id", "in", tmpl_ids]],
        [
            "id",
            "product_tmpl_id",
            "partner_id",
            "sequence",
            "delay",
            "min_qty",
            "price"
        ]
    )

    supplier_map = {}

    for r in rows:
        tmpl_id = safe_m2o(r.get("product_tmpl_id"), 0)
        supplier_name = safe_m2o(r.get("partner_id"), 1)

        if not tmpl_id or not supplier_name:
            continue

        supplier_map.setdefault(tmpl_id, []).append({
            "supplier_name": supplier_name,
            "sequence": r.get("sequence") or 999,
            "delay": r.get("delay") or 4,
            "min_qty": r.get("min_qty") or 0,
            "price": r.get("price") or 0
        })

    output = []

    for tmpl_id, suppliers in supplier_map.items():
        product_id = tmpl_to_product.get(tmpl_id)

        if not product_id:
            continue

        suppliers_sorted = sorted(
            suppliers,
            key=lambda x: (x["sequence"], x["price"] or 0)
        )

        primary_supplier = suppliers_sorted[0]["supplier_name"] if len(suppliers_sorted) >= 1 else None
        backup_supplier = suppliers_sorted[1]["supplier_name"] if len(suppliers_sorted) >= 2 else None
        lead_time_days = suppliers_sorted[0]["delay"] if len(suppliers_sorted) >= 1 else 4

        output.append({
            "product_id": product_id,
            "primary_supplier": primary_supplier,
            "backup_supplier": backup_supplier,
            "lead_time_days": lead_time_days,
            "updated_at": datetime.now(timezone.utc).isoformat()
        })

    count = upsert_in_batches(
        "sku_supplier_settings",
        output,
        conflict_column="product_id"
    )

    log_sync("supplier_settings", "success", "Supplier settings synced", count)
    print(f"Supplier settings synced: {count}")


def refresh_sku_master():
    print("Refreshing SKU master...")

    supabase.rpc("refresh_sku_master").execute()

    log_sync("refresh_sku_master", "success", "SKU master refreshed", 0)
    print("SKU master refreshed")


def main():
    try:
        uid = odoo_login()
        print(f"Connected to Odoo. UID: {uid}")

        sync_products(uid)
        sync_customers(uid)
        sync_sales_lines(uid)
        sync_stock_quants(uid)
        sync_supplier_settings(uid)
        sync_customer_product_history(uid)
        sync_product_sales_from_june1(uid)
        sync_current_stock_by_warehouse(uid)
        refresh_sku_master()

        print("Done ✅")

    except Exception as e:
        print("ERROR ❌")
        print(str(e))

        try:
            log_sync("full_sync", "failed", str(e), 0)
        except Exception:
            pass

        raise


if __name__ == "__main__":
    main()
