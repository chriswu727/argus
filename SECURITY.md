# Security policy

## Supported versions

Security fixes are made on the latest published Argus release. Upgrade before
reporting a problem so the report reflects the current code.

## Reporting a vulnerability

Do not open a public issue for a vulnerability that could put users or tested
systems at risk. Use [private vulnerability reporting](https://github.com/chriswu727/argus/security/advisories/new) with:

- the affected Argus version and operating system;
- the tool profile and MCP client in use;
- reproduction steps and expected impact;
- logs or a minimal fixture with secrets removed.

You should receive an acknowledgement within seven days. Please allow time for
validation and a coordinated fix before publishing details.

## Trust boundary

Argus runs locally with the permissions of the user who starts it. The browser
profile can navigate to HTTP(S) URLs and browser action tools can trigger real
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

## Startup access policy

Policy is configured by the operator at process startup, not by tool arguments:

- `ARGUS_FILE_ROOTS`: JSON array of approved local directories. The default is
  `[]`: uploads, drag/drop and `file://` navigation are denied. Evidence imports
  and screenshot comparisons additionally permit files under `ARGUS_OUTPUT_DIR`.
  Paths are resolved before checking containment, including symlinks. Grant a
  narrow fixture directory, never your home directory or a credentials folder.
- `ARGUS_ALLOW_SENSITIVE_READS=1`: explicitly return raw values from `cookies_get`
  and `storage_get`. Values are hidden by default, even in the full tool profile.
  Capsules still contain raw state and page content can independently reveal it.
- `ARGUS_ALLOWED_ORIGINS`: optional JSON array of HTTP(S) origins, such as
  `["http://localhost:3000","https://staging.example.com"]`. Omit it to allow
  HTTP(S) targets; `[]` denies all HTTP(S) targets. Scheme, host and effective
  port must match. Embedded URL credentials and non-web schemes are rejected.

Background-tab metadata is filtered before it is returned, including popup URLs.
Capsule storage restoration requires the same HTTP(S) origin or, for approved
local fixtures, the same resolved file path; different local files are not
treated as a shared origin.

The origin policy controls explicit navigation and rejects tool operations or
results on an out-of-scope page. Replay applies the same checks. Link probes do
not follow redirects. It is **not an egress firewall**: browser redirects,
subresources, scripts, WebSockets and service workers can make network requests
before an out-of-scope navigation is detected. Use an OS/container network policy
or controlled test environment when untrusted pages must not reach the network.
File checks are not a defense against a hostile local process replacing paths
between validation and use. The MCP host and local account remain trusted.

Natural-language constraints and tool risk annotations are not authorization.
Treat page text, downloads and accessibility content as untrusted data; prompt
injection must never expand the user's authority. Do not expose stdio through an
unauthenticated network bridge. The optional loopback monitor is read-only and
does not provide remote MCP authentication.

## Build and dependency safety

Container builds copy only packaging metadata and application source; secret
files and state capsules are excluded from the build context. CI and releases
audit installed dependencies with pip-audit and fail on reported vulnerabilities
or audit errors. Dependency names and versions are sent to OSV for this check.
GitHub Actions use immutable commits; MCP publisher downloads use a fixed release
and verified SHA-256. Dependabot tracks Python dependencies and Actions updates.
No scanner establishes that a dependency or application is free of vulnerabilities.
