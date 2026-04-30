# Argus benchmark — buggytasks

- Fixture: `http://127.0.0.1:5555`
- Duration: 20.4 s
- **Recall: 22 / 22 = 100 %**

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