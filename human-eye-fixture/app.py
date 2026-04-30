"""DarkShop — 12-bug e-commerce fixture for human-eye testing.

Companion fixture to BuggyTasks. Where BuggyTasks seeds mostly mechanical
bugs that static analysis catches (console errors, count mismatches,
fake delete), DarkShop seeds bugs only a *human* (or a sufficiently
smart LLM) catches: dark patterns, cross-page state inconsistency,
visual hierarchy inverted, deceptive feedback, engineer-speak errors,
checkout flows that are subtly hostile.

Run:
    python human-eye-fixture/app.py    # http://127.0.0.1:5556

=== BUG CATALOG ===

DARK PATTERNS / DECEPTIVE COPY:
  1. "Only 3 left!" stock badge — value is hardcoded and never changes
     no matter how many people add to cart.
  2. "-50% Sale" badge on every product — `original_price` and
     `sale_price` are identical, so the discount is a lie.
  3. "Free shipping over $50" banner on the catalog — but cart and
     checkout always charge a flat $5 shipping fee, regardless.

VISUAL HIERARCHY INVERTED:
  4. Product page: "Add to Cart" rendered with a faded grey style,
     while "Subscribe to Newsletter" is the prominent green primary
     button. The dev mistakenly applied the primary class to the
     less-important action.
  5. Star rating "4.8 ★★★★★" displayed prominently, but the
     "(based on 1 review)" hint is in tiny grey text below — typical
     review-count obfuscation.

CHECKOUT-FLOW HOSTILE:
  6. Checkout form lays CVV BEFORE card number — cognitively wrong;
     real checkouts never do this.
  7. Order-summary side panel shows pre-tax subtotal; the main
     checkout area shows post-tax total — the two numbers visibly
     differ on the same screen.
  8. "Place Order" rendered as a small grey link in the corner;
     "Edit Cart" rendered as the giant green primary CTA — every
     user who is "almost done" is funnelled back to the cart.
  9. The bottom legal checkbox is marked with the same red asterisk
     as required fields, but its label says "(optional)" — directly
     contradictory.

CROSS-PAGE INCONSISTENCY:
 10. Profile name change: editing the display name on /account
     succeeds, but the navbar greeting still shows the OLD name on
     every page until full hard refresh.
 11. Cart count badge in the navbar reports `cart_count` from a
     stale cookie — adding an item updates /cart but not the badge.

SILENT FAILURE / MISLEADING FEEDBACK:
 12. Discount code "SAVE10" silently clears the field on submit
     with no message — the user thinks the input was rejected when
     in fact the code was applied (or vice versa: nothing happened).
"""
import json
import time
import html as html_mod
from starlette.applications import Starlette
from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse
from starlette.routing import Route
from starlette.requests import Request


# ── In-memory state ───────────────────────────────────────────────────

_products = []
_cart = []  # list of {product_id, qty}
_account = {"name": "Alex", "email": "alex@example.com"}
_orders = []
_seen_codes = set()


def _seed_products():
    global _products
    _products = [
        {
            "id": 1, "name": "Wireless Headphones",
            "price": 89.99, "original_price": 89.99,  # BUG #2: same as price
            "stock_label": "Only 3 left!",  # BUG #1: hardcoded
            "rating": 4.8, "review_count": 1,  # BUG #5: misleading rating
            "image": None,
            "description": "Premium over-ear cans with 40 hour battery...",
        },
        {
            "id": 2, "name": "Smart Mug Warmer",
            "price": 39.99, "original_price": 39.99,  # BUG #2
            "stock_label": "Only 3 left!",  # BUG #1
            "rating": 4.9, "review_count": 1,  # BUG #5
            "image": None,
            "description": "Keeps your coffee at the perfect temperature.",
        },
        {
            "id": 3, "name": "Mechanical Keyboard",
            "price": 129.99, "original_price": 129.99,  # BUG #2
            "stock_label": "Only 3 left!",  # BUG #1
            "rating": 4.7, "review_count": 1,  # BUG #5
            "image": None,
            "description": "Hot-swappable switches and per-key RGB.",
        },
    ]


_seed_products()
esc = html_mod.escape


# ── Layout ────────────────────────────────────────────────────────────

def _nav(cart_count_badge=None):
    """Render the top nav. The cart badge intentionally reads from
    `_account["stale_cart_count"]` — set on login, never updated when
    items are added. (BUG #11)"""
    if cart_count_badge is None:
        cart_count_badge = _account.get("stale_cart_count", 0)
    name = _account.get("nav_display_name", _account["name"])  # BUG #10
    return f"""
    <nav>
      <div class="nav-brand">DarkShop</div>
      <div class="nav-links">
        <a href="/">Home</a>
        <a href="/shop">Shop</a>
        <a href="/cart">Cart ({cart_count_badge})</a>
        <a href="/account">Account</a>
      </div>
      <div class="nav-greet">Hi, {esc(name)}</div>
    </nav>
    """


def _layout(title, body):
    return f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{esc(title)} — DarkShop</title>
<style>
* {{ margin:0; padding:0; box-sizing:border-box; }}
body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
        background: #f7f8fa; color: #1a1a2e; }}
nav {{ background: #1a1a2e; color: #fff; padding: 12px 32px; display:flex;
       justify-content:space-between; align-items:center; }}
.nav-brand {{ font-weight: 700; font-size: 1.15rem; }}
.nav-links a {{ color: #ccc; text-decoration:none; margin: 0 12px; font-size:.95rem; }}
.nav-links a:hover {{ color:#fff; }}
.nav-greet {{ font-size:.9rem; color:#ccc; }}
.banner {{ background:#fff7e6; color:#a5630a; text-align:center; padding:8px;
           font-size:.95rem; border-bottom:1px solid #f0d8a8; }}
.container {{ max-width: 1100px; margin: 0 auto; padding: 24px; }}
h1 {{ font-size:1.6rem; margin-bottom:1rem; }}
h2 {{ font-size:1.2rem; color:#444; margin: 24px 0 12px; }}
.grid {{ display:grid; grid-template-columns: repeat(auto-fill, minmax(240px, 1fr));
         gap: 18px; }}
.card {{ background:#fff; border-radius:8px; padding:16px; box-shadow:0 1px 4px rgba(0,0,0,.08); }}
.product-img {{ background:#eee; border-radius:6px; height:140px;
                display:flex; align-items:center; justify-content:center;
                color:#aaa; font-size:.9rem; margin-bottom:10px; }}
.price {{ font-size:1.15rem; font-weight:700; color:#1a1a2e; }}
.original-price {{ text-decoration:line-through; color:#999; margin-right:8px; font-weight:400; }}
.sale-badge {{ display:inline-block; background:#dc2626; color:#fff; padding:2px 8px;
               border-radius:12px; font-size:.75rem; font-weight:700; margin-left:8px; }}
.stock-warn {{ color:#dc2626; font-size:.85rem; font-weight:600; margin:6px 0; }}
.rating {{ color:#f59e0b; font-weight:700; font-size:1.1rem; }}
.review-count-tiny {{ color:#aaa; font-size:.7rem; margin-left:4px; }}
.btn {{ display:inline-block; padding:10px 18px; border-radius:6px;
        font-size:.95rem; font-weight:600; text-decoration:none; cursor:pointer; border:none; }}
.btn-primary {{ background:#16a34a; color:#fff; }}
.btn-primary:hover {{ background:#15803d; }}
.btn-secondary {{ background:#e5e7eb; color:#666; }}
.btn-secondary:hover {{ background:#d1d5db; color:#444; }}
.btn-small {{ padding:6px 10px; font-size:.85rem; }}
.btn-link {{ background:none; color:#666; text-decoration:underline; padding:8px; font-weight:400; }}
.required-mark {{ color:#dc2626; font-weight:700; }}
input, select {{ padding:8px 10px; border:1px solid #ccc; border-radius:6px;
                  font-size:.95rem; width:100%; }}
label {{ display:block; font-size:.9rem; color:#333; margin: 12px 0 4px; }}
.toast-success {{ background:#d1fae5; color:#065f46; padding:10px 14px;
                  border-radius:6px; margin-bottom:12px; }}
.toast-error {{ background:#fee2e2; color:#991b1b; padding:10px 14px;
                border-radius:6px; margin-bottom:12px; }}
.summary-side {{ background:#f9fafb; padding:16px; border-radius:8px; }}
.row {{ display:flex; gap:24px; }}
.row > .main {{ flex: 1; }}
.row > .side {{ width: 280px; }}
footer {{ background:#f0f0f0; text-align:center; color:#888;
          font-size:.7rem; padding:18px; margin-top:48px; }}
.error-msg {{ color:#991b1b; font-family: ui-monospace, monospace; font-size:.8rem;
              background:#fef2f2; padding:8px; border-radius:6px; margin:8px 0; }}
</style>
</head><body>
{_nav()}
{body}
<footer>
  &copy; 2026 DarkShop. <a href="/faq" style="color:#888; text-decoration:underline;">FAQ</a> &middot;
  Need to cancel a subscription? Email <a href="mailto:support@darkshop.test" style="color:#888;">support@darkshop.test</a>.
</footer>
</body></html>"""


# ── Pages ─────────────────────────────────────────────────────────────


async def homepage(request: Request):
    body = f"""
    <div class="banner">Free shipping on orders over $50! Shop now.</div>  <!-- BUG #3 -->
    <div class="container">
      <h1>Welcome to DarkShop</h1>
      <p>Curated tech essentials. <a href="/shop">Browse the shop &rarr;</a></p>
    </div>
    """
    return HTMLResponse(_layout("Home", body))


async def shop_page(request: Request):
    cards = []
    for p in _products:
        # BUG #2: original_price == price, so discount is fake
        original = p["original_price"]
        price = p["price"]
        sale_html = f'<span class="original-price">${original:.2f}</span>' \
                    f'<span class="sale-badge">-50%</span>' \
                    if original >= price else ""
        cards.append(f"""
        <div class="card">
          <a href="/product/{p['id']}" style="text-decoration:none;color:inherit;">
            <div class="product-img">[no image]</div>
            <h3 style="font-size:1rem;margin-bottom:6px;">{esc(p['name'])}</h3>
            <p>{sale_html}<span class="price">${price:.2f}</span></p>
            <p class="stock-warn">{esc(p['stock_label'])}</p>  <!-- BUG #1 -->
            <p><span class="rating">{p['rating']} &#9733;</span></p>
          </a>
        </div>""")
    body = f"""
    <div class="banner">Free shipping on orders over $50! Shop now.</div>  <!-- BUG #3 -->
    <div class="container">
      <h1>Shop</h1>
      <div class="grid">
        {''.join(cards)}
      </div>
    </div>
    """
    return HTMLResponse(_layout("Shop", body))


async def product_page(request: Request):
    pid = int(request.path_params["product_id"])
    p = next((x for x in _products if x["id"] == pid), None)
    if p is None:
        return HTMLResponse("Not found", status_code=404)

    # BUG #5: rating prominent, review count tiny
    # BUG #4: "Add to Cart" rendered as secondary, "Newsletter" as primary
    body = f"""
    <div class="banner">Free shipping on orders over $50!</div>
    <div class="container">
      <div class="row">
        <div class="main">
          <div class="product-img" style="height:320px;">[no image]</div>
          <h1>{esc(p['name'])}</h1>
          <p>
            <span class="rating">{p['rating']} &#9733;&#9733;&#9733;&#9733;&#9733;</span>
            <span class="review-count-tiny">(based on {p['review_count']} review)</span>
          </p>
          <p style="margin:12px 0;"><span class="price">${p['price']:.2f}</span></p>
          <p class="stock-warn">{esc(p['stock_label'])}</p>
          <p style="margin:18px 0; color:#444;">{esc(p['description'])}</p>

          <form method="POST" action="/api/cart/add" style="display:inline-block; margin-right:8px;">
            <input type="hidden" name="product_id" value="{p['id']}">
            <button type="submit" class="btn btn-secondary">Add to Cart</button>
          </form>
          <form method="POST" action="/api/newsletter" style="display:inline-block;">
            <button type="submit" class="btn btn-primary">Subscribe to Newsletter</button>
          </form>
        </div>
        <div class="side"></div>
      </div>
    </div>
    """
    return HTMLResponse(_layout(p["name"], body))


async def cart_page(request: Request):
    rows = []
    subtotal = 0.0
    for item in _cart:
        p = next((x for x in _products if x["id"] == item["product_id"]), None)
        if p is None:
            continue
        line = p["price"] * item["qty"]
        subtotal += line
        rows.append(f"""
        <tr>
          <td>{esc(p['name'])}</td>
          <td>
            <input type="number" name="qty_{p['id']}" value="{item['qty']}" min="-99" max="99999" style="width:70px;">
          </td>
          <td>${p['price']:.2f}</td>
          <td>${line:.2f}</td>
          <td><a href="/api/cart/remove/{p['id']}" class="btn btn-link">Remove</a></td>
        </tr>""")

    # BUG #9: subtotal off by 1 cent
    displayed_subtotal = subtotal - 0.01 if rows else 0.0
    flash = ""
    last_code = request.query_params.get("code")
    # BUG #12: discount code field — successful "SAVE10" gives no feedback
    if last_code and last_code.upper() == "SAVE10":
        # silently accept (no flash)
        pass
    elif last_code:
        # silently clear (no flash either!)
        pass

    body = f"""
    <div class="container">
      <h1>Your Cart</h1>
      {flash}
      <table style="width:100%; background:#fff; border-radius:8px; padding:16px;">
        <tr style="text-align:left; border-bottom:1px solid #eee;">
          <th>Item</th><th>Qty</th><th>Price</th><th>Line</th><th></th>
        </tr>
        {''.join(rows) if rows else '<tr><td colspan="5" style="padding:18px;color:#888;">Cart is empty.</td></tr>'}
      </table>

      <form method="GET" action="/cart" style="margin-top:18px;">
        <label>Discount code:</label>
        <input type="text" name="code" placeholder="Enter promo code" style="width:240px; display:inline-block;">
        <button type="submit" class="btn btn-secondary btn-small">Apply</button>
      </form>

      <p style="margin-top:24px; font-size:1.1rem;">
        Subtotal: <strong>${displayed_subtotal:.2f}</strong>
      </p>

      <div style="margin-top:24px;">
        <a href="/cart" class="btn btn-primary" style="font-size:1.05rem; padding:14px 22px;">Edit Cart</a>
        <a href="/checkout" class="btn btn-link" style="margin-left:8px; font-size:.85rem;">Place Order</a>
      </div>
    </div>
    """
    # BUG #8: "Place Order" is a tiny link, "Edit Cart" is huge primary CTA
    return HTMLResponse(_layout("Cart", body))


async def checkout_page(request: Request):
    if request.method == "POST":
        form = await request.form()
        # Persist a fake order
        global _orders
        _orders.append({
            "id": None,  # BUG #14: order placed with no ID
            "items": list(_cart),
            "ts": time.time(),
        })
        return RedirectResponse("/order-placed", status_code=303)

    # Calculate subtotals
    subtotal = sum(p["price"] * item["qty"]
                   for item in _cart
                   for p in [next((x for x in _products if x["id"] == item["product_id"]), None)]
                   if p)
    shipping = 5.00  # BUG #3: always charge shipping despite "free over $50" banner
    tax = round(subtotal * 0.08, 2)
    total_with_tax = subtotal + shipping + tax

    # BUG #7: side-panel shows pre-tax subtotal; main area shows post-tax total
    side = f"""
    <div class="summary-side">
      <h2 style="margin-top:0;">Order Summary</h2>
      <p>Subtotal: ${subtotal:.2f}</p>
      <p style="font-size:1.1rem; margin-top:12px;">Total: <strong>${subtotal:.2f}</strong></p>
    </div>
    """

    # BUG #6: CVV before card number
    # BUG #9: "(optional)" with red asterisk
    main = f"""
    <h1>Checkout</h1>
    <form method="POST" action="/checkout">
      <h2>Payment</h2>
      <label>CVV <span class="required-mark">*</span></label>
      <input type="text" name="cvv" required>

      <label>Card number <span class="required-mark">*</span></label>
      <input type="text" name="card" required>

      <label>Expiry (MM/YY) <span class="required-mark">*</span></label>
      <input type="text" name="expiry" required>

      <h2>Shipping</h2>
      <label>Address line 1 <span class="required-mark">*</span></label>
      <input type="text" name="addr1" required>

      <label>City <span class="required-mark">*</span></label>
      <input type="text" name="city" required>

      <label style="margin-top:16px;">
        <input type="checkbox" name="legal" style="width:auto; margin-right:8px;">
        I agree to the terms <span class="required-mark">*</span> (optional)
      </label>

      <p style="margin-top:24px;">
        <strong>Total with shipping &amp; tax: ${total_with_tax:.2f}</strong>
      </p>

      <button type="submit" class="btn btn-link" style="margin-top:8px;">Place Order</button>
      <a href="/cart" class="btn btn-primary" style="margin-left:8px; font-size:1.1rem; padding:14px 24px;">Edit Cart</a>
    </form>
    """

    body = f"""
    <div class="container">
      <div class="row">
        <div class="main">{main}</div>
        <div class="side">{side}</div>
      </div>
    </div>
    """
    return HTMLResponse(_layout("Checkout", body))


async def order_placed_page(request: Request):
    # BUG #14: no order ID shown
    body = f"""
    <div class="container">
      <h1>Order placed!</h1>
      <p>Thanks for your purchase. You'll receive an email confirmation shortly.</p>
      <p style="margin-top:24px;"><a href="/shop" class="btn btn-secondary">Continue shopping</a></p>
    </div>
    """
    return HTMLResponse(_layout("Order placed", body))


async def account_page(request: Request):
    flash = ""
    if request.method == "POST":
        form = await request.form()
        new_name = (form.get("name") or "").strip()
        if new_name:
            _account["name"] = new_name
            # BUG #10: nav_display_name NOT updated
            flash = '<div class="toast-success">Account name updated.</div>'

    body = f"""
    <div class="container">
      <h1>Account</h1>
      {flash}
      <form method="POST" action="/account" style="max-width:480px;">
        <label>Display name</label>
        <input type="text" name="name" value="{esc(_account['name'])}">
        <label>Email</label>
        <input type="email" name="email" value="{esc(_account['email'])}" disabled>

        <button type="submit" class="btn btn-primary" style="margin-top:18px;">Save changes</button>
      </form>

      <h2 style="margin-top:36px;">Subscriptions</h2>
      <p style="color:#888;">Newsletter subscription: <strong>active</strong></p>
      <!-- BUG #15-style: no cancel button here; the only path is the
           support@ link in the 8pt grey footer. We expose the issue via
           the FAQ page. -->
    </div>
    """
    return HTMLResponse(_layout("Account", body))


async def faq_page(request: Request):
    # The cancel-subscription path is buried here.
    body = """
    <div class="container">
      <h1>FAQ</h1>
      <h2>How do I cancel a subscription?</h2>
      <p>Email <a href="mailto:support@darkshop.test">support@darkshop.test</a> with your account email.</p>
    </div>
    """
    return HTMLResponse(_layout("FAQ", body))


# ── API ───────────────────────────────────────────────────────────────


async def api_cart_add(request: Request):
    form = await request.form()
    pid = int(form.get("product_id", "0"))
    if pid:
        existing = next((c for c in _cart if c["product_id"] == pid), None)
        if existing:
            existing["qty"] += 1
        else:
            _cart.append({"product_id": pid, "qty": 1})
    # BUG #11: nav badge NOT updated. _account["stale_cart_count"] stays 0.
    return RedirectResponse("/cart", status_code=303)


async def api_cart_remove(request: Request):
    global _cart
    pid = int(request.path_params["product_id"])
    _cart = [c for c in _cart if c["product_id"] != pid]
    return RedirectResponse("/cart", status_code=303)


async def api_newsletter(request: Request):
    return RedirectResponse("/", status_code=303)


# ── Argus test-fixture convention ─────────────────────────────────────


async def api_test_state(request: Request):
    return JSONResponse({
        "products": _products,
        "cart": _cart,
        "account": _account,
        "orders": _orders,
        "cart_total_items": sum(c["qty"] for c in _cart),
    })


async def api_test_reset(request: Request):
    global _cart, _account, _orders
    mode = request.query_params.get("mode", "seeded")
    _cart = []
    _account = {"name": "Alex", "email": "alex@example.com",
                "stale_cart_count": 0, "nav_display_name": "Alex"}
    _orders = []
    _seed_products()
    if mode == "with_items":
        _cart = [{"product_id": 1, "qty": 2}, {"product_id": 2, "qty": 1}]
    elif mode == "renamed":
        _account["name"] = "Alex-Renamed"
        # nav_display_name intentionally still "Alex" to expose BUG #10
    return JSONResponse({"ok": True, "mode": mode})


# ── Routes ─────────────────────────────────────────────────────────────


routes = [
    Route("/", homepage),
    Route("/shop", shop_page),
    Route("/product/{product_id:int}", product_page),
    Route("/cart", cart_page),
    Route("/checkout", checkout_page, methods=["GET", "POST"]),
    Route("/order-placed", order_placed_page),
    Route("/account", account_page, methods=["GET", "POST"]),
    Route("/faq", faq_page),
    Route("/api/cart/add", api_cart_add, methods=["POST"]),
    Route("/api/cart/remove/{product_id:int}", api_cart_remove),
    Route("/api/newsletter", api_newsletter, methods=["POST"]),
    Route("/api/test/state", api_test_state, methods=["GET"]),
    Route("/api/test/reset", api_test_reset, methods=["POST"]),
]


app = Starlette(routes=routes)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=5556)
