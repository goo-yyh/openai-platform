# OpenAI 转发网关

使用 Caddy 透明转发 OpenAI API，为本地 Codex CLI 提供独立入口。上游固定为 `https://api.openai.com`，使用项目 `.env` 中的 `OPENAI_API_KEY`，费用计入该 Key 所属的 OpenAI Platform 项目。

```text
Codex CLI（独立配置、网关凭证）
    → https://explainrepo.com/v1（或本地 / SSH 隧道入口）
    → Caddy 容器（替换上游鉴权）
    → https://api.openai.com/v1
```

支持本机运行，也支持服务器部署后通过 **HTTPS 域名**或 **SSH 加密隧道**访问。桌面 App 配置不会被自动修改；客户端默认使用 HTTP/SSE，WebSocket 需另外验证。

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
- 将回环地址和配置的网关域名加入 `NO_PROXY`，避免客户端系统代理干扰连接；系统级 TUN 仍由本机代理规则控制。
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
- 网关容器未开启访问日志，容器内 Caddy 运行日志丢弃，Docker 日志驱动为 `none`。
- 网关容器关闭 Caddy 管理端口及配置自动保存，根文件系统只读，运行目录使用内存文件系统。
- `.env` 中保存上游 Key 和网关凭证；容器运行环境也需要持有它们。Docker 管理权限等同于能够访问这些凭证。
- 网关容器端口只绑定 `127.0.0.1`，公网通过服务器的 HTTPS 入口访问。
- `/v1/*` 在鉴权后按原方法、路径、查询和内容转发；网关凭证替换成上游 Key。Cookie 与 ChatGPT 账号头不会转发。
- 网关不重写模型、工具、`store` 或计费参数。测试使用 `store: false`，但 OpenAI 的数据保留政策仍独立适用。
- 不实施每模型配额和花费上限；只授权可信客户端使用网关凭证。

## 服务器部署与 SSH 隧道

在本地 `.env` 中配置 `IP`、`NAME` 和 `PASSWORD`。可选 `SSH_PORT` 默认为 `22`，`SERVER_PATH` 默认为服务器用户目录下的 `services/openai-platform`。密码含空格或 `#` 时请用单引号包住值。服务器需已安装 Docker、Compose、Git、Python 3.10+，且该 SSH 用户可操作 Docker、访问本仓库和 OpenAI API。

先将本地代码提交并推送到本仓库的 `main`，然后运行：

```bash
python3 scripts/server.py deploy
python3 scripts/server.py tunnel
python3 scripts/gateway.py codex
```

使用隧道时，将 `.env` 的 `GATEWAY_URL` 留空或设为 `http://127.0.0.1:8787`；配置了 HTTPS 地址时，Codex 启动器会优先使用该地址。

部署脚本在服务器首次克隆仓库，后续执行 `git pull --ff-only`。它拒绝覆盖已有脏工作区或来源不匹配的仓库，并核对远端版本与本地提交一致。运行目录由 SSH 用户拥有，不修改服务器已有的 Web 服务。

只有 `OPENAI_API_KEY`、`GATEWAY_TOKEN` 和 `CODEX_MODEL` 会通过 SSH 传到远端 `.env`，文件权限为 `0600`。服务器的 IP、SSH 用户名和密码不写入远端 `.env`。SSH 密码只提供给本地 OpenSSH 的 askpass 子进程，不放在命令行参数或脚本正文中。

主机密钥保存在本项目 `.local/ssh/known_hosts`；首次使用采用 OpenSSH 的首次信任方式，后续主机密钥变化会拒绝连接，不会自动清除旧记录。

```text
本地 Codex → 本地 127.0.0.1:8787
          → SSH 加密隧道
          → 服务器 127.0.0.1:8787 → Caddy → OpenAI API
```

本地和服务器的 8787 都只绑定回环地址。无需域名或公开 HTTP 端口；公网只使用服务器已有的 SSH 入口。隧道在后台运行，网络中断后可重新执行 `tunnel`。它不会自动安装开机启动项，服务器容器则使用 `unless-stopped` 重启策略。

本地 Docker 网关和 SSH 隧道共用 8787 端口，切换到服务器前先停止本地网关：

```bash
python3 scripts/gateway.py down
python3 scripts/server.py tunnel
```

查看服务器、验证完整链路：

```bash
python3 scripts/server.py status
python3 scripts/verify.py --remote
python3 scripts/verify_codex.py --remote
```

远端验证报告分别写入 `outputs/server-verification.json` 和 `outputs/server-codex-verification.json`，不覆盖本地验收结果。

关闭隧道与停止远端网关是两项独立操作：

```bash
python3 scripts/server.py untunnel  # 仅关闭本地 SSH 隧道
python3 scripts/server.py down      # 停止服务器上的网关
```

## HTTPS 域名访问

当前域名入口为 `https://explainrepo.com/v1`。Vercel 管理 DNS，根域名的 A 记录指向服务器；API 请求经过服务器原有的系统 Caddy，由它签发、续期证书并转发到回环地址上的网关容器。

```text
Codex / OpenAI SDK → HTTPS :443 → 系统 Caddy
                 → 127.0.0.1:8787 → 网关容器 → OpenAI API
```

本地 `.env` 配置网关源站地址，不带 `/v1`：

```dotenv
GATEWAY_URL=https://explainrepo.com
```

随后直接运行 `python3 scripts/gateway.py codex`，无需开启 SSH 隧道。其他兼容客户端的 `base_url` 设为 `https://explainrepo.com/v1`，`api_key` 使用本地 `.env` 中的 `GATEWAY_TOKEN`。上游 `OPENAI_API_KEY` 仅供服务器网关使用。

HTTPS 配置适用于已有 `/etc/caddy/Caddyfile`、`caddy` 系统用户与 systemd 服务的 Linux 服务器。DNS 指向该服务器，80/443 可达，SSH 用户能够用 `.env` 中的密码执行 sudo 时，可运行：

```bash
python3 scripts/server.py https --domain explainrepo.com
python3 scripts/verify.py --https
python3 scripts/verify_codex.py --https
```

配置脚本只添加自己的域名块，先验证，再备份和 reload，保留原有站点；重载失败时恢复原文件。它不会创建 DNS 记录。系统 Caddy 保留证书和站点配置供自动续期，域名访问日志被丢弃，网关容器继续使用前述无会话存储配置。

HTTPS 检查额外验证可信证书、HTTP 到 HTTPS 重定向和远端容器状态；API 请求直接使用域名，SSH 仅用于检查服务器状态。结果写入 `outputs/https-verification.json` 和 `outputs/https-codex-verification.json`。重新验证会产生少量真实 API 用量。

## 参考

- [Codex 网关兼容要求](https://learn.chatgpt.com/docs/enterprise/gateway-compatibility)
- [Codex 网关配置](https://learn.chatgpt.com/docs/enterprise/connect-to-a-gateway)
- [Caddy 反向代理与流式传输](https://caddyserver.com/docs/caddyfile/directives/reverse_proxy)
