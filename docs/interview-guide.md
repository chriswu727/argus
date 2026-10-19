# Argus 0.7：面试讲解与证据

定位：一个本地运行、由 MCP host 驱动的探索式 QA 工具。它的工程价值是把
不确定的模型判断放进有边界的执行、验证和证据系统，而不是“AI 保证找到所有 bug”。

## 可以扎实讲清楚的设计

1. **为什么分层，而不是直接把所有工具写在 MCP server？**
   `entrypoints/mcp/server.py` 只负责工具注册、协议错误和图片适配；
   `application/` 执行用例；`domain/` 定义模型、状态与验证契约；
   `bootstrap.py` 注入具体依赖。增加验证器、报告格式和存储实现都有明确位置。
   证据：`tests/test_architecture_runtime.py` 检查依赖边界和运行时导入环。

2. **同时收到 click 和 end_session 怎么办？**
   `SessionManager.execute` 串行化所有操作，不只锁 start/end。等待超时不取消
   正在工作的调用；执行超时则取消当前动作并清理后端。shutdown 先标记不就绪，
   再取消并等待工作结束。证据：同文件中的排队、超时、shutdown 测试。
   必须承认：外部点击可能已生效，不能把超时等同于“没有副作用”。因此不自动重试。

3. **浏览器启动一半失败会泄漏吗？**
   会话先拥有新后端，再启动；失败路径清理已分配资源。旧后端关闭失败时不能
   静默丢掉引用并开启新会话。清理也有期限；失败资源仍可再次清理。
   证据：`application/lifecycle.py`、`application/manager.py` 和失败替换测试。

4. **如何区分“找到 bug”和“工具坏了”？**
   工具操作有 `ErrorCode`；验证结果有 reproduced / not reproduced / inconclusive。
   一个成功的工具调用可以发现应用 bug；一次验证失败不等于证明应用正常。
   CLI 对 inconclusive 返回 3，记录损坏返回 2，不把未知当作通过。
   证据：`domain/verification.py`、`application/regression.py`、回归 CLI 测试。

5. **监控是什么设计？**
   可选 loopback HTTP 端口，提供 health/readiness、snapshot、SSE、Prometheus 指标。
   一个有界事件环加游标；落后客户端得到 gap，重拉 snapshot；断开释放订阅。
   只允许读取状态，不传页面或输入。证据：`adapters/monitoring.py`、
   `tests/test_monitoring.py`。这是本机可观测性，不是远程多租户监控平台。

6. **如何避免密码进入报告？**
   输入 vault 与可导出 action trace 分离；批量表单、粘贴和选择也遵守同一规则。
   导出标记缺少输入，回放必须重新提供真实值。证据：`domain/replay.py`、
   `security.py`、`tests/test_replay_trace.py`。截图、页面内容和人工证据仍可能
   含敏感信息；不要声称做到了完整 DLP 或加密擦除。

7. **为什么用文件而不是 Redis/Postgres？**
   当前需求是单机、小规模、用户可带走的报告；原子替换与 OS 文件锁足够覆盖
   当前竞争条件。journal merge 在 repository 内完成。跨进程互斥不靠删除
   “过期锁文件”。证据：`adapters/journal.py`、`storage.py`、存储测试。
   承认无跨文件事务、无网络文件系统承诺；需求增长时可替换 `JournalPort`。

8. **测试是不是只有 mock？**
   不是：故障注入用 fake backend；浏览器用本地网页；协议测试启动真实 stdio
   子进程并走 MCP initialize/list/call/image/end；监控测试用真实 HTTP socket。
   CI 覆盖 3.10/3.12，浏览器依赖缺失不能被 skip 掩盖。
   证据：`tests/test_stdio_protocol.py`、`tests/test_monitoring.py` 和 CI workflow。

## 五分钟演示

启动 MCP 时加 `--monitor-port 8765`，打开监控页；让 host 启动本地测试网站会话，
观察 idle/starting/active 和操作事件；填写表单、记录带验证条件的发现；结束会话，
展示 HTML/JSON 证据与不含输入值的 replay trace；再运行 `argus-regression`。
失败演示用本地断开的端口或确定性测试，不操作真实支付、账号或生产数据。

## 不该写进简历的夸大说法

- “34/34” 是人工编排的工具能力基准，不是任意模型自主发现率，也不是生产召回率。
- 没有真实用户/吞吐数据，就不写高并发、万人使用、降低百分之多少线上故障。
- 不是完整分布式系统，没有 durable worker、租户隔离、远程鉴权或 exactly-once。
- 类型检查仅覆盖新增核心，不是整个仓库 strict typed。
- 这次没有用付费模型证明发现能力提升，也没有替代 macOS 真机权限集成验收。

简历应描述实际实现与已运行的测试；最有说服力的是能展示代码、失败用例和设计取舍，
而不是堆叠技术名词。测试数量、版本和发布状态以本次交付结果为准。
