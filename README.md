# Gate Switch（`sgate`）

`sgate` 是一个面向 macOS 的 Codex / ChatGPT.app 渠道切换脚本。它可以保存多个 OpenAI Responses API 兼容渠道，自动拉取模型，并通过终端复选界面配置 ChatGPT.app 中可切换的模型与推理强度。

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

# 查看实际配置和已保存渠道
sgate status
sgate list

# 添加渠道并自动拉取模型
sgate add

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

ChatGPT.app 的 app-server 不会热加载 `model_catalog_json`。因此交互模式在最终确认后会直接重启 ChatGPT.app；命令行模式可添加 `--restart-app`。

为了兼容旧版 `codex-channel`，SGate 会继续读取原来的渠道文件、Keychain 服务名以及旧备份，不需要重新录入 API Key。

## 系统要求

- macOS
- Python 3.10 或更高版本
- ChatGPT.app 或 standalone Codex CLI
