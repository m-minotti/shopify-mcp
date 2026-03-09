"""
Shopify MCP Server
Connects Claude to your Shopify store via the Admin REST API.
"""

import os
from datetime import datetime
from typing import Any, Dict, Optional

import httpx
from mcp.server.fastmcp import FastMCP
from pydantic import BaseModel, ConfigDict, Field

# ── Configuration ────────────────────────────────────────────────────────────

SHOPIFY_ACCESS_TOKEN = os.environ.get("SHOPIFY_ACCESS_TOKEN", "")
SHOPIFY_STORE_URL = os.environ.get("SHOPIFY_STORE_URL", "")
API_VERSION = "2024-10"
BASE_URL = f"https://{SHOPIFY_STORE_URL}/admin/api/{API_VERSION}"

# ── FastMCP Server ────────────────────────────────────────────────────────────

mcp = FastMCP("shopify_mcp", stateless_http=True)

# ── Shared helpers ────────────────────────────────────────────────────────────

def _get_headers() -> Dict[str, str]:
    return {
        "X-Shopify-Access-Token": SHOPIFY_ACCESS_TOKEN,
        "Content-Type": "application/json",
    }

async def _get(endpoint: str, params: Optional[Dict] = None) -> Dict[str, Any]:
    url = f"{BASE_URL}{endpoint}"
    async with httpx.AsyncClient(timeout=30) as client:
        response = await client.get(url, headers=_get_headers(), params=params)
        response.raise_for_status()
        return response.json()

def _handle_error(e: Exception) -> str:
    if isinstance(e, httpx.HTTPStatusError):
        code = e.response.status_code
        if code == 401:
            return "Error: Invalid access token. Please check SHOPIFY_ACCESS_TOKEN."
        if code == 403:
            return "Error: Permission denied. Check API scopes."
        if code == 404:
            return "Error: Resource not found."
        if code == 429:
            return "Error: Rate limit hit. Please wait and retry."
        return f"Error: Shopify API status {code}: {e.response.text}"
    if isinstance(e, httpx.TimeoutException):
        return "Error: Request timed out."
    return f"Error: {type(e).__name__}: {str(e)}"

def _fmt_date(iso: Optional[str]) -> str:
    if not iso:
        return "N/A"
    try:
        return datetime.fromisoformat(iso.replace("Z", "+00:00")).strftime("%Y-%m-%d %H:%M UTC")
    except Exception:
        return iso

# ── Input Models ──────────────────────────────────────────────────────────────

class ListOrdersInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    limit: int = Field(default=20, ge=1, le=250, description="Number of orders to return (max 250)")
    status: str = Field(default="any", description="Order status: 'open', 'closed', 'cancelled', 'any'")
    financial_status: Optional[str] = Field(default=None, description="Financial status: 'paid', 'pending', 'refunded'")
    fulfillment_status: Optional[str] = Field(default=None, description="Fulfillment: 'fulfilled', 'unfulfilled', 'partial'")

class GetOrderInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    order_id: str = Field(..., description="Shopify order ID (numeric)")

class ListProductsInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    limit: int = Field(default=20, ge=1, le=250, description="Number of products to return (max 250)")
    status: str = Field(default="active", description="Product status: 'active', 'archived', 'draft'")
    vendor: Optional[str] = Field(default=None, description="Filter by vendor name")

class GetProductInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    product_id: str = Field(..., description="Shopify product ID (numeric)")

class ListCustomersInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    limit: int = Field(default=20, ge=1, le=250, description="Number of customers to return (max 250)")
    query: Optional[str] = Field(default=None, description="Search by name, email, or phone")

class SalesReportInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    days: int = Field(default=30, ge=1, le=365, description="Number of past days to include")

# ── Tools ─────────────────────────────────────────────────────────────────────

@mcp.tool(name="shopify_store_info", annotations={"readOnlyHint": True, "destructiveHint": False})
async def shopify_store_info() -> str:
    """Get general information about the Shopify store including name, currency, timezone, and plan."""
    try:
        data = await _get("/shop.json")
        s = data.get("shop", {})
        return (
            f"## Store: {s.get('name')}\n\n"
            f"**Domain:** {s.get('domain')}\n"
            f"**Email:** {s.get('email')}\n"
            f"**Currency:** {s.get('currency')}\n"
            f"**Timezone:** {s.get('iana_timezone')}\n"
            f"**Plan:** {s.get('plan_display_name')}\n"
            f"**Country:** {s.get('country_name')}\n"
            f"**Created:** {_fmt_date(s.get('created_at'))}"
        )
    except Exception as e:
        return _handle_error(e)


@mcp.tool(name="shopify_list_orders", annotations={"readOnlyHint": True, "destructiveHint": False})
async def shopify_list_orders(params: ListOrdersInput) -> str:
    """List recent orders from the Shopify store with status, customer, and pricing info."""
    try:
        query: Dict[str, Any] = {"limit": params.limit, "status": params.status}
        if params.financial_status:
            query["financial_status"] = params.financial_status
        if params.fulfillment_status:
            query["fulfillment_status"] = params.fulfillment_status

        data = await _get("/orders.json", query)
        orders = data.get("orders", [])
        if not orders:
            return "No orders found."

        lines = [f"## Orders ({len(orders)} results)\n"]
        for o in orders:
            customer = o.get("customer") or {}
            name = f"{customer.get('first_name', '')} {customer.get('last_name', '')}".strip() or "Guest"
            lines.append(
                f"- **#{o['order_number']}** | {name} | ${o['total_price']} {o['currency']} | "
                f"Financial: {o['financial_status']} | "
                f"Fulfillment: {o.get('fulfillment_status') or 'unfulfilled'} | "
                f"Date: {_fmt_date(o.get('created_at'))} | ID: {o['id']}"
            )
        return "\n".join(lines)
    except Exception as e:
        return _handle_error(e)


@mcp.tool(name="shopify_get_order", annotations={"readOnlyHint": True, "destructiveHint": False})
async def shopify_get_order(params: GetOrderInput) -> str:
    """Get full details of a single Shopify order including line items, pricing, and shipping."""
    try:
        data = await _get(f"/orders/{params.order_id}.json")
        o = data.get("order", {})
        customer = o.get("customer") or {}
        name = f"{customer.get('first_name', '')} {customer.get('last_name', '')}".strip() or "Guest"
        items = "\n".join(
            f"  - {i['name']} x{i['quantity']} @ ${i['price']}"
            for i in o.get("line_items", [])
        )
        ship = o.get("shipping_address") or {}
        address = (
            f"{ship.get('address1')}, {ship.get('city')}, {ship.get('province')}, {ship.get('country')}"
            if ship else "N/A"
        )
        return (
            f"## Order #{o.get('order_number')} (ID: {o.get('id')})\n\n"
            f"**Customer:** {name} ({customer.get('email', 'N/A')})\n"
            f"**Date:** {_fmt_date(o.get('created_at'))}\n"
            f"**Financial:** {o.get('financial_status')} | **Fulfillment:** {o.get('fulfillment_status') or 'unfulfilled'}\n\n"
            f"**Items:**\n{items}\n\n"
            f"**Subtotal:** ${o.get('subtotal_price')} | **Total:** ${o.get('total_price')} {o.get('currency')}\n"
            f"**Ship To:** {address}\n"
            f"**Note:** {o.get('note') or 'None'}"
        )
    except Exception as e:
        return _handle_error(e)


@mcp.tool(name="shopify_list_products", annotations={"readOnlyHint": True, "destructiveHint": False})
async def shopify_list_products(params: ListProductsInput) -> str:
    """List products in the Shopify store with prices and inventory counts."""
    try:
        query: Dict[str, Any] = {"limit": params.limit, "status": params.status}
        if params.vendor:
            query["vendor"] = params.vendor

        data = await _get("/products.json", query)
        products = data.get("products", [])
        if not products:
            return "No products found."

        lines = [f"## Products ({len(products)} results)\n"]
        for p in products:
            variants = p.get("variants", [])
            prices = [float(v["price"]) for v in variants if v.get("price")]
            inventory = sum(v.get("inventory_quantity", 0) for v in variants)
            price_str = f"${min(prices):.2f}–${max(prices):.2f}" if len(set(prices)) > 1 else (f"${prices[0]:.2f}" if prices else "N/A")
            lines.append(
                f"- **{p['title']}** | {p.get('vendor', 'N/A')} | Price: {price_str} | "
                f"Inventory: {inventory} | Status: {p.get('status')} | ID: {p['id']}"
            )
        return "\n".join(lines)
    except Exception as e:
        return _handle_error(e)


@mcp.tool(name="shopify_get_product", annotations={"readOnlyHint": True, "destructiveHint": False})
async def shopify_get_product(params: GetProductInput) -> str:
    """Get full details of a single Shopify product including variants, SKUs, and inventory."""
    try:
        data = await _get(f"/products/{params.product_id}.json")
        p = data.get("product", {})
        variants = "\n".join(
            f"  - {v.get('title', 'Default')} | SKU: {v.get('sku') or 'N/A'} | "
            f"Price: ${v.get('price')} | Inventory: {v.get('inventory_quantity', 0)}"
            for v in p.get("variants", [])
        )
        return (
            f"## {p.get('title')}\n\n"
            f"**ID:** {p.get('id')} | **Vendor:** {p.get('vendor')} | **Status:** {p.get('status')}\n"
            f"**Tags:** {p.get('tags') or 'None'}\n"
            f"**Created:** {_fmt_date(p.get('created_at'))}\n\n"
            f"**Variants:**\n{variants}"
        )
    except Exception as e:
        return _handle_error(e)


@mcp.tool(name="shopify_list_customers", annotations={"readOnlyHint": True, "destructiveHint": False})
async def shopify_list_customers(params: ListCustomersInput) -> str:
    """List or search customers with order counts and total spend."""
    try:
        query: Dict[str, Any] = {"limit": params.limit}
        if params.query:
            query["query"] = params.query
        endpoint = "/customers/search.json" if params.query else "/customers.json"
        data = await _get(endpoint, query)
        customers = data.get("customers", [])
        if not customers:
            return "No customers found."

        lines = [f"## Customers ({len(customers)} results)\n"]
        for c in customers:
            name = f"{c.get('first_name', '')} {c.get('last_name', '')}".strip() or "No name"
            lines.append(
                f"- **{name}** | {c.get('email', 'No email')} | "
                f"Orders: {c.get('orders_count', 0)} | Spent: ${c.get('total_spent', '0.00')} | "
                f"Since: {_fmt_date(c.get('created_at'))} | ID: {c['id']}"
            )
        return "\n".join(lines)
    except Exception as e:
        return _handle_error(e)


@mcp.tool(name="shopify_sales_report", annotations={"readOnlyHint": True, "destructiveHint": False})
async def shopify_sales_report(params: SalesReportInput) -> str:
    """Generate a sales summary report for the past N days with revenue, top products, and order breakdown."""
    try:
        from datetime import timedelta, timezone
        since = (datetime.now(timezone.utc) - timedelta(days=params.days)).isoformat()
        data = await _get("/orders.json", {"status": "any", "created_at_min": since, "limit": 250})
        orders = data.get("orders", [])
        if not orders:
            return f"No orders in the past {params.days} days."

        paid = [o for o in orders if o.get("financial_status") == "paid"]
        revenue = sum(float(o["total_price"]) for o in paid)
        avg = revenue / len(paid) if paid else 0
        currency = orders[0].get("currency", "") if orders else ""

        status_counts: Dict[str, int] = {}
        product_qty: Dict[str, int] = {}
        for o in orders:
            s = o.get("financial_status", "unknown")
            status_counts[s] = status_counts.get(s, 0) + 1
            for item in o.get("line_items", []):
                t = item.get("title", "Unknown")
                product_qty[t] = product_qty.get(t, 0) + item.get("quantity", 0)

        top = sorted(product_qty.items(), key=lambda x: x[1], reverse=True)[:5]

        lines = [
            f"## Sales Report — Last {params.days} Days\n",
            f"**Total Orders:** {len(orders)} | **Paid:** {len(paid)}",
            f"**Revenue:** ${revenue:,.2f} {currency} | **Avg Order:** ${avg:,.2f}",
            "\n### Status Breakdown",
        ] + [f"- {k.capitalize()}: {v}" for k, v in sorted(status_counts.items())] + [
            "\n### Top 5 Products by Units Sold",
        ] + [f"- {t}: {q} units" for t, q in top]

        return "\n".join(lines)
    except Exception as e:
        return _handle_error(e)


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    if not SHOPIFY_ACCESS_TOKEN or not SHOPIFY_STORE_URL:
        print("ERROR: Set SHOPIFY_ACCESS_TOKEN and SHOPIFY_STORE_URL environment variables.")
        exit(1)
    import uvicorn
    port = int(os.environ.get("PORT", 8000))
    app = mcp.streamable_http_app()
    uvicorn.run(app, host="0.0.0.0", port=port)
