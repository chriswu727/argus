# Argus benchmark — darkshop

- Fixture: `http://127.0.0.1:5556`
- Duration: 8.3 s
- **Recall: 12 / 12 = 100 %**

| #  | Seeded bug                                              | Caught | Method        | Notes                          |
|----|---------------------------------------------------------|--------|---------------|--------------------------------|
|  1 | Hardcoded 'Only 3 left!' stock label                    | yes    | agent-record  |                                |
|  2 | Fake -50% sale badge (original_price == price)          | yes    | agent-record  |                                |
|  3 | Free-shipping banner contradicted by checkout fee       | yes    | agent-record  |                                |
|  4 | Visual hierarchy inverted on product page               | yes    | agent-record  |                                |
|  5 | Misleading rating — 4.8★ huge, '1 review' tiny          | yes    | agent-record  |                                |
|  6 | CVV field ordered before card number                    | yes    | agent-record  |                                |
|  7 | Side-panel subtotal vs main-area total mismatch         | yes    | agent-record  |                                |
|  8 | Place Order demoted to a link; Edit Cart is the CTA     | yes    | agent-record  |                                |
|  9 | Legal checkbox '(optional)' carries required-mark       | yes    | agent-record  |                                |
| 10 | Account rename does not update nav greeting             | yes    | agent-record  |                                |
| 11 | Nav cart-count badge stale vs cart contents             | yes    | agent-record  |                                |
| 12 | Discount code field clears silently with no feedback    | yes    | agent-record  |                                |