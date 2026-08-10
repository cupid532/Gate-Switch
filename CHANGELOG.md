# Changelog

## 2.0.0 - 2026-08-10

- 同时支持官方 Anthropic API 与 Anthropic-compatible gateway root，严格拒绝 `/v1`、`/messages`、凭据、query 和 fragment。
- 保留 Claude `opus` / `sonnet` / `haiku` 到真实模型 ID 的独立映射，官方与第三方使用不同 beta 策略。
- 新增脱敏 `/v1/messages` 协议诊断、错误分类、JSON 输出和不读取密钥的 dry-run。
- 使用本地 mock HTTP server 断言 Messages URL、headers、body、成功与典型 400。
- 保持 Keychain helper、旧 `codex-channel` 前缀、渠道 JSON 迁移、原子写入与 fail-closed journal 恢复兼容。
- 新增 Python 3.10 版本门槛提示、包元数据和 Python 3.10-3.13/macOS CI。
