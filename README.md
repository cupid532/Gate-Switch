# Gate Switch（`sgate`）

`sgate` 是一个面向 macOS 的 Codex / OpenCode / ChatGPT.app 渠道切换脚本。它可以保存多个 OpenAI 兼容渠道，自动拉取模型，并通过终端复选界面配置模型与推理强度。

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

## 主要功能

- API Key 只写入 macOS Keychain，不写入 `config.toml` 或命令行参数。
- 自动请求渠道的 `/models` 接口并缓存模型列表。
- 模型支持多选：在项目上按 `Space` 勾选，再按一次取消。
- 推理强度支持多选：`minimal`、`low`、`medium`、`high`、`xhigh`。
- 按 `Enter` 时，将光标所在模型或推理强度设为默认，同时保留其他已勾选候选。
- 自动生成独立模型目录，让 ChatGPT.app 的模型和推理强度控件显示多个候选。
- 应用交互配置后立即重启 ChatGPT.app，避免配置写入但运行中 App 未重新加载。
- 可“停用”渠道但保留渠道记录和 Keychain 密钥，下次可以直接重新启用。
- 每次修改前自动备份 Codex 配置。
- 启动后可先选择 `Codex` 或 `OpenCode`，两套配置互不覆盖。
- 交互菜单将“渠道管理”和“工具配置”分开：新增、删除、总览、连接检查在外层完成；进入 Codex/OpenCode 后再选择模型和推理强度。
- OpenCode 使用合法的 `provider`、`model` 和 `agent.build.variant` 配置，保留 `minimal`、`low`、`medium`、`high`、`xhigh` 思考强度。
- OpenCode API Key 仍以 macOS Keychain 为密钥源；运行 OpenCode 时写入每渠道独立、权限为 `0600` 的临时引用文件，并通过 `{file:...}` 引用，不写入 `opencode.json`。

## 交互按键

- `↑` / `↓` 或 `j` / `k`：移动光标
- `Space`：勾选或取消当前项目，可同时选择多个
- `Enter`：将光标项设为默认并继续
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
# └─ OpenCode：选择渠道、模型、推理强度并启用

# 直接打开外层渠道管理菜单
sgate channels

# 直接打开 OpenCode 菜单
sgate opencode

# 直接切换 OpenCode 渠道和思考强度
sgate opencode use fusiongate --model gpt-5.6-sol --reasoning xhigh

# 查看 OpenCode 当前实际配置
sgate opencode status

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
- OpenCode 运行时密钥：`~/.config/opencode/.sgate/<channel>-api-key`

ChatGPT.app 和 OpenCode 都不会让已经运行的会话热切换配置。切换后请重启对应工具；Codex 交互模式仍会按原逻辑处理 ChatGPT.app，OpenCode 切换完成后会明确提示重启。

为了兼容旧版 `codex-channel`，SGate 会继续读取原来的渠道文件、Keychain 服务名以及旧备份，不需要重新录入 API Key。

## 系统要求

- macOS
- Python 3.10 或更高版本
- ChatGPT.app 或 standalone Codex CLI
