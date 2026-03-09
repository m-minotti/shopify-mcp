"""
Shopify MCP Server
Connects Claude to your Shopify store via the Admin REST API.
"""

import json
import os
from datetime import datetime
from typing import Any, Dict, List, Optional

import httpx
from mcp.server.fastmcp import FastMCP
from pydantic import BaseModel, ConfigDict, Field

# ── Configuration ────────────────────────────────────────────────────────────

SHOPIFY_ACCESS_TOKEN = os.environ.get("SHOPIFY_ACCESS_TOKEN", "")
SHOPIFY_STORE_URL = os.environ.get("SHOPIFY_STORE_URL", "")  # e.g. my-store.myshopify.com
API_VERSION = "2024-10"
BASE_URL = f"https://{SHOPIFY_STORE_URL}/admin/api/{API_VERSION}"

# ── FastMCP Server ────────────────────────────────────────────────────────────

mcp = FastMCP("shopify_mcp")

# ── Shared HTTP client ────────────────────────────────────────────────────────

def _get_headers() -> Dict[str, str]:
    return {
        "X-Shopify-Access-Token": SHOPIFY_ACCESS_TOKEN,
        "Content-Type": "application/json",
    }

async def _get(endpoint: str, params: Optional[Dict] = None) -> Dict[str, Any]:
    """Make a GET request to the Shopify Admin API."""
    url = f"{BASE_URL}{endpoint}"
    async with httpx.AsyncClient(timeout=30) as client:
        response = client.get(url, headers=_get_headers(), params=params)
        response = await client.get(url, headers=_get_headers(), params=params)
        response.raise_for_status()
        return response.json()

def _handle_error(e: Exception) -> str:
    if isinstance(e, httpx.HTTPStatusError):
        code = e.response.status_code
        if code == 401:
            return "Error: Invalid access token. Please check your SHOPIFY_ACCESS_TOKEN."
        if code == 403:
            return "Error: Permission denied. Make sure the app has the required API scopes."
        if code == 404:
            return "Error: Resource not found. Please check the ID."
        if code == 429:
            return "Error: Rate limit hit. Please wait a moment and try again."
        return f"Error: Shopify API returned status {code}: {e.response.text}"
    if isinstance(e, httpx.TimeoutException):
        return "Error: Request timed out. Please try again."
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
    status: str = Field(default="any", description="Order status filter: 'open', 'closed', 'cancelled', 'any'")
    financial_status: Optional[str] = Field(default=None, description="Financial status: 'paid', 'pending', 'refunded', etc.")
    fulfillment_status: Optional[str] = Field(default=None, description="Fulfillment status: 'fulfilled', 'unfulfilled', 'partial'")

class GetOrderInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    order_id: str = Field(..., description="Shopify order ID (numeric, e.g. '5678901234')")

class ListProductsInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    limit: int = Field(default=20, ge=1, le=250, description="Number of products to return (max 250)")
    status: str = Field(default="active", description="Product status: 'active', 'archived', 'draft'")
    vendor: Optional[str] = Field(default=None, description="Filter by vendor name")
    product_type: Optional[str] = Field(default=None, description="Filter by product type")

class GetProductInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    product_id: str = Field(..., description="Shopify product ID (numeric)")

class ListCustomersInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    limit: int = Field(default=20, ge=1, le=250, description="Number of customers to return (max 250)")
    query: Optional[str] = Field(default=None, description="Search query (name, email, phone, etc.)")

class SalesReportInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    days: int = Field(default=30, ge=1, le=365, description="Number of past days to include in the report")

# ── Tools ─────────────────────────────────────────────────────────────────────

@mcp.tool(
    name="shopify_list_orders",
    annotations={"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True}
)
async def shopify_list_orders(params: ListOrdersInput) -> str:
    """List recent orders from the Shopify store.

    Returns order number, customer name, total price, financial and fulfillment
    status, and creation date for each order.

    Args:
        params: limit, status, financial_status, fulfillment_status

    Returns:
        str: Markdown-formatted list of orders with key details.
    """
    try:
        query: Dict[str, Any] = {"limit": params.limit, "status": params.status}
        if params.financial_status:
            query["financial_status"] = params.financial_status
        if params.fulfillment_status:
            query["fulfillment_status"] = params.fulfillment_status

        data = await _get("/orders.json", query)
        orders = data.get("orders", [])

        if not orders:
            return "No orders found matching the criteria."

        lines = [f"## Orders ({len(orders)} results)\n"]
        for o in orders:
            customer = o.get("customer") or {}
            name = f"{customer.get('first_name', '')} {customer.get('last_name', '')}".strip() or "Guest"
            lines.append(
                f"- **#{o['order_number']}** | {name} | "
                f"${o['total_price']} {o['currency']} | "
                f"Financial: {o['financial_status']} | "
                f"Fulfillment: {o.get('fulfillment_status') or 'unfulfilled'} | "
                f"Date: {_fmt_date(o.get('created_at'))} | "
                f"ID: {o['id']}"
            )
        return "\n".join(lines)
    except Exception as e:
        return _handle_error(e)


@mcp.tool(
    name="shopify_get_order",
    annotations={"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True}
)
async def shopify_get_order(params: GetOrderInput) -> str:
    """Get full details of a single Shopify order by ID.

    Returns customer info, line items, pricing breakdown, shipping address,
    fulfillment status, and timeline notes.

    Args:
        params: order_id (numeric Shopify order ID)

    Returns:
        str: Markdown-formatted order details.
    """
    try:
        data = await _get(f"/orders/{params.order_id}.json")
        o = data.get("order", {})

        customer = o.get("customer") or {}
        name = f"{customer.get('first_name', '')} {customer.get('last_name', '')}".strip() or "Guest"
        email = customer.get("email", "N/A")

        # Line items
        items_lines = []
        for item in o.get("line_items", []):
            items_lines.append(
                f"  - {item['name']} x{item['quantity']} @ ${item['price']} each"
            )

        # Shipping address
        ship = o.get("shipping_address") or {}
        address = (
            f"{ship.get('address1', '')}, {ship.get('city', '')}, "
            f"{ship.get('province', '')}, {ship.get('country', '')}"
            if ship else "N/A"
        )

        return (
            f"## Order #{o.get('order_number')} (ID: {o.get('id')})\n\n"
            f"**Customer:** {name} ({email})\n"
            f"**Date:** {_fmt_date(o.get('created_at'))}\n"
            f"**Financial Status:** {o.get('financial_status')}\n"
            f"**Fulfillment Status:** {o.get('fulfillment_status') or 'unfulfilled'}\n\n"
            f"**Items:**\n" + "\n".join(items_lines) + "\n\n"
            f"**Subtotal:** ${o.get('subtotal_price')}\n"
            f"**Shipping:** ${o.get('total_shipping_price_set', {}).get('shop_money', {}).get('amount', '0.00')}\n"
            f"**Total:** ${o.get('total_price')} {o.get('currency')}\n\n"
            f"**Shipping Address:** {address}\n"
            f"**Note:** {o.get('note') or 'None'}"
        )
    except Exception as e:
        return _handle_error(e)


@mcp.tool(
    name="shopify_list_products",
    annotations={"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True}
)
async def shopify_list_products(params: ListProductsInput) -> str:
    """List products in the Shopify store.

    Returns product title, vendor, type, price range, inventory count,
    and status for each product.

    Args:
        params: limit, status, vendor, product_type

    Returns:
        str: Markdown-formatted product list.
    """
    try:
        query: Dict[str, Any] = {"limit": params.limit, "status": params.status}
        if params.vendor:
            query["vendor"] = params.vendor
        if params.product_type:
            query["product_type"] = params.product_type

        data = await _get("/products.json", query)
        products = data.get("products", [])

        if not products:
            return "No products found matching the criteria."

        lines = [f"## Products ({len(products)} results)\n"]
        for p in products:
            variants = p.get("variants", [])
            prices = [float(v["price"]) for v in variants if v.get("price")]
            inventory = sum(v.get("inventory_quantity", 0) for v in variants)
            price_str = (
                f"${min(prices):.2f} – ${max(prices):.2f}"
                if len(set(prices)) > 1
                else f"${prices[0]:.2f}" if prices else "N/A"
            )
            lines.append(
                f"- **{p['title']}** | {p.get('vendor', 'N/A')} | "
                f"{p.get('product_type') or 'No type'} | "
                f"Price: {price_str} | Inventory: {inventory} | "
                f"Status: {p.get('status')} | ID: {p['id']}"
            )
        return "\n".join(lines)
    except Exception as e:
        return _handle_error(e)


@mcp.tool(
    name="shopify_get_product",
    annotations={"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True}
)
async def shopify_get_product(params: GetProductInput) -> str:
    """Get full details of a single Shopify product by ID.

    Returns title, description, variants with prices and inventory,
    images, tags, and SEO info.

    Args:
        params: product_id (numeric Shopify product ID)

    Returns:
        str: Markdown-formatted product details.
    """
    try:
        data = await _get(f"/products/{params.product_id}.json")
        p = data.get("product", {})

        variant_lines = []
        for v in p.get("variants", []):
            variant_lines.append(
                f"  - {v.get('title', 'Default')} | "
                f"SKU: {v.get('sku') or 'N/A'} | "
                f"Price: ${v.get('price')} | "
                f"Inventory: {v.get('inventory_quantity', 0)}"
            )

        return (
            f"## {p.get('title')}\n\n"
            f"**ID:** {p.get('id')}\n"
            f"**Vendor:** {p.get('vendor')}\n"
            f"**Type:** {p.get('product_type') or 'N/A'}\n"
            f"**Status:** {p.get('status')}\n"
            f"**Tags:** {p.get('tags') or 'None'}\n"
            f"**Created:** {_fmt_date(p.get('created_at'))}\n\n"
            f"**Description:**\n{p.get('body_html') or 'No description'}\n\n"
            f"**Variants:**\n" + "\n".join(variant_lines)
        )
    except Exception as e:
        return _handle_error(e)


@mcp.tool(
    name="shopify_list_customers",
    annotations={"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True}
)
async def shopify_list_customers(params: ListCustomersInput) -> str:
    """List or search customers in the Shopify store.

    Returns name, email, total orders, total spent, and account creation date.

    Args:
        params: limit, query (optional search string)

    Returns:
        str: Markdown-formatted customer list.
    """
    try:
        query: Dict[str, Any] = {"limit": params.limit}
        if params.query:
            query["query"] = params.query

        data = await _get("/customers/search.json" if params.query else "/customers.json", query)
        customers = data.get("customers", [])

        if not customers:
            return "No customers found."

        lines = [f"## Customers ({len(customers)} results)\n"]
        for c in customers:
            name = f"{c.get('first_name', '')} {c.get('last_name', '')}".strip() or "No name"
            lines.append(
                f"- **{name}** | {c.get('email', 'No email')} | "
                f"Orders: {c.get('orders_count', 0)} | "
                f"Total Spent: ${c.get('total_spent', '0.00')} | "
                f"Since: {_fmt_date(c.get('created_at'))} | "
                f"ID: {c['id']}"
            )
        return "\n".join(lines)
    except Exception as e:
        return _handle_error(e)


@mcp.tool(
    name="shopify_sales_report",
    annotations={"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True}
)
async def shopify_sales_report(params: SalesReportInput) -> str:
    """Generate a sales summary report for the past N days.

    Calculates total revenue, order count, average order value,
    top products by quantity sold, and order status breakdown.

    Args:
        params: days (number of past days to include, default 30)

    Returns:
        str: Markdown-formatted sales report.
    """
    try:
        from datetime import timedelta, timezone
        since = (datetime.now(timezone.utc) - timedelta(days=params.days)).isoformat()

        data = await _get("/orders.json", {
            "status": "any",
            "created_at_min": since,
            "limit": 250,
        })
        orders = data.get("orders", [])

        if not orders:
            return f"No orders found in the past {params.days} days."

        # Revenue & counts
        paid_orders = [o for o in orders if o.get("financial_status") == "paid"]
        total_revenue = sum(float(o["total_price"]) for o in paid_orders)
        avg_order = total_revenue / len(paid_orders) if paid_orders else 0

        # Status breakdown
        status_counts: Dict[str, int] = {}
        for o in orders:
            s = o.get("financial_status", "unknown")
            status_counts[s] = status_counts.get(s, 0) + 1

        # Top products
        product_qty: Dict[str, int] = {}
        for o in orders:
            for item in o.get("line_items", []):
                title = item.get("title", "Unknown")
                product_qty[title] = product_qty.get(title, 0) + item.get("quantity", 0)
        top_products = sorted(product_qty.items(), key=lambda x: x[1], reverse=True)[:5]

        currency = orders[0].get("currency", "") if orders else ""

        lines = [
            f"## Sales Report — Last {params.days} Days\n",
            f"**Total Orders:** {len(orders)}",
            f"**Paid Orders:** {len(paid_orders)}",
            f"**Total Revenue:** ${total_revenue:,.2f} {currency}",
            f"**Average Order Value:** ${avg_order:,.2f} {currency}",
            f"\n### Order Status Breakdown",
        ]
        for status, count in sorted(status_counts.items()):
            lines.append(f"- {status.capitalize()}: {count}")

        lines.append("\n### Top 5 Products by Units Sold")
        for title, qty in top_products:
            lines.append(f"- {title}: {qty} units")

        return "\n".join(lines)
    except Exception as e:
        return _handle_error(e)


@mcp.tool(
    name="shopify_store_info",
    annotations={"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True}
)
async def shopify_store_info() -> str:
    """Get general information about the Shopify store.

    Returns store name, email, currency, timezone, plan, and domain.

    Returns:
        str: Markdown-formatted store overview.
    """
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


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    if not SHOPIFY_ACCESS_TOKEN or not SHOPIFY_STORE_URL:
        print("ERROR: Set SHOPIFY_ACCESS_TOKEN and SHOPIFY_STORE_URL environment variables.")
        exit(1)
    import uvicorn
    port = int(os.environ.get("PORT", 8000))
    app = mcp.streamable_http_app()
    uvicorn.run(app, host="0.0.0.0", port=port)
