# OpenAI 本地转发网关

使用 Caddy 透明转发 OpenAI API，为本地 Codex CLI 提供独立入口。上游固定为 `https://api.openai.com`，使用项目 `.env` 中的 `OPENAI_API_KEY`，费用计入该 Key 所属的 OpenAI Platform 项目。

```text
Codex CLI（独立配置、网关凭证）
    → http://127.0.0.1:8787/v1
    → Caddy 容器（替换上游鉴权）
    → https://api.openai.com/v1
```

当前交付范围是**本机 HTTP/SSE 模式**，已验证真实 OpenAI Responses 请求和 Codex CLI 文件操作。桌面 App 配置没有修改；远程服务器、公开 HTTPS 和 WebSocket 模式没有纳入本次验收。

## 运行条件

- 已运行的 Docker / Docker Compose。
- Python 3.10 或更高版本；脚本只用标准库。
- 使用 Codex 时，本机已安装 `codex`。
- `.env` 中有有效的 `OPENAI_API_KEY`。

Caddy 镜像已固定到经过验证的 digest，本次实际版本为 `2.11.7`。不会自动跟随 `latest` 更新。

## 启动与停止

在本目录运行：

```bash
python3 scripts/gateway.py init
python3 scripts/gateway.py up
python3 scripts/gateway.py status
```

`init` 会保留已有的 OpenAI Key，生成独立的 `GATEWAY_TOKEN`（已存在则复用），并把 `.env` 权限设为 `0600`。反复执行不会轮换已有凭证。不要把 `.env` 提交到版本库。

`up` 会校验配置并重新创建容器，因此修改 Caddyfile 或 `.env` 后也执行这条命令。重新创建会中断正在进行的请求。

```bash
python3 scripts/gateway.py down
```

只检查配置，不启动常驻服务：

```bash
python3 scripts/gateway.py validate
```

服务入口为 `http://127.0.0.1:8787/v1`。无需凭证的 `GET /healthz` 只表示本地进程健康，不代表上游模型或余额可用。

## 使用 Codex CLI

在本目录启动：

```bash
python3 scripts/gateway.py codex
```

在任意目录，指定要操作的项目：

```bash
python3 /Users/yuyuehui/openai-platform/scripts/gateway.py codex -- -C /你的项目绝对路径
```

启动器会：

- 使用本项目的 `.local/codex/`，不修改已有的 `~/.codex/config.toml` 或登录状态。
- 将本项目的网关凭证提供给 Codex；移除子进程环境中的 `OPENAI_*` 上游配置。
- 明确设置本地地址的 `NO_PROXY`，避免 macOS 系统代理导致本地连接返回 502。
- 使用 `wire_api = "responses"`，本次验证走 HTTP/SSE，关闭 WebSocket。
- 从本机官方 Codex 模型缓存中复制所选模型的元数据（如果存在），仅复制模型信息，不复制账户身份或登录凭证。当前 CLI 对 `gpt-6.1-sol` 没有内置元数据，这一步避免回退为通用工具设置。

默认模型为 `gpt-6.1-sol`。可以在 `.env` 中设置 `CODEX_MODEL`，也可以通过 CLI 的 `--model` 参数选择已支持的模型。新模型须同时具备 API 项目访问权限和与客户端匹配的模型元数据。本地只提供普通 OpenAI `/v1/models` 透传，没有额外实现 Codex 专用的远程模型目录接口。

独立 Codex 环境不会自动继承原账号的插件、MCP 或历史。正常交互会在 `.local/codex/` 保存客户端自己的状态；这与网关保存会话是两回事。

## 重新验证

以下两条命令会产生真实 API 用量，使用小型测试任务，不读取或上传用户项目文件：

```bash
python3 scripts/verify.py
python3 scripts/verify_codex.py
```

`verify.py` 验证：

- 本地健康状态、缺失和错误凭证、非 API 路径。
- 经网关访问官方模型列表。
- 上游无效模型的 HTTP 状态与错误码传递。
- SSE 事件增量到达与 `response.completed` 结束事件。
- 函数名称、参数、`call_id` 和工具结果的完整传递。
- `store: false` 下重放上下文的多轮对话。
- Caddy 没有自动保存展开后的配置。

`verify_codex.py` 在 `work/` 内生成一次性测试文件，通过实际安装的 Codex CLI 读取、转换、写入并校验文件。测试使用 `--ephemeral`；报告只保存测试结果和用量，不保存凭证。

结果文件：

- `outputs/verification.json`：网关 API 检查、上游 request ID、流式时序和用量。
- `outputs/codex-verification.json`：Codex CLI 的实际执行结果。
- `outputs/验证报告.md`：本次交付的可读说明。

## 存储和转发边界

- 无数据库、无会话存储、无缓存服务；请求内容仅在内存中转发。
- 未开启访问日志，Caddy 运行日志丢弃，Docker 日志驱动为 `none`。
- 关闭 Caddy 管理端口及配置自动保存，根文件系统只读，运行目录使用内存文件系统。
- `.env` 中保存上游 Key 和网关凭证；容器运行环境也需要持有它们。Docker 管理权限等同于能够访问这些凭证。
- 端口只绑定 `127.0.0.1`，局域网和公网不能直接连接。
- `/v1/*` 在鉴权后按原方法、路径、查询和内容转发；网关凭证替换成上游 Key。Cookie 与 ChatGPT 账号头不会转发。
- 网关不重写模型、工具、`store` 或计费参数。测试使用 `store: false`，但 OpenAI 的数据保留政策仍独立适用。
- 不实施每模型配额和花费上限；只授权可信客户端使用网关凭证。

## 之后部署到服务器

当前 Compose 有意只开放本机 HTTP 端口。正式远程部署应配置域名和 HTTPS、维护证书存储，并从你的本地 Codex 再验证一遍。不要直接把 `127.0.0.1:8787` 改成公网绑定后通过明文 HTTP 使用。

## 参考

- [Codex 网关兼容要求](https://learn.chatgpt.com/docs/enterprise/gateway-compatibility)
- [Codex 网关配置](https://learn.chatgpt.com/docs/enterprise/connect-to-a-gateway)
- [Caddy 反向代理与流式传输](https://caddyserver.com/docs/caddyfile/directives/reverse_proxy)
