# Privacy

Argus is a local-first testing tool. The project does not operate an Argus
cloud service, user account system, analytics endpoint, or telemetry collector.

## Data Argus handles

During a test, Argus may process page text, accessibility data, URLs, browser
console and network events, testing goals and constraints, verification targets,
cookies or storage when the corresponding tools are called, screenshots,
downloaded-file metadata, and native macOS accessibility information in screen
mode. Coverage reports omit typed and pasted input values from action references,
but goals, constraints, verification targets, finding titles, and screenshots are
evidence and may contain application data; do not put secrets in those fields.

Reports, screenshots, reproduction receipts, state capsules, and regression
journals are written locally under `./argus-reports` by default. Set
`ARGUS_OUTPUT_DIR` to choose another location. Remove that directory when the
artifacts are no longer needed.

## Where data can go

- The MCP host and its configured model provider receive the Argus tool inputs
  and outputs that the host includes in a conversation. Their privacy terms
  apply independently of Argus.
- Browser tests connect to the URLs the user or agent chooses to test, along
  with resources those pages load.
- CLI mode uses LiteLLM and sends planner context to the model provider selected
  by the user. MCP mode does not require an Argus API key or an Argus-operated
  model service.
- Installing or upgrading through PyPI, `pip`, or `uvx` contacts those package
  distribution services under their own policies.

Argus does not intentionally transmit test artifacts to the project maintainer.
Review reports and screenshots before sharing them because they can contain
sensitive application data.
