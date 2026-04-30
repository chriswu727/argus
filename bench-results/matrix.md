# Argus benchmark matrix

**34 / 34 = 100 %** in 29.3 s across 2 fixture(s).

| Fixture     | Recall            | Duration | Fixture URL                  |
|-------------|-------------------|----------|------------------------------|
| buggytasks  | 22 / 22 = 100 %   | 21.0 s   | `http://127.0.0.1:5555` |
| darkshop    | 12 / 12 = 100 %   | 8.3 s    | `http://127.0.0.1:5556` |

Argus's MCP surface is fixture-agnostic — both BuggyTasks (mechanical bugs) and DarkShop (human-eye bugs) are exercised through the same description-keyed tools.

## buggytasks — 22 / 22 = 100 % in 21.0 s

| #  | Seeded bug                                              | Caught | Method        |
|----|---------------------------------------------------------|--------|---------------|
|  1 | Console ReferenceError on homepage (appConfig)          | yes    | auto-event    |
|  2 | Dead nav link /help -> 404                              | yes    | agent-record  |
|  3 | POST /api/newsletter -> 500                             | yes    | agent-record  |
|  4 | Login accepts ANY credentials                           | yes    | agent-record  |
|  5 | Register: mismatched passwords still create account     | yes    | agent-record  |
|  6 | Register: form data cleared on validation error         | yes    | agent-record  |
|  7 | Search XSS reflection                                   | yes    | agent-record  |
|  8 | Double-submit creates duplicate task                    | yes    | agent-record  |
|  9 | Dashboard task count off-by-one                         | yes    | agent-record  |
| 10 | Delete fake-success: still present after refresh        | yes    | agent-record  |
| 11 | Edit silent failure: data not actually updated          | yes    | agent-record  |
| 12 | Toggle race condition (no server lock)                  | yes    | agent-record  |
| 13 | Load More: JS init error blocks pagination              | yes    | auto-event    |
| 14 | Empty state shows 'Loading...' forever                  | yes    | agent-record  |
| 15 | Search is case-sensitive                                | yes    | agent-record  |
| 16 | Date display: '1.0 days ago' decimal format             | yes    | agent-record  |
| 17 | Settings 'saved!' even when 500                         | yes    | agent-record  |
| 18 | Long titles silently truncated by CSS                   | yes    | agent-record  |
| 19 | Priority field accepts arbitrary values                 | yes    | agent-record  |
| 20 | Navbar still shows 'Login' after authentication         | yes    | agent-record  |
| 21 | Whitespace-only task title creates empty task           | yes    | agent-record  |
| 22 | 0 tasks remaining shown in alarming red                 | yes    | agent-record  |

## darkshop — 12 / 12 = 100 % in 8.3 s

| #  | Seeded bug                                              | Caught | Method        |
|----|---------------------------------------------------------|--------|---------------|
|  1 | Hardcoded 'Only 3 left!' stock label                    | yes    | agent-record  |
|  2 | Fake -50% sale badge (original_price == price)          | yes    | agent-record  |
|  3 | Free-shipping banner contradicted by checkout fee       | yes    | agent-record  |
|  4 | Visual hierarchy inverted on product page               | yes    | agent-record  |
|  5 | Misleading rating — 4.8★ huge, '1 review' tiny          | yes    | agent-record  |
|  6 | CVV field ordered before card number                    | yes    | agent-record  |
|  7 | Side-panel subtotal vs main-area total mismatch         | yes    | agent-record  |
|  8 | Place Order demoted to a link; Edit Cart is the CTA     | yes    | agent-record  |
|  9 | Legal checkbox '(optional)' carries required-mark       | yes    | agent-record  |
| 10 | Account rename does not update nav greeting             | yes    | agent-record  |
| 11 | Nav cart-count badge stale vs cart contents             | yes    | agent-record  |
| 12 | Discount code field clears silently with no feedback    | yes    | agent-record  |
