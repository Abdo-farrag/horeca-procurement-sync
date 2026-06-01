import os
import xmlrpc.client
from datetime import datetime
from supabase import create_client

ODOO_URL = os.getenv("ODOO_URL")
ODOO_DB = os.getenv("ODOO_DB")
ODOO_USERNAME = os.getenv("ODOO_USERNAME")
ODOO_PASSWORD = os.getenv("ODOO_PASSWORD")

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_SERVICE_KEY")

supabase = create_client(SUPABASE_URL, SUPABASE_KEY)

common = xmlrpc.client.ServerProxy(f"{ODOO_URL}/xmlrpc/2/common")
uid = common.authenticate(ODOO_DB, ODOO_USERNAME, ODOO_PASSWORD, {})

models = xmlrpc.client.ServerProxy(f"{ODOO_URL}/xmlrpc/2/object")


def clean_many2one(value):
    if isinstance(value, list) and len(value) >= 2:
        return value[0], value[1]
    return None, None


def upsert(table, rows, chunk_size=500):
    if not rows:
        return

    for i in range(0, len(rows), chunk_size):
        chunk = rows[i:i + chunk_size]
        supabase.schema("intelligence").table(table).upsert(chunk).execute()


def sync_customers():
    fields = [
        "id", "name", "mobile", "phone", "street", "city",
        "state_id", "user_id", "team_id",
        "customer_rank", "active", "create_date", "write_date",
        "partner_latitude", "partner_longitude",
        "category_id", "industry_id"
    ]

    records = models.execute_kw(
        ODOO_DB, uid, ODOO_PASSWORD,
        "res.partner", "search_read",
        [[["customer_rank", ">", 0]]],
        {"fields": fields, "limit": 0}
    )

    rows = []

    for r in records:
        state_id, state_name = clean_many2one(r.get("state_id"))
        user_id, user_name = clean_many2one(r.get("user_id"))
        team_id, team_name = clean_many2one(r.get("team_id"))
        industry_id, industry_name = clean_many2one(r.get("industry_id"))

        tags = r.get("category_id") or []

        rows.append({
            "customer_id": r["id"],
            "customer_name": r.get("name"),
            "mobile": r.get("mobile"),
            "phone": r.get("phone"),
            "address": r.get("street"),
            "city": r.get("city"),
            "state": state_name,
            "salesperson_id": user_id,
            "salesperson_name": user_name,
            "sales_team_id": team_id,
            "sales_team_name": team_name,
            "customer_rank": r.get("customer_rank"),
            "active": r.get("active"),
            "create_date": r.get("create_date"),
            "write_date": r.get("write_date"),
            "latitude": r.get("partner_latitude"),
            "longitude": r.get("partner_longitude"),
            "customer_tags": ",".join(map(str, tags)) if tags else None,
            "activity": industry_name,
            "synced_at": datetime.utcnow().isoformat()
        })

    upsert("odoo_customers_v2", rows)
    print(f"Customers synced: {len(rows)}")


def sync_products():
    fields = [
        "id", "display_name", "default_code", "categ_id",
        "active", "create_date", "write_date"
    ]

    records = models.execute_kw(
        ODOO_DB, uid, ODOO_PASSWORD,
        "product.product", "search_read",
        [[]],
        {"fields": fields, "limit": 0}
    )

    rows = []

    for r in records:
        category_id, category_name = clean_many2one(r.get("categ_id"))

        rows.append({
            "product_id": r["id"],
            "product_name": r.get("display_name"),
            "default_code": r.get("default_code"),
            "category_id": category_id,
            "category_name": category_name,
            "brand": None,
            "active": r.get("active"),
            "create_date": r.get("create_date"),
            "write_date": r.get("write_date"),
            "synced_at": datetime.utcnow().isoformat()
        })

    upsert("odoo_products_v2", rows)
    print(f"Products synced: {len(rows)}")


def sync_sales_orders():
    fields = [
        "id", "name", "partner_id", "date_order", "amount_total",
        "state", "user_id", "team_id", "create_date", "write_date"
    ]

    records = models.execute_kw(
        ODOO_DB, uid, ODOO_PASSWORD,
        "sale.order", "search_read",
        [[["state", "in", ["sale", "done"]]]],
        {"fields": fields, "limit": 0}
    )

    rows = []

    for r in records:
        customer_id, _ = clean_many2one(r.get("partner_id"))
        user_id, user_name = clean_many2one(r.get("user_id"))
        team_id, team_name = clean_many2one(r.get("team_id"))

        rows.append({
            "order_id": r["id"],
            "order_name": r.get("name"),
            "customer_id": customer_id,
            "order_date": r.get("date_order"),
            "amount_total": r.get("amount_total"),
            "state": r.get("state"),
            "salesperson_id": user_id,
            "salesperson_name": user_name,
            "sales_team_id": team_id,
            "sales_team_name": team_name,
            "create_date": r.get("create_date"),
            "write_date": r.get("write_date"),
            "synced_at": datetime.utcnow().isoformat()
        })

    upsert("odoo_sales_orders_v2", rows)
    print(f"Sales orders synced: {len(rows)}")


def sync_sales_order_lines():
    fields = [
        "id", "order_id", "product_id", "product_uom_qty",
        "price_unit", "price_subtotal", "price_total",
        "create_date", "write_date"
    ]

    records = models.execute_kw(
        ODOO_DB, uid, ODOO_PASSWORD,
        "sale.order.line", "search_read",
        [[["order_id.state", "in", ["sale", "done"]]]],
        {"fields": fields, "limit": 0}
    )

    rows = []

    for r in records:
        order_id, order_name = clean_many2one(r.get("order_id"))
        product_id, product_name = clean_many2one(r.get("product_id"))

        # نجيب بيانات الأوردر علشان customer_id/order_date/state
        order = models.execute_kw(
            ODOO_DB, uid, ODOO_PASSWORD,
            "sale.order", "read",
            [[order_id]],
            {"fields": ["partner_id", "date_order", "state"]}
        )[0]

        customer_id, _ = clean_many2one(order.get("partner_id"))

        rows.append({
            "line_id": r["id"],
            "order_id": order_id,
            "customer_id": customer_id,
            "product_id": product_id,
            "product_name": product_name,
            "qty": r.get("product_uom_qty"),
            "price_unit": r.get("price_unit"),
            "subtotal": r.get("price_subtotal"),
            "total": r.get("price_total"),
            "order_date": order.get("date_order"),
            "state": order.get("state"),
            "create_date": r.get("create_date"),
            "write_date": r.get("write_date"),
            "synced_at": datetime.utcnow().isoformat()
        })

    upsert("odoo_sales_order_lines_v2", rows)
    print(f"Sales order lines synced: {len(rows)}")


if __name__ == "__main__":
    sync_customers()
    sync_products()
    sync_sales_orders()
    sync_sales_order_lines()
    print("V2 sync completed")
