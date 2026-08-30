"""A股代码与输入解析（从 stock-research inputs.py 原样迁移）。"""

from __future__ import annotations

import csv
from pathlib import Path
import re

from .errors import UserInputError
from .models import PositionInput, StockCode


CODE_RE = re.compile(r"^\d{6}$")


def code_to_ts_code(code: str) -> str:
    if not CODE_RE.match(code):
        raise UserInputError(f"Invalid stock code: {code}")
    if code.startswith(("6", "9", "4", "8", "5")):
        return f"{code}.SH"
    if code.startswith(("0", "2", "3")):
        return f"{code}.SZ"
    if code.startswith(("4", "8", "9")):
        return f"{code}.BJ"
    return f"{code}.SZ"


def normalize_code(raw: str) -> StockCode:
    value = (raw or "").strip().upper()
    if not value:
        raise UserInputError("Empty stock code")

    if "." in value:
        base, suffix = value.rsplit(".", 1)
        if suffix in {"SH", "SZ", "BJ"} and CODE_RE.match(base):
            return StockCode(raw=raw, code=base, ts_code=f"{base}.{suffix}")

    if value.startswith(("SH", "SZ", "BJ")) and len(value) == 8:
        base = value[2:]
        if CODE_RE.match(base):
            return StockCode(raw=raw, code=base, ts_code=code_to_ts_code(base))

    if value.isdigit() and len(value) == 7 and value.startswith(("0", "1")):
        value = value[1:]

    if CODE_RE.match(value):
        return StockCode(raw=raw, code=value, ts_code=code_to_ts_code(value))

    raise UserInputError(f"Unrecognized stock code: {raw}")


def parse_codes_arg(codes: str | None) -> tuple[list[StockCode], list[str]]:
    if not codes:
        return [], []
    return _normalize_many([item.strip() for item in codes.split(",") if item.strip()])


def parse_input_file(path: str | Path | None) -> tuple[list[StockCode], list[str]]:
    if path is None:
        return [], []
    input_path = Path(path)
    if not input_path.exists():
        raise UserInputError(f"Input file does not exist: {input_path}")
    suffix = input_path.suffix.lower()
    if suffix == ".csv":
        return _parse_csv(input_path)
    return _parse_plain_codes(input_path)


def parse_positions_file(path: str | Path | None) -> tuple[list[StockCode], list[str], dict[str, dict[str, object]]]:
    if path is None:
        return [], [], {}
    input_path = Path(path)
    if not input_path.exists():
        raise UserInputError(f"Positions file does not exist: {input_path}")
    if input_path.suffix.lower() != ".csv":
        raise UserInputError("--positions must point to a CSV file")

    positions: list[PositionInput] = []
    invalid: list[str] = []
    seen: set[str] = set()
    with input_path.open("r", encoding="utf-8-sig", newline="") as fp:
        reader = csv.DictReader(fp)
        if not reader.fieldnames:
            raise UserInputError(f"CSV has no header: {input_path}")
        fields = {name.lower(): name for name in reader.fieldnames}
        code_field = fields.get("ts_code") or fields.get("code")
        if not code_field:
            raise UserInputError("Positions CSV must contain code or ts_code column")
        for row in reader:
            raw = (row.get(code_field) or "").strip()
            if not raw:
                continue
            try:
                code = normalize_code(raw)
                cost_price = _parse_optional_float(row.get(fields.get("cost_price", "")), raw)
            except UserInputError:
                invalid.append(raw)
                continue
            if code.ts_code in seen:
                continue
            seen.add(code.ts_code)
            positions.append(
                PositionInput(
                    code=code,
                    cost_price=cost_price,
                    position_size=_optional_text(row.get(fields.get("position_size", ""))),
                    buy_date=_optional_text(row.get(fields.get("buy_date", ""))),
                    position_type=_optional_text(row.get(fields.get("position_type", ""))),
                    thesis=_optional_text(row.get(fields.get("thesis", ""))),
                    notes=_optional_text(row.get(fields.get("notes", ""))),
                )
            )
    return [item.code for item in positions], invalid, {item.code.ts_code: item.to_context() for item in positions}


def _parse_plain_codes(path: Path) -> tuple[list[StockCode], list[str]]:
    raw_codes = [
        line.strip()
        for line in path.read_text(encoding="utf-8-sig").splitlines()
        if line.strip()
    ]
    return _normalize_many(raw_codes)


def _parse_csv(path: Path) -> tuple[list[StockCode], list[str]]:
    raw_codes: list[str] = []
    with path.open("r", encoding="utf-8-sig", newline="") as fp:
        reader = csv.DictReader(fp)
        if not reader.fieldnames:
            raise UserInputError(f"CSV has no header: {path}")
        fields = {name.lower(): name for name in reader.fieldnames}
        code_field = fields.get("ts_code") or fields.get("code")
        if not code_field:
            raise UserInputError("CSV input must contain code or ts_code column")
        for row in reader:
            value = row.get(code_field)
            if value:
                raw_codes.append(value)
    return _normalize_many(raw_codes)


def _normalize_many(raw_codes: list[str]) -> tuple[list[StockCode], list[str]]:
    seen: set[str] = set()
    codes: list[StockCode] = []
    invalid: list[str] = []
    for raw in raw_codes:
        try:
            item = normalize_code(raw)
        except UserInputError:
            invalid.append(raw)
            continue
        if item.ts_code not in seen:
            seen.add(item.ts_code)
            codes.append(item)
    return codes, invalid


def _parse_optional_float(value: str | None, raw_code: str) -> float | None:
    text = (value or "").strip()
    if not text:
        return None
    try:
        parsed = float(text)
    except ValueError as exc:
        raise UserInputError(f"Invalid cost_price for {raw_code}: {text}") from exc
    if parsed <= 0:
        raise UserInputError(f"Invalid cost_price for {raw_code}: {text}")
    return parsed


def _optional_text(value: str | None) -> str | None:
    text = (value or "").strip()
    return text or None