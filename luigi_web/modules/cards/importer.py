"""Parse and apply Archidekt, Moxfield, MTGO, and plain-text deck lists."""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Iterable

from . import repository as cards

MAX_IMPORT_BYTES = 500_000
MAX_IMPORT_LINES = 2_000

_LINE_RE = re.compile(
    r"""
    ^\s*
    (?P<qty>\d+)\s*x?\s+
    (?P<name>[^()\n]+?)
    (?:\s*\(\s*(?P<set>[A-Za-z0-9]{2,8})\s*\)\s*
        (?P<cn>[A-Za-z0-9\-★]+)?)?
    (?P<mods>(?:\s*\*[^*]+\*)*)?
    \s*$
    """,
    re.VERBOSE,
)

_BOARD_HEADERS = {
    "sideboard": "side",
    "side board": "side",
    "sb": "side",
    "maybeboard": "maybe",
    "maybe board": "maybe",
    "maybe": "maybe",
    "mainboard": "main",
    "main board": "main",
    "deck": "main",
    "commander": "commander",
    "commanders": "commander",
}

_BRACKETED_HEADER_RE = re.compile(r"^\[([^\[\]\n]{1,100})\]\s*:?$")
_NAMED_CATEGORY_RE = re.compile(
    r"^(?:category|section)\s*:\s*([^\[\]\n]{1,100})$",
    re.IGNORECASE,
)
_TRAILING_CATEGORY_RE = re.compile(
    r"\s+\[([^\[\]\n]{1,100})\](?=\s*(?:\*[^*]+\*\s*)*$)"
)
_TRAILING_HASH_CATEGORY_RE = re.compile(
    r"\s+#\s*([^#\n]{1,100}?)(?=\s*(?:\*[^*]+\*\s*)*$)"
)
_HEADER_COUNT_RE = re.compile(
    r"\s*(?:\(\s*\d+\s*(?:cards?)?\s*\)|\[\s*\d+\s*\]|:\s*\d+)\s*$",
    re.IGNORECASE,
)
_COUNTED_CATEGORY_RE = re.compile(
    r"^(?P<label>[^\[\]\n]{1,100}?)\s*"
    r"(?:\(\s*\d+\s*(?:cards?)?\s*\)|\[\s*\d+\s*\]|:\s*\d+)\s*:?$",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class ParsedLine:
    raw: str
    qty: int
    name: str
    set_code: str | None = None
    collector_number: str | None = None
    board: str = "main"
    category: str | None = None
    is_commander: bool = False
    is_foil: bool = False


@dataclass
class ImportRow:
    line: ParsedLine
    card_id: int | None = None
    matched_name: str | None = None
    matched_set: str | None = None


@dataclass
class ImportReport:
    matched: list[ImportRow] = field(default_factory=list)
    unmatched: list[ImportRow] = field(default_factory=list)
    unparsed: list[str] = field(default_factory=list)
    sections: list[dict[str, str | None]] = field(default_factory=list)

    @property
    def matched_count(self) -> int:
        return sum(row.line.qty for row in self.matched)

    @property
    def unmatched_count(self) -> int:
        return sum(row.line.qty for row in self.unmatched)

    def as_dict(self) -> dict:
        return {
            "matched": [
                {
                    "raw": row.line.raw.strip(),
                    "qty": row.line.qty,
                    "name": row.matched_name,
                    "set": row.matched_set,
                    "board": row.line.board,
                    "category": row.line.category,
                }
                for row in self.matched
            ],
            "unmatched": [
                {
                    "raw": row.line.raw.strip(),
                    "qty": row.line.qty,
                    "name": row.line.name,
                    "set": row.line.set_code,
                    "board": row.line.board,
                    "category": row.line.category,
                }
                for row in self.unmatched
            ],
            "unparsed": self.unparsed,
            "sections": self.sections,
            "counts": {
                "matched": self.matched_count,
                "unmatched": self.unmatched_count,
                "unparsed": len(self.unparsed),
            },
        }


def parse(text: str) -> tuple[list[ParsedLine], list[str]]:
    raw_text = str(text or "")
    if len(raw_text.encode("utf-8")) > MAX_IMPORT_BYTES:
        raise ValueError("deck import is too large")
    lines = raw_text.replace("\r", "").split("\n")
    if len(lines) > MAX_IMPORT_LINES:
        raise ValueError("deck import has too many lines")

    parsed: list[ParsedLine] = []
    unparsed: list[str] = []
    board = "main"
    category: str | None = None
    cards_since_board_header = 0
    for raw in lines:
        stripped = raw.strip()
        if not stripped:
            continue
        if stripped.startswith(("#", "//")):
            comment_header = stripped[2:].strip() if stripped.startswith("//") else stripped[1:].strip()
            comment_key = _header_key(comment_header)
            if comment_key in _BOARD_HEADERS:
                board = _BOARD_HEADERS[comment_key]
                category = None
                cards_since_board_header = 0
            elif comment_header:
                board = _category_board(board, cards_since_board_header)
                category = _category(_category_header(comment_header))
            continue
        bracketed = _BRACKETED_HEADER_RE.match(stripped)
        header_text = bracketed.group(1).strip() if bracketed else stripped
        header = _header_key(header_text)
        if header in _BOARD_HEADERS:
            board = _BOARD_HEADERS[header]
            category = None
            cards_since_board_header = 0
            continue
        category_match = (
            bracketed
            or _NAMED_CATEGORY_RE.match(stripped)
            or _COUNTED_CATEGORY_RE.match(stripped)
        )
        if category_match:
            board = _category_board(board, cards_since_board_header)
            category = _category(category_match.group(1))
            continue
        if stripped.endswith(":") and not stripped.upper().startswith("SB:"):
            board = _category_board(board, cards_since_board_header)
            category = _category(stripped[:-1])
            continue
        card_text, inline_category = _extract_trailing_category(stripped)
        if stripped.upper().startswith("SB:"):
            card_text, inline_category = _extract_trailing_category(
                stripped[3:].strip()
            )
            match = _LINE_RE.match(card_text)
            if match:
                parsed.append(_from_match(
                    match,
                    raw,
                    "side",
                    inline_category or (category if board == "side" else None),
                ))
                cards_since_board_header += 1
                continue
        match = _LINE_RE.match(card_text)
        if not match:
            unparsed.append(raw[:500])
            continue
        parsed.append(_from_match(
            match,
            raw,
            _category_board(board, cards_since_board_header)
            if inline_category else board,
            inline_category or category,
        ))
        cards_since_board_header += 1
    return parsed, unparsed


def _category(value: str) -> str:
    category = " ".join(str(value).split()).strip(" :")
    if not category:
        raise ValueError("deck category cannot be empty")
    if len(category) > 100:
        raise ValueError("deck category must be at most 100 characters")
    return category


def _header_key(value: str) -> str:
    cleaned = str(value).strip().rstrip(":").strip()
    if cleaned.startswith("[") and cleaned.endswith("]"):
        cleaned = cleaned[1:-1].strip()
    without_count = _HEADER_COUNT_RE.sub("", cleaned)
    return without_count.rstrip(":").strip().lower()


def _category_header(value: str) -> str:
    counted = _COUNTED_CATEGORY_RE.match(str(value).strip())
    return counted.group("label") if counted else str(value)


def _category_board(board: str, cards_since_board_header: int) -> str:
    if board == "commander" and cards_since_board_header:
        return "main"
    return board


def _extract_trailing_category(value: str) -> tuple[str, str | None]:
    matches = [
        *list(_TRAILING_CATEGORY_RE.finditer(value)),
        *list(_TRAILING_HASH_CATEGORY_RE.finditer(value)),
    ]
    if not matches:
        return value, None
    match = max(matches, key=lambda found: found.start())
    category = _category(match.group(1))
    return f"{value[:match.start()]}{value[match.end():]}".strip(), category


def _from_match(
    match: re.Match[str],
    raw: str,
    board: str,
    category: str | None = None,
) -> ParsedLine:
    qty = int(match.group("qty"))
    if qty < 1 or qty > 9999:
        raise ValueError("deck quantities must be between 1 and 9999")
    modifiers = (match.group("mods") or "").upper()
    is_commander = "*CMDR*" in modifiers or "*COMMANDER*" in modifiers
    if is_commander:
        board = "commander"
    return ParsedLine(
        raw=raw,
        qty=qty,
        name=match.group("name").strip(),
        set_code=(match.group("set") or "").upper() or None,
        collector_number=match.group("cn") or None,
        board=board,
        category=category,
        is_commander=is_commander,
        is_foil="*F*" in modifiers or "*FOIL*" in modifiers,
    )


def resolve(game_code: str, lines: Iterable[ParsedLine]) -> ImportReport:
    report = ImportReport()
    section_keys: set[tuple[str, str | None]] = set()
    for line in lines:
        section_key = (line.board, line.category)
        if section_key not in section_keys:
            section_keys.add(section_key)
            report.sections.append({
                "board": line.board,
                "category": line.category,
            })
        matched = cards.find_card(
            game_code,
            line.name,
            set_code=line.set_code,
            collector_number=line.collector_number,
        )
        row = ImportRow(line=line)
        if matched is None:
            report.unmatched.append(row)
            continue
        row.card_id = int(matched["id"])
        row.matched_name = matched["name"]
        row.matched_set = matched["set_code"]
        report.matched.append(row)
    return report


def preview(game_code: str, text: str) -> ImportReport:
    parsed, unparsed = parse(text)
    report = resolve(game_code, parsed)
    report.unparsed.extend(unparsed)
    return report


def apply(deck_id: int, report: ImportReport, *, conn=None) -> int:
    """Apply every matched line in one transaction."""

    def write(active) -> int:
        added = 0
        for row in report.matched:
            if row.card_id is None:
                raise ValueError("resolved import row is missing a card id")
            cards.add_card_to_deck(
                deck_id,
                row.card_id,
                qty=row.line.qty,
                board=row.line.board,
                category=row.line.category or "",
                conn=active,
            )
            added += row.line.qty
        return added
    if conn is not None:
        return write(conn)
    with cards.transaction() as active:
        return write(active)


def import_text(game_code: str, deck_id: int, text: str) -> dict:
    report = preview(game_code, text)
    added = apply(deck_id, report)
    result = report.as_dict()
    result["added"] = added
    return result