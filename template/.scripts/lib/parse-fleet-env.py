#!/usr/bin/env python3
"""Parse the supported fleet.env assignment grammar without executing it."""

from __future__ import annotations

import re
import sys
from pathlib import Path


HEADER = b"PJANGLER_FLEET_ENV_V1"
FOOTER = b"PJANGLER_FLEET_ENV_END"
NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
ASSIGNMENT = re.compile(r"[ \t]*(?:export[ \t]+)?([A-Za-z_][A-Za-z0-9_]*)=")


class FleetEnvParseError(ValueError):
    def __init__(self, line: int, message: str) -> None:
        super().__init__(f"line {line}: {message}")


def line_number(text: str, offset: int) -> int:
    return text.count("\n", 0, offset) + 1


def line_end(text: str, offset: int) -> int:
    end = text.find("\n", offset)
    return len(text) if end < 0 else end


def next_line(text: str, end: int) -> int:
    return end if end == len(text) else end + 1


def validate_suffix(text: str, offset: int, origin: int) -> int:
    end = line_end(text, offset)
    suffix = text[offset:end]
    if not re.fullmatch(r"[ \t]*(?:#.*)?", suffix):
        raise FleetEnvParseError(
            line_number(text, origin), "unexpected content after quoted value"
        )
    return next_line(text, end)


def parse_single_quoted(text: str, offset: int) -> tuple[str, int]:
    origin = offset
    closing = text.find("'", offset + 1)
    if closing < 0:
        raise FleetEnvParseError(line_number(text, origin), "unterminated single quote")
    return text[offset + 1 : closing], validate_suffix(text, closing + 1, origin)


def parse_double_quoted(text: str, offset: int) -> tuple[str, int]:
    origin = offset
    cursor = offset + 1
    value: list[str] = []
    while cursor < len(text):
        char = text[cursor]
        if char == '"':
            return "".join(value), validate_suffix(text, cursor + 1, origin)
        if char in {"$", "`"}:
            raise FleetEnvParseError(
                line_number(text, cursor),
                "dynamic expansion is not supported in fleet.env",
            )
        if char != "\\":
            value.append(char)
            cursor += 1
            continue
        if cursor + 1 >= len(text):
            raise FleetEnvParseError(line_number(text, cursor), "unterminated escape")
        escaped = text[cursor + 1]
        if escaped == "\n":
            cursor += 2
            continue
        if escaped in {'"', "\\", "$", "`"}:
            value.append(escaped)
        else:
            value.extend(("\\", escaped))
        cursor += 2
    raise FleetEnvParseError(line_number(text, origin), "unterminated double quote")


ANSI_ESCAPES = {
    "a": "\a",
    "b": "\b",
    "e": "\x1b",
    "E": "\x1b",
    "f": "\f",
    "n": "\n",
    "r": "\r",
    "t": "\t",
    "v": "\v",
    "\\": "\\",
    "'": "'",
    '"': '"',
    "?": "?",
}


def parse_hex_escape(text: str, offset: int, maximum: int) -> tuple[str, int]:
    cursor = offset
    while (
        cursor < len(text)
        and cursor - offset < maximum
        and text[cursor] in "0123456789abcdefABCDEF"
    ):
        cursor += 1
    if cursor == offset:
        raise FleetEnvParseError(line_number(text, offset), "empty hexadecimal escape")
    return chr(int(text[offset:cursor], 16)), cursor


def parse_ansi_c_quoted(text: str, offset: int) -> tuple[str, int]:
    origin = offset
    cursor = offset + 2
    value: list[str] = []
    while cursor < len(text):
        char = text[cursor]
        if char == "'":
            parsed = "".join(value)
            if "\0" in parsed:
                raise FleetEnvParseError(
                    line_number(text, origin), "NUL is not allowed"
                )
            return parsed, validate_suffix(text, cursor + 1, origin)
        if char != "\\":
            value.append(char)
            cursor += 1
            continue
        if cursor + 1 >= len(text):
            raise FleetEnvParseError(line_number(text, cursor), "unterminated escape")
        escaped = text[cursor + 1]
        if escaped == "\n":
            cursor += 2
            continue
        if escaped in ANSI_ESCAPES:
            value.append(ANSI_ESCAPES[escaped])
            cursor += 2
            continue
        if escaped == "x":
            decoded, cursor = parse_hex_escape(text, cursor + 2, 2)
            value.append(decoded)
            continue
        if escaped in {"u", "U"}:
            width = 4 if escaped == "u" else 8
            start = cursor + 2
            digits = text[start : start + width]
            if len(digits) != width or any(
                c not in "0123456789abcdefABCDEF" for c in digits
            ):
                raise FleetEnvParseError(
                    line_number(text, cursor), "invalid Unicode escape"
                )
            try:
                value.append(chr(int(digits, 16)))
            except ValueError as error:
                raise FleetEnvParseError(
                    line_number(text, cursor), "invalid Unicode code point"
                ) from error
            cursor = start + width
            continue
        if escaped in "01234567":
            start = cursor + 1
            end = start
            while end < len(text) and end - start < 3 and text[end] in "01234567":
                end += 1
            value.append(chr(int(text[start:end], 8)))
            cursor = end
            continue
        value.extend(("\\", escaped))
        cursor += 2
    raise FleetEnvParseError(line_number(text, origin), "unterminated ANSI-C quote")


def parse_unquoted(text: str, offset: int) -> tuple[str, int]:
    end = line_end(text, offset)
    raw = text[offset:end]
    comment = re.search(r"[ \t]+#", raw)
    if comment:
        raw = raw[: comment.start()]
    if not raw:
        return "", next_line(text, end)
    if re.search(r"[ \t;&|<>()`$\\'\"]", raw):
        raise FleetEnvParseError(
            line_number(text, offset), "unquoted value contains shell syntax"
        )
    return raw, next_line(text, end)


def parse_value(text: str, offset: int) -> tuple[str, int]:
    if offset >= len(text) or text[offset] == "\n":
        end = line_end(text, offset)
        return "", next_line(text, end)
    if text.startswith("$'", offset):
        return parse_ansi_c_quoted(text, offset)
    if text[offset] == "'":
        return parse_single_quoted(text, offset)
    if text[offset] == '"':
        return parse_double_quoted(text, offset)
    return parse_unquoted(text, offset)


def parse(text: str) -> list[tuple[str, str]]:
    if "\0" in text:
        raise FleetEnvParseError(1, "NUL is not allowed")
    if "\r" in text:
        text = text.replace("\r\n", "\n")
        if "\r" in text:
            raise FleetEnvParseError(1, "bare carriage return is not supported")

    cursor = 0
    records: list[tuple[str, str]] = []
    seen: set[str] = set()
    while cursor < len(text):
        end = line_end(text, cursor)
        physical = text[cursor:end]
        if not physical.strip() or physical.lstrip().startswith("#"):
            cursor = next_line(text, end)
            continue
        match = ASSIGNMENT.match(text, cursor)
        if not match or match.end() > end:
            raise FleetEnvParseError(
                line_number(text, cursor), "expected KEY=value or export KEY=value"
            )
        key = match.group(1)
        if not NAME.fullmatch(key):
            raise FleetEnvParseError(line_number(text, cursor), "invalid variable name")
        if key in seen:
            raise FleetEnvParseError(
                line_number(text, cursor), f"duplicate variable {key}"
            )
        value, cursor = parse_value(text, match.end())
        seen.add(key)
        records.append((key, value))
    return records


def main() -> int:
    if len(sys.argv) != 2:
        print("usage: parse-fleet-env.py PATH", file=sys.stderr)
        return 2
    path = Path(sys.argv[1])
    try:
        text = path.read_text(encoding="utf-8")
        records = parse(text)
    except (OSError, UnicodeError, FleetEnvParseError) as error:
        print(f"fleet environment parse error: {error}", file=sys.stderr)
        return 2

    framed = [HEADER]
    framed.extend(f"{key}={value}".encode("utf-8") for key, value in records)
    framed.extend((FOOTER, b"", b""))
    sys.stdout.buffer.write(b"\0".join(framed))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
