#!/usr/bin/env python3
"""SGate: interactive global AI coding-tool channel switcher for macOS.

- API keys live in macOS Keychain, never in config.toml or shell arguments.
- config.toml keeps the active provider/model/reasoning settings used by both
  the standalone Codex CLI and the Codex binary bundled with ChatGPT.app.
- Claude Code uses ~/.claude/settings.json. Claude Desktop does not expose a
  custom model/API-provider setting, so SGate reports its supported surface
  instead of writing unsupported fields into its config.
"""
from __future__ import annotations

import argparse
import getpass
import json
import os
import re
import select
import shlex
import shutil
import sqlite3
import subprocess
import sys
import termios
import textwrap
import time
import tty
import unicodedata
try:
    import tomllib  # Python 3.11+
except ModuleNotFoundError:  # macOS systems that still ship Python 3.8/3.9
    tomllib = None
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

CODEX_HOME = Path(os.environ.get("CODEX_HOME", Path.home() / ".codex")).expanduser()
CONFIG_PATH = CODEX_HOME / "config.toml"
CHANNELS_PATH = CODEX_HOME / "codex-channels.json"
OPENCODE_CONFIG_PATH = Path(os.environ.get(
    "OPENCODE_CONFIG", Path.home() / ".config" / "opencode" / "opencode.json"
)).expanduser()
OPENCODE_CREDENTIALS_DIR = OPENCODE_CONFIG_PATH.parent / ".sgate"
CLAUDE_HOME = Path(os.environ.get("CLAUDE_HOME", Path.home() / ".claude")).expanduser()
CLAUDE_CODE_SETTINGS_PATH = Path(os.environ.get(
    "CLAUDE_CODE_SETTINGS", CLAUDE_HOME / "settings.json"
)).expanduser()
CLAUDE_DESKTOP_CONFIG_PATH = Path(os.environ.get(
    "CLAUDE_DESKTOP_CONFIG",
    Path.home() / "Library" / "Application Support" / "Claude" / "claude_desktop_config.json",
)).expanduser()
CLAUDE_BACKUP_DIR = CLAUDE_HOME / "sgate-backups"
CLAUDE_ORIGINAL_KEY_SLUG = "__sgate_claude_original__"
# Keep the legacy service name so upgrades retain existing Keychain secrets.
KEYCHAIN_PREFIX = "codex-channel"
SCRIPT_PATH = Path(__file__).resolve()
CHATGPT_CODEX = Path("/Applications/ChatGPT.app/Contents/Resources/codex")
CCSWITCH_DB = Path.home() / ".cc-switch" / "cc-switch.db"
DEFAULT_MODEL = "gpt-5.6"
DEFAULT_EFFORT = "high"
EFFORTS = ("minimal", "low", "medium", "high", "xhigh")
CLAUDE_EFFORTS = ("low", "medium", "high")
CLAUDE_ROLES = ("opus", "sonnet", "haiku")
CLAUDE_MANAGED_ENV = (
    "ANTHROPIC_BASE_URL",
    "ANTHROPIC_DEFAULT_OPUS_MODEL",
    "ANTHROPIC_DEFAULT_SONNET_MODEL",
    "ANTHROPIC_DEFAULT_HAIKU_MODEL",
)
CLAUDE_TAKEOVER_KEY = "claude_code"
# An identity-only sentinel cannot collide with a legitimate JSON value. New
# journal entries also persist an explicit ``exists`` bit for portability.
_MISSING = object()
_KEY_PUSHBACK: dict[int, list[bytes]] = {}

# ---------------------------------------------------------------------------
# Terminal styling
#
# Colors are disabled automatically when stdout is not a TTY, when TERM=dumb,
# or when NO_COLOR is set, so piped output and logs stay plain text.
# ---------------------------------------------------------------------------

ICON_ON = "●"
ICON_OFF = "○"
ICON_OK = "✓"
ICON_WARN = "!"
ICON_ERR = "✗"
ICON_ARROW = "→"


def color_enabled() -> bool:
    if os.environ.get("NO_COLOR"):
        return False
    if os.environ.get("SGATE_FORCE_COLOR"):
        return True
    return bool(sys.stdout.isatty()) and os.environ.get("TERM", "") != "dumb"


def paint(text: str, *codes: str) -> str:
    if not codes or not color_enabled():
        return text
    return f"\033[{';'.join(codes)}m{text}\033[0m"


def bold(text: str) -> str:
    return paint(text, "1")


def dim(text: str) -> str:
    return paint(text, "2")


def cyan(text: str) -> str:
    return paint(text, "36")


def green(text: str) -> str:
    return paint(text, "32")


def yellow(text: str) -> str:
    return paint(text, "33")


def red(text: str) -> str:
    return paint(text, "31")


def magenta(text: str) -> str:
    return paint(text, "35")


def display_width(text: str) -> int:
    """Visible width, ignoring ANSI codes and counting CJK glyphs as two cells."""
    plain = re.sub(r"\033\[[0-9;]*m", "", text)
    width = 0
    for char in plain:
        width += 2 if unicodedata.east_asian_width(char) in ("W", "F") else 1
    return width


def pad(text: str, width: int) -> str:
    return text + " " * max(0, width - display_width(text))


def rule(width: int = 0) -> str:
    if not width:
        width = min(shutil.get_terminal_size((88, 24)).columns, 88)
    return dim("─" * width)


def print_heading(title: str, subtitle: str = "") -> None:
    print()
    print(bold(cyan(title)))
    if subtitle:
        print(dim(f"  {subtitle}"))
    print(rule())


def print_field(label: str, value: Any, *, indent: int = 2, label_width: int = 14,
                tone: str = "") -> None:
    text = str(value)
    if tone == "ok":
        text = green(text)
    elif tone == "warn":
        text = yellow(text)
    elif tone == "err":
        text = red(text)
    elif tone == "accent":
        text = cyan(text)
    print(f"{' ' * indent}{dim(pad(label, label_width))}{text}")


def print_note(message: str, *, kind: str = "info", indent: int = 2) -> None:
    icons = {
        "info": dim(ICON_ARROW),
        "ok": green(ICON_OK),
        "warn": yellow(ICON_WARN),
        "err": red(ICON_ERR),
    }
    print(f"{' ' * indent}{icons.get(kind, icons['info'])} {message}")


def print_table(headers: list[str], rows: list[list[str]], *, indent: int = 2) -> None:
    if not rows:
        return
    widths = [display_width(h) for h in headers]
    for row in rows:
        for i, cell in enumerate(row):
            widths[i] = max(widths[i], display_width(cell))
    terminal_width = max(20, shutil.get_terminal_size((88, 24)).columns - indent)
    separator_width = 2 * (len(widths) - 1)
    while sum(widths) + separator_width > terminal_width and max(widths) > 8:
        index = max(range(len(widths)), key=widths.__getitem__)
        widths[index] -= 1

    def clip(value: str, width: int) -> str:
        if display_width(value) <= width:
            return value
        # Do not split ANSI escape sequences when a colored cell must be truncated.
        value = re.sub(r"\033\[[0-9;]*m", "", value)
        suffix = "…"
        out = ""
        for char in value:
            if display_width(out + char + suffix) > width:
                break
            out += char
        return out + suffix

    prefix = " " * indent
    print(prefix + "  ".join(dim(bold(pad(clip(h, widths[i]), widths[i]))) for i, h in enumerate(headers)).rstrip())
    print(prefix + dim("─" * (sum(widths) + separator_width)))
    for row in rows:
        print(prefix + "  ".join(pad(clip(cell, widths[i]), widths[i]) for i, cell in enumerate(row)).rstrip())


def die(message: str, code: int = 1) -> None:
    print(f"{red(ICON_ERR)} 错误：{message}", file=sys.stderr)
    raise SystemExit(code)


def now_stamp() -> str:
    return datetime.now().strftime("%Y%m%d-%H%M%S-%f")


def keychain_account() -> str:
    return os.environ.get("USER") or getpass.getuser()


def keychain_service(slug: str) -> str:
    return f"{KEYCHAIN_PREFIX}:{slug}"


def run_checked(argv: list[str], *, capture: bool = True) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(argv, text=True, capture_output=capture, check=True)
    except FileNotFoundError:
        die(f"找不到命令：{argv[0]}")
    except subprocess.CalledProcessError as exc:
        detail = (exc.stderr or exc.stdout or "").strip()
        die(detail or f"命令执行失败：{' '.join(argv)}")


def keychain_set(slug: str, value: str) -> None:
    if sys.platform != "darwin":
        die("此脚本使用 macOS Keychain，只支持 macOS。")
    run_checked([
        "security", "add-generic-password", "-a", keychain_account(),
        "-s", keychain_service(slug), "-w", value, "-U",
    ])


def keychain_get(slug: str) -> str:
    if sys.platform != "darwin":
        die("此脚本使用 macOS Keychain，只支持 macOS。")
    try:
        proc = subprocess.run(
            ["security", "find-generic-password", "-a", keychain_account(),
             "-s", keychain_service(slug), "-w"],
            text=True, capture_output=True, check=True,
        )
    except (FileNotFoundError, subprocess.CalledProcessError):
        die(f"Keychain 中没有渠道 {slug!r} 的 API Key，请先执行 add。")
    value = proc.stdout.strip()
    if not value:
        die(f"渠道 {slug!r} 的 API Key 为空。")
    return value


def keychain_delete(slug: str) -> None:
    if sys.platform != "darwin":
        return
    subprocess.run(
        ["security", "delete-generic-password", "-a", keychain_account(),
         "-s", keychain_service(slug)],
        text=True, capture_output=True,
    )


def slugify(value: str) -> str:
    value = re.sub(r"[^a-z0-9_-]+", "-", value.strip().lower())
    value = re.sub(r"-+", "-", value).strip("-_")
    if not value:
        die("渠道名称必须包含字母、数字、下划线或短横线。")
    if value[0].isdigit():
        value = f"c-{value}"
    return value[:48]


def terminal_ui_available() -> bool:
    return bool(sys.stdin.isatty() and sys.stdout.isatty() and os.environ.get("TERM", "") != "dumb")


def _read_key_byte(fd: int) -> bytes:
    pending = _KEY_PUSHBACK.get(fd)
    if pending:
        value = pending.pop(0)
        if not pending:
            _KEY_PUSHBACK.pop(fd, None)
        return value
    return os.read(fd, 1)


def _push_key_byte(fd: int, value: bytes) -> None:
    _KEY_PUSHBACK.setdefault(fd, []).insert(0, value)


def _prepare_picker_input(fd: int, *, quiet_for: float = 0.18, max_wait: float = 0.50) -> None:
    """Discard the key event that opened this screen, including delayed CR/LF tails.

    Netcatty can deliver the Return used on the previous screen after the next
    picker has already started. Waiting for a short *quiet window*, rather than
    sleeping once and flushing once, prevents a single Return from confirming
    several screens in succession.
    """
    _KEY_PUSHBACK.pop(fd, None)
    deadline = time.monotonic() + max_wait
    quiet_deadline = time.monotonic() + quiet_for
    while True:
        now = time.monotonic()
        if now >= deadline:
            break
        timeout = min(deadline - now, quiet_deadline - now)
        if timeout <= 0:
            break
        if not select.select([fd], [], [], timeout)[0]:
            break
        while select.select([fd], [], [], 0)[0]:
            os.read(fd, 1024)
        quiet_deadline = time.monotonic() + quiet_for
    termios.tcflush(fd, termios.TCIFLUSH)


def _consume_enter_tail(fd: int, first: bytes) -> None:
    """Consume CRLF/LFCR generated by one Return without eating the next key."""
    deadline = time.monotonic() + 0.10
    while time.monotonic() < deadline:
        timeout = deadline - time.monotonic()
        if not select.select([fd], [], [], timeout)[0]:
            return
        following = os.read(fd, 1)
        if following in (b"\r", b"\n"):
            # A terminal may emit more than one newline byte for a single key.
            continue
        _push_key_byte(fd, following)
        return


def _raw_key(fd: int) -> str:
    raw = _read_key_byte(fd)
    if not raw:
        return "eof"
    if raw in (b"\r", b"\n"):
        _consume_enter_tail(fd, raw)
        return "enter"
    if raw == b" ":
        return "space"
    if raw == b"\x03":
        raise KeyboardInterrupt
    if raw in (b"\x7f", b"\x08"):
        return "backspace"
    if raw == b"\x1b":
        # Read exactly one escape sequence. Do not greedily consume the next
        # Space/Enter when a user selects quickly after pressing an arrow key.
        if not select.select([fd], [], [], 0.04)[0]:
            return "escape"
        first = _read_key_byte(fd)
        if first not in (b"[", b"O"):
            _push_key_byte(fd, first)
            return "escape"
        if not select.select([fd], [], [], 0.04)[0]:
            return "escape"
        second = _read_key_byte(fd)
        tail = first + second
        if second.isdigit():
            while select.select([fd], [], [], 0.01)[0] and len(tail) < 6:
                char = _read_key_byte(fd)
                tail += char
                if char == b"~":
                    break
        mapping = {
            b"[A": "up", b"OA": "up", b"[B": "down", b"OB": "down",
            b"[C": "right", b"OC": "right", b"[D": "left", b"OD": "left",
            b"[5~": "pageup", b"[6~": "pagedown", b"[H": "home", b"[F": "end",
        }
        return mapping.get(tail, "escape")
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return ""


def _paint_picker(title: str, options: list[tuple[str, str]], cursor: int, selected: int | set[int] | None,
                  *, help_text: str, query: str = "", radio: bool = True) -> None:
    _, rows = shutil.get_terminal_size((100, 28))
    title_lines = title.splitlines() or [title]
    header_rows = len(title_lines) + (3 if query else 2) + 3
    page_size = max(5, rows - header_rows)
    start = max(0, min(cursor - page_size // 2, max(0, len(options) - page_size)))
    end = min(len(options), start + page_size)
    out = ["\033[2J\033[H"]
    for i, line in enumerate(title_lines):
        out.append(f"  {bold(cyan(line))}" if i == 0 else f"  {dim(line)}")
    out.append(f"  {rule()}")
    if query:
        out.extend((f"  {dim('搜索：')}{yellow(query)}", ""))
    if start > 0:
        out.append(dim("  ↑ 还有更多"))
    if not options:
        out.append(dim("    没有匹配项，请继续输入或按 Esc 清空搜索"))
    for index in range(start, end):
        _, label = options[index]
        is_cursor = index == cursor
        if radio:
            is_selected = index in selected if isinstance(selected, set) else index == selected
            pointer = cyan("❯") if is_cursor else " "
            marker = green(f"[{ICON_OK}]") if is_selected else dim("[ ]")
            text = bold(label) if is_cursor else label
            line = f"  {pointer} {marker} {text}"
        else:
            line = f"    {label}"
            if is_cursor:
                line = f"  {cyan('❯')} {bold(label)}"
        out.append(line)
    if end < len(options):
        out.append(dim("  ↓ 还有更多"))
    out.extend(("", f"  {dim(help_text)}"))
    # setraw() disables terminal newline translation on several terminal apps.
    # Always emit CRLF explicitly so every menu row starts in column zero.
    sys.stdout.write("\r\n".join(out))
    sys.stdout.flush()


def terminal_radio(title: str, options: list[tuple[str, str]], *, default: str | None = None,
                   searchable: bool = False) -> str | None:
    """Arrow-key radio picker. Space toggles, Enter confirms, Esc cancels."""
    if not options:
        return None
    if not terminal_ui_available():
        for i, (_, label) in enumerate(options, 1):
            print(f"{i}) {label}")
        raw = input("输入序号（回车取消）：").strip()
        return options[int(raw) - 1][0] if raw.isdigit() and 1 <= int(raw) <= len(options) else None

    all_options = options
    filtered = options
    selected_value = default if any(value == default for value, _ in options) else None
    cursor = next((i for i, (value, _) in enumerate(filtered) if value == selected_value), 0)
    query = ""
    searching = False
    fd = sys.stdin.fileno()
    old = termios.tcgetattr(fd)
    sys.stdout.write("\033[?1049h\033[?25l")
    sys.stdout.flush()
    try:
        tty.setcbreak(fd)
        _prepare_picker_input(fd)
        while True:
            selected = next((i for i, (value, _) in enumerate(filtered) if value == selected_value), None)
            help_text = (
                "输入关键词筛选 · Backspace 删除 · Enter 完成筛选 · Esc 清空"
                if searching else
                "↑↓ 移动 · Space 选择/取消 · Enter 确认 · / 搜索 · Esc 取消"
            )
            _paint_picker(title, filtered, cursor, selected, help_text=help_text, query=query)
            key = _raw_key(fd)
            if searching:
                if key == "enter":
                    searching = False
                elif key == "escape":
                    query = ""
                    filtered = all_options
                    cursor = 0
                    searching = False
                elif key == "backspace":
                    query = query[:-1]
                elif len(key) == 1 and key.isprintable():
                    query += key
                filtered = [item for item in all_options if query.casefold() in item[1].casefold()]
                if not filtered:
                    filtered = all_options
                    query = ""
                cursor = min(cursor, len(filtered) - 1)
                continue
            if key in ("up", "k"):
                cursor = (cursor - 1) % len(filtered)
            elif key in ("down", "j"):
                cursor = (cursor + 1) % len(filtered)
            elif key == "pageup":
                cursor = max(0, cursor - 10)
            elif key == "pagedown":
                cursor = min(len(filtered) - 1, cursor + 10)
            elif key == "home":
                cursor = 0
            elif key == "end":
                cursor = len(filtered) - 1
            elif key == "space":
                value = filtered[cursor][0]
                selected_value = None if selected_value == value else value
            elif key == "enter":
                return selected_value
            elif key == "/" and searchable:
                query = ""
                searching = True
            elif key in ("escape", "q", "eof"):
                return None
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old)
        sys.stdout.write("\033[0m\033[?25h\033[?1049l")
        sys.stdout.flush()


def terminal_multi(
    title: str,
    options: list[tuple[str, str]],
    *,
    defaults: list[str] | tuple[str, ...] | set[str] | None = None,
    default_value: str | None = None,
    searchable: bool = False,
) -> tuple[list[str], str] | None:
    """Checkbox picker. Space selects, ``d`` sets the default, Enter confirms."""
    if not options:
        return None
    allowed = {value for value, _ in options}
    selected_values = {value for value in (defaults or []) if value in allowed}
    if default_value in allowed:
        selected_values.add(str(default_value))

    if not terminal_ui_available():
        for i, (value, label) in enumerate(options, 1):
            marker = "x" if value in selected_values else " "
            print(f"{i}) [{marker}] {label}")
        raw = input("输入多个序号（逗号分隔，回车保留当前）：").strip()
        if raw:
            indexes = []
            for part in re.split(r"[,，\s]+", raw):
                if part.isdigit() and 1 <= int(part) <= len(options):
                    indexes.append(int(part) - 1)
            selected_values = {options[index][0] for index in indexes}
        if not selected_values:
            return None
        default = default_value if default_value in selected_values else next(
            (value for value, _ in options if value in selected_values), None
        )
        return ([value for value, _ in options if value in selected_values], str(default))

    all_options = options
    filtered = options
    current_default = default_value if default_value in allowed else next(iter(selected_values), None)
    cursor = next((i for i, (value, _) in enumerate(filtered) if value == current_default), 0)
    query = ""
    searching = False
    fd = sys.stdin.fileno()
    old = termios.tcgetattr(fd)
    sys.stdout.write("\033[?1049h\033[?25l")
    sys.stdout.flush()
    try:
        tty.setcbreak(fd)
        _prepare_picker_input(fd)
        while True:
            selected_indexes = {i for i, (value, _) in enumerate(filtered) if value in selected_values}
            help_text = (
                "输入关键词筛选 · Backspace 删除 · Enter 完成筛选 · Esc 清空"
                if searching else
                "↑↓ 移动 · Space 勾选/取消 · d 设为默认 · Enter 确认 · / 搜索 · Esc 取消"
            )
            _paint_picker(title, filtered, cursor, selected_indexes, help_text=help_text, query=query)
            key = _raw_key(fd)
            if searching:
                if key == "enter":
                    searching = False
                elif key == "escape":
                    query = ""
                    filtered = all_options
                    cursor = 0
                    searching = False
                elif key == "backspace":
                    query = query[:-1]
                elif len(key) == 1 and key.isprintable():
                    query += key
                filtered = [item for item in all_options if query.casefold() in item[1].casefold()]
                if not filtered:
                    filtered = all_options
                    query = ""
                cursor = min(cursor, len(filtered) - 1)
                continue
            if key in ("up", "k"):
                cursor = (cursor - 1) % len(filtered)
            elif key in ("down", "j"):
                cursor = (cursor + 1) % len(filtered)
            elif key == "pageup":
                cursor = max(0, cursor - 10)
            elif key == "pagedown":
                cursor = min(len(filtered) - 1, cursor + 10)
            elif key == "home":
                cursor = 0
            elif key == "end":
                cursor = len(filtered) - 1
            elif key == "space":
                value = filtered[cursor][0]
                if value in selected_values:
                    selected_values.remove(value)
                else:
                    selected_values.add(value)
            elif key == "d":
                current_default = filtered[cursor][0]
                selected_values.add(current_default)
            elif key == "enter":
                selected = [value for value, _ in all_options if value in selected_values]
                if not selected:
                    return None
                default = current_default if current_default in selected_values else selected[0]
                return selected, default
            elif key == "/" and searchable:
                query = ""
                searching = True
            elif key in ("escape", "q", "eof"):
                return None
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old)
        sys.stdout.write("\033[0m\033[?25h\033[?1049l")
        sys.stdout.flush()


def terminal_menu(title: str, options: list[tuple[str, str]], *, default: str | None = None) -> str | None:
    """Menu variant: moving the cursor and pressing Enter immediately chooses an action."""
    if not options:
        return None
    if not terminal_ui_available():
        for i, (_, label) in enumerate(options, 1):
            print(f"{i}) {label}")
        raw = input("请选择（回车取消）：").strip()
        return options[int(raw) - 1][0] if raw.isdigit() and 1 <= int(raw) <= len(options) else None
    cursor = next((i for i, (value, _) in enumerate(options) if value == default), 0)
    fd = sys.stdin.fileno()
    old = termios.tcgetattr(fd)
    sys.stdout.write("\033[?1049h\033[?25l")
    sys.stdout.flush()
    try:
        tty.setcbreak(fd)
        _prepare_picker_input(fd)
        while True:
            _paint_picker(title, options, cursor, None, help_text="↑↓ 移动 · Enter 确认 · Esc 返回", radio=False)
            key = _raw_key(fd)
            if key in ("up", "k"):
                cursor = (cursor - 1) % len(options)
            elif key in ("down", "j"):
                cursor = (cursor + 1) % len(options)
            elif key == "home":
                cursor = 0
            elif key == "end":
                cursor = len(options) - 1
            elif key == "enter":
                return options[cursor][0]
            elif key in ("escape", "q", "eof"):
                return None
            elif key.isdigit() and key != "0":
                index = int(key) - 1
                if 0 <= index < len(options):
                    return options[index][0]
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old)
        sys.stdout.write("\033[0m\033[?25h\033[?1049l")
        sys.stdout.flush()


def confirm_action(message: str, *, default: bool = False) -> bool:
    if terminal_ui_available():
        picked = terminal_menu(message, [("no", "取消"), ("yes", "确定")], default="yes" if default else "no")
        return picked == "yes"
    suffix = "[Y/n]" if default else "[y/N]"
    answer = input(f"{message} {suffix} ").strip().lower()
    return default if not answer else answer in ("y", "yes")


def pause_after_action() -> None:
    if terminal_ui_available():
        try:
            input("\n按 Enter 返回菜单……")
        except (KeyboardInterrupt, EOFError):
            pass


def migrate_legacy_claude_profiles(data: dict[str, Any]) -> bool:
    """Mark legacy Claude selections as ambiguous without inventing endpoints/maps."""
    changed = False
    channels = data.get("channels", {})
    if not isinstance(channels, dict):
        return False
    legacy_names = (
        "claude_model", "claude_selected_models", "claude_selected",
        "claude_reasoning_effort", "claude_selected_efforts",
    )
    for channel in channels.values():
        if not isinstance(channel, dict):
            continue
        protocols = channel.get("protocols")
        protocols = protocols if isinstance(protocols, dict) else {}
        anthropic = protocols.get("anthropic")
        if ((isinstance(anthropic, dict) and anthropic.get("base_url")) or channel.get("claude_base_url")):
            continue
        hint = {name: channel[name] for name in legacy_names if name in channel}
        if "claude_model" in hint and "selected" in channel:
            hint["selected"] = channel["selected"]
        if not hint:
            continue
        anthropic = dict(anthropic) if isinstance(anthropic, dict) else {}
        if anthropic.get("migration_status") != "needs_configuration" or anthropic.get("legacy_hint") != hint:
            anthropic.update({
                "migration_status": "needs_configuration",
                "legacy_hint": hint,
            })
            protocols["anthropic"] = anthropic
            channel["protocols"] = protocols
            changed = True
    return changed


def load_channels() -> dict[str, Any]:
    if not CHANNELS_PATH.exists():
        return {"version": 1, "active": None, "channels": {}}
    try:
        data = json.loads(CHANNELS_PATH.read_text(encoding="utf-8"))
    except Exception as exc:
        die(f"无法读取 {CHANNELS_PATH}：{exc}")
    if not isinstance(data, dict) or not isinstance(data.get("channels", {}), dict):
        die(f"渠道文件格式损坏：{CHANNELS_PATH}")
    # Keep migration in memory until the next intentional mutation so opening or
    # cancelling an interactive picker never writes channels.json by itself.
    migrate_legacy_claude_profiles(data)
    return data


def save_channels(data: dict[str, Any]) -> None:
    data["version"] = max(2, int(data.get("version", 1) or 1))
    CODEX_HOME.mkdir(parents=True, exist_ok=True)
    tmp = CHANNELS_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.chmod(tmp, 0o600)
    os.replace(tmp, CHANNELS_PATH)
    os.chmod(CHANNELS_PATH, 0o600)


def toml_string(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


def read_config() -> str:
    if not CONFIG_PATH.exists():
        die(f"找不到 Codex 配置文件：{CONFIG_PATH}")
    return CONFIG_PATH.read_text(encoding="utf-8")


def _fallback_toml_string(value: str) -> str:
    value = value.strip()
    if value.startswith('"') and value.endswith('"'):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return value[1:-1]
    return value


def parse_config() -> dict[str, Any]:
    content = read_config()
    if tomllib is not None:
        try:
            return tomllib.loads(content)
        except Exception as exc:
            die(f"config.toml 解析失败：{exc}")

    # Minimal read-only fallback for Python 3.8/3.9. The script only needs
    # the active provider, model, reasoning, and provider name/base_url.
    result: dict[str, Any] = {}
    for key in ("model_provider", "model", "model_reasoning_effort", "model_catalog_json"):
        match = re.search(rf"(?m)^{re.escape(key)}\s*=\s*(.+)$", content)
        if match:
            result[key] = _fallback_toml_string(match.group(1))
    providers: dict[str, dict[str, Any]] = {}
    table_re = re.compile(r"(?ms)^\[model_providers\.([A-Za-z0-9_-]+)\]\s*$\n(.*?)(?=^\[|\Z)")
    for match in table_re.finditer(content):
        provider: dict[str, Any] = {}
        for key in ("name", "base_url", "wire_api"):
            value_match = re.search(rf"(?m)^{re.escape(key)}\s*=\s*(.+)$", match.group(2))
            if value_match:
                provider[key] = _fallback_toml_string(value_match.group(1))
        providers[match.group(1)] = provider
    result["model_providers"] = providers
    return result


def current_config_info() -> dict[str, Any]:
    cfg = parse_config()
    provider_id = str(cfg.get("model_provider", "openai"))
    providers = cfg.get("model_providers", {})
    provider = providers.get(provider_id, {}) if isinstance(providers, dict) else {}
    return {
        "provider_id": provider_id,
        "provider": provider if isinstance(provider, dict) else {},
        "model": str(cfg.get("model", "(未设置)")),
        "reasoning_effort": str(cfg.get("model_reasoning_effort", "(未设置)")),
        "model_catalog_json": str(cfg.get("model_catalog_json", "(未设置)")),
    }


def ccswitch_current_info() -> dict[str, Any] | None:
    """Read only CC Switch's current Codex profile, without reading its API key."""
    if not CCSWITCH_DB.exists():
        return None
    try:
        db = sqlite3.connect(f"file:{CCSWITCH_DB}?mode=ro", uri=True, timeout=1)
        row = db.execute(
            "select name, settings_config from providers "
            "where app_type='codex' and is_current=1 limit 1"
        ).fetchone()
        db.close()
    except (sqlite3.Error, OSError, ValueError):
        return None
    if not row:
        return None
    name, raw_settings = row
    try:
        settings = json.loads(raw_settings or "{}")
    except json.JSONDecodeError:
        settings = {}
    content = settings.get("config", "") if isinstance(settings, dict) else ""
    if not isinstance(content, str):
        content = ""

    def assignment(key: str, default: str = "(未设置)") -> str:
        match = re.search(rf"(?m)^{re.escape(key)}\s*=\s*(.+)$", content)
        return _fallback_toml_string(match.group(1)) if match else default

    base_match = re.search(r"(?m)^base_url\s*=\s*(.+)$", content)
    return {
        "name": str(name),
        "provider_id": assignment("model_provider"),
        "model": assignment("model"),
        "base_url": _fallback_toml_string(base_match.group(1)) if base_match else "(未设置)",
    }


def update_top_level(content: str, values: dict[str, str]) -> str:
    """Update assignments before the first TOML table and preserve everything else."""
    lines = content.splitlines(keepends=True)
    first_table = next((i for i, line in enumerate(lines) if re.match(r"^\s*\[", line)), len(lines))
    prefix, suffix = lines[:first_table], lines[first_table:]
    found: set[str] = set()
    for i, line in enumerate(prefix):
        for key, value in values.items():
            if re.match(rf"^\s*{re.escape(key)}\s*=", line):
                ending = "\n" if line.endswith("\n") else ""
                prefix[i] = f"{key} = {toml_string(value)}{ending}"
                found.add(key)
                break
    missing = [f"{key} = {toml_string(value)}\n" for key, value in values.items() if key not in found]
    if missing:
        if prefix and not prefix[-1].endswith("\n"):
            prefix[-1] += "\n"
        prefix.extend(missing)
    return "".join(prefix + suffix)


def provider_id(slug: str) -> str:
    return f"codex_channel_{slug.replace('-', '_')}"


def provider_block(channel: dict[str, Any]) -> str:
    slug = channel["slug"]
    pid = provider_id(slug)
    return textwrap.dedent(f"""\
        # >>> codex-channel managed provider: {slug}
        [model_providers.{pid}]
        name = {toml_string(channel['name'])}
        base_url = {toml_string(channel['base_url'])}
        wire_api = "responses"
        request_max_retries = 4
        stream_max_retries = 5

        [model_providers.{pid}.auth]
        command = {toml_string(str(SCRIPT_PATH))}
        args = ["token", {toml_string(slug)}]
        timeout_ms = 5000
        # <<< codex-channel managed provider: {slug}
        """)


def update_provider_block(content: str, channel: dict[str, Any]) -> str:
    slug = channel["slug"]
    block = provider_block(channel).rstrip() + "\n"
    marker = re.compile(
        rf"(?ms)^# >>> codex-channel managed provider: {re.escape(slug)}\n.*?^# <<< codex-channel managed provider: {re.escape(slug)}\n?"
    )
    if marker.search(content):
        return marker.sub(block, content, count=1)
    if content and not content.endswith("\n"):
        content += "\n"
    return content + "\n" + block


def backup_config() -> Path | None:
    if not CONFIG_PATH.exists():
        return None
    backup = CONFIG_PATH.with_name(f"config.toml.sgate-{now_stamp()}.bak")
    shutil.copy2(CONFIG_PATH, backup)
    return backup


def write_config(content: str) -> None:
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = CONFIG_PATH.with_suffix(".tmp")
    tmp.write_text(content, encoding="utf-8")
    os.chmod(tmp, 0o600)
    os.replace(tmp, CONFIG_PATH)
    os.chmod(CONFIG_PATH, 0o600)


def opencode_provider_id(slug: str) -> str:
    return f"sgate_{slug.replace('-', '_')}"


def opencode_read_config(*, strict: bool = True) -> dict[str, Any]:
    if not OPENCODE_CONFIG_PATH.exists():
        return {"$schema": "https://opencode.ai/config.json"}
    try:
        value = json.loads(OPENCODE_CONFIG_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        if strict:
            die(f"无法读取 OpenCode 配置 {OPENCODE_CONFIG_PATH}：{exc}")
        return {"$schema": "https://opencode.ai/config.json"}
    if not isinstance(value, dict):
        if strict:
            die(f"OpenCode 配置必须是 JSON 对象：{OPENCODE_CONFIG_PATH}")
        return {"$schema": "https://opencode.ai/config.json"}
    return value


def opencode_backup_config() -> Path | None:
    if not OPENCODE_CONFIG_PATH.exists():
        return None
    backup = OPENCODE_CONFIG_PATH.with_name(f"opencode.json.sgate-{now_stamp()}.bak")
    shutil.copy2(OPENCODE_CONFIG_PATH, backup)
    return backup


def opencode_write_config(config: dict[str, Any]) -> None:
    OPENCODE_CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = OPENCODE_CONFIG_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(config, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.chmod(tmp, 0o600)
    os.replace(tmp, OPENCODE_CONFIG_PATH)
    os.chmod(OPENCODE_CONFIG_PATH, 0o600)


def opencode_config_is_valid() -> bool:
    if not OPENCODE_CONFIG_PATH.exists():
        return True
    try:
        value = json.loads(OPENCODE_CONFIG_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    return isinstance(value, dict)


def opencode_credentials_path(slug: str) -> Path:
    return OPENCODE_CREDENTIALS_DIR / f"{slug}-api-key"


def claude_settings_read() -> dict[str, Any]:
    if not CLAUDE_CODE_SETTINGS_PATH.exists():
        return {}
    try:
        value = json.loads(CLAUDE_CODE_SETTINGS_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        die(f"无法读取 Claude Code 配置 {CLAUDE_CODE_SETTINGS_PATH}：{exc}")
    if not isinstance(value, dict):
        die(f"Claude Code 配置必须是 JSON 对象：{CLAUDE_CODE_SETTINGS_PATH}")
    return value


def validate_claude_settings_shape(settings: dict[str, Any]) -> None:
    """Fail closed before any backup, Keychain, journal, or settings write."""
    if "env" in settings and not isinstance(settings.get("env"), dict):
        die("Claude Code settings.json 的 env 必须是 JSON 对象；已停止，未修改任何配置。")


def _safe_claude_settings_snapshot(settings: dict[str, Any]) -> dict[str, Any]:
    """Return a value-free manifest; never duplicate settings or commands."""
    env = settings.get("env") if isinstance(settings, dict) else None
    return {
        "_sgate_backup_manifest": True,
        "keys": sorted(str(key) for key in settings) if isinstance(settings, dict) else [],
        "env_keys": sorted(str(key) for key in env) if isinstance(env, dict) else [],
        "created_at": datetime.now(timezone.utc).isoformat(),
    }


def claude_settings_backup(settings: dict[str, Any] | None = None) -> Path | None:
    if not CLAUDE_CODE_SETTINGS_PATH.exists():
        return None
    CLAUDE_BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    backup = CLAUDE_BACKUP_DIR / f"settings.json.sgate-{now_stamp()}.bak"
    safe = _safe_claude_settings_snapshot(settings if settings is not None else claude_settings_read())
    backup.write_text(json.dumps(safe, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.chmod(backup, 0o600)
    return backup


def claude_settings_write(settings: dict[str, Any]) -> None:
    """Atomically replace settings after a single caller-owned read."""
    CLAUDE_CODE_SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = CLAUDE_CODE_SETTINGS_PATH.with_name(
        f".{CLAUDE_CODE_SETTINGS_PATH.name}.sgate-{os.getpid()}-{now_stamp()}.tmp"
    )
    try:
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(settings, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, CLAUDE_CODE_SETTINGS_PATH)
        os.chmod(CLAUDE_CODE_SETTINGS_PATH, 0o600)
    finally:
        try:
            tmp.unlink()
        except FileNotFoundError:
            pass


def claude_env(settings: dict[str, Any]) -> dict[str, Any]:
    """Return settings.env after validating its JSON shape.

    Claude settings permits an object here. Silently coercing a string/list to
    an empty object would destroy user configuration, so callers must fail
    closed when a non-object env is present.
    """
    if "env" not in settings:
        return {}
    value = settings.get("env")
    if not isinstance(value, dict):
        die("Claude Code settings.json 的 env 必须是 JSON 对象；已停止，未修改配置。")
    return json.loads(json.dumps(value, ensure_ascii=False))


def _claude_helper_slug(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    try:
        argv = shlex.split(value)
    except ValueError:
        return None
    if len(argv) != 3 or argv[1] != "claude-token":
        return None
    try:
        owned = Path(argv[0]).expanduser().resolve() == SCRIPT_PATH.resolve()
    except OSError:
        owned = False
    return argv[2] if owned else None


def _is_sgate_claude_helper(value: Any) -> bool:
    return _claude_helper_slug(value) is not None


def _is_sgate_channel_helper(value: Any) -> bool:
    slug = _claude_helper_slug(value)
    return bool(slug and slug != CLAUDE_ORIGINAL_KEY_SLUG)


def _pointer_parts(path: str) -> list[str]:
    if not path.startswith("/"):
        raise ValueError(f"invalid JSON pointer: {path}")
    return [part.replace("~1", "/").replace("~0", "~") for part in path[1:].split("/")]


def _pointer_get(document: dict[str, Any], path: str) -> Any:
    value: Any = document
    for part in _pointer_parts(path):
        if not isinstance(value, dict) or part not in value:
            return _MISSING
        value = value[part]
    return json.loads(json.dumps(value, ensure_ascii=False))


def _pointer_set(document: dict[str, Any], path: str, value: Any) -> None:
    parts = _pointer_parts(path)
    target: dict[str, Any] = document
    for part in parts[:-1]:
        if part not in target:
            target[part] = {}
        elif not isinstance(target[part], dict):
            raise ValueError(f"cannot write JSON pointer through non-object ancestor: {path}")
        target = target[part]
    target[parts[-1]] = json.loads(json.dumps(value, ensure_ascii=False))


def _pointer_delete(document: dict[str, Any], path: str) -> None:
    parts = _pointer_parts(path)
    target: Any = document
    for part in parts[:-1]:
        if not isinstance(target, dict) or part not in target:
            return
        target = target[part]
    if isinstance(target, dict):
        target.pop(parts[-1], None)


def _is_missing(value: Any) -> bool:
    return value is _MISSING


class ClaudeManagedPlan:
    __slots__ = ("desired", "managed_paths", "diagnostics", "supported", "pointer_values")

    def __init__(
        self, desired: dict[str, Any], managed_paths: tuple[str, ...],
        diagnostics: tuple[str, ...], supported: bool,
        pointer_values: dict[str, Any] | None = None,
    ):
        self.desired = desired
        self.managed_paths = managed_paths
        self.diagnostics = diagnostics
        self.supported = supported
        self.pointer_values = pointer_values or {}

    @property
    def desired_pointers(self) -> dict[str, Any]:
        return dict(self.pointer_values)

    def __getitem__(self, key: str) -> Any:
        return getattr(self, key)

    def get(self, key: str, default: Any = None) -> Any:
        return getattr(self, key, default)

    def __repr__(self) -> str:
        return (
            f"ClaudeManagedPlan(desired={self.desired!r}, managed_paths={self.managed_paths!r}, "
            f"diagnostics={self.diagnostics!r}, supported={self.supported!r})"
        )


def _anthropic_profile_sections(profile: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    protocols = profile.get("protocols")
    protocols = protocols if isinstance(protocols, dict) else {}
    anthropic = protocols.get("anthropic")
    anthropic = dict(anthropic) if isinstance(anthropic, dict) else {}
    if not anthropic:
        flat_map = profile.get("claude_model_map")
        anthropic = {
            "base_url": profile.get("anthropic_base_url") or profile.get("claude_base_url"),
            "models": (
                profile.get("anthropic_models") if isinstance(profile.get("anthropic_models"), list)
                else profile.get("claude_models") if isinstance(profile.get("claude_models"), list)
                else None
            ),
            "models_source": profile.get("anthropic_models_source") or profile.get("claude_models_source"),
            "auth": profile.get("anthropic_auth") if isinstance(profile.get("anthropic_auth"), dict) else {
                "mode": profile.get("auth_mode") or profile.get("claude_auth_mode"),
                "secret_ref": profile.get("auth_secret_ref") or profile.get("claude_secret_ref"),
            },
        }
    runtimes = profile.get("runtimes")
    runtimes = runtimes if isinstance(runtimes, dict) else {}
    runtime = runtimes.get("claude_code")
    runtime = dict(runtime) if isinstance(runtime, dict) else {}
    if not runtime:
        flat_map = profile.get("claude_model_map")
        runtime = {
            "default_role": profile.get("default_role") or profile.get("claude_default_role"),
            "effort": profile.get("effort") or profile.get("claude_effort") or profile.get("claude_reasoning_effort"),
            "model_map": (
                profile.get("model_map") if isinstance(profile.get("model_map"), dict)
                else flat_map if isinstance(flat_map, dict) else None
            ),
        }
    # A catalog list never implies a role mapping. Every Claude alias must be
    # selected explicitly (or through --map-all at the input boundary).
    return anthropic, runtime


def compile_claude_managed_values(
    profile: dict[str, Any], capabilities: dict[str, Any] | None = None,
) -> ClaudeManagedPlan:
    """Compile the exact Claude Code settings managed by SGate, failing closed."""
    if not isinstance(profile, dict):
        return ClaudeManagedPlan({}, (), ("profile must be an object",), False)
    anthropic, runtime = _anthropic_profile_sections(profile)
    diagnostics: list[str] = []
    base_url = str(anthropic.get("base_url") or "").strip().rstrip("/")
    if not re.match(r"^https?://", base_url, re.I):
        diagnostics.append("Anthropic Base URL is required and must start with http:// or https://")
    model_map = runtime.get("model_map")
    if not isinstance(model_map, dict):
        model_map = {}
    normalized_map = {role: str(model_map.get(role) or "").strip() for role in CLAUDE_ROLES}
    missing = [role for role, model in normalized_map.items() if not model]
    extra = [str(role) for role in model_map if role not in CLAUDE_ROLES]
    if missing:
        diagnostics.append(f"explicit model mapping required for: {', '.join(missing)}")
    if extra:
        diagnostics.append(f"unsupported Claude aliases: {', '.join(extra)}")
    default_role = str(runtime.get("default_role") or "").strip().casefold()
    if default_role not in CLAUDE_ROLES:
        diagnostics.append("default_role must be one of: opus, sonnet, haiku")
    effort = str(runtime.get("effort") or "").strip().casefold()
    if effort not in CLAUDE_EFFORTS:
        diagnostics.append("effort must be one of: low, medium, high")
    auth = anthropic.get("auth")
    auth = auth if isinstance(auth, dict) else {}
    auth_mode = str(auth.get("mode") or "api_key_helper").strip()
    if auth_mode != "api_key_helper":
        diagnostics.append(
            f"auth mode {auth_mode!r} is unsupported for persistent Claude settings; use api_key_helper"
        )
    secret_ref = str(auth.get("secret_ref") or profile.get("slug") or "").strip()
    if not secret_ref:
        diagnostics.append("auth.secret_ref or profile.slug is required")
    if diagnostics:
        return ClaudeManagedPlan({}, (), tuple(diagnostics), False)
    helper = f"{shlex.quote(str(SCRIPT_PATH))} claude-token {shlex.quote(secret_ref)}"
    pointer_values = {
        "/model": default_role,
        "/effortLevel": effort,
        "/apiKeyHelper": helper,
        "/env/ANTHROPIC_BASE_URL": base_url,
        "/env/ANTHROPIC_DEFAULT_OPUS_MODEL": normalized_map["opus"],
        "/env/ANTHROPIC_DEFAULT_SONNET_MODEL": normalized_map["sonnet"],
        "/env/ANTHROPIC_DEFAULT_HAIKU_MODEL": normalized_map["haiku"],
    }
    # Keep the public result convenient for callers while applications use exact pointers.
    desired = {
        "model": default_role,
        "effortLevel": effort,
        "apiKeyHelper": helper,
        "env": {
            "ANTHROPIC_BASE_URL": base_url,
            "ANTHROPIC_DEFAULT_OPUS_MODEL": normalized_map["opus"],
            "ANTHROPIC_DEFAULT_SONNET_MODEL": normalized_map["sonnet"],
            "ANTHROPIC_DEFAULT_HAIKU_MODEL": normalized_map["haiku"],
        },
    }
    caps = capabilities if isinstance(capabilities, dict) else {}
    # Keep pointer aliases for callers/tests while the actual settings patch uses
    # the structured top-level representation above.
    desired.update(pointer_values)
    return ClaudeManagedPlan(desired, tuple(pointer_values), (), True, pointer_values)


def _claude_profile(
    channel: dict[str, Any], *, anthropic_base_url: str | None = None,
    model_map: dict[str, str] | None = None, default_role: str | None = None,
    effort: str | None = None, auth_mode: str | None = None,
) -> dict[str, Any]:
    profile = json.loads(json.dumps(channel, ensure_ascii=False))
    anthropic, runtime = _anthropic_profile_sections(profile)
    if anthropic_base_url is not None:
        anthropic["base_url"] = anthropic_base_url
    if model_map is not None:
        # The Anthropic catalog is a list; the runtime role mapping is the
        # separate authoritative dictionary.
        anthropic["models"] = list(dict.fromkeys(str(value) for value in model_map.values()))
        runtime["model_map"] = dict(model_map)
    if default_role is not None:
        runtime["default_role"] = default_role
    if effort is not None:
        runtime["effort"] = effort
    auth = anthropic.get("auth")
    auth = dict(auth) if isinstance(auth, dict) else {}
    auth["mode"] = auth_mode or auth.get("mode") or "api_key_helper"
    auth["secret_ref"] = channel.get("slug")
    anthropic["auth"] = auth
    protocols = profile.setdefault("protocols", {})
    protocols["anthropic"] = anthropic
    runtimes = profile.setdefault("runtimes", {})
    runtimes["claude_code"] = runtime
    return profile


def _secret_before_reference(path: str, before: Any) -> Any:
    if path not in ("/env/ANTHROPIC_API_KEY", "/env/ANTHROPIC_AUTH_TOKEN") or _is_missing(before):
        return before
    restore_slug = (
        CLAUDE_ORIGINAL_KEY_SLUG
        if path.endswith("ANTHROPIC_AUTH_TOKEN")
        else f"{CLAUDE_ORIGINAL_KEY_SLUG}-api-key"
    )
    keychain_set(restore_slug, str(before))
    return {"keychain_restore_ref": restore_slug}


def _journal_value(value: Any) -> Any:
    if _is_missing(value):
        return {"__sgate_journal_missing__": True}
    return json.loads(json.dumps(value, ensure_ascii=False))


def _journal_decode(value: Any) -> Any:
    if isinstance(value, dict) and value.get("__sgate_journal_missing__") is True:
        return _MISSING
    return value


def _journal_before_value(entry: dict[str, Any]) -> Any:
    before = _journal_decode(entry.get("before", _MISSING))
    if isinstance(before, dict) and before.get("keychain_restore_ref"):
        slug = str(before["keychain_restore_ref"])
        return keychain_get(slug)
    return before


def _apply_claude_plan(
    settings: dict[str, Any], data: dict[str, Any], plan: ClaudeManagedPlan,
) -> dict[str, Any]:
    state = data.setdefault("runtime_state", {})
    claude_state = state.setdefault(CLAUDE_TAKEOVER_KEY, {})
    takeover = claude_state.get("takeover")
    if not isinstance(takeover, dict):
        takeover = {
            "target_path": str(CLAUDE_CODE_SETTINGS_PATH),
            "file_existed": CLAUDE_CODE_SETTINGS_PATH.exists(),
            "env_existed": isinstance(settings.get("env"), dict),
            "original_mode": (
                CLAUDE_CODE_SETTINGS_PATH.stat().st_mode & 0o777
                if CLAUDE_CODE_SETTINGS_PATH.exists() else None
            ),
            "entries": [],
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        claude_state["takeover"] = takeover
    entries = takeover.setdefault("entries", [])
    by_path = {entry.get("path"): entry for entry in entries if isinstance(entry, dict)}
    managed_now = list(plan.managed_paths)
    for path in (
        "/env/ANTHROPIC_API_KEY", "/env/ANTHROPIC_AUTH_TOKEN",
        "/env/ANTHROPIC_MODEL", "/env/ANTHROPIC_DEFAULT_FABLE_MODEL",
        "/env/CLAUDE_CODE_EFFORT_LEVEL",
    ):
        if not _is_missing(_pointer_get(settings, path)) and path not in managed_now:
            managed_now.append(path)
    for path in managed_now:
        desired = plan.pointer_values.get(path, _MISSING)
        before = _pointer_get(settings, path)
        entry = by_path.get(path)
        if entry is None:
            entry = {
                "path": path,
                "before": _journal_value(_secret_before_reference(path, before)),
                "applied": _journal_value(desired),
            }
            entries.append(entry)
            by_path[path] = entry
        else:
            entry["applied"] = _journal_value(desired)
        if _is_missing(desired):
            _pointer_delete(settings, path)
        else:
            _pointer_set(settings, path, desired)
    takeover["updated_at"] = datetime.now(timezone.utc).isoformat()
    return takeover


def _restore_claude_takeover(
    settings: dict[str, Any], takeover: dict[str, Any],
) -> tuple[dict[str, Any], list[str]]:
    conflicts: list[str] = []
    entries = takeover.get("entries", [])
    for entry in entries if isinstance(entries, list) else []:
        if not isinstance(entry, dict) or not isinstance(entry.get("path"), str):
            continue
        path = entry["path"]
        local = _pointer_get(settings, path)
        applied = _journal_decode(entry.get("applied", _MISSING))
        try:
            before = _journal_before_value(entry)
        except SystemExit:
            conflicts.append(f"{path} (restore credential unavailable)")
            continue
        if local == applied:
            if _is_missing(before):
                _pointer_delete(settings, path)
            else:
                _pointer_set(settings, path, before)
        elif local == before:
            continue
        else:
            conflicts.append(path)
    env = settings.get("env")
    if isinstance(env, dict) and not env and not takeover.get("env_existed", False):
        settings.pop("env", None)
    return settings, conflicts


def claude_current_info() -> dict[str, Any]:
    settings = claude_settings_read()
    env = claude_env(settings)
    model = str(settings.get("model") or "default")
    effort = str(settings.get("effortLevel") or "auto")
    base_url = env.get("ANTHROPIC_BASE_URL", "(未配置)")
    token_set = bool(env.get("ANTHROPIC_AUTH_TOKEN") or env.get("ANTHROPIC_API_KEY") or settings.get("apiKeyHelper"))
    return {
        "model": model,
        "effort": effort,
        "base_url": base_url,
        "token_set": token_set,
        "settings": settings,
    }


def claude_provider_models(channel: dict[str, Any]) -> list[str]:
    anthropic, runtime = _anthropic_profile_sections(channel)
    model_map = runtime.get("model_map")
    if not isinstance(model_map, dict):
        return _normalized_values(anthropic.get("models"))
    return [str(model_map[role]) for role in CLAUDE_ROLES if isinstance(model_map.get(role), str) and model_map.get(role)]


def _parse_model_map(values: list[str] | None, map_all: str | None = None) -> dict[str, str] | None:
    if map_all and values:
        die("--map-all 与 --map 不能同时使用")
    if map_all:
        return {role: map_all for role in CLAUDE_ROLES}
    if not values:
        return None
    result: dict[str, str] = {}
    for value in values:
        if "=" not in value:
            die(f"Claude 映射必须为 role=model：{value}")
        role, model = (part.strip() for part in value.split("=", 1))
        role = role.casefold()
        if role not in CLAUDE_ROLES or not model:
            die(f"Claude 映射必须覆盖 opus/sonnet/haiku：{value}")
        if role in result:
            die(f"Claude 映射重复指定 role：{role}")
        result[role] = model
    return result


def select_claude_code_channel(
    slug: str,
    *,
    anthropic_base_url: str | None = None,
    model_map: dict[str, str] | None = None,
    default_role: str | None = None,
    effort: str | None = None,
    auth_mode: str | None = None,
    model: str | None = None,
    selected_models: list[str] | None = None,
    selected_efforts: list[str] | None = None,
) -> None:
    """Activate a complete explicit Claude profile; ``model`` is map-all only."""
    data = load_channels()
    channel = data.get("channels", {}).get(slug)
    if not isinstance(channel, dict):
        die(f"渠道不存在：{slug}。先执行 add。")
    if model:
        if model_map:
            die("--model 与 --map/--map-all 不能同时使用")
        print_note("--model 是兼容输入：当前值将显式映射到 opus/sonnet/haiku。", kind="warn")
        model_map = {role: model for role in CLAUDE_ROLES}
    profile = _claude_profile(
        channel, anthropic_base_url=anthropic_base_url, model_map=model_map,
        default_role=default_role, effort=effort, auth_mode=auth_mode,
    )
    plan = compile_claude_managed_values(profile)
    if not plan.supported:
        die("Claude 配置不完整：" + "；".join(plan.diagnostics))
    protocols = profile["protocols"]
    runtimes = profile["runtimes"]
    protocols["anthropic"].pop("migration_status", None)
    protocols["anthropic"].pop("legacy_hint", None)
    protocols["anthropic"].setdefault("models_source", "explicit_map")
    settings = claude_settings_read()
    validate_claude_settings_shape(settings)
    backup = claude_settings_backup(settings)
    _apply_claude_plan(settings, data, plan)
    # Persist the journal before replacing settings so recovery never relies on a full snapshot.
    save_channels(data)
    claude_settings_write(settings)
    channel["protocols"] = protocols
    channel["runtimes"] = runtimes
    anthropic = protocols["anthropic"]
    runtime = runtimes["claude_code"]
    channel.update({
        "claude_base_url": anthropic["base_url"],
        "claude_models": anthropic["models"],
        "claude_model_map": runtime["model_map"],
        "claude_default_role": runtime["default_role"],
        "claude_effort": runtime["effort"],
        "claude_auth_mode": anthropic["auth"]["mode"],
        "claude_enabled": True,
        "claude_last_enabled_at": datetime.now(timezone.utc).isoformat(),
    })
    for item in data.get("channels", {}).values():
        if item is not channel and isinstance(item, dict):
            item["claude_enabled"] = False
    data["claude_active"] = slug
    save_channels(data)
    print_heading("Claude Code 已启用", f"{channel.get('name', slug)} ({slug})")
    print_field("Anthropic URL", anthropic["base_url"])
    print_field("默认 alias", runtime["default_role"], tone="accent")
    print_field("思考强度", runtime["effort"], tone="accent")
    for role in CLAUDE_ROLES:
        print_field(f"{role} →", runtime["model_map"][role])
    if backup:
        print_note(f"已写入脱敏备份：{backup}")
    print_note("Claude Code 新会话会读取新配置；运行中的会话请重启。", kind="warn")


def deactivate_claude_code_channel(slug: str | None = None, *, quiet: bool = False) -> None:
    data = load_channels()
    active = str(data.get("claude_active") or slug or "")
    state = data.get("runtime_state", {})
    claude_state = state.get(CLAUDE_TAKEOVER_KEY, {}) if isinstance(state, dict) else {}
    takeover = claude_state.get("takeover") if isinstance(claude_state, dict) else None
    if not isinstance(takeover, dict):
        legacy = data.get("claude_fallback")
        if not quiet:
            if isinstance(legacy, dict):
                print_note("检测到旧版 Claude 整体快照；来源含义不明确，未自动覆盖当前配置。", kind="warn")
            else:
                print_note("当前没有 SGate pointer journal，未修改 Claude Code 配置。", kind="warn")
        return
    target_path = str(Path(str(takeover.get("target_path", ""))).expanduser().resolve())
    current_path = str(CLAUDE_CODE_SETTINGS_PATH.expanduser().resolve())
    if target_path != current_path:
        die(f"Claude journal 属于 {target_path}，当前配置是 {current_path}；已停止恢复，未修改配置。")
    settings = claude_settings_read()
    validate_claude_settings_shape(settings)
    backup = claude_settings_backup(settings)
    settings, conflicts = _restore_claude_takeover(settings, takeover)
    file_existed = bool(takeover.get("file_existed"))
    if not file_existed and not settings:
        try:
            CLAUDE_CODE_SETTINGS_PATH.unlink()
        except FileNotFoundError:
            pass
    else:
        claude_settings_write(settings)
        mode = takeover.get("original_mode")
        if isinstance(mode, int) and file_existed:
            os.chmod(CLAUDE_CODE_SETTINGS_PATH, mode)
    channel = data.get("channels", {}).get(active)
    if isinstance(channel, dict):
        channel["claude_enabled"] = False
        channel["claude_last_disabled_at"] = datetime.now(timezone.utc).isoformat()
    data["claude_active"] = None
    claude_state.pop("takeover", None)
    if not claude_state and isinstance(state, dict):
        state.pop(CLAUDE_TAKEOVER_KEY, None)
    save_channels(data)
    if not quiet:
        if backup:
            print_note(f"已写入脱敏备份：{backup}")
        if conflicts:
            print_note("以下路径已被用户或其他工具修改，SGate 保留现值：" + ", ".join(conflicts), kind="warn")
        else:
            print_note("已按 pointer journal 精确恢复 Claude Code 配置。", kind="ok")
        print_note("新会话会读取恢复后的配置；运行中的会话请重启。", kind="warn")


def configure_claude_code_channel(
    slug: str | None = None, *, anthropic_base_url: str | None = None,
    model_map: dict[str, str] | None = None, default_role: str | None = None,
    effort: str | None = None, auth_mode: str | None = None,
) -> None:
    slug = slug or choose_channel("Claude Code：选择渠道", runtime="claude")
    if not slug:
        return
    data = load_channels()
    channel = data.get("channels", {}).get(slug)
    if not isinstance(channel, dict):
        die(f"渠道不存在：{slug}")
    anthropic, runtime = _anthropic_profile_sections(channel)
    if anthropic_base_url is None:
        current_url = str(anthropic.get("base_url") or "")
        anthropic_base_url = input(
            f"Anthropic Base URL（独立于 OpenAI URL） [{current_url}]："
        ).strip() or current_url
    models = _normalized_values(channel.get("models"))
    if model_map is None:
        existing_map = runtime.get("model_map") if isinstance(runtime.get("model_map"), dict) else {}
        selected: dict[str, str] = {}
        for role in CLAUDE_ROLES:
            default_model = existing_map.get(role)
            pick = terminal_radio(
                f"Claude alias -> 网关模型 ID\n当前 alias：{role}",
                [(name, name + ("  · 当前" if name == default_model else "")) for name in models],
                default=default_model,
                searchable=True,
            )
            if pick is None:
                print_note("已取消，Claude 配置未写入。", kind="warn")
                return
            selected[role] = pick
        model_map = selected
    if default_role is None:
        default_role = terminal_radio(
            "Claude Code 默认 alias",
            [(role, f"{role} -> {model_map[role]}") for role in CLAUDE_ROLES],
            default=str(runtime.get("default_role") or "sonnet"),
        )
        if default_role is None:
            print_note("已取消，Claude 配置未写入。", kind="warn")
            return
    if effort is None:
        effort = terminal_radio(
            "Claude Code 思考强度",
            [(value, value) for value in CLAUDE_EFFORTS],
            default=str(runtime.get("effort") or "high"),
        )
        if effort is None:
            print_note("已取消，Claude 配置未写入。", kind="warn")
            return
    select_claude_code_channel(
        slug, anthropic_base_url=anthropic_base_url, model_map=model_map,
        default_role=default_role, effort=effort, auth_mode=auth_mode,
    )


def print_claude_code_status() -> None:
    info = claude_current_info()
    data = load_channels()
    active = data.get("claude_active")
    channel = data.get("channels", {}).get(active) if active else None
    print_heading("Claude Code 当前配置")
    print_field("配置文件", CLAUDE_CODE_SETTINGS_PATH)
    print_field("当前渠道", f"{channel.get('name', active)} ({active})" if channel else "官方 Anthropic / 未登记", tone="ok" if channel else "warn")
    print_field("Base URL", info["base_url"])
    print_field("默认模型", info["model"], tone="accent")
    print_field("思考强度", info["effort"], tone="accent")
    print_field("认证状态", "已配置" if info["token_set"] else "未配置", tone="ok" if info["token_set"] else "warn")
    print_note("Claude Code 持久配置仅支持 low / medium / high。", kind="info")
    if channel:
        _, runtime = _anthropic_profile_sections(channel)
        model_map = runtime.get("model_map") if isinstance(runtime.get("model_map"), dict) else {}
        for role in CLAUDE_ROLES:
            print_field(f"{role} →", model_map.get(role, "未配置"), tone="accent" if role in model_map else "warn")
    if isinstance(data.get("claude_fallback"), dict):
        print_note("存在旧版 Claude fallback 快照：其恢复语义为 legacy/ambiguous。", kind="warn")


def claude_desktop_status() -> None:
    print_heading("Claude Desktop 当前配置")
    print_field("配置文件", CLAUDE_DESKTOP_CONFIG_PATH)
    print_field("Code tab 配置", CLAUDE_CODE_SETTINGS_PATH)
    info = claude_current_info()
    print_field("Code alias", info["model"], tone="accent")
    print_field("Code effort", info["effort"], tone="accent")
    print_field("Anthropic URL", info["base_url"])
    print_field("应用", "已安装" if Path("/Applications/Claude.app").exists() else "未检测到", tone="ok" if Path("/Applications/Claude.app").exists() else "warn")
    print_note("Desktop JSON 只提供受支持的应用/MCP 设置；自定义 provider 与 Bearer-only 持久认证为 unsupported。", kind="warn")
    if not CLAUDE_DESKTOP_CONFIG_PATH.exists():
        print_note("尚未找到 Claude Desktop 配置文件。", kind="warn")
        return
    try:
        config = json.loads(CLAUDE_DESKTOP_CONFIG_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        print_note(f"配置无法解析：{exc}", kind="err")
        return
    mcp = config.get("mcpServers", {}) if isinstance(config, dict) else {}
    print_field("MCP 服务", f"{len(mcp)} 个")
    print_note("Claude Desktop 原生不提供自定义 API Base URL、API Key 或模型 provider 配置；Bearer-only 持久认证为 unsupported。", kind="warn")
    print_note("Desktop 的 Code tab 复用 Claude Code settings.json；Desktop JSON 本身保持只读。", kind="ok")
    print_note("Desktop Chat/Cowork 渠道仍由 Claude Desktop 自身的账户、连接器和扩展设置管理。")


def select_claude_desktop_channel(slug: str, **options: Any) -> None:
    """Apply the Claude planner to Desktop's Code tab; never edit Desktop JSON."""
    if options.get("auth_mode") in ("auth_token", "bearer", "plaintext"):
        die("Claude Desktop 的 bearer-only 持久认证无法安全表达，unsupported；请使用 api_key_helper。")
    select_claude_code_channel(slug, **options)
    print_note("Desktop JSON 保持只读；Code tab 在重启或新建 session 后读取 Claude Code 配置。", kind="ok")


def claude_desktop_interactive() -> None:
    while True:
        try:
            info = claude_current_info()
            choice = terminal_menu(
                "Claude Desktop\n"
                f"Code tab：{info['model']} · {info['effort']} · {info['base_url']}",
                [
                    ("use", "[Code tab] 选择渠道、模型和思考强度"),
                    ("status", "[状态] 查看 Desktop 配置、MCP 和 Code tab 状态"),
                    ("back", "[返回] 回到工具选择"),
                ],
                default="use",
            )
            if choice in (None, "back"):
                return
            if choice == "use":
                configure_claude_code_channel()
                print_note("上述选择会应用到 Claude Desktop 的 Code tab。", kind="ok")
            elif choice == "status":
                claude_desktop_status()
            pause_after_action()
        except (KeyboardInterrupt, EOFError):
            print("\n已退出。")
            return


def claude_code_interactive() -> None:
    while True:
        try:
            info = claude_current_info()
            title = f"Claude Code 渠道切换器\n当前：{info['model']} · {info['effort']} · {info['base_url']}"
            choice = terminal_menu(title, [
                ("use", "[切换] 选择渠道、模型和思考强度"),
                ("configure", "[配置] 重新选择当前渠道的模型和思考强度"),
                ("disable", "[停用] 恢复 Claude Code 原有配置"),
                ("status", "[状态] 查看 Claude Code 实际配置"),
                ("back", "[返回] 回到工具选择"),
            ], default="use")
            if choice in (None, "back"):
                return
            if choice in ("use", "configure"):
                configure_claude_code_channel()
            elif choice == "disable":
                deactivate_claude_code_channel()
            elif choice == "status":
                print_claude_code_status()
            pause_after_action()
        except (KeyboardInterrupt, EOFError):
            print("\n已退出。")
            return


def opencode_write_runtime_key(slug: str) -> None:
    """Materialize the selected Keychain secret for OpenCode's file interpolation."""
    key = keychain_get(slug)
    OPENCODE_CREDENTIALS_DIR.mkdir(parents=True, exist_ok=True)
    os.chmod(OPENCODE_CREDENTIALS_DIR, 0o700)
    credentials_path = opencode_credentials_path(slug)
    tmp = credentials_path.with_suffix(".tmp")
    tmp.write_text(key + "\n", encoding="utf-8")
    os.chmod(tmp, 0o600)
    os.replace(tmp, credentials_path)
    os.chmod(credentials_path, 0o600)


def opencode_provider_block(channel: dict[str, Any]) -> dict[str, Any]:
    efforts = _normalized_values(_channel_value(channel, "opencode", "selected_efforts"), EFFORTS) or list(EFFORTS)
    models = _normalized_values(_channel_value(channel, "opencode", "selected_models")) or [
        str(_channel_value(channel, "opencode", "model") or DEFAULT_MODEL)
    ]
    variants = {effort: {"reasoningEffort": effort} for effort in efforts}
    return {
        "name": channel.get("name", channel["slug"]),
        "npm": "@ai-sdk/openai-compatible",
        "options": {
            "apiKey": "{file:" + str(opencode_credentials_path(str(channel["slug"]))) + "}",
            "baseURL": str(channel["base_url"]).rstrip("/") + "/v1" if not str(channel["base_url"]).rstrip("/").endswith("/v1") else str(channel["base_url"]).rstrip("/"),
            "setCacheKey": True,
        },
        "models": {
            model: {
                "name": model,
                "reasoning": True,
                "tool_call": True,
                "variants": variants,
            }
            for model in models
        },
    }


def current_opencode_info(*, strict: bool = True) -> dict[str, Any]:
    config = opencode_read_config(strict=strict)
    model_ref = str(config.get("model", ""))
    provider_id_value, _, model = model_ref.partition("/")
    providers = config.get("provider") if isinstance(config.get("provider"), dict) else {}
    provider = providers.get(provider_id_value, {}) if isinstance(providers, dict) else {}
    variants = {}
    if isinstance(provider, dict) and isinstance(provider.get("models"), dict) and model in provider["models"]:
        variants = provider["models"][model].get("variants", {}) or {}
    build_agent = config.get("agent", {}).get("build", {}) if isinstance(config.get("agent"), dict) else {}
    effort = str(build_agent.get("variant", "")) if isinstance(build_agent, dict) else ""
    if effort not in EFFORTS and isinstance(variants, dict) and variants:
        effort = next(iter(variants), "")
    managed = sorted(
        pid for pid in (providers or {})
        if isinstance(pid, str) and pid.startswith("sgate_")
    )
    return {
        "provider_id": provider_id_value,
        "provider": provider if isinstance(provider, dict) else {},
        "model": model or "(未设置)",
        "reasoning_effort": effort or "(未设置)",
        "managed_providers": managed,
    }


def opencode_enabled_slugs(*, strict: bool = False) -> list[str]:
    """Slugs whose provider block is currently present in opencode.json."""
    managed = set(current_opencode_info(strict=strict)["managed_providers"])
    return [
        slug for slug in load_channels().get("channels", {})
        if opencode_provider_id(slug) in managed
    ]


def _remember_opencode_fallback(data: dict[str, Any], config: dict[str, Any]) -> None:
    """Snapshot the pre-SGate OpenCode defaults so they can be restored later."""
    current_provider = str(config.get("model", "")).partition("/")[0]
    if not current_provider or current_provider.startswith("sgate_"):
        return
    build = config.get("agent", {}).get("build", {}) if isinstance(config.get("agent"), dict) else {}
    data["opencode_fallback"] = {
        "model": config.get("model"),
        "small_model": config.get("small_model"),
        "build_model": build.get("model") if isinstance(build, dict) else None,
        "build_variant": build.get("variant") if isinstance(build, dict) else None,
        "saved_at": datetime.now(timezone.utc).isoformat(),
    }


def _resolve_opencode_selection(
    channel: dict[str, Any],
    model: str | None,
    effort: str | None,
    selected_models: list[str] | None,
    selected_efforts: list[str] | None,
) -> tuple[str, str, list[str], list[str]]:
    model = model or str(_channel_value(channel, "opencode", "model") or DEFAULT_MODEL)
    effort = effort or str(_channel_value(channel, "opencode", "reasoning_effort") or DEFAULT_EFFORT)
    if effort not in EFFORTS:
        die(f"推理强度必须是：{', '.join(EFFORTS)}")
    models = (
        _normalized_values(selected_models)
        if selected_models is not None
        else _normalized_values(_channel_value(channel, "opencode", "selected_models"))
    ) or [model]
    if model not in models:
        models.append(model)
    efforts = (
        _normalized_values(selected_efforts, EFFORTS)
        if selected_efforts is not None
        else _normalized_values(_channel_value(channel, "opencode", "selected_efforts"), EFFORTS)
    ) or list(EFFORTS)
    if effort not in efforts:
        efforts.append(effort)
    return model, effort, models, efforts


def select_opencode_channel(
    slug: str,
    *,
    model: str | None = None,
    effort: str | None = None,
    selected_models: list[str] | None = None,
    selected_efforts: list[str] | None = None,
    make_default: bool = True,
    quiet: bool = False,
) -> None:
    """Add or update one OpenCode provider.

    OpenCode keeps every provider in `provider`, so enabling a channel never
    removes the others. `make_default` decides whether this channel also becomes
    the top-level `model` used by new sessions.
    """
    data = load_channels()
    channel = data.get("channels", {}).get(slug)
    if not channel:
        die(f"渠道不存在：{slug}。先执行 add。")
    config = opencode_read_config()
    model, effort, chosen_models, chosen_efforts = _resolve_opencode_selection(
        channel, model, effort, selected_models, selected_efforts
    )
    channel.update({
        "opencode_model": model,
        "opencode_reasoning_effort": effort,
        "opencode_selected_models": chosen_models,
        "opencode_selected_efforts": chosen_efforts,
        "opencode_enabled": True,
        "opencode_last_enabled_at": datetime.now(timezone.utc).isoformat(),
    })
    if make_default:
        data["opencode_active"] = slug
    data["active"] = data.get("codex_active", data.get("active"))
    _remember_opencode_fallback(data, config)
    save_channels(data)
    opencode_write_runtime_key(slug)

    config["$schema"] = "https://opencode.ai/config.json"
    provider_id_value = opencode_provider_id(slug)
    config.setdefault("provider", {})[provider_id_value] = opencode_provider_block(channel)
    model_ref = f"{provider_id_value}/{model}"
    if make_default:
        config["model"] = model_ref
        build_agent = config.setdefault("agent", {}).setdefault("build", {})
        if isinstance(build_agent, dict):
            build_agent["model"] = model_ref
            build_agent["variant"] = effort
    backup = opencode_backup_config()
    opencode_write_config(config)
    if quiet:
        return
    if backup:
        print_note(f"已备份 OpenCode 配置：{dim(str(backup))}")
    label = "已启用并设为默认" if make_default else "已启用"
    print_heading(f"OpenCode {label}", f"{channel.get('name', slug)} ({slug})")
    print_field("Model", model, tone="accent")
    print_field("Reasoning", effort, tone="accent")
    print_field("可用模型", f"{len(chosen_models)} 个")
    print_field("可用强度", f"{len(chosen_efforts)} 个")
    print_field("配置", OPENCODE_CONFIG_PATH)
    enabled = opencode_enabled_slugs()
    if len(enabled) > 1:
        print_field("同时启用", f"{len(enabled)} 个渠道：{', '.join(enabled)}")
    print_note("OpenCode 需要重启后读取新配置。", kind="warn")


def sync_opencode_channels(slugs: list[str], default_slug: str | None = None) -> None:
    """Make opencode.json contain exactly these managed channels."""
    data = load_channels()
    known = data.get("channels", {})
    wanted = [slug for slug in slugs if slug in known]
    if not wanted:
        for slug in opencode_enabled_slugs():
            deactivate_opencode_channel(slug, quiet=True)
        print_note("已停用所有 OpenCode 渠道。", kind="ok")
        return
    if default_slug not in wanted:
        default_slug = wanted[0]

    for slug in opencode_enabled_slugs():
        if slug not in wanted:
            deactivate_opencode_channel(slug, quiet=True)
    for slug in wanted:
        select_opencode_channel(slug, make_default=(slug == default_slug), quiet=True)

    print_heading("OpenCode 渠道已同步", f"共 {len(wanted)} 个渠道")
    rows = []
    for slug in wanted:
        channel = known[slug]
        is_default = slug == default_slug
        rows.append([
            green(ICON_ON) if is_default else dim(ICON_ON),
            channel.get("name", slug),
            slug,
            str(_channel_value(channel, "opencode", "model") or "(未选)"),
            str(_channel_value(channel, "opencode", "reasoning_effort") or DEFAULT_EFFORT),
            green("默认") if is_default else dim("可切换"),
        ])
    print_table(["", "名称", "slug", "模型", "强度", "状态"], rows)
    print_note(f"配置：{OPENCODE_CONFIG_PATH}")
    print_note("在 OpenCode 中可用 /models 在这些渠道之间直接切换。", kind="ok")
    print_note("OpenCode 需要重启后读取新配置。", kind="warn")


def remove_opencode_provider(slug: str, *, restore: bool = False) -> None:
    config = opencode_read_config()
    provider_id_value = opencode_provider_id(slug)
    providers = config.get("provider")
    if isinstance(providers, dict):
        providers.pop(provider_id_value, None)
    current_provider = str(config.get("model", "")).partition("/")[0]
    deleted_prefix = provider_id_value + "/"
    if str(config.get("model", "")).startswith(deleted_prefix):
        config.pop("model", None)
    if str(config.get("small_model", "")).startswith(deleted_prefix):
        config.pop("small_model", None)
    agents = config.get("agent")
    if isinstance(agents, dict):
        for agent in agents.values():
            if isinstance(agent, dict) and str(agent.get("model", "")).startswith(deleted_prefix):
                agent.pop("model", None)
    if restore and current_provider == provider_id_value:
        data = load_channels()
        fallback = data.get("opencode_fallback") if isinstance(data.get("opencode_fallback"), dict) else {}
        if fallback.get("model"):
            config["model"] = fallback["model"]
        else:
            config.pop("model", None)
        if fallback.get("small_model"):
            config["small_model"] = fallback["small_model"]
        else:
            config.pop("small_model", None)
        build = config.get("agent", {}).get("build", {}) if isinstance(config.get("agent"), dict) else {}
        if isinstance(build, dict):
            if fallback.get("build_model"):
                build["model"] = fallback["build_model"]
            else:
                build.pop("model", None)
            if fallback.get("build_variant"):
                build["variant"] = fallback["build_variant"]
            else:
                build.pop("variant", None)
    if OPENCODE_CONFIG_PATH.exists():
        backup = opencode_backup_config()
        opencode_write_config(config)
        if backup:
            print(f"已备份 OpenCode 配置：{backup}")


def deactivate_opencode_channel(slug: str, *, quiet: bool = False) -> None:
    """Remove one provider, promoting another managed channel if it was default."""
    remaining = [s for s in opencode_enabled_slugs() if s != slug]
    remove_opencode_provider(slug, restore=not remaining)
    data = load_channels()
    channel = data.get("channels", {}).get(slug)
    if channel:
        channel["opencode_enabled"] = False
        channel["opencode_last_disabled_at"] = datetime.now(timezone.utc).isoformat()
    promoted: str | None = None
    if data.get("opencode_active") == slug:
        data["opencode_active"] = remaining[0] if remaining else None
        promoted = remaining[0] if remaining else None
    if not remaining:
        data.pop("opencode_fallback", None)
    save_channels(data)
    if promoted:
        select_opencode_channel(promoted, make_default=True, quiet=True)
    if quiet:
        return
    print_note(f"已从 OpenCode 配置移除渠道 {slug}。", kind="ok")
    if promoted:
        print_note(f"默认渠道已改为 {promoted}。")
    elif not remaining:
        print_note("已恢复 OpenCode 原有默认配置。")
    print_note("重启 OpenCode 后生效。", kind="warn")


def configure_opencode_channel() -> None:
    slug = choose_channel("OpenCode：选择渠道", runtime="opencode")
    if not slug:
        return
    data = load_channels()
    channel = data["channels"][slug]
    models = _normalized_values(channel.get("models")) or _normalized_values(
        _channel_value(channel, "opencode", "selected_models")
    )
    model_pick = choose_models(
        models,
        defaults=_normalized_values(_channel_value(channel, "opencode", "selected_models")),
        default_value=_channel_value(channel, "opencode", "model"),
    )
    if not model_pick:
        return
    selected_models, model = model_pick
    effort_pick = choose_efforts(
        defaults=_normalized_values(_channel_value(channel, "opencode", "selected_efforts"), EFFORTS),
        default_value=_channel_value(channel, "opencode", "reasoning_effort"),
    )
    if not effort_pick:
        return
    selected_efforts, effort = effort_pick
    channel["opencode_selected_models"] = selected_models
    channel["opencode_selected_efforts"] = selected_efforts
    channel["opencode_model"] = model
    channel["opencode_reasoning_effort"] = effort
    save_channels(data)
    select_opencode_channel(slug, model=model, effort=effort)


def choose_opencode_channels() -> None:
    """Enable several OpenCode channels at once and pick the default."""
    data = load_channels()
    channels = data.get("channels", {})
    if not channels:
        print_note("还没有渠道，请先在渠道管理中新增。", kind="warn")
        return
    enabled = opencode_enabled_slugs()
    active = data.get("opencode_active")
    options = []
    for slug, channel in channels.items():
        model = _channel_value(channel, "opencode", "model") or "(未选模型)"
        effort = _channel_value(channel, "opencode", "reasoning_effort") or DEFAULT_EFFORT
        tag = green("  [默认]") if slug == active else ("  [已启用]" if slug in enabled else "")
        options.append((slug, f"{channel.get('name', slug)} ({slug}) · {model} · {effort}{tag}"))
    picked = terminal_multi(
        "OpenCode 多渠道\n  Space 勾选要同时启用的渠道；d 设默认；Enter 确认",
        options,
        defaults=enabled,
        default_value=active if active in channels else (enabled[0] if enabled else None),
        searchable=True,
    )
    if picked is None:
        print_note("已取消，OpenCode 配置未改动。")
        return
    slugs, default_slug = picked
    sync_opencode_channels(slugs, default_slug)


def print_opencode_status(*, strict: bool = True) -> None:
    info = current_opencode_info(strict=strict)
    data = load_channels()
    channels = data.get("channels", {})
    enabled = [
        slug for slug in channels
        if opencode_provider_id(slug) in set(info["managed_providers"])
    ]
    print_heading("OpenCode 当前配置")
    print_field("配置文件", OPENCODE_CONFIG_PATH)
    if not opencode_config_is_valid():
        print_note("配置文件无法解析，以下信息可能不完整。", kind="err")
    matched = next(
        (c for s, c in channels.items() if opencode_provider_id(s) == info["provider_id"]),
        None,
    )
    print_field("默认渠道", matched.get("name", matched.get("slug")) if matched else
                (info["provider_id"] or "(未设置)"),
                tone="ok" if matched else "warn")
    print_field("默认模型", info["model"], tone="accent")
    print_field("思考强度", info["reasoning_effort"], tone="accent")
    print_field("启用渠道数", len(enabled) if enabled else "0")
    if enabled:
        rows = []
        for slug in enabled:
            channel = channels[slug]
            is_default = opencode_provider_id(slug) == info["provider_id"]
            models = _normalized_values(_channel_value(channel, "opencode", "selected_models"))
            efforts = _normalized_values(_channel_value(channel, "opencode", "selected_efforts"), EFFORTS)
            rows.append([
                green(ICON_ON) if is_default else dim(ICON_ON),
                channel.get("name", slug),
                slug,
                str(_channel_value(channel, "opencode", "model") or "(未选)"),
                f"{len(models) or 1} 个",
                f"{len(efforts) or len(EFFORTS)} 个",
                green("默认") if is_default else dim("可切换"),
            ])
        print()
        print_table(["", "名称", "slug", "默认模型", "模型", "强度", "状态"], rows)
    unmanaged = [
        pid for pid in info["managed_providers"]
        if not any(opencode_provider_id(s) == pid for s in channels)
    ]
    if unmanaged:
        print()
        print_note(f"配置中存在已删除渠道的残留 provider：{', '.join(unmanaged)}", kind="warn")


def opencode_interactive() -> None:
    while True:
        try:
            info = current_opencode_info(strict=False)
            enabled = opencode_enabled_slugs()
            data = load_channels()
            matched = next(
                (c for s, c in data.get("channels", {}).items()
                 if opencode_provider_id(s) == info["provider_id"]),
                None,
            )
            name = matched.get("name", matched.get("slug")) if matched else (
                info["provider_id"] or "未设置"
            )
            title = (
                "OpenCode 渠道切换器\n"
                f"默认：{name} · {info['model']} · {info['reasoning_effort']}"
                f"　|　已启用 {len(enabled)} 个渠道"
            )
            choice = terminal_menu(title, [
                ("use", "[切换] 选择渠道并设为默认，同时配置模型 / 思考强度"),
                ("multi", "[多渠道] 同时启用多个渠道，并指定默认渠道"),
                ("configure", "[配置] 重新选择默认渠道的模型 / 思考强度"),
                ("refresh", "[模型] 拉取最新模型列表，用 Space 勾选"),
                ("disable", "[停用] 从 OpenCode 配置移除某个渠道"),
                ("status", "[状态] 查看 OpenCode 实际配置"),
                ("back", "[返回] 回到工具选择"),
            ], default="use")
            if choice in (None, "back"):
                return
            if choice in ("use", "configure"):
                configure_opencode_channel()
            elif choice == "multi":
                choose_opencode_channels()
            elif choice == "refresh":
                slug = choose_channel("OpenCode：刷新哪个渠道的模型？", runtime="opencode")
                if slug:
                    refresh_models(slug, restart_app=False, runtime="opencode")
            elif choice == "disable":
                if not enabled:
                    print_note("当前没有已启用的 OpenCode 渠道。", kind="warn")
                else:
                    slug = terminal_menu("停用哪个 OpenCode 渠道？", [
                        (s, f"{data['channels'][s].get('name', s)} ({s})"
                            f"{green('  [默认]') if opencode_provider_id(s) == info['provider_id'] else ''}")
                        for s in enabled
                    ])
                    if slug:
                        deactivate_opencode_channel(slug)
            elif choice == "status":
                print_opencode_status(strict=False)
            pause_after_action()
        except (KeyboardInterrupt, EOFError):
            print("\n已退出。")
            return


def engine_interactive() -> None:
    while True:
        try:
            data = load_channels()
            total = len(data.get("channels", {}))
            try:
                codex_info = current_config_info()
                codex_name = next(
                    (c.get("name", s) for s, c in data.get("channels", {}).items()
                     if provider_id(s) == codex_info["provider_id"]),
                    "官方登录" if codex_info["provider_id"] == "openai" else codex_info["provider_id"],
                )
            except SystemExit:
                codex_info = {"model": "配置不可读"}
                codex_name = "不可用"
            try:
                oc_enabled = opencode_enabled_slugs()
            except SystemExit:
                oc_enabled = []
            try:
                claude_info = claude_current_info()
            except SystemExit:
                claude_info = {"model": "配置不可读"}
            claude_name = data.get("claude_active") or "未启用"
            choice = terminal_menu(
                "SGate 渠道切换器\n"
                f"渠道：{total} 个\n"
                f"Codex：{codex_name} · {codex_info['model']}\n"
                f"OpenCode：{len(oc_enabled)} 个已启用\n"
                f"Claude Code：{claude_name} · {claude_info['model']}",
                [
                    ("channels", "[渠道管理] 新增、删除、总览和连接检查"),
                    ("codex", "[Codex] 选择渠道、模型、思考强度并启用"),
                    ("opencode", "[OpenCode] 多渠道启用、模型与思考强度"),
                    ("claude-code", "[Claude Code] 渠道、模型与思考强度"),
                    ("claude-desktop", "[Claude Desktop] 查看账户、MCP 与配置能力"),
                    ("exit", "[退出] 关闭菜单"),
                ], default="codex")
            if choice in (None, "exit"):
                return
            if choice == "channels":
                channel_management()
            elif choice == "codex":
                interactive()
            elif choice == "opencode":
                opencode_interactive()
            elif choice == "claude-code":
                claude_code_interactive()
            elif choice == "claude-desktop":
                claude_desktop_interactive()
        except (KeyboardInterrupt, EOFError):
            print("\n已退出。")
            return


def channel_management() -> None:
    while True:
        try:
            data = load_channels()
            channels = data.get("channels", {})
            choice = terminal_menu(
                "渠道管理\n"
                f"已保存 {len(channels)} 个渠道　|　API Key 存放于 macOS Keychain",
                [
                    ("add", "[新增] 添加渠道、保存 API Key 并拉取模型"),
                    ("remove", "[删除] 永久删除渠道及 Keychain 密钥"),
                    ("list", "[总览] 查看渠道、模型缓存和工具启用状态"),
                    ("doctor", "[检查] 测试共享 API 连接并更新模型缓存"),
                    ("back", "[返回] 回到工具选择"),
                ],
                default="add",
            )
            if choice in (None, "back"):
                return
            if choice == "add":
                add_channel(argparse.Namespace(
                    name=None, slug=None, base_url=None, model=None, reasoning=None,
                    force=False, use=False, restart_app=False,
                ), configure=False)
            elif choice == "remove":
                slug = choose_channel("删除哪个渠道？", runtime="all")
                if slug:
                    remove_channel(slug)
            elif choice == "list":
                list_channels()
            elif choice == "doctor":
                slug = choose_channel("检查哪个渠道？", runtime="all")
                if slug:
                    doctor(slug)
            pause_after_action()
        except (KeyboardInterrupt, EOFError):
            print("\n已退出渠道管理。")
            return


def models_url(base_url: str) -> str:
    base = base_url.rstrip("/")
    for suffix in ("/responses", "/chat/completions", "/models"):
        if base.endswith(suffix):
            base = base[: -len(suffix)]
    return base + "/models"


def fetch_models(base_url: str, api_key: str) -> tuple[list[str], str | None]:
    url = models_url(base_url)
    request = urllib.request.Request(
        url,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Accept": "application/json",
            "User-Agent": "sgate/3.0",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            raw = response.read(8_000_000)
            if response.status < 200 or response.status >= 300:
                return [], f"HTTP {response.status}"
    except urllib.error.HTTPError as exc:
        body = exc.read(1000).decode("utf-8", "replace").replace("\n", " ")
        return [], f"HTTP {exc.code}: {body[:300]}"
    except urllib.error.URLError as exc:
        return [], f"网络错误：{exc.reason}"
    except TimeoutError:
        return [], "请求超时"
    try:
        obj = json.loads(raw)
    except json.JSONDecodeError:
        return [], "返回内容不是 JSON"

    if isinstance(obj, list):
        items = obj
    elif isinstance(obj, dict):
        items = obj.get("data") or obj.get("models") or obj.get("items") or []
    else:
        items = []
    names: list[str] = []
    for item in items if isinstance(items, list) else []:
        if isinstance(item, str):
            name = item
        elif isinstance(item, dict):
            name = item.get("id") or item.get("model") or item.get("name")
        else:
            name = None
        if isinstance(name, str) and name.strip() and name.strip() not in names:
            names.append(name.strip())
    return sorted(names, key=str.casefold), None


def _model_sort_key(name: str, default: str | None) -> tuple[Any, ...]:
    lower = name.casefold()
    incompatible = any(token in lower for token in (
        "image", "dall-e", "embedding", "whisper", "tts", "audio", "moderation", "realtime",
    ))
    preferred = "codex" in lower or lower.startswith("gpt-5")
    numbers = tuple(-int(part) for part in re.findall(r"\d+", lower)[:4])
    return (name != default, incompatible, not preferred, numbers, lower)


def _model_label(name: str, default: str | None) -> str:
    lower = name.casefold()
    notes: list[str] = []
    if name == default:
        notes.append("当前")
    if any(token in lower for token in ("image", "dall-e", "embedding", "whisper", "tts", "audio", "moderation")):
        notes.append("通常不适合 Codex")
    elif "codex" in lower or lower.startswith("gpt-5"):
        notes.append("适合代码任务")
    return f"{name}  [{' · '.join(notes)}]" if notes else name


def choose_model(models: list[str], default: str | None = None) -> str | None:
    picked = choose_models(models, defaults=[default] if default else None, default_value=default)
    return picked[1] if picked else None


def choose_models(
    models: list[str],
    *,
    defaults: list[str] | None = None,
    default_value: str | None = None,
) -> tuple[list[str], str] | None:
    if not models:
        manual = input(f"未拉取到模型，请手动输入模型名 [{default_value or DEFAULT_MODEL}]（回车取消）：").strip()
        model = manual or (default_value if default_value else None)
        return ([model], model) if model else None
    ordered = sorted(dict.fromkeys(models), key=lambda name: _model_sort_key(name, default_value))
    options = [(name, _model_label(name, default_value)) for name in ordered]
    print(f"\n已自动拉取 {len(ordered)} 个可用模型。")
    return terminal_multi(
        "选择可在 ChatGPT.app 中切换的模型\n  Space 勾选；d 设默认；Enter 确认",
        options,
        defaults=defaults,
        default_value=default_value,
        searchable=True,
    )


def _effort_labels() -> dict[str, str]:
    return {
        "minimal": "minimal  · 最低，速度优先",
        "low": "low      · 较低",
        "medium": "medium   · 平衡",
        "high": "high     · 较高",
        "xhigh": "xhigh    · 最高，质量优先",
    }


def choose_effort(default: str | None = None) -> str | None:
    picked = choose_efforts(defaults=[default] if default else None, default_value=default)
    return picked[1] if picked else None


def choose_efforts(
    *,
    defaults: list[str] | None = None,
    default_value: str | None = None,
) -> tuple[list[str], str] | None:
    labels = _effort_labels()
    current = default_value if default_value in EFFORTS else DEFAULT_EFFORT
    return terminal_multi(
        "选择可在 ChatGPT.app 中切换的推理强度\n  Space 勾选；d 设默认；Enter 确认",
        [(effort, labels[effort] + ("  [当前默认]" if effort == current else "")) for effort in EFFORTS],
        defaults=defaults,
        default_value=current,
    )


def channel_catalog_path() -> Path:
    return CODEX_HOME / "sgate-model-catalog.json"


def _read_catalog_entries(path: Path) -> list[dict[str, Any]]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return []
    entries = raw.get("models", []) if isinstance(raw, dict) else raw
    return [entry for entry in entries if isinstance(entry, dict) and entry.get("slug")] if isinstance(entries, list) else []


def _model_template_map() -> dict[str, dict[str, Any]]:
    templates: dict[str, dict[str, Any]] = {}
    for path in (CODEX_HOME / "models_cache.json", CODEX_HOME / "cc-switch-model-catalog.json"):
        for entry in _read_catalog_entries(path):
            templates.setdefault(str(entry["slug"]), entry)
    return templates


def _minimal_catalog_entry(model: str) -> dict[str, Any]:
    return {
        "slug": model,
        "display_name": model,
        "description": model,
        "default_reasoning_level": DEFAULT_EFFORT,
        "supported_reasoning_levels": [],
        "shell_type": "shell_command",
        "visibility": "list",
        "supported_in_api": True,
        "priority": 1000,
        "additional_speed_tiers": [],
        "service_tiers": [],
        "availability_nux": None,
        "upgrade": None,
        "base_instructions": "You are Codex, a coding agent. You and the user share the same workspace and collaborate to achieve the user's goals.",
        "context_window": 128000,
        "max_context_window": 128000,
        "effective_context_window_percent": 95,
        "input_modalities": ["text", "image"],
        "supports_reasoning_summaries": True,
        "default_reasoning_summary": "none",
        "supports_parallel_tool_calls": True,
        "supports_search_tool": False,
        "support_verbosity": False,
        "experimental_supported_tools": [],
        "truncation_policy": {"mode": "bytes", "limit": 10000},
    }


def _normalized_values(values: Any, allowed: list[str] | tuple[str, ...] | None = None) -> list[str]:
    source = values if isinstance(values, list) else []
    result: list[str] = []
    for value in source:
        text = str(value).strip()
        if text and text not in result and (allowed is None or text in allowed):
            result.append(text)
    return result


def _channel_value(channel: dict[str, Any], runtime: str, name: str, legacy_name: str | None = None) -> Any:
    key = f"{runtime}_{name}"
    if key in channel:
        return channel[key]
    if runtime == "claude":
        _, claude_runtime = _anthropic_profile_sections(channel)
        if name in ("model", "default_role"):
            role = str(claude_runtime.get("default_role") or channel.get("claude_default_role") or "")
            model_map = claude_runtime.get("model_map")
            if name == "default_role":
                return role or None
            if isinstance(model_map, dict) and role in model_map:
                return model_map[role]
        if name in ("reasoning_effort", "effort"):
            return claude_runtime.get("effort") or channel.get("claude_effort")
    return channel.get(legacy_name or name)


def write_model_catalog(channel: dict[str, Any]) -> Path:
    models = _normalized_values(_channel_value(channel, "codex", "selected_models"))
    default_model = str(_channel_value(channel, "codex", "model") or DEFAULT_MODEL)
    if default_model not in models:
        models.append(default_model)
    models = [default_model, *[name for name in models if name != default_model]]
    efforts = _normalized_values(_channel_value(channel, "codex", "selected_efforts"), EFFORTS)
    default_effort = str(_channel_value(channel, "codex", "reasoning_effort") or DEFAULT_EFFORT)
    if default_effort not in EFFORTS:
        default_effort = DEFAULT_EFFORT
    if default_effort not in efforts:
        efforts.append(default_effort)
    if not efforts:
        efforts = [DEFAULT_EFFORT]

    descriptions = {
        "minimal": "Minimal reasoning for fastest responses",
        "low": "Fast responses with lighter reasoning",
        "medium": "Balances speed and reasoning depth",
        "high": "Greater reasoning depth for complex problems",
        "xhigh": "Extra high reasoning depth for complex problems",
    }
    templates = _model_template_map()
    entries: list[dict[str, Any]] = []
    for priority, model in enumerate(models):
        template = templates.get(model)
        entry = json.loads(json.dumps(template, ensure_ascii=False)) if template else _minimal_catalog_entry(model)
        known_descriptions = {
            str(level.get("effort")): str(level.get("description") or descriptions.get(str(level.get("effort")), ""))
            for level in entry.get("supported_reasoning_levels", []) if isinstance(level, dict)
        }
        entry.update({
            "slug": model,
            "display_name": entry.get("display_name") or model,
            "description": entry.get("description") or model,
            "default_reasoning_level": default_effort,
            "supported_reasoning_levels": [
                {"effort": effort, "description": known_descriptions.get(effort) or descriptions[effort]}
                for effort in efforts
            ],
            "visibility": "list",
            "supported_in_api": True,
            "priority": priority,
        })
        entries.append(entry)

    path = channel_catalog_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps({"models": entries}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)
    os.chmod(path, 0o600)
    return path


def _managed_provider_ids(data: dict[str, Any]) -> set[str]:
    return {provider_id(slug) for slug in data.get("channels", {})}


def _settings_from_content(content: str) -> dict[str, str]:
    values: dict[str, str] = {}
    for key in ("model_provider", "model", "model_reasoning_effort", "model_catalog_json"):
        match = re.search(rf"(?m)^{re.escape(key)}\s*=\s*(.+)$", content)
        if match:
            values[key] = _fallback_toml_string(match.group(1))
    return values


def _discover_previous_config(data: dict[str, Any]) -> dict[str, str] | None:
    """Find the newest pre-switch backup whose provider is not managed here."""
    managed = _managed_provider_ids(data)
    backups = sorted(
        [*CONFIG_PATH.parent.glob("config.toml.sgate-*.bak"), *CONFIG_PATH.parent.glob("config.toml.codex-channel-*.bak")],
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    for path in backups:
        try:
            values = _settings_from_content(path.read_text(encoding="utf-8"))
        except OSError:
            continue
        candidate = values.get("model_provider")
        if candidate and candidate not in managed:
            return {
                "model_provider": candidate,
                "model": values.get("model", DEFAULT_MODEL),
                "model_reasoning_effort": values.get("model_reasoning_effort", DEFAULT_EFFORT),
                "model_catalog_json": values.get("model_catalog_json", "models_cache.json"),
            }
    return None


def _remember_fallback(data: dict[str, Any], info: dict[str, Any]) -> None:
    """Remember the config that should return after a managed channel is disabled."""
    values: dict[str, Any] | None = None
    if info["provider_id"] not in _managed_provider_ids(data):
        values = {
            "model_provider": info["provider_id"],
            "model": info["model"],
            "model_reasoning_effort": info["reasoning_effort"],
            "model_catalog_json": info.get("model_catalog_json", "models_cache.json"),
        }
    elif not isinstance(data.get("fallback"), dict):
        # Upgrade path from version 1: recover the provider that existed before
        # SGate (formerly codex-channel) first overwrote the top-level settings.
        values = _discover_previous_config(data)
    elif not data.get("fallback", {}).get("model_catalog_json"):
        previous = _discover_previous_config(data)
        if previous and previous.get("model_catalog_json"):
            data["fallback"]["model_catalog_json"] = previous["model_catalog_json"]
    if values:
        data["fallback"] = {
            **values,
            "saved_at": datetime.now(timezone.utc).isoformat(),
        }


def _valid_setting(value: Any, fallback: str) -> str:
    text = str(value or "").strip()
    return fallback if not text or text == "(未设置)" else text


def select_channel(
    slug: str,
    *,
    model: str | None = None,
    effort: str | None = None,
    selected_models: list[str] | None = None,
    selected_efforts: list[str] | None = None,
    restart_app: bool = False,
) -> None:
    data = load_channels()
    channel = data.get("channels", {}).get(slug)
    if not channel:
        die(f"渠道不存在：{slug}。先执行 list 或 add。")
    effort = effort or channel.get("reasoning_effort") or DEFAULT_EFFORT
    if effort not in EFFORTS:
        die(f"推理强度必须是：{', '.join(EFFORTS)}")
    model = model or channel.get("model") or DEFAULT_MODEL

    available_models = _normalized_values(channel.get("models"))
    chosen_models = _normalized_values(selected_models) if selected_models is not None else _normalized_values(channel.get("selected_models"))
    if not chosen_models:
        chosen_models = [name for name in available_models if not any(
            token in name.casefold() for token in ("image", "dall-e", "embedding", "whisper", "tts", "audio", "moderation")
        )]
    if model not in chosen_models:
        chosen_models.append(model)
    chosen_efforts = _normalized_values(selected_efforts, EFFORTS) if selected_efforts is not None else _normalized_values(channel.get("selected_efforts"), EFFORTS)
    if not chosen_efforts:
        chosen_efforts = list(EFFORTS)
    if effort not in chosen_efforts:
        chosen_efforts.append(effort)

    current = current_config_info()
    _remember_fallback(data, current)
    for item in data.get("channels", {}).values():
        item["enabled"] = False
    channel["model"] = model
    channel["reasoning_effort"] = effort
    channel["selected_models"] = chosen_models
    channel["selected_efforts"] = chosen_efforts
    channel["codex_model"] = model
    channel["codex_reasoning_effort"] = effort
    channel["codex_selected_models"] = chosen_models
    channel["codex_selected_efforts"] = chosen_efforts
    channel["enabled"] = True
    channel["last_enabled_at"] = datetime.now(timezone.utc).isoformat()
    data["codex_active"] = slug
    data["active"] = slug
    catalog = write_model_catalog(channel)
    save_channels(data)

    content = update_provider_block(read_config(), channel)
    content = update_top_level(content, {
        "model_provider": provider_id(slug),
        "model": model,
        "model_reasoning_effort": effort,
        "model_catalog_json": str(catalog),
    })
    backup = backup_config()
    write_config(content)
    if backup:
        print(f"已备份配置：{backup}")
    print(f"已启用：{channel['name']} ({slug})")
    print(f"  Base URL：{channel['base_url']}\n  默认 Model：{model}\n  默认 Reasoning：{effort}")
    print(f"  可切换模型：{len(chosen_models)} 个 · 可切换推理强度：{len(chosen_efforts)} 个")
    print(f"  模型目录：{catalog}\n  实际配置：{CONFIG_PATH}")
    if ccswitch_is_running():
        print("  注意：检测到 CC Switch 正在运行；以后在 CC Switch 中切换渠道可能再次覆盖本文件。")
    if chatgpt_is_running():
        if restart_app:
            restart_chatgpt(ask=False)
        else:
            print("  注意：ChatGPT.app 当前正在运行；需要重启 App 才能读取新配置。")
    else:
        print("  ChatGPT.app 当前未运行；下次启动时将使用此渠道。")


def deactivate_channel(*, restart_app: bool = False) -> None:
    """Disable the managed channel without deleting its record or Keychain secret."""
    data = load_channels()
    info = current_config_info()
    current_slug = next(
        (slug for slug in data.get("channels", {}) if provider_id(slug) == info["provider_id"]),
        data.get("active"),
    )
    _remember_fallback(data, info)
    fallback = data.get("fallback") if isinstance(data.get("fallback"), dict) else {}
    fallback_provider = _valid_setting(fallback.get("model_provider"), "openai")
    if fallback_provider in _managed_provider_ids(data):
        fallback_provider = "openai"
    fallback_model = _valid_setting(fallback.get("model"), _valid_setting(info.get("model"), DEFAULT_MODEL))
    fallback_effort = _valid_setting(fallback.get("model_reasoning_effort"), DEFAULT_EFFORT)
    if fallback_effort not in EFFORTS:
        fallback_effort = DEFAULT_EFFORT

    fallback_catalog = _valid_setting(fallback.get("model_catalog_json"), "models_cache.json")
    values = {
        "model_provider": fallback_provider,
        "model": fallback_model,
        "model_reasoning_effort": fallback_effort,
        "model_catalog_json": fallback_catalog,
    }
    backup = backup_config()
    write_config(update_top_level(read_config(), values))
    for item in data.get("channels", {}).values():
        item["enabled"] = False
    if current_slug in data.get("channels", {}):
        data["channels"][current_slug]["last_disabled_at"] = datetime.now(timezone.utc).isoformat()
    data["codex_active"] = None
    data["active"] = None
    save_channels(data)
    if backup:
        print(f"已备份配置：{backup}")
    if current_slug:
        print(f"已取消启用渠道：{current_slug}（渠道和 API Key 都已保留，可随时再次启用）。")
    else:
        print("当前没有启用由本脚本管理的渠道；已恢复备用配置。")
    print(f"  替代 provider：{fallback_provider}\n  Model：{fallback_model}\n  Reasoning：{fallback_effort}\n  Model catalog：{fallback_catalog}")
    print("  已运行的会话不会热切换；下次打开或重启 ChatGPT.app 后使用替代配置。")
    if restart_app and chatgpt_is_running():
        restart_chatgpt(ask=False)


def switch_login(model: str | None = None, effort: str | None = None) -> None:
    if effort is not None and effort not in EFFORTS:
        die(f"推理强度必须是：{', '.join(EFFORTS)}")
    info = current_config_info()
    selected_effort = effort or _valid_setting(info.get("reasoning_effort"), DEFAULT_EFFORT)
    if selected_effort not in EFFORTS:
        selected_effort = DEFAULT_EFFORT
    values = {
        "model_provider": "openai",
        "model": model or _valid_setting(info.get("model"), DEFAULT_MODEL),
        "model_reasoning_effort": selected_effort,
    }
    backup = backup_config()
    write_config(update_top_level(read_config(), values))
    data = load_channels()
    for item in data.get("channels", {}).values():
        item["enabled"] = False
    data["codex_active"] = None
    data["active"] = None
    data["fallback"] = {
        **values,
        "saved_at": datetime.now(timezone.utc).isoformat(),
    }
    save_channels(data)
    if backup:
        print(f"已备份配置：{backup}")
    print("已切换到官方登录渠道；没有删除 auth.json，也没有删除任何已保存渠道。")
    print(f"  Model：{values['model']}\n  Reasoning：{values['model_reasoning_effort']}")
    print("注意：已经运行的 ChatGPT/Codex 会话不会热加载配置；重启 ChatGPT 后生效。")


def add_channel(args: argparse.Namespace, *, configure: bool = True) -> None:
    data = load_channels()
    name = args.name or input("渠道名称（如：公司网关）：").strip()
    if not name:
        die("渠道名称不能为空。")
    slug = slugify(args.slug or name)
    base_url = (args.base_url or input("Base URL（例如 https://api.example.com/v1）：").strip()).rstrip("/")
    if not re.match(r"^https?://", base_url, re.I):
        die("Base URL 必须以 http:// 或 https:// 开头。")

    api_key = getpass.getpass("API Key（不会回显，保存到 macOS Keychain）：").strip()
    if not api_key:
        die("API Key 不能为空。")
    print(f"正在从 {models_url(base_url)} 自动拉取模型列表……")
    models, error = fetch_models(base_url, api_key)
    if error:
        print(f"模型自动拉取失败：{error}")
        if configure:
            if not confirm_action("模型拉取失败，仍然手动输入模型名？"):
                print("已取消；API Key 尚未保存。")
                return
        elif not confirm_action("模型拉取失败，仍然保存渠道并稍后在工具菜单中配置模型？"):
            print("已取消；API Key 尚未保存。")
            return
    else:
        print(f"模型列表拉取成功，共 {len(models)} 个模型。")

    old = data.get("channels", {}).get(slug)
    old_models = _normalized_values(_channel_value(old, "codex", "selected_models")) if old else []
    old_efforts = _normalized_values(_channel_value(old, "codex", "selected_efforts"), EFFORTS) if old else []
    default_model = args.model or (_channel_value(old, "codex", "model") if old else None)
    default_effort = args.reasoning or (_channel_value(old, "codex", "reasoning_effort") if old else DEFAULT_EFFORT)
    if configure:
        if args.model:
            model_pick = (old_models or [args.model], args.model)
        else:
            model_pick = choose_models(models, defaults=old_models, default_value=default_model)
        if not model_pick:
            print("已取消；没有选择模型，API Key 尚未保存。")
            return
        selected_models, model = model_pick

        if args.reasoning:
            effort_pick = (old_efforts or [args.reasoning], args.reasoning)
        else:
            effort_pick = choose_efforts(defaults=old_efforts or list(EFFORTS), default_value=default_effort)
        if not effort_pick:
            print("已取消；没有选择推理强度，API Key 尚未保存。")
            return
        selected_efforts, effort = effort_pick
    else:
        model = default_model or (models[0] if models else None)
        selected_models = old_models
        effort = default_effort if default_effort in EFFORTS else DEFAULT_EFFORT
        selected_efforts = old_efforts

    if old and not args.force:
        if not confirm_action(f"渠道 {slug} 已存在，覆盖它？"):
            print("已取消；API Key 尚未写入。")
            return
    channel = dict(old or {})
    channel.update({
        "slug": slug, "name": name, "base_url": base_url,
        "model": model, "reasoning_effort": effort,
        "selected_models": selected_models, "selected_efforts": selected_efforts,
        "models": models, "models_fetched_at": datetime.now(timezone.utc).isoformat(),
        "created_at": old.get("created_at") if old else datetime.now(timezone.utc).isoformat(),
        "enabled": bool(old and old.get("enabled")),
        "opencode_enabled": bool(old and old.get("opencode_enabled")),
    })
    keychain_set(slug, api_key)
    data.setdefault("channels", {})[slug] = channel
    save_channels(data)
    print(f"已保存渠道：{name} ({slug})，默认模型：{model}，已缓存模型：{len(models)} 个")
    if not configure:
        print("  尚未选择工具专属的模型和推理强度；请进入 Codex 或 OpenCode 菜单完成配置。")
    if args.use:
        select_channel(
            slug, model=model, effort=effort,
            selected_models=selected_models, selected_efforts=selected_efforts,
            restart_app=args.restart_app,
        )
    else:
        print(f"执行 `sgate use {slug}` 即可切换。")


def refresh_models(
    slug: str,
    *,
    choose: bool = True,
    restart_app: bool = False,
    runtime: str = "codex",
) -> None:
    data = load_channels()
    channel = data.get("channels", {}).get(slug)
    if not channel:
        die(f"渠道不存在：{slug}")
    key = keychain_get(slug)
    print(f"正在从 {models_url(channel['base_url'])} 拉取模型列表……")
    models, error = fetch_models(channel["base_url"], key)
    if error:
        die(f"模型拉取失败：{error}")
    channel["models"] = models
    channel["models_fetched_at"] = datetime.now(timezone.utc).isoformat()
    selected_models = _normalized_values(_channel_value(channel, runtime, "selected_models"))
    default_model = str(_channel_value(channel, runtime, "model") or DEFAULT_MODEL)
    if choose:
        picked = choose_models(models, defaults=selected_models, default_value=default_model)
        if picked is None:
            print("已取消模型变更；仅更新模型缓存。")
            save_channels(data)
            return
        selected_models, default_model = picked
        channel[f"{runtime}_selected_models"] = selected_models
        channel[f"{runtime}_model"] = default_model
    save_channels(data)
    if runtime == "opencode":
        is_active = current_opencode_info(strict=False)["provider_id"] == opencode_provider_id(slug)
    else:
        is_active = current_config_info()["provider_id"] == provider_id(slug)
    if choose and is_active:
        if runtime == "opencode":
            if confirm_action(
                f"应用 OpenCode 模型选择：默认 {default_model} / 共 {len(selected_models)} 个模型？",
                default=True,
            ):
                select_opencode_channel(
                    slug,
                    model=default_model,
                    effort=_channel_value(channel, "opencode", "reasoning_effort"),
                    selected_models=selected_models,
                    selected_efforts=_normalized_values(
                        _channel_value(channel, "opencode", "selected_efforts"), EFFORTS
                    ),
                )
            else:
                print("模型缓存和候选已保存，但当前 OpenCode 配置没有改动。")
        else:
            if confirm_action(
                f"应用模型选择并立即重启 ChatGPT：默认 {default_model} / 共 {len(selected_models)} 个模型？",
                default=True,
            ):
                select_channel(
                    slug,
                    model=default_model,
                    effort=_channel_value(channel, "codex", "reasoning_effort"),
                    selected_models=selected_models,
                    selected_efforts=_normalized_values(_channel_value(channel, "codex", "selected_efforts"), EFFORTS),
                    restart_app=restart_app,
                )
            else:
                print("模型缓存和候选已保存，但当前 config.toml 没有改动。")
    else:
        print(f"已更新 {len(models)} 个模型，默认：{default_model}，候选：{len(selected_models)} 个")
        if not is_active:
            target = "OpenCode" if runtime == "opencode" else "Codex"
            print(f"该渠道当前未在 {target} 启用；下次启用时会使用这些候选模型。")


def choose_channel(title: str = "选择渠道", *, runtime: str = "codex") -> str | None:
    data = load_channels()
    channels = data.get("channels", {})
    if not channels:
        print("还没有 API Key 渠道，请先添加。")
        return None
    codex_actual = current_config_info()["provider_id"] if runtime in ("codex", "all") else ""
    opencode_actual = current_opencode_info(strict=False)["provider_id"] if runtime in ("opencode", "all") else ""
    claude_actual = load_channels().get("claude_active") if runtime in ("claude", "all") else None
    options = []
    for slug, channel in channels.items():
        active_labels = []
        if runtime in ("codex", "all") and provider_id(slug) == codex_actual:
            active_labels.append("Codex")
        if runtime in ("opencode", "all") and opencode_provider_id(slug) == opencode_actual:
            active_labels.append("OpenCode")
        if runtime in ("claude", "all") and slug == claude_actual:
            active_labels.append("Claude Code")
        active = f"  [当前：{' / '.join(active_labels)}]" if active_labels else ""
        model = _channel_value(channel, runtime, "model") if runtime in ("opencode", "claude") else channel.get("model")
        options.append((slug, f"{channel.get('name', slug)} ({slug}) · {model or '(未选)'}{active}"))
    return terminal_menu(title, options)


def configure_and_enable_channel(slug: str, *, restart_app: bool = True) -> None:
    data = load_channels()
    channel = data.get("channels", {}).get(slug)
    if not channel:
        die(f"渠道不存在：{slug}")
    info = current_config_info()
    is_active = info["provider_id"] == provider_id(slug)
    models = channel.get("models", []) if isinstance(channel.get("models"), list) else []
    default_model = info["model"] if is_active and info["model"] in models else channel.get("model")
    default_effort = info["reasoning_effort"] if is_active and info["reasoning_effort"] in EFFORTS else channel.get("reasoning_effort")
    selected_models = _normalized_values(channel.get("selected_models"))
    selected_efforts = _normalized_values(channel.get("selected_efforts"), EFFORTS)
    if not selected_models:
        selected_models = [name for name in models if not any(
            token in name.casefold() for token in ("image", "dall-e", "embedding", "whisper", "tts", "audio", "moderation")
        )]
    if not selected_efforts:
        selected_efforts = list(EFFORTS)

    model_pick = choose_models(models, defaults=selected_models, default_value=default_model)
    if model_pick is None:
        print("已取消切换。")
        return
    selected_models, model = model_pick
    effort_pick = choose_efforts(defaults=selected_efforts, default_value=default_effort)
    if effort_pick is None:
        print("已取消切换。")
        return
    selected_efforts, effort = effort_pick
    message = (
        f"应用配置并立即重启 ChatGPT：{channel.get('name', slug)} / 默认 {model} / {effort} / "
        f"{len(selected_models)} 个模型 / {len(selected_efforts)} 个强度？"
    )
    if not confirm_action(message, default=True):
        print("已取消切换，配置没有改动。")
        return
    select_channel(
        slug, model=model, effort=effort,
        selected_models=selected_models, selected_efforts=selected_efforts,
        restart_app=restart_app,
    )


def configure_current(*, restart_app: bool = True) -> None:
    info = current_config_info()
    data = load_channels()
    slug = next((slug for slug in data.get("channels", {}) if provider_id(slug) == info["provider_id"]), None)
    if slug:
        configure_and_enable_channel(slug, restart_app=restart_app)
        return
    model = input(f"当前是非脚本渠道，请输入模型 [{info['model']}]（回车保持）：").strip() or info["model"]
    effort = choose_effort(info.get("reasoning_effort"))
    if effort is None:
        print("已取消修改。")
        return
    if info["provider_id"] == "openai":
        switch_login(model, effort)
    else:
        backup = backup_config()
        write_config(update_top_level(read_config(), {"model": model, "model_reasoning_effort": effort}))
        if backup:
            print(f"已备份配置：{backup}")
        print(f"已更新当前 provider 的 Model={model}, Reasoning={effort}。重启 App 后生效。")


def print_current_status() -> None:
    info = current_config_info()
    data = load_channels()
    provider_id_value = info["provider_id"]
    matched = next((c for c in data.get("channels", {}).values() if provider_id(c.get("slug", "")) == provider_id_value), None)
    print_heading("Codex 当前配置")
    print_field("CODEX_HOME", CODEX_HOME)
    print_field("config.toml", CONFIG_PATH)
    print_field("provider", provider_id_value, tone="accent")
    if provider_id_value == "openai":
        print_field("渠道类型", "官方登录", tone="ok")
    elif matched:
        print_field("渠道名称", f"{matched.get('name', matched.get('slug'))} ({matched.get('slug')})", tone="ok")
        print_field("Base URL", matched.get("base_url", "(未记录)"))
    else:
        provider = info["provider"]
        print_field("渠道类型", "未登记的已有 provider", tone="warn")
        print_field("名称", provider.get("name", "(未设置)"))
        print_field("Base URL", provider.get("base_url", "(未设置)"))
        print_note("这通常是手工配置或旧版配置；脚本不会读取 auth.json 中的密钥。")
    print_field("默认模型", info["model"], tone="accent")
    print_field("思考强度", info["reasoning_effort"], tone="accent")
    print_field("已存渠道", f"{len(data.get('channels', {}))} 个")
    cc = ccswitch_current_info()
    if cc:
        print_field("CC Switch", f"{cc['name']} | provider={cc['provider_id']} | model={cc['model']}")
        if ccswitch_is_running():
            print_note("CC Switch 正在运行；以后在其中切换可能覆盖本文件。", kind="warn")
    print_note("以上 provider/模型/强度就是 Codex 下次启动使用的值。", kind="ok")


def list_channels() -> None:
    print_current_status()
    print_opencode_status(strict=False)
    data = load_channels()
    channels = data.get("channels", {})
    if not channels:
        print()
        print_note("暂无由本脚本管理的渠道。执行 `sgate add` 或在渠道管理中新增。", kind="warn")
        return
    codex_actual = current_config_info()["provider_id"]
    opencode_info = current_opencode_info(strict=False)
    opencode_managed = set(opencode_info["managed_providers"])
    claude_active = data.get("claude_active")
    print_heading("已保存的渠道", f"共 {len(channels)} 个")
    rows = []
    for slug, channel in channels.items():
        codex_on = provider_id(slug) == codex_actual
        oc_pid = opencode_provider_id(slug)
        oc_default = oc_pid == opencode_info["provider_id"]
        oc_on = oc_pid in opencode_managed
        codex_model = _channel_value(channel, "codex", "model") or "(未选)"
        codex_effort = _channel_value(channel, "codex", "reasoning_effort") or DEFAULT_EFFORT
        opencode_model = _channel_value(channel, "opencode", "model") or "(未选)"
        opencode_effort = _channel_value(channel, "opencode", "reasoning_effort") or DEFAULT_EFFORT
        if oc_default:
            oc_state = green(f"{ICON_ON} 默认")
        elif oc_on:
            oc_state = cyan(f"{ICON_ON} 启用")
        else:
            oc_state = dim(f"{ICON_OFF} 未用")
        rows.append([
            channel.get("name", slug),
            slug,
            green(f"{ICON_ON} 启用") if codex_on else dim(f"{ICON_OFF} 未用"),
            f"{codex_model}/{codex_effort}",
            oc_state,
            f"{opencode_model}/{opencode_effort}",
            str(len(channel.get("models", []))),
        ])
    print_table(
        ["名称", "slug", "Codex", "Codex 配置", "OpenCode", "OpenCode 配置", "模型"],
        rows,
    )
    print()
    claude_rows = []
    for slug, channel in channels.items():
        _, claude_runtime = _anthropic_profile_sections(channel)
        claude_map = claude_runtime.get("model_map") if isinstance(claude_runtime.get("model_map"), dict) else {}
        claude_effort = str(claude_runtime.get("effort") or "(未选)")
        mapping_text = ", ".join(f"{role}→{claude_map.get(role, '?')}" for role in CLAUDE_ROLES)
        is_active = slug == claude_active
        claude_rows.append([
            channel.get("name", slug),
            slug,
            green(f"{ICON_ON} 当前") if is_active else dim(f"{ICON_OFF} 未用"),
            f"{mapping_text} / {claude_effort}",
        ])
    print_heading("Claude Code 渠道", f"当前：{claude_active or '未启用'}")
    print_table(["名称", "slug", "状态", "alias → 网关模型 / effort"], claude_rows)
    print_note("Claude Desktop 不提供自定义 API provider；请通过 Desktop 账户、Code tab 和 MCP 设置管理。")


def remove_channel(slug: str) -> None:
    data = load_channels()
    channel = data.get("channels", {}).get(slug)
    if not channel:
        die(f"渠道不存在：{slug}")
    if not confirm_action(f"确认永久删除渠道 {channel.get('name', slug)} 及其 Keychain 密钥？"):
        print("已取消。")
        return
    codex_current = current_config_info()["provider_id"] == provider_id(slug)
    opencode_current = current_opencode_info(strict=False)["provider_id"] == opencode_provider_id(slug)
    if codex_current:
        deactivate_channel()
        data = load_channels()
    if opencode_config_is_valid():
        if opencode_current:
            deactivate_opencode_channel(slug)
            data = load_channels()
        else:
            remove_opencode_provider(slug)
    elif opencode_current:
        print("警告：OpenCode 配置损坏，已跳过配置清理；请修复后手动删除该渠道的 OpenCode provider。")
    if data.get("claude_active") == slug:
        deactivate_claude_code_channel(slug, quiet=True)
        data = load_channels()
    try:
        opencode_credentials_path(slug).unlink()
    except FileNotFoundError:
        pass
    data["channels"].pop(slug, None)
    save_channels(data)
    keychain_delete(slug)
    print_note(f"已删除渠道 {slug} 及其 Keychain 密钥。", kind="ok")


def doctor(slug: str | None) -> None:
    data = load_channels()
    slug = slug or next((s for s, c in data.get("channels", {}).items() if provider_id(s) == current_config_info()["provider_id"]), None)
    if not slug:
        print_current_status()
        print("当前不是本脚本管理的 API Key 渠道，未执行 Keychain 检查。")
        return
    channel = data.get("channels", {}).get(slug)
    if not channel:
        die(f"渠道不存在：{slug}")
    key = keychain_get(slug)
    url = models_url(channel["base_url"])
    print_heading("渠道连接检查", f"{channel['name']} ({slug})")
    print_field("请求地址", url)
    models, error = fetch_models(channel["base_url"], key)
    if error:
        print_note(f"模型接口失败：{error}", kind="err")
        die(f"模型接口失败：{error}")
    channel["models"] = models
    channel["models_fetched_at"] = datetime.now(timezone.utc).isoformat()
    save_channels(data)
    print_field("HTTP", "200", tone="ok")
    print_field("模型数量", f"{len(models)} 个", tone="ok")
    print()
    print_table(
        ["工具", "默认模型", "思考强度"],
        [
            ["Codex",
             str(_channel_value(channel, "codex", "model") or "(未选择)"),
             str(_channel_value(channel, "codex", "reasoning_effort") or DEFAULT_EFFORT)],
            ["OpenCode",
             str(_channel_value(channel, "opencode", "model") or "(未选择)"),
             str(_channel_value(channel, "opencode", "reasoning_effort") or DEFAULT_EFFORT)],
        ],
    )
    print_note("模型缓存已更新，可在各工具菜单中重新勾选。", kind="ok")


def executable_version(path: str) -> str:
    try:
        proc = subprocess.run([path, "--version"], text=True, capture_output=True, timeout=8)
        return (proc.stdout or proc.stderr).strip().splitlines()[0]
    except Exception as exc:
        return f"不可用：{exc}"


def diagnose() -> None:
    print_current_status()
    print("\n运行时检查")
    print(f"  当前 PATH 中 codex：{shutil.which('codex') or '(未找到)'}")
    if shutil.which("codex"):
        print(f"  standalone 版本：{executable_version(shutil.which('codex') or 'codex')}")
    print(f"  ChatGPT.app 内置 codex：{CHATGPT_CODEX if CHATGPT_CODEX.exists() else '(未找到)'}")
    if CHATGPT_CODEX.exists():
        print(f"  ChatGPT.app Codex 版本：{executable_version(str(CHATGPT_CODEX))}")
    cc = ccswitch_current_info()
    if cc:
        cc_state = "正在运行" if ccswitch_is_running() else "未运行"
        print(f"  CC Switch：{cc_state}，当前记录为 {cc['name']} / provider={cc['provider_id']} / model={cc['model']}")
        active = current_config_info()
        if cc["provider_id"] != active["provider_id"] or cc["model"] != active["model"]:
            print("  CC Switch 与 config.toml 不一致：当前实际 Codex 配置以 config.toml 为准；不要在 CC Switch 中再次切换，否则可能覆盖本次结果。")
    print("\n作用判断")
    print("  脚本修改的是 CODEX_HOME/config.toml；当前 CODEX_HOME 为上面显示的路径。")
    print("  ChatGPT.app 正在运行时，已有 app-server 不会自动重新读取配置。")
    print("  切换后请关闭并重新打开 ChatGPT.app，再从 Codex 页面新建会话。")
    print("  若要验证内置 Codex 是否能读取配置：sgate app-doctor")


def app_doctor() -> None:
    if not CHATGPT_CODEX.exists():
        die(f"找不到 ChatGPT.app 内置 Codex：{CHATGPT_CODEX}")
    print(f"正在用 ChatGPT.app 内置 Codex 检查：{CHATGPT_CODEX}")
    proc = subprocess.run(
        [str(CHATGPT_CODEX), "--strict-config", "doctor"],
        text=True, capture_output=True,
    )
    output = proc.stdout or proc.stderr
    interesting = []
    for line in output.splitlines():
        if any(token in line for token in ("Codex Doctor", "version", "default model provider", "model provider", "model                    ", "config.toml parse", "auth")):
            interesting.append(line)
    print("\n".join(interesting[-30:]) if interesting else output[-4000:])
    if proc.returncode != 0:
        print(f"\n注意：doctor 返回码为 {proc.returncode}，这通常表示网络/渠道可达性检查失败；上面的 config.toml parse/provider 结果仍然有效。")


def process_matches(pattern: str) -> bool:
    try:
        proc = subprocess.run(["pgrep", "-f", pattern], text=True, capture_output=True)
    except FileNotFoundError:
        return False
    return proc.returncode == 0


def chatgpt_pids() -> list[int]:
    try:
        proc = subprocess.run(["ps", "-axo", "pid=,command="], text=True, capture_output=True)
    except FileNotFoundError:
        return []
    target = "/Applications/ChatGPT.app/Contents/MacOS/ChatGPT"
    pids: list[int] = []
    for line in proc.stdout.splitlines():
        fields = line.strip().split(None, 1)
        if len(fields) == 2 and fields[0].isdigit() and fields[1] == target:
            pids.append(int(fields[0]))
    return pids


def chatgpt_is_running() -> bool:
    return bool(chatgpt_pids())


def ccswitch_is_running() -> bool:
    return process_matches(r"/Applications/CC Switch\.app/Contents/")


def restart_chatgpt(*, ask: bool = True) -> None:
    if ask:
        try:
            confirmed = confirm_action("关闭并重新打开 ChatGPT？这可能中断正在运行的会话。", default=True)
        except (KeyboardInterrupt, EOFError):
            print("\n未重启 ChatGPT；请稍后手动重启。")
            return
        if not confirmed:
            print("未重启 ChatGPT；请稍后手动重启。")
            return

    old_pids = set(chatgpt_pids())
    if old_pids:
        subprocess.run(["osascript", "-e", 'tell application "ChatGPT" to quit'], check=False, capture_output=True)
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline and old_pids.intersection(chatgpt_pids()):
            time.sleep(0.25)
        remaining = old_pids.intersection(chatgpt_pids())
        if remaining:
            subprocess.run(["kill", *[str(pid) for pid in sorted(remaining)]], check=False, capture_output=True)
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline and remaining.intersection(chatgpt_pids()):
                time.sleep(0.2)

    opened = subprocess.run(["open", "-a", "ChatGPT"], text=True, capture_output=True)
    if opened.returncode != 0:
        die((opened.stderr or opened.stdout or "无法启动 ChatGPT.app").strip())
    deadline = time.monotonic() + 20
    new_pids: set[int] = set()
    while time.monotonic() < deadline:
        new_pids = set(chatgpt_pids()) - old_pids
        if new_pids:
            break
        time.sleep(0.25)
    if not new_pids:
        die("已退出旧 ChatGPT.app，但没有检测到新的 ChatGPT 主进程。")
    print(f"已重启 ChatGPT.app；新 PID：{', '.join(map(str, sorted(new_pids)))}。新进程将读取刚写入的配置。")


def token_command(slug: str) -> None:
    # Used internally by Codex's model provider auth.command.
    sys.stdout.write(keychain_get(slug) + "\n")


def interactive() -> None:
    while True:
        try:
            info = current_config_info()
            data = load_channels()
            matched = next(
                (channel for slug, channel in data.get("channels", {}).items() if provider_id(slug) == info["provider_id"]),
                None,
            )
            channel_name = matched.get("name", matched.get("slug")) if matched else (
                "官方登录" if info["provider_id"] == "openai" else info["provider_id"]
            )
            title = (
                "Codex 渠道切换器\n"
                f"  当前：{channel_name} · {info['model']} · reasoning={info['reasoning_effort']}"
            )
            choice = terminal_menu(title, [
                ("use", "[切换] 启用已保存渠道，并选择模型 / 推理强度"),
                ("configure", "[配置] 修改当前模型 / 推理强度"),
                ("refresh", "[模型] 拉取最新列表，用 Space 选择 / 取消"),
                ("disable", "[停用] 取消当前渠道（保留配置和 API Key）"),
                ("login", "[官方] 切换到官方登录"),
                ("status", "[状态] 查看当前实际配置"),
                ("doctor", "[检查] 测试渠道连接"),
                ("diagnose", "[诊断] 检查 CLI 与 ChatGPT.app"),
                ("exit", "[退出] 关闭菜单"),
            ], default="use")
            if choice in (None, "exit"):
                return
            if choice == "use":
                slug = choose_channel("启用哪个渠道？")
                if slug:
                    configure_and_enable_channel(slug, restart_app=True)
            elif choice == "configure":
                configure_current()
            elif choice == "refresh":
                slug = choose_channel("刷新哪个渠道的模型？")
                if slug:
                    refresh_models(slug, restart_app=True)
            elif choice == "disable":
                if confirm_action("取消启用当前脚本渠道？配置和 API Key 会保留，可随时重新启用。"):
                    deactivate_channel(restart_app=True)
                else:
                    print("已返回，未修改配置。")
            elif choice == "login":
                effort = choose_effort(info.get("reasoning_effort"))
                if effort is not None:
                    switch_login(info.get("model"), effort)
                    if chatgpt_is_running():
                        restart_chatgpt(ask=False)
            elif choice == "status":
                list_channels()
            elif choice == "doctor":
                slug = choose_channel("检查哪个渠道？")
                if slug:
                    doctor(slug)
            elif choice == "diagnose":
                diagnose()
            pause_after_action()
        except (KeyboardInterrupt, EOFError):
            print("\n已退出。")
            return


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="sgate",
        description="切换 Codex、OpenCode、Claude Code 及 Claude Desktop Code tab 渠道；API Key 存储在 macOS Keychain。",
    )
    sub = parser.add_subparsers(dest="command")

    p_add = sub.add_parser("add", help="添加/更新渠道，并自动拉取模型列表")
    p_add.add_argument("name", nargs="?", help="显示名称")
    p_add.add_argument("--slug", help="稳定标识")
    p_add.add_argument("--base-url", help="OpenAI Responses API Base URL")
    p_add.add_argument("--model", help="跳过选择器，直接指定模型；不填则自动拉取并选择")
    p_add.add_argument("--reasoning", choices=EFFORTS, help="默认推理强度")
    p_add.add_argument("--force", action="store_true", help="覆盖已有渠道而不询问")
    p_add.add_argument("--use", action="store_true", help="添加后立即切换")
    p_add.add_argument("--restart-app", action="store_true", help="切换后立即重启 ChatGPT.app")

    sub.add_parser("channels", aliases=["channel"], help="打开外层渠道管理菜单")

    p_use = sub.add_parser("use", help="切换 API Key 渠道")
    p_use.add_argument("slug")
    p_use.add_argument("--model", help="指定模型")
    p_use.add_argument("--reasoning", choices=EFFORTS, help="指定推理强度")
    p_use.add_argument("--restart-app", action="store_true", help="切换后立即重启 ChatGPT.app")

    p_login = sub.add_parser("login", help="切回官方登录，不删除 auth.json")
    p_login.add_argument("--model")
    p_login.add_argument("--reasoning", choices=EFFORTS)
    p_login.add_argument("--restart-app", action="store_true", help="切换后立即重启 ChatGPT.app")

    p_disable = sub.add_parser("disable", aliases=["cancel", "deactivate"], help="取消启用当前脚本渠道，但保留渠道和 Keychain 密钥")
    p_disable.add_argument("--restart-app", action="store_true", help="停用后立即重启 ChatGPT.app")

    p_configure = sub.add_parser("configure", aliases=["config"], help="交互修改渠道的模型和推理强度")
    p_configure.add_argument("slug", nargs="?", help="渠道 slug；不填则修改当前配置")
    p_configure.add_argument("--restart-app", action="store_true", help="修改后立即重启 ChatGPT.app")

    sub.add_parser("list", aliases=["ls"], help="显示当前实际渠道和已保存渠道")
    sub.add_parser("status", aliases=["current"], help="只显示当前实际生效配置")

    p_refresh = sub.add_parser("refresh", help="重新拉取模型并重新选择")
    p_refresh.add_argument("slug")
    p_refresh.add_argument("--restart-app", action="store_true", help="选定后立即重启 ChatGPT.app")

    p_rm = sub.add_parser("remove", aliases=["rm"], help="删除渠道和 Keychain 密钥")
    p_rm.add_argument("slug")

    p_doctor = sub.add_parser("doctor", help="检查 API Key 渠道 /models")
    p_doctor.add_argument("slug", nargs="?")
    sub.add_parser("diagnose", help="检查 CLI、ChatGPT.app 和实际 config")
    sub.add_parser("app-doctor", help="用 ChatGPT.app 内置 Codex 检查 config")

    p_run = sub.add_parser("run", help="用当前全局配置启动 standalone codex")
    p_run.add_argument("args", nargs=argparse.REMAINDER)

    p_oc = sub.add_parser("opencode", help="交互配置 OpenCode 渠道（支持多渠道同时启用）")
    p_oc.add_argument(
        "action", nargs="?",
        choices=("use", "add", "configure", "status", "disable", "sync"),
        default=None,
        help="use=设为默认；add=启用但不设默认；disable=移除；sync=一次性指定全部启用渠道",
    )
    p_oc.add_argument("slug", nargs="*", help="渠道 slug；sync 可传多个")
    p_oc.add_argument("--model")
    p_oc.add_argument("--reasoning", choices=EFFORTS)
    p_oc.add_argument("--default", dest="default_slug", help="sync 时指定默认渠道")

    def add_claude_arguments(command: argparse.ArgumentParser) -> None:
        command.add_argument("slug", nargs="?")
        command.add_argument("--anthropic-base-url", help="独立 Anthropic API Base URL")
        command.add_argument(
            "--map", dest="model_maps", action="append", default=[], metavar="ROLE=MODEL",
            help="显式 alias 映射，可重复：opus=... / sonnet=... / haiku=...",
        )
        command.add_argument("--map-all", metavar="MODEL", help="将同一模型显式映射到三个 alias")
        command.add_argument("--model", help="兼容参数，等同 --map-all 并打印提示")
        command.add_argument("--default-role", choices=CLAUDE_ROLES)
        command.add_argument("--effort", choices=CLAUDE_EFFORTS)
        command.add_argument(
            "--auth-mode", default=None,
            choices=("api_key_helper", "auth_token", "api_key", "bearer", "plaintext"),
            help="持久配置仅支持 api_key_helper；其他模式会明确拒绝",
        )

    p_cc = sub.add_parser("claude-code", help="切换 Claude Code Anthropic 渠道与 alias 映射")
    p_cc.add_argument("action", nargs="?", choices=("use", "configure", "config", "disable", "status"), default=None)
    add_claude_arguments(p_cc)

    p_cd = sub.add_parser("claude-desktop", help="配置 Desktop Code tab 或只读查看 MCP 状态")
    p_cd.add_argument("action", nargs="?", choices=("use", "configure", "config", "disable", "status"), default=None)
    add_claude_arguments(p_cd)

    p_token = sub.add_parser("token", help=argparse.SUPPRESS)
    p_token.add_argument("slug")

    p_claude_token = sub.add_parser("claude-token", help=argparse.SUPPRESS)
    p_claude_token.add_argument("slug")
    return parser


def main() -> None:
    args = build_parser().parse_args()
    if not args.command:
        engine_interactive()
    elif args.command in ("channels", "channel"):
        channel_management()
    elif args.command == "add":
        add_channel(args)
    elif args.command == "use":
        select_channel(args.slug, model=args.model, effort=args.reasoning, restart_app=args.restart_app)
    elif args.command == "login":
        switch_login(args.model, args.reasoning)
        if args.restart_app and chatgpt_is_running():
            restart_chatgpt(ask=False)
    elif args.command in ("disable", "cancel", "deactivate"):
        deactivate_channel(restart_app=args.restart_app)
    elif args.command in ("configure", "config"):
        if args.slug:
            configure_and_enable_channel(args.slug, restart_app=args.restart_app)
        else:
            configure_current(restart_app=args.restart_app)
    elif args.command in ("list", "ls"):
        list_channels()
    elif args.command in ("status", "current"):
        print_current_status()
    elif args.command == "refresh":
        refresh_models(args.slug, restart_app=args.restart_app)
    elif args.command in ("remove", "rm"):
        remove_channel(args.slug)
    elif args.command == "doctor":
        doctor(args.slug)
    elif args.command == "diagnose":
        diagnose()
    elif args.command == "app-doctor":
        app_doctor()
    elif args.command == "run":
        os.execvp("codex", ["codex", *args.args])
    elif args.command == "opencode":
        slugs = args.slug or []
        if args.action == "status":
            print_opencode_status(strict=False)
        elif args.action in ("use", "add"):
            if not slugs:
                die(f"OpenCode {args.action} 需要渠道 slug")
            select_opencode_channel(
                slugs[0], model=args.model, effort=args.reasoning,
                make_default=(args.action == "use"),
            )
        elif args.action == "disable":
            if not slugs:
                die("OpenCode disable 需要渠道 slug")
            deactivate_opencode_channel(slugs[0])
        elif args.action == "sync":
            if not slugs:
                die("OpenCode sync 需要至少一个渠道 slug")
            sync_opencode_channels(slugs, args.default_slug)
        elif args.action == "configure":
            configure_opencode_channel()
        else:
            opencode_interactive()
    elif args.command in ("claude-code", "claude-desktop"):
        model_map = _parse_model_map(args.model_maps, args.map_all)
        if args.model:
            if model_map:
                die("--model 与 --map/--map-all 不能同时使用")
            print_note("--model 是兼容参数，本次会显式 map-all 到 opus/sonnet/haiku。", kind="warn")
            model_map = {role: args.model for role in CLAUDE_ROLES}
        options = {
            "anthropic_base_url": args.anthropic_base_url,
            "model_map": model_map,
            "default_role": args.default_role,
            "effort": args.effort,
            "auth_mode": args.auth_mode,
        }
        if args.action == "status":
            print_claude_code_status() if args.command == "claude-code" else claude_desktop_status()
        elif args.action == "use":
            if not args.slug:
                die(f"{'Claude Code' if args.command == 'claude-code' else 'Claude Desktop'} use 需要渠道 slug")
            if args.command == "claude-code":
                select_claude_code_channel(args.slug, **options)
            else:
                select_claude_desktop_channel(args.slug, **options)
        elif args.action in ("configure", "config"):
            configure_claude_code_channel(args.slug, **options)
        elif args.action == "disable":
            deactivate_claude_code_channel(args.slug)
        elif args.command == "claude-code":
            claude_code_interactive()
        else:
            claude_desktop_interactive()
    elif args.command == "token":
        token_command(args.slug)
    elif args.command == "claude-token":
        sys.stdout.write(keychain_get(args.slug) + "\n")


if __name__ == "__main__":
    main()
