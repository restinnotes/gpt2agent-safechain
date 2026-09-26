# Web session 审计与接入（2026-09-26）

## 可复核基线

- safechain：`65d314f1314c5358d680e84053e89d6d1967b2f1`，vendor 标记 upstream `04e3d93` / 0.0.11。
- 最新 upstream：[`e911a3a11f2dc46236e377b8df058b8d5a25d683`](https://github.com/robotlearning123/gpt2agent/tree/e911a3a11f2dc46236e377b8df058b8d5a25d683)，0.0.23，2026-09-23。
- 参考实现：upstream `sim.py`、`backend.py`、`sse.py`、`sentinel_bridge.py:get_conduit`。没有引入 bridge/挑战 VM。
- 匿名 homepage GET 于本次审计返回 HTTP 200，实际 `data-build` 为 `prod-13d1d77cfd998748ed8c4624d0b63ae9cf603370`。这只验证主页元数据可读，不证明登录后会话协议有效。

## 差异与取舍

| 项目 | 原 safechain | 最新 upstream | 本次修改 |
|---|---|---|---|
| 并发 | 默认 10；每 turn 新 backend | 共享 profile、队列与限流模块 | 单 provider 只允许 1；账号文件锁覆盖上传、准备、提交、artifact |
| 身份 | device 按主机保存；session 每 client 新建 | host 级 sim-state 持久 ID | 按 JWT subject/account 隔离；刷新 token 保留 ID；切换账号拒绝复用 cookie |
| cookie / 连接 | Sentinel/SSE 新 AsyncSession，cookie 部分回传 | warm profile jar，但存在额外 session 与 drop_session | 成功 turn 复用 sync/async session；cookie 保留 domain/path/expiry，磁盘持久化 |
| UA / TLS | chrome131 + Mac UA | chrome136 + Windows UA | 同一 chrome136/Windows UA/sec-ch-ua 套件；不随机轮换 |
| timezone | -480，与部分 UTC 字段矛盾 | IP geo 探测 + profile | 显式 IANA 配置，默认 UTC；JS offset 随 DST 正确计算，不查询 IP |
| locale/build | 固定旧值 | homepage data-build、旧 build number、模拟 echo logs | locale 持久化；每 worker 一次正常主页 GET 更新真实 data-build，不模拟 echo logs |
| 普通会话 | `/backend-api/conversation` | bridge 准备 conduit 后 `/f/conversation`，可降级 | safechain `complete_chat` 必须 prepare 成功、有 conduit 后才 POST `/f/conversation`；失败不降级重发 |
| 流解析 | classic SSE + Celsius handoff | frontend v1 patches | v1 normalization 后沿用原 lifecycle/handoff/artifact；保留临时会话不查 history |
| 保护响应 | 部分 429 latch | 持久 cooldown、部分自动重试/丢 session | 403/429/挑战 fail fast；Retry-After/冷却持久；保留原进程 latch；不自动重试 |

原 PoW helper 每次随机挑选屏幕/语言相关字段和新 UUID；safechain 现在显式传入同一 runtime 的 session ID、locale、timezone 和真实部署版本，保留既有协议的固定兼容字段。屏幕/内存/核心常量不是实测浏览器遥测，不声称模拟完全等价于真实浏览器。每次获取仍有新 nonce 与时间，这是请求新鲜度而非身份轮换。PoW 预算耗尽直接停止，不发送 stub proof；服务器实际拒绝时停止并冷却。prepare 的 required 元数据本身不是拒绝，仍需完成原有 finalize 流程，由服务器签发最终令牌。

### 没有依据的假设

不能凭客户端改动承诺“不触发风控”或消除“隐藏降智”。短响应耗时不能证明模型替换，因此现有可选耗时 gate 保留默认关闭；明确 model slug/banner 会被记录并触发现有 fallback/保护逻辑。

持久 session ID 是连续 worker 的本地策略，不是对 Web session 永久不变的断言。故障可以回收连接，但不会随机换身份；账号变更需要关闭并重建 provider。

上游仍硬编码 client build number `5955942`，本次匿名主页没有提供可验证的新 build number，因此保留该值并支持首次初始化时配置 `GPT2AGENT_CLIENT_BUILD`。UA/TLS 套件是该传输实现所支持的固定版本，不声称是当前最新浏览器。已有账号状态不会因每次环境变量变化而轮换 locale/timezone/build。

本次 frontend prepare 移植只作用于 safechain 的普通 `stream/complete_chat` 入口。专用 image/tool/deep-research 方法保留各自协议和恢复方式，共用持久传输；它们没有经过在线矩阵验证。旧 probe/brain 脚本的手工 HTTP 请求不属于这个接入路径。

## Runtime 配置

账号状态默认在 `~/.gpt2agent/accounts/<account-key>/runtime.json`；仅保存身份、cookie、部署元数据和冷却时间，不保存 bearer。不要提交或共享这个目录；POSIX 使用 0600，Windows 应放在当前用户的受限目录中。

首次初始化前可设置：

```powershell
$env:GPT2AGENT_TIMEZONE = 'America/New_York' # 改为实际使用环境的时区
$env:GPT2AGENT_LOCALE = 'en-US'             # 改为实际浏览器语言
$env:GPT2AGENT_MAX_ACTIVE_TURNS = '1'
# 可选：GPT2AGENT_RUNTIME_DIR、GPT2AGENT_DEVICE_ID、GPT2AGENT_CLIENT_BUILD
```

同一账号、同一机器的所有 worker 必须使用相同 runtime 目录。账号识别只用于本地隔离，不用于授权验证；不透明 token 会按 token 哈希隔离，刷新时不能保证复用。跨机器没有分布式账号锁，建议一个账号只运行一个 worker。

默认账号请求组至少间隔 5 秒（`GPT2AGENT_MIN_ACCOUNT_INTERVAL_SECONDS`）；整个 turn 串行，现有 launch gate 可继续叠加。429 至少冷却 60 秒并尊重更长 Retry-After；403/挑战至少冷却 900 秒。上层原有 latch 会进一步停止本进程。PRE_SUBMIT 表示没有提交，不表示当前可以忽略冷却重试。

## 当前音乐项目接入

当前项目没有 safechain provider 路由，`tools/runner.py` 只调 Codex/opencode 等 CLI。这次新增独立 `tools/safechain_runner.py`，保留现有模型路由和历史 run。

安装本地依赖（无需替换全局 gpt2agent）：

```powershell
Set-Location D:\musicclaude\gpt2agent-safechain
.\.venv\Scripts\python.exe -m pip install -r requirements-runtime.txt
```

在登录已配置的环境中，准备 UTF-8 `jobs.json`。model 必须填写账号实际可用的服务端 slug，不自动替换模型：

```json
[
  {"job_id": "web_smoke_01", "model": "YOUR_AVAILABLE_MODEL_SLUG",
   "prompt": "请只回复：会话测试完成。", "thinking_effort": "min",
   "expect_artifact": false}
]
```

启动整个批次一次，避免每 job 启一个进程：

```powershell
D:\musicclaude\gpt2agent-safechain\.venv\Scripts\python.exe `
  D:\musicclaude\unified_run_handoff_20260924\unified_run_handoff_20260924\tools\safechain_runner.py `
  --jobs .\jobs.json --run-dir .\runs\safechain_smoke_01
```

批次使用一个 provider、一个 asyncio loop，顺序执行；输出沿用 `prompts/outputs/meta` 布局。`attachments` 传文件路径数组，`expect_artifact=true` 时沿用现有 artifact 返回/下载逻辑，保存到 `artifacts/<job_id>/`。

提交 hook 用独占创建的 `meta/<job_id>.submitted.json` 标记提交边界；同一 job id 再次启动会拒绝重提。任何失败停止剩余批次，不自动重试或换模型。标记已存在但输出缺失时按提交状态不明处理，不删标记重跑。

程序内接入：导入这个 adapter 的 `run_jobs(run_dir, jobs)` 并在已有 event loop 中 `await`。只有调用入口最外层使用一次 `asyncio.run`。不要逐 turn 调用 `asyncio.run`；退出时 adapter 会 `await provider.aclose()`。

## 验证边界

单元测试覆盖现有 provider 行为，以及持久 ID、刷新/切换账号、cookie 同名不同路径、跨进程互斥、冷却持久化、timezone、prepare 失败、防提交重放和 frontend v1。所有测试使用 mock/临时目录，不消耗 ChatGPT 额度。

本次还进行了一次登录准备探测（`check_readiness(probe_sentinel=True)`），没有提交会话：主页版本读取成功，保存 8 个 cookie，Sentinel 准备未通过；本地账号冷却状态已打开。未继续重复探测、换身份或发送真实 turn。该结果不证明当前账号会话可用。

随后按用户明确要求，用当前账号经音乐项目 `safechain_runner.run_jobs` 执行真实批次，输入为 `A_int_v0.txt` + Op.60。等待原有冷却到期后，主页 HTTP 200，Sentinel prepare HTTP 200 但要求额外 challenge，任务在 PRE_SUBMIT 阶段停止；没有提交音乐 turn，artifact 任务未启动。记录位于音乐项目 `runs/safechain_live_20260926/REPORT.md`。不能将此结果描述为已经跑通；正常 challenge 要求也不等于账号已触发风控。

### 更正与最终在线结果

初次“challenge 导致停止”实际来自本次新增的本地误判，不能解释成服务器封锁。对照原链路后已移除该判断，仅修正有对应 HTTP200/零提交记录的本地误判冷却，账号 ID/cookie 未更换。

第二轮 finalize、conduit 和 conversation POST 全部 HTTP200，证明正常准备数据即使 required=true 也可以完成原有协议；音乐文本在本地新解析器处失败。此项已提交，保留 POST_SUBMIT_AMBIGUOUS/提交标记，不重提。

修正 frontend sparse list、metadata append、省略字段语义后，第三轮用当前账户经音乐项目入口顺序执行两个真实任务：Op.61 中文文本成功（109 字符）；Op.60 代码生成 JSON 和实际文件下载成功。JSON 已解析验证，UTF-8 无替换字符。服务端报告模型 `gpt-5-6`；两项各提交一次、无保护拒绝响应。

最终离线结果 **40 项通过**。在线报告：音乐项目 `runs/safechain_live_20260926_r3/REPORT.md` 和 `LIVE_REPORT.json`。长任务/其他模型/handoff 矩阵与长期风控表现仍未验证。
