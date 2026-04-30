"""DarkShop bench scenarios — 12 scripted competent-agent runs against
the seeded human-eye bugs in human-eye-fixture/app.py.

Where BuggyTasks is the mechanical-bug benchmark (recall mostly via
detector channels), DarkShop is the human-judgement benchmark — every
scenario hinges on the agent (a) observing the right surface and
(b) reasoning that what it observed is wrong. Static analysis catches
roughly none of these.
"""
from __future__ import annotations

import re
from typing import Awaitable, Callable, List, Tuple

import argus.mcp_server as mcp_module
from .runner import (
    call,
    reset as _reset,
    bugs_added_since as _bugs_added_since,
    records_match as _records_match,
)


BASE_URL = "http://127.0.0.1:5556"
BASE = BASE_URL


# ── Scenarios ──────────────────────────────────────────────────────


async def d01_hardcoded_stock(s):
    """BUG #1: 'Only 3 left!' is hardcoded — never decreases."""
    await _reset("seeded")
    pre = len(s.bugs)
    await call(mcp_module.navigate, BASE + "/shop")
    obs = await call(mcp_module.observe)
    # Count how many products show "Only 3 left!"
    count_seeded = obs.count("Only 3 left!")
    if count_seeded < 2:
        # Either the page didn't render or the bug isn't here as expected.
        return False, "skipped"
    # Add to cart twice and re-observe — the value should be unchanged.
    state_before = await call(
        mcp_module.eval_js,
        code="() => fetch('/api/test/state').then(r => r.json()).then(d => d.products[0].stock_label)",
    )
    # Hit /api/cart/add twice
    await call(mcp_module.eval_js,
        code=(
            "async () => { const body = new URLSearchParams({product_id:1}); "
            "for (let i=0; i<2; i++) await fetch('/api/cart/add', {method:'POST', "
            "headers:{'Content-Type':'application/x-www-form-urlencoded'}, body}); "
            "return 'ok'; }"
        ),
    )
    state_after = await call(
        mcp_module.eval_js,
        code="() => fetch('/api/test/state').then(r => r.json()).then(d => d.products[0].stock_label)",
    )
    # If both are identical "Only 3 left!", the stock indicator is fake.
    if "Only 3 left!" in state_before and "Only 3 left!" in state_after:
        await call(
            mcp_module.record_bug,
            title="Stock label 'Only 3 left!' is hardcoded — never changes with cart actions",
            severity="medium",
            evidence={"bug_type": "ux_issue", "screenshot": "skip",
                      "description": f"before: {state_before}; after 2 adds: {state_after}"},
        )
    new = _bugs_added_since(s, pre)
    return _records_match(new, ["stock", "hardcoded", "never changes"]), "agent-record"


async def d02_fake_sale_badge(s):
    """BUG #2: -50% sale badge but original_price == sale_price."""
    await _reset("seeded")
    pre = len(s.bugs)
    state_json = await call(
        mcp_module.eval_js,
        code=(
            "() => fetch('/api/test/state').then(r => r.json())"
            ".then(d => d.products.map(p => ({p: p.price, op: p.original_price})))"
        ),
    )
    # If every product has price == original_price but the page renders a sale badge,
    # the discount is a lie.
    await call(mcp_module.navigate, BASE + "/shop")
    obs = await call(mcp_module.observe)
    has_sale_badge = "-50%" in obs or "sale" in obs.lower()
    # Verify same prices via state
    fake_sale = "p" in state_json and "op" in state_json and (
        # simple substring check — every product carries identical p and op
        # pattern visible in JSON (e.g. {"p": 89.99, "op": 89.99})
        bool(re.search(r'\{"p":\s*([\d.]+),\s*"op":\s*\1\}', state_json))
    )
    if has_sale_badge and fake_sale:
        await call(
            mcp_module.record_bug,
            title="-50% sale badge is fake — original_price equals sale_price for every product",
            severity="medium",
            evidence={"bug_type": "ux_issue", "screenshot": "skip",
                      "description": state_json[:300]},
        )
    new = _bugs_added_since(s, pre)
    return _records_match(new, ["sale", "fake", "original_price"]), "agent-record"


async def d03_free_shipping_lie(s):
    """BUG #3: 'Free shipping over $50' banner — checkout always charges $5."""
    await _reset("with_items")
    pre = len(s.bugs)
    # Banner present on /shop
    await call(mcp_module.navigate, BASE + "/shop")
    shop_obs = await call(mcp_module.observe)
    has_banner = "free shipping" in shop_obs.lower()
    # Checkout charges shipping
    await call(mcp_module.navigate, BASE + "/checkout")
    co_obs = await call(mcp_module.observe)
    # Subtotal vs total-with-shipping disagreement is the smoking gun.
    sub_match = re.search(r"Subtotal:\s*\$([\d.]+)", co_obs)
    total_match = re.search(r"Total with shipping[^$]*\$([\d.]+)", co_obs)
    if has_banner and sub_match and total_match:
        sub_v = float(sub_match.group(1))
        total_v = float(total_match.group(1))
        if total_v > sub_v + 0.01:  # something added beyond subtotal
            await call(
                mcp_module.record_bug,
                title="'Free shipping over $50' banner contradicted by flat shipping fee in checkout",
                severity="medium",
                evidence={"bug_type": "ux_issue", "screenshot": "skip",
                          "description": f"banner present; subtotal {sub_v} vs total {total_v}"},
            )
    new = _bugs_added_since(s, pre)
    return _records_match(new, ["free shipping", "shipping fee", "contradicted"]), "agent-record"


async def d04_visual_hierarchy_inverted(s):
    """BUG #4: 'Subscribe Newsletter' is btn-primary; 'Add to Cart' is btn-secondary."""
    import json as _json
    await _reset("seeded")
    pre = len(s.bugs)
    await call(mcp_module.navigate, BASE + "/product/1")
    classes = await call(
        mcp_module.eval_js,
        code=(
            "() => Array.from(document.querySelectorAll('button'))"
            ".map(b => ({text: b.textContent.trim(), cls: b.className}))"
        ),
    )
    # eval_js wraps the JSON in 'eval_js result: <json>'. Pull out the JSON tail.
    payload = classes.split("result:", 1)[-1].strip()
    try:
        buttons = _json.loads(payload)
    except Exception:
        buttons = []

    by_text = {b.get("text", "").lower(): b.get("cls", "") for b in buttons}
    add_cls = by_text.get("add to cart", "")
    sub_cls = by_text.get("subscribe to newsletter", "")
    # Inversion: Add to Cart got secondary; Newsletter got primary.
    if "btn-secondary" in add_cls and "btn-primary" in sub_cls:
        await call(
            mcp_module.record_bug,
            title="Product page visual hierarchy inverted — newsletter button is primary, Add to Cart is secondary",
            severity="medium",
            evidence={"bug_type": "ux_issue", "screenshot": "skip",
                      "description": f"add-to-cart cls={add_cls!r}; newsletter cls={sub_cls!r}"},
        )
    new = _bugs_added_since(s, pre)
    return _records_match(new, ["hierarchy", "newsletter", "inverted"]), "agent-record"


async def d05_misleading_rating(s):
    """BUG #5: '4.8 ★★★★★' big, '(based on 1 review)' tiny grey."""
    await _reset("seeded")
    pre = len(s.bugs)
    await call(mcp_module.navigate, BASE + "/product/1")
    info = await call(
        mcp_module.eval_js,
        code=(
            "() => {"
            "const rating = document.querySelector('.rating');"
            "const tiny = document.querySelector('.review-count-tiny');"
            "if (!rating || !tiny) return null;"
            "const rs = window.getComputedStyle(rating);"
            "const ts = window.getComputedStyle(tiny);"
            "return {ratingPx: parseFloat(rs.fontSize), tinyPx: parseFloat(ts.fontSize),"
            " ratingText: rating.textContent.trim(), tinyText: tiny.textContent.trim()};"
            "}"
        ),
    )
    # Look for big rating + small review-count + low review count.
    if "ratingPx" in info and "tinyPx" in info:
        m = re.search(r'"ratingPx":\s*([\d.]+).*"tinyPx":\s*([\d.]+)', info)
        if m:
            rpx = float(m.group(1))
            tpx = float(m.group(2))
            if rpx >= tpx * 1.5 and "1 review" in info.lower():
                await call(
                    mcp_module.record_bug,
                    title="Rating prominence misleading — 4.8 stars huge, '1 review' in tiny grey text",
                    severity="low",
                    evidence={"bug_type": "ux_issue", "screenshot": "skip",
                              "description": info[:300]},
                )
    new = _bugs_added_since(s, pre)
    return _records_match(new, ["rating", "review", "misleading"]), "agent-record"


async def d06_cvv_before_card_number(s):
    """BUG #6: CVV field appears before card number in the form."""
    await _reset("with_items")
    pre = len(s.bugs)
    await call(mcp_module.navigate, BASE + "/checkout")
    order = await call(
        mcp_module.eval_js,
        code=(
            "() => {"
            "const inputs = Array.from(document.querySelectorAll('input[name]'))"
            ".map(i => i.name);"
            "return inputs;"
            "}"
        ),
    )
    # Find positions of cvv and card.
    m_cvv = re.search(r'"cvv"', order)
    m_card = re.search(r'"card"', order)
    if m_cvv and m_card and m_cvv.start() < m_card.start():
        await call(
            mcp_module.record_bug,
            title="Checkout form orders CVV before card number — cognitively wrong",
            severity="low",
            evidence={"bug_type": "ux_issue", "screenshot": "skip",
                      "description": f"input order: {order}"},
        )
    new = _bugs_added_since(s, pre)
    return _records_match(new, ["cvv", "card number", "before"]), "agent-record"


async def d07_subtotal_vs_total_mismatch(s):
    """BUG #7: side panel pre-tax subtotal vs main post-tax total — same screen."""
    await _reset("with_items")
    pre = len(s.bugs)
    await call(mcp_module.navigate, BASE + "/checkout")
    obs = await call(mcp_module.observe)
    sub_match = re.search(r"Subtotal:\s*\$([\d.]+)", obs)
    total_match = re.search(r"Total with shipping[^$]*\$([\d.]+)", obs)
    if sub_match and total_match:
        sv = float(sub_match.group(1))
        tv = float(total_match.group(1))
        if abs(tv - sv) > 0.5:
            await call(
                mcp_module.record_bug,
                title="Checkout side-panel subtotal disagrees with main-area total — confusing on the same screen",
                severity="medium",
                evidence={"bug_type": "ux_issue", "screenshot": "skip",
                          "description": f"side: ${sv}; main: ${tv}"},
            )
    new = _bugs_added_since(s, pre)
    return _records_match(new, ["subtotal", "total", "disagree"]), "agent-record"


async def d08_place_order_demoted(s):
    """BUG #8: Place Order = btn-link tiny; Edit Cart = btn-primary giant."""
    await _reset("with_items")
    pre = len(s.bugs)
    await call(mcp_module.navigate, BASE + "/checkout")
    info = await call(
        mcp_module.eval_js,
        code=(
            "() => {"
            "const all = Array.from(document.querySelectorAll('button, a'))"
            ".filter(e => /Place Order|Edit Cart/i.test(e.textContent));"
            "return all.map(e => ({text: e.textContent.trim(), cls: e.className,"
            " fontSize: parseFloat(window.getComputedStyle(e).fontSize),"
            " padding: window.getComputedStyle(e).padding}));"
            "}"
        ),
    )
    place_secondary = "place order" in info.lower() and "btn-link" in info.lower()
    edit_primary = "edit cart" in info.lower() and "btn-primary" in info.lower()
    if place_secondary and edit_primary:
        await call(
            mcp_module.record_bug,
            title="Place Order rendered as a tiny link while Edit Cart is the prominent CTA — funnels users away from purchase",
            severity="high",
            evidence={"bug_type": "ux_issue", "screenshot": "skip"},
        )
    new = _bugs_added_since(s, pre)
    return _records_match(new, ["place order", "edit cart", "funnels"]), "agent-record"


async def d09_optional_required_contradiction(s):
    """BUG #9: legal checkbox has red asterisk (required-mark) AND label '(optional)'."""
    await _reset("with_items")
    pre = len(s.bugs)
    await call(mcp_module.navigate, BASE + "/checkout")
    obs = await call(mcp_module.observe)
    # Search for "agree to the terms" + "(optional)" + a required-mark indicator.
    text_blob = obs
    has_agree = "agree to the terms" in text_blob.lower()
    has_optional = "(optional)" in text_blob.lower()
    # Required-mark: a span with class required-mark — appears near labels.
    has_red_mark = await call(
        mcp_module.eval_js,
        code=(
            "() => {"
            "const lab = Array.from(document.querySelectorAll('label')).find(l => /agree to the terms/i.test(l.textContent));"
            "if (!lab) return false;"
            "return !!lab.querySelector('.required-mark');"
            "}"
        ),
    )
    if has_agree and has_optional and "true" in has_red_mark.lower():
        await call(
            mcp_module.record_bug,
            title="Legal checkbox label contradicts itself — red required-mark next to '(optional)' text",
            severity="low",
            evidence={"bug_type": "ux_issue", "screenshot": "skip"},
        )
    new = _bugs_added_since(s, pre)
    return _records_match(new, ["legal", "optional", "contradicts", "contradictory"]), "agent-record"


async def d10_cross_page_stale_nav_name(s):
    """BUG #10: rename succeeds on /account, but nav greeting stays old."""
    await _reset("renamed")
    pre = len(s.bugs)
    # /account already renamed to "Alex-Renamed", but nav_display_name was kept stale.
    await call(mcp_module.navigate, BASE + "/account")
    account_obs = await call(mcp_module.observe)
    # Page should show new name in the form, nav should show old.
    has_new_in_account = "Alex-Renamed" in account_obs
    nav_has_old = re.search(r"Hi,\s*Alex(?!-)", account_obs)
    if has_new_in_account and nav_has_old:
        await call(
            mcp_module.record_bug,
            title="Account name change does not update nav greeting — cross-page state inconsistency",
            severity="medium",
            evidence={"bug_type": "ux_issue", "screenshot": "skip",
                      "description": "Form has 'Alex-Renamed' but navbar still says 'Hi, Alex' on /account"},
        )
    new = _bugs_added_since(s, pre)
    return _records_match(new, ["nav", "stale", "cross-page", "name"]), "agent-record"


async def d11_cart_count_badge_stale(s):
    """BUG #11: nav cart count badge stays at (0) even with items."""
    await _reset("with_items")
    pre = len(s.bugs)
    await call(mcp_module.navigate, BASE + "/")
    obs = await call(mcp_module.observe)
    nav_count_match = re.search(r"Cart\s*\((\d+)\)", obs)
    actual_count_json = await call(
        mcp_module.eval_js,
        code="() => fetch('/api/test/state').then(r => r.json()).then(d => d.cart_total_items)",
    )
    actual = re.search(r"result:\s*(\d+)", actual_count_json)
    if nav_count_match and actual:
        nav_n = int(nav_count_match.group(1))
        actual_n = int(actual.group(1))
        if nav_n != actual_n and actual_n > 0:
            await call(
                mcp_module.record_bug,
                title=f"Nav cart-count badge ({nav_n}) disagrees with actual cart ({actual_n}) — stale state",
                severity="medium",
                evidence={"bug_type": "ux_issue", "screenshot": "skip"},
            )
    new = _bugs_added_since(s, pre)
    return _records_match(new, ["cart", "badge", "stale", "disagree"]), "agent-record"


async def d12_silent_discount_code(s):
    """BUG #12: discount code field clears silently — no success or error feedback."""
    await _reset("with_items")
    pre = len(s.bugs)
    await call(mcp_module.navigate, BASE + "/cart")
    # Submit with valid code.
    await call(mcp_module.navigate, BASE + "/cart?code=SAVE10")
    obs = await call(mcp_module.observe)
    has_success_toast = "success" in obs.lower() or "applied" in obs.lower()
    has_error = "invalid" in obs.lower() or "error" in obs.lower()
    if not has_success_toast and not has_error:
        await call(
            mcp_module.record_bug,
            title="Discount code field clears silently after submit — no success or error feedback",
            severity="medium",
            evidence={"bug_type": "ux_issue", "screenshot": "skip",
                      "description": "Submitted SAVE10; page rerendered with no toast / inline message."},
        )
    new = _bugs_added_since(s, pre)
    return _records_match(new, ["silent", "discount", "feedback"]), "agent-record"


SCENARIOS: List[Tuple[int, str, Callable[[object], Awaitable[Tuple[bool, str]]]]] = [
    (1,  "Hardcoded 'Only 3 left!' stock label",                  d01_hardcoded_stock),
    (2,  "Fake -50% sale badge (original_price == price)",        d02_fake_sale_badge),
    (3,  "Free-shipping banner contradicted by checkout fee",      d03_free_shipping_lie),
    (4,  "Visual hierarchy inverted on product page",              d04_visual_hierarchy_inverted),
    (5,  "Misleading rating — 4.8★ huge, '1 review' tiny",        d05_misleading_rating),
    (6,  "CVV field ordered before card number",                   d06_cvv_before_card_number),
    (7,  "Side-panel subtotal vs main-area total mismatch",        d07_subtotal_vs_total_mismatch),
    (8,  "Place Order demoted to a link; Edit Cart is the CTA",    d08_place_order_demoted),
    (9,  "Legal checkbox '(optional)' carries required-mark",      d09_optional_required_contradiction),
    (10, "Account rename does not update nav greeting",            d10_cross_page_stale_nav_name),
    (11, "Nav cart-count badge stale vs cart contents",            d11_cart_count_badge_stale),
    (12, "Discount code field clears silently with no feedback",   d12_silent_discount_code),
]
