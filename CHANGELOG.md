# Changelog

## 2.0.4 - 2026-09-20

- 修复 Claude Code 上下文窗口后缀导致的误判：Claude Code 会在 `settings.json` 的 `/model` 字段追加 `[1m]`、`[200k]` 等上下文窗口后缀（如 `opus[1m]`），SGate 写入的是裸角色名（如 `opus`），此前 journal 对比视为外部修改并拒绝覆盖。现在对 `/model` 路径的冲突检测、停用恢复与脱离判定均忽略该后缀，消除误报。

## 2.0.3 - 2026-08-13

- 修复 Claude Code 切换失败：当 `settings.json` 被其他工具整体替换（SGate 的 applied 值无一生还，即使新配置复用了 `ANTHROPIC_BASE_URL` 等键名）时，下一次显式切换自动以当前文件重新建立恢复基线，而不是报“拒绝覆盖”卡死；部分外部修改仍 fail-closed。
- Claude Code 支持 `--disable-tool` 声明不兼容工具，通过 `permissions.deny` 精确合并，不覆盖用户已有 deny 项。
- Claude Code 接管 journal 增加 `permissions` 存在性校验与恢复；诊断请求使用 macOS 系统根证书的 TLS 上下文。

## 2.0.0 - 2026-08-10

- 同时支持官方 Anthropic API 与 Anthropic-compatible gateway root，严格拒绝 `/v1`、`/messages`、凭据、query 和 fragment。
- 保留 Claude `opus` / `sonnet` / `haiku` 到真实模型 ID 的独立映射，官方与第三方使用不同 beta 策略。
- 新增脱敏 `/v1/messages` 协议诊断、错误分类、JSON 输出和不读取密钥的 dry-run。
- 使用本地 mock HTTP server 断言 Messages URL、headers、body、成功与典型 400。
- 保持 Keychain helper、旧 `codex-channel` 前缀、渠道 JSON 迁移、原子写入与 fail-closed journal 恢复兼容。
- 新增 Python 3.10 版本门槛提示、包元数据和 Python 3.10-3.13/macOS CI。
