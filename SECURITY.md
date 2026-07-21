# Security policy

## Supported versions

Security fixes are made on the latest published Argus release. Upgrade before
reporting a problem so the report reflects the current code.

## Reporting a vulnerability

Do not open a public issue for a vulnerability that could put users or tested
systems at risk. Email `yichenwujob@gmail.com` with:

- the affected Argus version and operating system;
- the tool profile and MCP client in use;
- reproduction steps and expected impact;
- logs or a minimal fixture with secrets removed.

You should receive an acknowledgement within seven days. Please allow time for
validation and a coordinated fix before publishing details.

## Trust boundary

Argus runs locally with the permissions of the user who starts it. The browser
profile can navigate to arbitrary URLs and browser action tools can trigger real
application side effects. Native screen mode can control the foreground macOS
app after the user grants Screen Recording and Accessibility access.

The default server does not expose arbitrary page JavaScript execution.
`eval_js` becomes operational only when Argus starts with `--unsafe`; it can read
page data, modify state, and issue requests available to that page. Do not use
`--unsafe` against untrusted targets.

Use test accounts and non-production data whenever possible. Do not authorize
an agent to make purchases, publish content, delete production data, or perform
other irreversible actions unless that exact external effect is intended.

Argus tool definitions include MCP risk annotations. These are hints for hosts,
not a sandbox or permission system.
