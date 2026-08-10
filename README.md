# Gate Switch（`sgate`）

当前版本：**2.0.0**。要求 Python 3.10+；脚本在导入其他现代语法前会对过旧解释器给出可操作提示。

`sgate` 是一个面向 macOS 的 Codex / OpenCode / Claude Code / Claude Desktop Code tab / ChatGPT.app 渠道切换脚本。它可以保存多个兼容渠道，自动拉取模型，并通过终端复选界面配置模型与推理强度。

## 快速安装

```sh
mkdir -p ~/.local/bin && \
curl -fsSL https://raw.githubusercontent.com/cupid532/Gate-Switch/main/sgate.py -o ~/.local/bin/sgate && \
chmod 700 ~/.local/bin/sgate
```

如果 `~/.local/bin` 尚未加入 `PATH`：

```sh
echo 'export PATH="$HOME/.local/bin:$PATH"' >> ~/.zshrc
source ~/.zshrc
```

安装后直接运行：

```sh
sgate
```

如果 GitHub 仓库已经更新但本机菜单仍是旧版，请重新安装到实际的 PATH 入口：

```sh
install -m 700 sgate.py "$HOME/.local/bin/sgate"
rehash 2>/dev/null || true
sgate --help
```

确认帮助中出现 `claude-code` 和 `claude-desktop` 后，再运行 `sgate`。

## 主要功能

- API Key 不写入 `config.toml` 或 SGate 配置文件，密钥源为 macOS Keychain；macOS `security add-generic-password -w` 的系统调用会短暂携带密钥参数，SGate 不记录或打印该值。
- 自动请求渠道的 `/models` 接口并缓存模型列表。
- 模型支持多选：在项目上按 `Space` 勾选，再按一次取消。
- Codex/OpenCode 推理强度继续支持多选：`minimal`、`low`、`medium`、`high`、`xhigh`。
- Claude Code 持久 effort 仅支持 `low`、`medium`、`high`；Claude alias 必须分别映射到 `opus`、`sonnet`、`haiku`。
- 多选界面中 `Space` 仅勾选，`d` 设置默认，`Enter` 仅确认。
- 自动生成独立模型目录，让 ChatGPT.app 的模型和推理强度控件显示多个候选。
- 应用交互配置后立即重启 ChatGPT.app，避免配置写入但运行中 App 未重新加载。
- 可“停用”渠道但保留渠道记录和 Keychain 密钥，下次可以直接重新启用。
- 每次修改前自动备份 Codex 配置。
- 启动后可先选择 `Codex` 或 `OpenCode`，两套配置互不覆盖。
- 交互菜单将“渠道管理”和“工具配置”分开：新增、删除、总览、连接检查在外层完成；进入 Codex/OpenCode 后再选择模型和推理强度。
- OpenCode 支持多渠道同时启用：所有勾选渠道都会写入 `provider`，其中一个作为默认，可在 OpenCode 内用 `/models` 直接切换。
- OpenCode 使用合法的 `provider`、`model` 和 `agent.build.variant` 配置，保留 `minimal`、`low`、`medium`、`high`、`xhigh` 思考强度。
- 状态、总览、连接检查均为带颜色的分栏表格输出；管道输出、`NO_COLOR` 或非 TTY 环境自动降级为纯文本。
- OpenCode API Key 仍以 macOS Keychain 为密钥源；启用 OpenCode 时写入每渠道独立、权限为 `0600` 的运行时引用文件，并通过 `{file:...}` 引用，不写入 `opencode.json`；停用或删除渠道时清理该文件。
- Claude Code 使用独立的 Anthropic 配置（`protocols.anthropic`），通过 macOS Keychain 和 `apiKeyHelper` 动态读取密钥；不会从 OpenAI Base URL 猜 Anthropic endpoint。启用自定义网关时会将默认 alias 映射为明确的 `ANTHROPIC_MODEL`，并设置 `ANTHROPIC_SMALL_FAST_MODEL`，避免网关收到字面量 `sonnet`/`opus`/`haiku`；同时设置 `CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS=1`，避免第三方网关因 Claude Code 的 beta header/字段返回 400。Anthropic Base URL 必须显式配置为 gateway root（例如 `https://gateway.example`，不能以 `/v1` 结尾），`auth.secret_ref` 必须是 Keychain slug 字符串。
- Claude Desktop JSON 只读：SGate 仅读取 MCP 信息，Code tab 复用 Claude Code settings；Desktop Chat/Cowork 的账户与 bearer-only 认证由官方应用管理，SGate 不写不受支持的 provider 字段。

## 交互按键

- `↑` / `↓` 或 `j` / `k`：移动光标
- `Space`：勾选或取消当前项目，可同时选择多个
- `d`：将光标项设为默认
- `Enter`：仅确认当前勾选
- `/`：搜索模型
- `Backspace`：删除搜索字符
- `Esc` / `q`：返回或取消

## 常用命令

```sh
# 打开交互菜单
sgate

# 交互菜单层级
# SGate
# ├─ 渠道管理：新增 / 删除 / 总览 / 检查
# ├─ Codex：选择渠道、模型、推理强度并启用
# ├─ OpenCode：多渠道启用 / 默认渠道 / 模型 / 推理强度
# ├─ Claude Code：渠道 / 模型 / 思考强度
# └─ Claude Desktop：Code tab 渠道 / MCP 状态

# 直接打开外层渠道管理菜单
sgate channels

# 直接打开 OpenCode 菜单
sgate opencode

# 切换 OpenCode 默认渠道和思考强度
sgate opencode use fusiongate --model gpt-5.6-sol --reasoning xhigh

# 追加启用一个渠道，但不改变当前默认渠道
sgate opencode add congee

# 一次性指定全部启用渠道，并选定默认渠道
sgate opencode sync fusiongate congee --default fusiongate

# 从 OpenCode 配置移除某个渠道
sgate opencode disable congee

# 查看 OpenCode 当前实际配置（含多渠道列表）
sgate opencode status

# 打开 Claude Code 渠道菜单
sgate claude-code

# 显式配置 Claude Code：Anthropic URL、三个 alias 映射、默认 alias 和 effort
sgate claude-code use fusiongate \
  --anthropic-base-url https://anthropic-gateway.example \
  --map opus=claude-opus-5 --map sonnet=claude-sonnet-5 --map haiku=claude-haiku-5 \
  --default-role sonnet --effort high

# 兼容旧 --model：明确 map-all 到三个 alias（不会猜模型族）
sgate claude-code use fusiongate --anthropic-base-url https://anthropic-gateway.example --model claude-sonnet-5 --default-role sonnet --effort high

# 查看 Claude Code 当前配置
sgate claude-code status

# 只校验配置和请求契约：不读取 Keychain、不发请求、不写配置
sgate claude-code use fusiongate \
  --anthropic-base-url https://anthropic-gateway.example \
  --map-all claude-sonnet-5 --default-role sonnet --effort high --dry-run

# 真实 POST /v1/messages 诊断；只发送固定的 health check，不记录用户提示或密钥
sgate claude-doctor fusiongate
sgate claude-doctor fusiongate --dry-run --json

# 打开 Claude Desktop 菜单，切换其 Code tab 或查看 MCP 状态
sgate claude-desktop

# 配置 Desktop Code tab（Desktop JSON 保持只读，参数同 Claude Code）
sgate claude-desktop use fusiongate \
  --anthropic-base-url https://anthropic-gateway.example \
  --map opus=claude-opus-5 --map sonnet=claude-sonnet-5 --map haiku=claude-haiku-5 \
  --default-role opus --effort high

# 查看 Claude Desktop 配置和 MCP 状态
sgate claude-desktop status

# 查看实际配置和已保存渠道
sgate status
sgate list

# 命令行添加渠道并自动拉取模型；命令行模式仍可直接进入选择器
sgate add

# 交互菜单中的“新增渠道”只保存渠道、Key 和模型缓存；
# 随后到 Codex 或 OpenCode 菜单分别勾选各自的模型与推理强度。

# 启用已保存渠道
sgate use <slug>

# 指定默认模型和推理强度，并立即重启 ChatGPT.app
sgate use <slug> --model gpt-5.6-luna --reasoning xhigh --restart-app

# 重新配置模型和推理强度
sgate configure <slug> --restart-app

# 刷新模型列表
sgate refresh <slug> --restart-app

# 停用当前脚本渠道，但保留渠道和 API Key
sgate disable --restart-app
# 等价命令：sgate cancel / sgate deactivate

# 切回官方登录
sgate login --reasoning high --restart-app

# 检查渠道与配置
sgate doctor <slug>
sgate diagnose
sgate app-doctor

# 永久删除渠道及其 Keychain 密钥
sgate remove <slug>
```

## 配置与生效方式

脚本默认使用：

- Codex 配置：`~/.codex/config.toml`
- 渠道记录：`~/.codex/codex-channels.json`
- SGate 模型目录：`~/.codex/sgate-model-catalog.json`
- 配置备份：`~/.codex/config.toml.sgate-*.bak`
- OpenCode 配置：`~/.config/opencode/opencode.json`（可由 `OPENCODE_CONFIG` 覆盖）
- OpenCode 运行时密钥：`~/.config/opencode/.sgate/<channel>-api-key`（启用时生成、停用/删除时清理，权限 `0600`）
- Claude Code 配置：`~/.claude/settings.json`（可由 `CLAUDE_CODE_SETTINGS` 覆盖）
- Claude Code 配置备份：`~/.claude/sgate-backups/settings.json.sgate-*.bak`
- Claude Desktop 配置：`~/Library/Application Support/Claude/claude_desktop_config.json`（可由 `CLAUDE_DESKTOP_CONFIG` 覆盖）

ChatGPT.app 和 OpenCode 都不会让已经运行的会话热切换配置。切换后请重启对应工具；Codex 交互模式仍会按原逻辑处理 ChatGPT.app，OpenCode 切换完成后会明确提示重启。

Codex 一次只能有一个生效 provider，因此 Codex 渠道是单选。OpenCode 的 `provider` 是一个映射，可以同时保留多个渠道，`model` 只决定默认值；所以 OpenCode 支持多渠道并存，启用新渠道不会移除已有渠道。停用默认渠道时，若仍有其他已启用渠道，会自动提升其中一个为默认；若已无渠道，则恢复 SGate 接管前的原始默认配置。

Claude Code 的渠道切换会更新独立 Anthropic Base URL、`model` alias、`effortLevel`、精确的 `ANTHROPIC_MODEL`/`ANTHROPIC_SMALL_FAST_MODEL`、`ANTHROPIC_DEFAULT_*_MODEL` 三项角色映射，并通过 `apiKeyHelper` 从 Keychain 读取当前渠道密钥。第三方网关默认设置 `CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS=1`，降低新版本 Claude Code 发送实验性 beta 字段导致的 400；直连 `api.anthropic.com` 时保留官方能力。每次接管写入受管 JSON pointer journal：停用时仅在本地值仍等于 SGate 上次 applied 值时恢复 before；冲突保留并报告。旧版 `claude_fallback` 仅标记为 legacy/ambiguous，不宣称 exact restore。Claude Desktop 的 JSON 只读，不写 provider 字段。

Claude Messages 诊断严格将 gateway root 规范化后仅追加一次 `/v1/messages`，并分类 URL、认证/权限、模型、400 schema/beta/header、404、429、5xx 与网络/TLS。输出只包含固定健康检查的结果并对疑似认证字段脱敏；不会接收或记录真实用户提示。官方端点保留稳定 `anthropic-version` 及可选 beta，第三方仅使用稳定 Messages headers，绝不猜测 beta。

## 开发与验证

```sh
python3.12 -m unittest -v
python3.12 -m compileall -q sgate.py test_sgate.py
python3.12 sgate.py --version
python3.12 sgate.py --help >/dev/null
```

测试使用本机 `ThreadingHTTPServer` 接收真实 HTTP 请求并断言 `/v1/messages` 的 URL、headers 和 JSON body；不依赖外网或真实凭据。

为了兼容旧版 `codex-channel`，SGate 会继续读取原来的渠道文件、Keychain 服务名以及旧备份，不需要重新录入 API Key。

## 系统要求

- macOS
- Python 3.10 或更高版本
- ChatGPT.app 或 standalone Codex CLI
