# Connect Repair MCP Server

把 Repair Service 工具封装成 MCP Server，部署到 AgentCore Runtime，通过 AgentCore Gateway 暴露给 Amazon Connect AI Agent。包含两个轻量身份核验工具（`verifyCustomer` + fallback `verifyCustomerByPhoneAndName`）、三个 repair 业务工具和一个 FAQ 检索工具。

## 架构

```
Connect AI Agent → AgentCore Gateway (mcpServer target) → AgentCore Runtime (Container) → Backend API
```

## 工具

| Tool | 功能 | 入参（前置校验要求见 docstring） |
|------|------|----------|
| `verifyCustomer` | 主核身（流程 1，每通电话必跑）：让客户口述手机号后 4 位（`smsToken`），由 LLM 从 Connect AI Agent 系统上下文的 `<customer_info>` 块里读出 `userNumber`（需要在 Orchestration Prompt 模板里写一行 `- userNumber: {{$.Custom.userNumber}}`，并在 Contact Flow 里通过 `Set contact attributes` 把这次通话的 userNumber 写进 `Custom.userNumber`）一并传给本工具。MCP 校验 `userNumber` 末 4 位 == `smsToken` 一致后**签发一个 HMAC token** 当作 `customerId` 返回（底层真实 customerId 直接复用 `userNumber`） | `smsToken`(4 位数字) 必填；`userNumber`(LLM 从 customer_info 里取) |
| `verifyCustomerByPhoneAndName` | Fallback 核身（流程 2）：用**完整手机号 + 姓名**查到客户后签发 token；仅在 `verifyCustomer` 返回 `CUSTOMER_NOT_FOUND` 后调用（stub：手机号末 4 位为 `0000` 时返回 `CUSTOMER_NOT_FOUND`，其他底层 customerId 为 `"PHN" + 末 4 位`） | `phoneNumber`(纯数字 6–15 位)、`fullName` 必填 |
| `requestRepair` | 创建维修工单 | `productCategory`, `productsubCategory`, `province`, `city`, `district`, `description`, `brand`, `customerId` 必填；`productModel`, `serialNumber` 可选 |
| `trackRepair` | 查询工单状态 | `woNumber`(10 位数字)、`customerId`，工具内强制校验 |
| `cancelRepair` | 取消工单 | `woNumber`(10 位数字)、`customerId`，工具内强制校验 |
| `faqSearch` | FAQ 知识库自然语言检索（产品使用 / 故障排查 / 保修 / 维修） | `query`(任意自然语言问题) 必填 |

> **设计原则**：所有 tool 的使用方式都写在各自的 docstring 顶部，LLM 通过 `toolConfigurationList` 拿到 description 即可正确使用，**Connect AI Agent 的 Orchestration Prompt 只需要做一处身份相关改动**（在 `<customer_info>` 块里加一行 `- userNumber: {{$.Custom.userNumber}}`，并在 Contact Flow 里把这通电话的 userNumber 写进 `Custom.userNumber` 属性）；其余所有约束（包括"必须先核身"）都内置在 MCP server 的工具签名 + 服务端校验里。
>
> ⚠️ **不要使用** Connect AI Agent 的 "Function input parameters → Set manually / Set dynamically" 来注入 `userNumber`：经端到端验证（Wisdom transcript + Gateway APPLICATION_LOGS 双向交叉对照），该 UI 配置在 **MCP Gateway 类型工具上不会生效** —— 配置可以保存，但 Connect 在转发 `tools/call` 时不会把它合并进 arguments。必须走"customer_info + LLM 主动取"这条路径。
>
> **身份核验流程**（每通电话强制执行，由 MCP server 服务端硬性兜底）：
> 1. **流程 1（主核身，每通电话必跑）**：机器人提示用户口述手机号后 4 位 → LLM 从系统上下文 `<customer_info>` 块里读出 `userNumber`（需要 Orchestration Prompt 里有一行 `- userNumber: {{$.Custom.userNumber}}`，且 Contact Flow 已经把这通电话的 userNumber 写进 `Custom.userNumber` 属性）→ LLM 调 `verifyCustomer(smsToken=4 位数字, userNumber=<从 customer_info 取到的值>)` → MCP 在服务端比较 `userNumber[-4:] == smsToken`，一致才放行并签发 token，真实底层 customerId 复用 `userNumber` 本身 → 成功返回 `{"customerId": "<token>"}`。这里的 `customerId` 是 MCP server 用 HMAC-SHA256 签发的**短期 token**（默认 60 分钟），**不是**真实的 customerId 字符串。⚠️ Connect AI Agent 的 "Function input parameters → Set manually" 在 MCP Gateway 类型工具上经验证不会被注入，因此不要依赖那条路径，必须靠 customer_info + LLM 主动取。
> 2. **整通电话复用一次核身**：Agent 把这个 token 保存在对话上下文里，后续 `requestRepair` / `trackRepair` / `cancelRepair` 一律把它当作 `customerId` 透传给 MCP server；MCP server 验签后取出真实 customerId 再打到后端。
> 3. **流程 2（fallback）**：流程 1 返回 `{"error": "CUSTOMER_NOT_FOUND"}`（口述后 4 位与 `userNumber` 末 4 位不一致）或 `{"error": "INVALID_USER_NUMBER"}`（Connect 没透传 `userNumber` 或长度不足 4）时进入此分支。Agent 提示"该手机号查不到客户信息，请提供另一个完整手机号 + 姓名"，收齐两个字段后调 `verifyCustomerByPhoneAndName(phoneNumber, fullName)`。命中同样返回 `{"customerId": "<token>"}`；若再次 `CUSTOMER_NOT_FOUND`，**不要循环**，转人工。
> 4. **服务端硬性兜底**：三个 repair tool 在入口处对 `customerId` 进行 HMAC 验签 + 过期检查。任何**没跑过 verify**、**篡改 token**、或**幻觉编造 customerId** 的调用一律被拦下：
>    - `MISSING_CUSTOMER_ID`：参数为空
>    - `IDENTITY_INVALID`：不是合法 token / 签名不匹配 / payload 损坏
>    - `IDENTITY_EXPIRED`：token 已过期，需要重新走 verify
>
>    无论 LLM 看不看得懂 prompt、有没有被劫持，**只要它没拿到 verify tool 颁发的合法 token，就一行后端接口都打不出去**。
>
> **签名密钥配置**：
> - **首次部署**：`.env` 里 `IDENTITY_TOKEN_SECRET=` 留空即可，`deploy.sh` 会自动跑一次 `openssl rand -hex 32` 生成 32 字节密钥并**回写到 `.env`**，之后每次部署都复用同一份 secret（避免重新部署把所有在线通话的 token 全废掉）
> - **轮换**：把 `.env` 里那一行清空再 `./deploy.sh` —— 会重新生成并回写；副作用是当前所有在线 token 立即失效，客户需要重新报手机后 4 位
> - **不要在 `.env` 里直接写 `$(openssl rand -hex 32)`** —— `deploy.sh` 用 `set -a; source .env`，会在每次部署时重新执行子 shell，相当于每次都换密钥，**正在通话中的客户全部 token 失效**
> - 不设也没回写时（比如 CI 直接跑 `mcp_server.py`）server 会用进程内随机 secret 并打 WARNING，单副本调试可用，但 Runtime 重启或扩成多副本后所有已发 token 立即失效
> - Token TTL 默认 3600 秒（60 分钟，覆盖典型通话时长），可通过 `IDENTITY_TOKEN_TTL_S` 调整
>
> **Stub 测试触发器**：在真实身份 API 接通前，`verifyCustomer` 的"对得上 / 对不上"完全由 Agent 传进来的 `userNumber` 控制 —— 只要让 `smsToken` 与 `userNumber` 末 4 位不一致就能复现 `CUSTOMER_NOT_FOUND`，把 `userNumber` 留空或不到 4 位即可复现 `INVALID_USER_NUMBER`，两者都会驱动 Agent 走 fallback。`verifyCustomerByPhoneAndName` 仍保留幻数：`phoneNumber` 末 4 位为 `0000` 时返回 `CUSTOMER_NOT_FOUND`，模拟"再次找不到"的人工兜底分支。

每个 tool 的 docstring 顶部都列出了 **PRECONDITIONS**，明确字段在调用前必须经过哪些上游校验接口（产品大/小类、地址映射、型号/SN）。

### 接口契约速查（MCP tool ↔ 后端 API）

> 三个 repair tool 收到的 `customerId` 已经透传给后端，但当前部署的 Lambda（`midea/connect-api-customer.yaml`）**还没读这个字段**，等真实身份系统接入后再加上 `customerId` 校验/查询；MCP 这层先把字段对齐，避免上线时再改 tool schema。
> 全部三个 repair tool 都会在拿到后端响应后过一次 Strands+Bedrock 归一化（详见上面的"响应归一化"章节），表里的"出参"指的就是归一化后的 schema —— 也是 LLM 实际看到的字段。

#### 1. `verifyCustomer` — 主核身（无后端 API，纯本地比对）

| 项 | 内容 |
|----|------|
| 入参 | `smsToken: str`(4 位数字，客户口述，**LLM 必填**); `userNumber: str`(LLM 从 `<customer_info>` 块的 `- userNumber: <digits>` 行原样读出后传入，digits-only) |
| 本地校验失败 | `{"error":"INVALID_SMS_TOKEN"}` / `{"error":"INVALID_USER_NUMBER"}` |
| 比对规则 | `userNumber[-4:] == smsToken` 才算通过；不一致 → `CUSTOMER_NOT_FOUND` |
| 成功出参 | `{"customerId":"<HMAC token，形如 base64url(payload).base64url(sig)>"}` —— 短期签名 token，token 内嵌的真实 customerId 即 `userNumber` 本身，repair tool 透传后服务端验签后再打到后端 |
| 失败出参 | `{"error":"CUSTOMER_NOT_FOUND","message":"... fall back to verifyCustomerByPhoneAndName"}` 或 `{"error":"INVALID_USER_NUMBER","message":"..."}` |
| 后端 API | 无（接入真实身份 API 时改写 `_verify_phone_tail_to_customer_id`，tool 签名不变） |

#### 2. `verifyCustomerByPhoneAndName` — Fallback 核身（无后端 API，纯 stub）

| 项 | 内容 |
|----|------|
| 入参 | `phoneNumber: str`(6–15 位纯数字), `fullName: str`(非空) |
| 本地校验失败 | `{"error":"INVALID_PHONE_NUMBER" \| "INVALID_NAME"}` |
| Stub 触发 | `phoneNumber` 末 4 位为 `0000` → `CUSTOMER_NOT_FOUND`（**不再循环，提示转人工**） |
| 成功出参 | `{"customerId":"<HMAC token>"}` —— 同 `verifyCustomer`，是签名 token 不是真实 ID |
| 失败出参 | `{"error":"CUSTOMER_NOT_FOUND","message":"... do NOT loop"}` |
| 后端 API | 无 |

#### 3. `requestRepair` — 创建工单

| 项 | 内容 |
|----|------|
| MCP 入参（必填） | `productCategory`, `productsubCategory`, `province`, `city`, `district`, `description`, `brand`, `customerId` |
| MCP 入参（可选） | `productModel`, `serialNumber` |
| MCP 本地校验 | `MISSING_CUSTOMER_ID` / `INVALID_SUB_CATEGORY` / `INVALID_PROVINCE` / `INVALID_CITY` / `INVALID_DISTRICT`（详见下面的"服务端校验"） |
| 后端 endpoint | `POST {REPAIR_API_URL}/repair/request`，header `X-API-Key: <REPAIR_API_KEY>` |
| 后端 body | `{productCategory, productsubCategory, productModel, serialNumber, province, city, district, description, brand, customerId}`（camelCase） |
| 后端必填校验 | `productCategory, productsubCategory, province, city, district, description, brand` 缺任意一个 → `400 {"error":"Missing required fields: ..."}` |
| 后端写库 | DynamoDB `RepairTicketsTable` PutItem：`ticketNumber`(随机 10 位) + 上面所有字段 + `status:"pending"` + `createdAt` + `updatedAt` |
| 后端响应（201） | `{"message":"Repair ticket created successfully","ticketNumber":"...","ticket":{...}}` |
| MCP 归一化出参（`RequestResponse`） | `{woNumber, created(bool), status, scheduledAt, message}` |

#### 4. `trackRepair` — 查询工单

| 项 | 内容 |
|----|------|
| MCP 入参（必填） | `woNumber`(10 位数字), `customerId` |
| MCP 本地校验 | `INVALID_WO_NUMBER` / `MISSING_CUSTOMER_ID` |
| 后端 endpoint | `POST {REPAIR_API_URL}/repair/track`，header `X-API-Key: <REPAIR_API_KEY>` |
| 后端 body | `{woNumber, customerId}` |
| 后端必填校验 | `woNumber` 必须 10 位数字；`customerId` 当前 Lambda 暂未读 |
| 后端响应（200） | `{"message":"Repair ticket found","ticket":{ticketNumber,status,productCategory,productsubCategory,productModel,serialNumber,brand,province,city,district,description,createdAt,updatedAt}}`；找不到 → `404` |
| MCP 归一化出参（`TrackResponse`） | `{woNumber, status, statusDescription, scheduledAt, technicianName, technicianPhone, address, lastUpdatedAt, remarks}` |

#### 5. `cancelRepair` — 取消工单

| 项 | 内容 |
|----|------|
| MCP 入参（必填） | `woNumber`(10 位数字), `customerId` |
| MCP 本地校验 | `INVALID_WO_NUMBER` / `MISSING_CUSTOMER_ID` |
| 后端 endpoint | `POST {REPAIR_API_URL}/repair/cancel`，header `X-API-Key: <REPAIR_API_KEY>` |
| 后端 body | `{woNumber, customerId}` |
| 后端必填校验 | `woNumber` 必须 10 位数字；`customerId` 当前 Lambda 暂未读 |
| 后端响应（200） | `{"message":"Repair ticket cancelled","ticketNumber":"...","status":"cancelled"}`；找不到 → `404`；状态已是 `cancelled`/`completed` → `409 {"error":"Work order is already ...","status":"..."}` |
| MCP 归一化出参（`CancelResponse`） | `{woNumber, cancelled(bool), status, message}` |

#### 错误码速查（MCP 本地拦截，不会打到后端）

| 错误码 | 触发位置 |
|--------|---------|
| `INVALID_SMS_TOKEN` | `verifyCustomer` |
| `INVALID_USER_NUMBER` | `verifyCustomer`（`userNumber` 缺失或长度不足 4） |
| `CUSTOMER_NOT_FOUND` | `verifyCustomer` / `verifyCustomerByPhoneAndName` |
| `INVALID_PHONE_NUMBER` / `INVALID_NAME` | `verifyCustomerByPhoneAndName` |
| `MISSING_CUSTOMER_ID` | `requestRepair` / `trackRepair` / `cancelRepair`（参数为空） |
| `IDENTITY_INVALID` | `requestRepair` / `trackRepair` / `cancelRepair`（token 非法 / 篡改 / LLM 幻觉） |
| `IDENTITY_EXPIRED` | `requestRepair` / `trackRepair` / `cancelRepair`（token 超过 `IDENTITY_TOKEN_TTL_S`） |
| `INVALID_SUB_CATEGORY` | `requestRepair` |
| `INVALID_PROVINCE` / `INVALID_CITY` / `INVALID_DISTRICT` | `requestRepair` |
| `INVALID_WO_NUMBER` | `trackRepair` / `cancelRepair` |
| `HTTP 400/404/409/500` | 后端透传，归一化时直接跳过（错误路径保持确定） |

### 服务端校验（`requestRepair`）

除了 `customerId` / 工单号格式校验之外，`requestRepair` 在打到后端之前会再做两层本地校验，校验失败立刻返回错误、不发出网络请求：

| 字段 | 规则 | 失败时返回 |
|------|------|------------|
| `productsubCategory` | 必须是 `smart version` / `premium version` / `elite version` 三个枚举值之一（大小写、内部空格不敏感） | `{"error": "INVALID_SUB_CATEGORY", "allowed": [...]}` |
| `province` / `city` / `district` | 用 `china_regions_pinyin.json` 校验，接受**中文**或**拼音**（带不带行政后缀都行）；三者必须层级一致。**直辖市特例**：北京/上海/天津/重庆没有真正的 city 层，允许 `city == province`（如 `province="Beijing", city="Beijing", district="Chaoyang"`）；docstring 已显式提示 LLM 不要追问 city | `{"error": "INVALID_PROVINCE" \| "INVALID_CITY" \| "INVALID_DISTRICT"}` |

数据来源：[modood/Administrative-divisions-of-China](https://github.com/modood/Administrative-divisions-of-China)（WTFPL）。当行政区划数据需要刷新时跑一次 `gen_regions.py` 重新生成 `china_regions_pinyin.json`（依赖 `pypinyin`，仅在生成时需要，运行时不依赖）。

### 响应归一化（`requestRepair` / `trackRepair` / `cancelRepair`）

不同 BU 的后端 API 返回字段名经常不一致 —— 同一个"工单状态"字段可能叫 `status`、`ticketstatus`、`tstatus`、`statusName`，工单号可能叫 `woNumber`、`wono`、`ticketId`、`orderNumber`，让上层 LLM 难以稳定播报。MCP server 在三个写/查工具拿到后端响应后，会通过 **Strands Agents SDK** 调用 **Bedrock**（默认 `us.anthropic.claude-haiku-4-5-20251001-v1:0`），用 `agent.structured_output(PydanticModel, prompt)` 把原始 JSON 强制映射到固定 Pydantic schema 上 —— Strands 走 Bedrock tool-use 通道，模型几乎不会吐出格式非法的 JSON。

| 工具 | 规范字段（来自 `mcp_server.py` 中的 Pydantic 模型） |
|------|-----------------------------------------------------|
| `requestRepair` | `RequestResponse`：`woNumber`, `created`(bool), `status`, `scheduledAt`, `message` |
| `trackRepair` | `TrackResponse`：`woNumber`, `status`, `statusDescription`, `scheduledAt`, `technicianName`, `technicianPhone`, `address`, `lastUpdatedAt`, `remarks` |
| `cancelRepair` | `CancelResponse`：`woNumber`, `cancelled`(bool), `status`, `message` |

> ⚠️ `requestRepair` 是延迟敏感路径，归一化每次约 +0.5–1s。如果实测延迟拖累播报体验，可以单独把 `requestRepair` 的 `_normalize_with_llm(...)` 一行注释掉，或全局 `NORMALIZE_RESPONSE=0` 关闭。

每个工具的 docstring `RETURNS` 段就是 LLM 拿到的最终 schema —— 修改 Pydantic 模型 + docstring 即可调整播报字段。

设计要点：

- **Fail-soft**：归一化超时 / 限流 / Pydantic 校验失败时返回**原始**响应，不让 Bedrock 故障拖垮工单查询。日志关键字 `normalize ok` / `normalize failed`。
- **错误透传**：原始响应里出现 `error` 键直接跳过归一化，错误路径保持确定。
- **成本/延迟**：每次查询多一次 Haiku 调用（典型 ~0.5–1s）。可以通过 `NORMALIZE_RESPONSE=0` 关掉。
- **单例 Agent**：`_normalize_agent` 懒加载并缓存，避免每次调用都重新构造 Bedrock client。

环境变量（写到 `.env`，部署时通过 Runtime 注入）：

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `NORMALIZE_RESPONSE` | `1` | 设 `0`/`false`/`off` 关闭归一化，工具直接返回后端原始 JSON |
| `NORMALIZE_MODEL_ID` | `us.anthropic.claude-haiku-4-5-20251001-v1:0` | Bedrock inference profile / model ID |
| `BEDROCK_REGION` | 同 `AWS_REGION`（默认 `us-east-1`） | 哪个 region 调 Bedrock |
| `NORMALIZE_TIMEOUT_S` | `4` | 单次归一化的 read timeout（秒），超时 fail-soft 退回原始响应 |
| `IDENTITY_TOKEN_SECRET` | 进程内随机（仅调试可用） | 身份 token HMAC 密钥；首次部署时 `.env` 留空，`deploy.sh` 会自动 `openssl rand -hex 32` 并回写 |
| `IDENTITY_TOKEN_TTL_S` | `3600` | 身份 token 有效期（秒），过期返回 `IDENTITY_EXPIRED` 强制重新核身 |

部署脚本已经给 Runtime execution role 加了 `bedrock:InvokeModel` 权限（针对 foundation-model + inference-profile 资源），所以重新跑一次 `./deploy.sh` 即可生效；如要换模型，改 `.env` 里的 `NORMALIZE_MODEL_ID` 再重新部署即可。

### 可观测性（OTEL → CloudWatch GenAI Observability）

MCP server 通过 **AWS Distro for OpenTelemetry (ADOT)** 把 trace / metric / log 送到 AgentCore 托管的 OTLP 端点；CloudWatch GenAI Observability 控制台直接可视化（含 Strands 的 LLM 调用、Bedrock 子调用、tokens / latency）。

实现要点：

- `requirements.txt` 新增 `aws-opentelemetry-distro>=0.10.0` —— 提供 `opentelemetry-instrument` 入口。
- `Dockerfile` 的 CMD 由 `python -m mcp_server` 改为 `opentelemetry-instrument python -m mcp_server`，自动加载 ADOT 配置。
- `deploy_runtime.py` 给 Runtime 注入 `OTEL_SERVICE_NAME=${AGENT_NAME}`，作为 CloudWatch 控制台里这个 service 的标签；其他 `OTEL_EXPORTER_OTLP_*` 由 AgentCore Runtime 自动注入，无须配置。
- `deploy.sh` 给 Runtime execution role 加了 `xray:PutTraceSegments`、`xray:PutSpans`、`logs:PutLogEvents`、`cloudwatch:PutMetricData` 等 ADOT 写权限（除 `BedrockAgentCoreFullAccess` 自带的之外的兜底）。
- Strands 框架对 OTEL 有内建支持，无需在代码里调用 `StrandsTelemetry()` —— 一旦容器入口是 `opentelemetry-instrument`、ADOT 装好，trace 自动产生。

**一次性 region 设置（部署前做一次即可）**：

1. **打开 CloudWatch Transaction Search**：CloudWatch 控制台 → Application Signals (APM) → **Transaction search** → **Enable Transaction Search**，勾选 *ingest spans as structured logs* 后保存。
2. **在 Runtime 上启用 Tracing**：`./deploy.sh` 创建/更新 Runtime 后，去 [Agent Runtime 控制台](https://console.aws.amazon.com/bedrock-agentcore/agents) → 选中本 Runtime → **Tracing** 区块 → **Edit** → toggle *Enable* → Save。Spans 之后会出现在 `aws/spans` log group。

**查看 trace**：CloudWatch 控制台 → **GenAI Observability**（左侧导航） → 按 service.name=`connect-repair-mcp-server` 过滤即可看到 Strands 调用 Bedrock 的耗时分布。

**临时关闭 ADOT**（调试需要）：在 Runtime 上设置 `DISABLE_ADOT_OBSERVABILITY=true` 重新部署即可，无需改代码。

## 前置条件

- AWS CLI 已配置
- Python 3.9+（用于创建本地 venv 跑 boto3）
- `zip` 命令
- 后端 Repair API 已部署（提供 `/repair/request`、`/repair/track`、`/repair/cancel`、`/faq/simple`）

> 不需要本地装 Docker，镜像构建在 CodeBuild 上完成。
> AgentCore Gateway **可选** —— 留空 `GATEWAY_ID` 时部署脚本会自动创建一个 (CUSTOM_JWT)。

## 配置

把 `.env.example` 复制为 `.env`，填写你的配置：

```bash
cp .env.example .env
```

`.env` 已在 `.gitignore` 中，不会被提交到 Git。

必填项：

| 变量 | 说明 |
|------|------|
| `REGION` | AWS Region，如 `us-east-1` |
| `ACCOUNT_ID` | AWS 账号 ID |
| `AGENT_NAME` | Runtime / 资源名（用短横线，如 `connect-repair-mcp-server`） |
| `ECR_REPO_NAME` | ECR 仓库名 |
| `TARGET_NAME` | Gateway target 名 |
| `REPAIR_API_URL` | 后端 API 地址 |
| `REPAIR_API_KEY` | 后端 API Key |

Gateway 相关（任选其一种模式）：

| 变量 | 说明 |
|------|------|
| `GATEWAY_ID` | **复用现有 Gateway**：填入 ID。同时必须填 `GATEWAY_SERVICE_ROLE`（角色名，非 ARN） |
| `GATEWAY_SERVICE_ROLE` | 现有 Gateway 的 IAM Role **名称** |
| `GATEWAY_JWT_DISCOVERY_URL` | **自动创建 Gateway**：`GATEWAY_ID` 留空时必填，填 IDP（如 Connect 实例）的 OIDC discovery URL |
| `GATEWAY_JWT_ALLOWED_AUDIENCE` | 可选；自动创建场景下脚本会**自动**把新 Gateway 的 ID 加到 audience 列表里，这里填的值仅作叠加 |
| `GATEWAY_JWT_ALLOWED_CLIENTS` | 可选；JWT 允许的 client ID 列表（逗号分隔） |

自动创建模式下，脚本会:
1. 创建 `${AGENT_NAME}-gw` Gateway：`protocolType=MCP`，`authorizerType=CUSTOM_JWT`
2. **关键**：`allowedAudience` 设置成 Gateway 自己的 ID。Connect 颁发给该 namespace 的 JWT 中 `aud` claim 等于 Gateway ID，audience 不一致会触发 `insufficient_scope` 错误
3. 创建并复用 IAM 角色 `${AGENT_NAME}-gateway-role`（带 `InvokeAgentRuntime` + 日志权限）

复用模式下查找现有 Gateway Service Role 名称：

```bash
.venv/bin/python -c "
import boto3
g = boto3.client('bedrock-agentcore-control', region_name='us-east-1') \
        .get_gateway(gatewayIdentifier='<GATEWAY_ID>')
print(g['roleArn'].split('/')[-1])"
```

## 部署

```bash
cd mcp-agent
chmod +x deploy.sh
./deploy.sh
```

耗时约 5-10 分钟，脚本依次执行：

| 步骤 | 内容 |
|-----|------|
| 1 | 创建 ECR 仓库 |
| 2 | 打包源码上传 S3 |
| 3 | 创建 CodeBuild 项目和 IAM 角色 |
| 4 | CodeBuild 构建 ARM64 镜像推送到 ECR |
| 5 | 创建 Runtime 执行角色 |
| 6 | 创建/更新 AgentCore Runtime |
| 6.5 | **(新)** Gateway 不存在则自动创建（含 IAM 角色） |
| 7 | 给 Gateway Service Role 加 `InvokeAgentRuntime` 权限 |
| 8 | 创建/更新 Gateway 的 mcpServer target |

成功后生成 `deployment-info.log`，包含 Runtime ID、ARN、MCP Endpoint 等信息。

## 验证

```bash
# Runtime 状态应为 READY（CLI 没有 bedrock-agentcore-control 子命令时改用 boto3）
.venv/bin/python -c "import boto3; c = boto3.client('bedrock-agentcore-control', region_name='us-east-1'); \
  print(c.get_agent_runtime(agentRuntimeId='<runtime-id>')['status'])"

# 查看 Runtime 日志
aws logs tail /aws/bedrock-agentcore/runtimes/<runtime-id>-DEFAULT \
  --region us-east-1 --follow
```

日志里看到 `INFO mcp.server.lowlevel.server Processing request of type PingRequest` 即表示 Gateway 健康检查正常。

`mcp_server.py` 使用标准 `logging`（logger 名 `mcp_server`）。在 CloudWatch Logs Insights 里筛自家日志：

```
fields @timestamp, @message
| filter @message like /mcp_server/
| sort @timestamp desc
| limit 50
```

`WARNING` / `ERROR` 行表示后端 API 调用失败，可作为告警依据。

## 在 Connect 中配置

Gateway target 就绪后，去 Amazon Connect 控制台：

1. **AI Agent Designer** → 选择 AI Agent → **Add tool** → **Add existing AI Tool**
2. Namespace 选 `gateway_<gateway-name>`
3. 把这六个 AI Tool 都加进来（每次重复 Add existing AI Tool）：
   - `connect-repair-mcp-agent___verifyCustomer`
   - `connect-repair-mcp-agent___verifyCustomerByPhoneAndName`
   - `connect-repair-mcp-agent___requestRepair`
   - `connect-repair-mcp-agent___trackRepair`
   - `connect-repair-mcp-agent___cancelRepair`
   - `connect-repair-mcp-agent___faqSearch`
4. 每个 tool 的 **Output Filters** → Select Property Keys 里加 `result`（必须勾选，否则 LLM 读不到 `customerId` 等返回值）
5. **不要**在 `verifyCustomer` 上配 Function input parameters（已验证不生效，详见上面的设计原则提示）
6. 编辑 AI Agent 的 **Orchestration Prompt**，在 `<customer_info>` 块里加一行（如已有 `phoneNumber` 等字段可以保留）：
   ```
   <customer_info>
   - userNumber: {{$.Custom.userNumber}}
   - BU: {{$.Custom.BU}}
   </customer_info>
   ```
   LLM 会把这里的 `userNumber` 当作 `verifyCustomer` 的 `userNumber` 入参原样传给 MCP server。
7. 在对应的 **Contact Flow** 里加一个 **Set contact attributes** block，destination=*User Defined*，key=`userNumber`，value 来自 lookup（CRM / DynamoDB / Lambda）或对接系统已知字段；Custom attribute 的命名要和上一步 Orchestration Prompt 里的占位符一致。
8. 点 **Update / Publish** 保存 AI Agent。

> **更新工具签名后必须重做引用**：MCP server 修改 tool 签名（参数名/必填项变化）并重新 deploy 后，AI Agent 持有的是更早部署时的工具描述快照。Gateway target 同步只刷新 Gateway 侧 schema，AI Agent 侧不会自动跟随。需要在 AI Agent Designer 里把这些工具 **Remove → 再 Add 回来**，让它拉到新 schema，否则 LLM 会按旧签名调用。

> **如何快速诊断 verifyCustomer 失败**：
> - 看 **AgentCore Gateway APPLICATION_LOGS**（log group `/aws/vendedlogs/bedrock-agentcore/gateway/APPLICATION_LOGS/<gateway-name>`）的 `tools/call` 行，确认 `arguments` 里是否有 `userNumber`。没有 → 上下文里取不到。
> - 看 **Connect Wisdom transcript**（log group `/aws/connect/wisdom/<assistant-id>`）的 `TRANSCRIPT_AGENTIC_MESSAGE` 里 `prompt.system` 中 `<customer_info>` 块是否真有 `- userNumber: <digits>`。空值或字段缺失 → Orchestration Prompt 模板没生效，或 Contact Flow 没写入对应 Custom attribute。
> - 同一 transcript 里 `TRANSCRIPT_LARGE_LANGUAGE_MODEL_INVOCATION.completion.toolUseList[].toolInput` 是 LLM 实际生成的入参，跟 Gateway 收到的 arguments 对比可以判断"丢字段"是 LLM 端还是 Connect 端发生的。

## 清理

```bash
./cleanup.sh
```

删除本次部署创建的所有资源（Gateway target、Runtime、ECR、S3、CodeBuild、IAM 角色）。脚本会自动判断 Gateway 是否是它自己创建的：
- **复用模式**（`.env` 里给了 `GATEWAY_ID`）：用户提供的 Gateway 与 service role **不会被删**，仅清理 target 和 InvokeAgentRuntime 内联策略
- **自动创建模式**（`.env` 里 `GATEWAY_ID` 为空）：连同 `${AGENT_NAME}-gw` Gateway 与 `${AGENT_NAME}-gateway-role` 一并删除

## 本地测试（可选）

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
export REPAIR_API_URL="https://xxx/<stage>"
export REPAIR_API_KEY="xxx"
python mcp_server.py
# 本地 MCP server 监听 http://localhost:8000/mcp
```

## 文件说明

| 文件 | 作用 |
|------|------|
| `.env.example` | 配置模板（复制为 `.env` 后填写） |
| `mcp_server.py` | MCP Server 实现（FastMCP + 6 个 tool：2 核验 + 3 repair + 1 FAQ） |
| `Dockerfile` | ARM64 容器（Python 3.11，非 root 用户） |
| `requirements.txt` | Python 依赖 |
| `buildspec.yml` | CodeBuild 构建脚本 |
| `deploy.sh` | 主部署脚本（Step 1-5 shell，Step 6-8 调 Python） |
| `deploy_runtime.py` | Step 6-8：Runtime、IAM、Gateway target |
| `cleanup.sh` | 清理脚本 |
| `china_regions_pinyin.json` | 中国省/市/区拼音清单（运行时用于地址校验） |
| `gen_regions.py` | 一次性生成上面 JSON 的脚本（数据源变更时再跑） |

## 注意事项

- Runtime 必须用 **ARM64** 镜像，CodeBuild 已配成 ARM 环境
- Dockerfile 遵循官方示例：Python 3.11 基础镜像、非 root 用户 `bedrock_agentcore`、启动命令 `opentelemetry-instrument python -m mcp_server`（ADOT 自动加载 OTEL，详见上面的"可观测性"章节）
- 所有 tool 都使用 `@mcp.tool(structured_output=False)` —— 返回体只含 `content[]`，没有 `structuredContent` 字段，避免 Connect AI Agent 解析路径上的兼容问题
- Tool 函数体内手工 `json.dumps(dict)`（字符串），Connect Output Filter 必须勾选 `result`，否则 LLM 拿不到返回值
- `trackRepair` / `cancelRepair` 在工具内部就会校验 `woNumber` 非空且为 10 位数字，校验失败直接返回 `{"error": "INVALID_WO_NUMBER"}`，不会发出网络请求
- Gateway name 受 AWS 限制：仅允许 `[0-9a-zA-Z-]`，最长 48 字符。脚本自动用 `${AGENT_NAME}-gw` 拼接并裁剪
- Gateway audience 必须等于 Gateway 自身的 ID。一旦看到 Gateway 日志报 `insufficient_scope - The request requires higher privileges than provided by the access token.`，多半就是 audience 配错了
- API Key 通过环境变量注入到 Runtime（生产环境建议改用 Secrets Manager）
- `deploy.sh` 幂等，重复运行会更新 Runtime 并重新同步 Gateway target
