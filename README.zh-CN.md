# Argus

**让 AI agent 像测试工程师一样探索用户流程、检查真实结果，并用证据复现缺陷现象的 MCP 工具集。**

[English — 主文档](README.md) · 简体中文 · [产品主页](https://yichenwu.dev/projects/argus) · [更新日志](CHANGELOG.md)

本文对应 **0.10.0**。英文 README 是项目的权威说明；代码、协议文档和发布说明以英文维护。

[![PyPI](https://img.shields.io/pypi/v/argus-testing)](https://pypi.org/project/argus-testing/)
[![Python](https://img.shields.io/pypi/pyversions/argus-testing)](https://pypi.org/project/argus-testing/)
[![License: MIT](https://img.shields.io/badge/license-MIT-green)](LICENSE)

## Argus 提供什么

Argus 是本地运行的 MCP server，为现有 coding agent 增加一套可追踪的测试工作流：

- 围绕用户目标探索真实流程，而不只是读取页面或执行预写脚本。
- 点击、输入、导航后直接返回页面观察，减少重复调用 `observe`。
- 用 fresh load 检查保存、删除等操作是否真的持久化，而不轻信成功提示。
- 独立复查指定缺陷现象，区分已观察、已复现、未复现和无法判断。
- 将目标覆盖、操作、检查、截图和发现关联起来，保留未完成的测试范围。
- 通过 MCP 进度通知和本地 dashboard 展示实际执行阶段。
- 导出 HTML、JSON、JUnit 和 SARIF，并支持对符合条件的已知缺陷做无模型回归检查。

宿主 agent 负责判断产品需求和选择下一步；Argus 提供执行、证据与确定性检查。
它不会保证发现所有 bug，也不会因为工具执行成功就认定应用通过测试。
浏览器模式跨平台可用；原生 macOS 模式使用独立工具配置。

## 0.10.0 更新

- **避免重复写入：**持久化检查发现状态丢失时，可以建议只读的 fresh-load 复查条件，
  无须再次执行 Save/Delete。已有 finding 最多显式复查两次，并保留历史 receipt。
- **修正证据归属：**在核验页面采集截图，再返回 agent 原来的页面。
  延后记录 bug 不再自动附上无关页面的截图；截图缺失或 URL 不匹配会明确给出 warning。
- **完整保存预期与实际：**`evidence.expected`、`evidence.actual` 会保留在 JSON 字段及各报告格式中。
  调用方提供的调用编号标记为 `claimed_evidence_calls`，不是独立核验过的 trace ID。
- **聚焦覆盖范围：**切换目标组会冻结上一组的证据窗口，但不会自动完成目标。
  回访页面和显式关联目标的延后 finding 会保留到对应流程中。
- **减少收尾调用：**可以用 `end_session(coverage_updates=[...])` 一次完成最终覆盖登记与报告导出；
  整批参数验证失败时会话保持打开。compact/delta 输出可共享重复证据并省略空的可选元数据。
- **更清楚的变化摘要：**识别页面文字与字段值变化，但不在差异摘要中导出输入值。

这不是一次新的模型效果或 token 成本评测。默认工具配置仍为 `core`，默认响应模式仍为
`compatible`，结果 envelope 版本仍为 `1`。详见 [协议](docs/agent-protocol.md)。

## 快速开始

需要 **Python 3.11+** 和 [uv](https://docs.astral.sh/uv/)。首次安装 Chromium：

```bash
uvx --from playwright playwright install chromium
```

升级后若提示浏览器可执行文件缺失，运行以下命令安装与 Argus 自身 Playwright 环境匹配的浏览器，
然后重启 MCP 宿主：

```bash
uvx --from argus-testing python -m playwright install chromium
```

将 Argus 加入 MCP 客户端：

```bash
# Claude Code
claude mcp add argus -- uvx --from argus-testing argus-mcp

# Codex
codex mcp add argus -- uvx --from argus-testing argus-mcp
```

Cursor 或其他支持标准 `mcpServers` 配置的客户端可以使用：

```json
{
  "mcpServers": {
    "argus": {
      "command": "uvx",
      "args": ["--from", "argus-testing", "argus-mcp"]
    }
  }
}
```

如果默认 Python 版本较旧，在启动参数中使用 `uvx --python 3.12 --from argus-testing argus-mcp`。
需要固定版本时，将包名替换为 `argus-testing==0.10.0`。也可以使用 pip：

```bash
pip install argus-testing
playwright install chromium
argus-mcp --version
```

升级后重启 MCP 宿主，避免继续使用旧进程里的工具表。安装检查不启动浏览器或调用模型：

```bash
uvx --from argus-testing python -m argus.install_check
```

然后在 agent 对话中提出测试任务，例如：“测试 http://localhost:3000，检查保存、刷新后的状态、
输入校验和跨页面一致性，给我带证据的真实问题。”

## 工具配置与响应模式

| 配置 | 工具数 | 用途 |
|---|---:|---|
| `web` | 28 | 默认配置：完整的浏览器测试闭环，使用简洁的工具描述。 |
| `advanced` | 61 | 在 web 基础上加入网络 mock、cookie/存储、多标签、弹窗、等待、坐标操作、爬取和链接/性能探测。 |
| `screen` | 14 | 原生 macOS Accessibility 与截图测试，需单独启用。 |
| `full` | 77 | 全部 web 与 screen 工具，包括屏幕坐标操作。 |

旧配置名仍可使用：`agent` 和 `core` 会启动 `web` 配置。

推荐支持 MCP `structuredContent` 的宿主使用：

```bash
uvx --from argus-testing argus-mcp --response-mode compact --monitor-port 8765
```

`compact` 减少迁移工具的重复文字，但保留结构化证据和图片。
`delta` 是额外的可选模式，仅适用于能保留 snapshot 并校验 `base_id` 的客户端。
缺失 base 时调用 `observe` 重新同步，不能把旧检查结果继承为当前结果。
旧客户端继续使用默认 `compatible`。用 `argus-mcp --list-tools` 查看实际工具表。

完整工具说明见[英文工具目录](README.md#tool-surface)，操作准则见 [AGENTS.md](AGENTS.md)。

## 推荐测试流程

1. `start_session` 传入 URL、明确目标、约束和可选时间预算。
2. 用 `coverage_update(..., status="in_progress")` 开始一个目标或一组共享流程的相关目标。
3. 使用 `click_what`、`test_action`、`test_form` 等执行操作，先读操作自带的观察结果。
4. 保存、删除等状态变更后调用 `verify_persistence`，检查 fresh load 后的真实状态。
5. 发现问题时，用 `record_bug` 保存需求、预期、实际、复现步骤与适当的独立核验条件；
   定性建议用 `record_observation`，不要冒充已确认 bug。
6. 在 `end_session` 中提交有实际证据的最终覆盖状态并导出报告，保留未测试或受阻目标。

例如，先确认保存后刷新丢失确实违反需求，再记录对应现象：

```json
{
  "title": "Saved display name resets after reload",
  "severity": "high",
  "evidence": {
    "goal": "save-and-reload",
    "expected": "The saved display name survives reload.",
    "actual": "The page shows the original display name after reload.",
    "steps": ["Save a new display name", "Reload the profile page"]
  },
  "verify": {
    "expect": "absent",
    "target_text": "New display name",
    "at_url": "/profile"
  }
}
```

`goal` 必须对应会话中声明的目标，`target_text` 必须是实际观察到并能证明该现象的内容，
不能直接照抄示例作为测试结论。持久化失败优先检查 `verify_persistence` 返回的 `suggested_verify`。
`replay: true` 会重新执行旅程，可能重复真实写入，必须有相应授权。
已有 finding 的复查使用 `finding_id`，保持标题和严重程度不变，省略 `evidence`。

`status: completed` 只表示工具完成；`exercised` 只表示目标被执行过，并不等于通过。
receipt 只能支持指定现象是否复现，不能单独证明根因或产品需求判断。

## 实时监控

```bash
argus-mcp --monitor-port 8765
# 自动选择空闲端口：
argus-mcp --monitor-port 0
```

打开 `http://127.0.0.1:8765`。选用随机端口时，实际地址输出到 stderr，不污染 MCP stdout。

| 路径 | 内容 |
|---|---|
| `/` | 本地实时 dashboard。 |
| `/healthz`、`/readyz` | 存活与就绪状态。 |
| `/api/status` | 会话、操作、计数与数值化 overhead。 |
| `/api/events` | 支持重连和 gap 提示的 SSE。 |
| `/metrics` | Prometheus 指标。 |

监控只监听 loopback，没有操作接口，不包含页面正文、URL、输入值、截图或工具参数。
它不是经过认证的远程服务，不能暴露到公网。MCP 进度通知是否显示由宿主决定；
通知表示真实阶段，不是完成百分比，也不是模型思维过程。

## 报告、回归与清理

默认输出目录是 `./argus-reports`，可通过 `ARGUS_OUTPUT_DIR` 修改。
HTML 通常引用紧凑 WebP 预览并保留原始截图；`ARGUS_PORTABLE_REPORT=1` 可生成单文件 HTML。
JSON 保留覆盖、receipt、预期与实际、截图和观察；JUnit/SARIF 便于下游集成。
URL 级发现不保证能生成源码行级 PR 标注。

```bash
argus-regression http://localhost:3000 --output ./argus-reports
argus-cleanup --output ./argus-reports --keep-runs 20
# 确认清理计划后才实际删除：
argus-cleanup --output ./argus-reports --keep-runs 20 --apply
```

回归退出码：`0` 未复现已知现象或 journal 为空，`1` 已知现象仍存在，`2` 操作/存储失败，
`3` 无法判断。`0` 不是应用完全正确的证明。自动回归不执行可能写入数据的 replay。
清理默认 dry-run，不删除 journal 和 state capsule。

## 原生 macOS 与独立 CLI

```bash
pip install 'argus-testing[mac]'
brew install cliclick
argus-mcp --doctor
argus-mcp --tool-profile screen
```

原生模式需要 Screen Recording 和 Accessibility 授权。默认有单次超时、30 分钟会话上限，
以及 `~/.argus/abort` 紧急停止文件。Linux CI 不验证真实 macOS 权限与应用交互。

Argus 使用宿主已有的模型，不需要 Argus 专用 provider key。

## 安全与隐私

Argus 没有维护者运营的遥测收集服务，但 MCP 宿主及其模型提供商可能收到对话中使用的工具结果。
浏览器或原生操作可以产生真实副作用，优先使用测试账号和非生产数据。

- `eval_js` 默认禁用；只有显式 `--unsafe` 才启用，不要用于不可信目标。
- 上传和 `file://` 页面需要在启动时用 `ARGUS_FILE_ROOTS` 授权窄范围目录，默认不允许。
- 可选 `ARGUS_ALLOWED_ORIGINS` 限制工具目标，但不是网络 egress 防火墙。
- Cookie/storage 值默认隐藏；仅在有授权时启用 `ARGUS_ALLOW_SENSITIVE_READS=1`。
- State capsule 故意包含原始凭据，未加密，不能公开分享。
- 常见凭据会脱敏，但截图、自由文本和应用特有秘密仍可能泄漏，分享前必须检查。
- 一个 runtime 只有一个活跃会话，操作串行执行；超时后的写入结果可能不确定，不能盲目重试。

完整边界见 [SECURITY.md](SECURITY.md) 和 [PRIVACY.md](PRIVACY.md)。

## 测试与效果边界

0.10 的无模型编码基准在 24 个合成响应上，delta 相比 compact 减少 **48.25%** 的结果 JSON 字节。
这是字节测量，不是模型 token、订阅消耗或测试质量的提升比例。

`34/34` 脚本基准表示工具表面能找到的合成缺陷上限，不是 agent 成功率。
历史 [0.8 模型评测](docs/evaluation-0.8.md) 包含十组匹配实验：Argus 找到 6/6 个缺陷机会，
浏览器基线为 5/6；双方未观察到误报、目标覆盖相同，Argus 使用更多时间和 token。
样本很小，且包含事后修正的 fixture 问题，不能泛化为产品排名或全面优势。

```bash
pip install -e '.[dev]'
playwright install chromium
ARGUS_REQUIRE_BROWSER=1 pytest -q
python -m argus_lab.evaluation --smoke
python -m argus_lab.evaluation.overhead
```

以上测试和 smoke 不调用模型。单独的模型评测需要明确授权，原始 traces 应保持私有。
方法和限制见[评测文档](docs/evaluation.md)，扩展位置见[架构](docs/architecture.md)与
[贡献指南](CONTRIBUTING.md)。

## 许可证

[MIT](LICENSE)。
