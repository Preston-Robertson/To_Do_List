"""Bounded, explicit import of Creative Commons D&D 5e SRD 5.1 data."""
from __future__ import annotations

import hashlib
import json
import re
from typing import Any, Callable

import httpx

from . import repository as rpg

PROVIDER = "5e-bits-srd-2014"
SOURCE_TITLE = "D&D 5e System Reference Document 5.1 via 5e API"
LICENSE_NAME = "CC BY 4.0"
RAW_BASE = "https://raw.githubusercontent.com/5e-bits/5e-database/main/src/2014/en"
API_BASE = "https://www.dnd5eapi.co"
MAX_FILE_BYTES = 2_000_000
MAX_RECORDS_PER_FILE = 5_000
MAX_DESCRIPTION_LENGTH = 20_000
DATASET_FILES = {
    "spells": "5e-SRD-Spells.json",
    "equipment": "5e-SRD-Equipment.json",
    "magic_items": "5e-SRD-Magic-Items.json",
    "features": "5e-SRD-Features.json",
    "levels": "5e-SRD-Levels.json",
    "subclasses": "5e-SRD-Subclasses.json",
    "feats": "5e-SRD-Feats.json",
    "conditions": "5e-SRD-Conditions.json",
}
_SLUG_RE = re.compile(r"[^a-z0-9]+")
_FEATURE_QUALIFIER_RE = re.compile(
    r"\s*\((?:d\d+|cr\b[^)]*|\d+|\d+\s*/\s*rest|"
    r"\d+\s+(?:die|dice|use|uses|type|types|enemies|terrain type|terrain types))\)\s*$",
    re.IGNORECASE,
)
_SUBCLASS_CHOICE_GROUPS = {
    "Barbarian": "Primal Path",
    "Bard": "Bard College",
    "Cleric": "Divine Domain",
    "Druid": "Druid Circle",
    "Fighter": "Martial Archetype",
    "Monk": "Monastic Tradition",
    "Paladin": "Sacred Oath",
    "Ranger": "Ranger Archetype",
    "Rogue": "Roguish Archetype",
    "Sorcerer": "Sorcerous Origin",
    "Warlock": "Otherworldly Patron",
    "Wizard": "Arcane Tradition",
}


def _clip(value: Any, limit: int) -> str:
    text = str(value or "").strip()
    if len(text) <= limit:
        return text
    suffix = "\n\n[Text truncated; open the source link for the complete entry.]"
    return text[:max(0, limit - len(suffix))].rstrip() + suffix


def _lines(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item).strip() for item in value if str(item).strip()]


def _description(*parts: Any) -> str:
    lines: list[str] = []
    for part in parts:
        if isinstance(part, list):
            lines.extend(_lines(part))
        elif part:
            lines.append(str(part).strip())
    return _clip("\n\n".join(lines), MAX_DESCRIPTION_LENGTH)


def _name(value: Any, fallback: str = "") -> str:
    if isinstance(value, dict):
        return str(value.get("name") or fallback).strip()
    return fallback


def _source_url(record: dict[str, Any]) -> str:
    path = str(record.get("url") or "").strip()
    return f"{API_BASE}{path}" if path.startswith("/api/2014/") else ""


def _base_entry(
    record: dict[str, Any],
    *,
    external_key: str,
    kind: str,
    summary: str = "",
    description: str = "",
    rank: int = 0,
    action_cost: str = "",
    quantity: int = 1,
    replacement_family: str = "",
) -> dict[str, Any]:
    return {
        "external_key": external_key,
        "kind": kind,
        "name": _clip(record.get("name"), 160),
        "summary": _clip(summary, 500),
        "description": _clip(description, MAX_DESCRIPTION_LENGTH),
        "rank": rank,
        "action_cost": _clip(action_cost, 80),
        "quantity": quantity,
        "source_url": _source_url(record),
        "source_title": SOURCE_TITLE,
        "license_name": LICENSE_NAME,
        "replacement_family": replacement_family,
    }


def _spell_entry(record: dict[str, Any]) -> dict[str, Any]:
    school = _name(record.get("school"))
    details = [
        value for value in (
            f"Level {record.get('level', 0)} {school}".strip(),
            str(record.get("casting_time") or "").strip(),
            str(record.get("range") or "").strip(),
            str(record.get("duration") or "").strip(),
            "Concentration" if record.get("concentration") else "",
            "Ritual" if record.get("ritual") else "",
        ) if value
    ]
    higher = _lines(record.get("higher_level"))
    higher_text = ["At Higher Levels", *higher] if higher else []
    return _base_entry(
        record,
        external_key=f"spell:{record.get('index')}",
        kind="spell",
        summary=" | ".join(details),
        description=_description(record.get("desc"), higher_text),
        rank=int(record.get("level") or 0),
        action_cost=str(record.get("casting_time") or ""),
    )


def _equipment_entry(record: dict[str, Any], *, magic: bool = False) -> dict[str, Any]:
    category = _name(record.get("equipment_category"), "Equipment")
    details = [category]
    if magic:
        rarity = _name(record.get("rarity"))
        if rarity:
            details.append(rarity)
    damage = record.get("damage")
    if isinstance(damage, dict) and damage.get("damage_dice"):
        details.append(f"{damage['damage_dice']} {_name(damage.get('damage_type')).lower()}".strip())
    armor_class = record.get("armor_class")
    if isinstance(armor_class, dict) and armor_class.get("base") is not None:
        details.append(f"AC {armor_class['base']}")
    cost = record.get("cost")
    if isinstance(cost, dict) and cost.get("quantity") is not None:
        details.append(f"{cost['quantity']} {cost.get('unit', '')}".strip())
    if record.get("weight") is not None:
        details.append(f"{record['weight']} lb.")
    quantity = record.get("quantity", 1)
    try:
        quantity = max(0, min(1_000_000, int(quantity)))
    except (TypeError, ValueError):
        quantity = 1
    prefix = "magic-item" if magic else "equipment"
    return _base_entry(
        record,
        external_key=f"{prefix}:{record.get('index')}",
        kind="inventory",
        summary=" | ".join(details),
        description=_description(record.get("special"), record.get("desc")),
        quantity=quantity,
    )


def _feature_family_key(record: dict[str, Any]) -> str:
    feature_name = str(record.get("name") or "").strip()
    family_name = (
        feature_name
        if feature_name.casefold().startswith("mystic arcanum")
        else _FEATURE_QUALIFIER_RE.sub("", feature_name)
    )
    index = str(record.get("index") or "")
    improvement = re.match(r"^(.*)-improvement(?:-\d+)?$", index)
    return _slug(improvement.group(1) if improvement else family_name)


def _feature_entry(record: dict[str, Any]) -> dict[str, Any]:
    class_name = _name(record.get("class"))
    subclass = _name(record.get("subclass"))
    replacement_family = ":".join((
        "srd-feature",
        _slug(_name(record.get("class"), "general")),
        _slug(_name(record.get("subclass"), "base")),
        _feature_family_key(record),
    ))
    details = [value for value in (
        f"{class_name} level {record.get('level')}" if class_name and record.get("level") else class_name,
        subclass,
    ) if value]
    return _base_entry(
        record,
        external_key=f"feature:{record.get('index')}",
        kind="feature",
        summary=" | ".join(details),
        description=_description(record.get("desc")),
        replacement_family=replacement_family,
    )


def _simple_entry(record: dict[str, Any], *, prefix: str, kind: str) -> dict[str, Any]:
    return _base_entry(
        record,
        external_key=f"{prefix}:{record.get('index')}",
        kind=kind,
        description=_description(record.get("desc")),
    )


def _slug(value: Any) -> str:
    return _SLUG_RE.sub("-", str(value or "").strip().lower()).strip("-")[:120]


def _option_references(value: Any) -> list[dict[str, str]]:
    found: list[dict[str, str]] = []
    if isinstance(value, list):
        for item in value:
            found.extend(_option_references(item))
    elif isinstance(value, dict):
        item = value.get("item")
        if isinstance(item, dict) and item.get("index") and item.get("name"):
            found.append({
                "index": str(item["index"]),
                "name": str(item["name"]),
                "url": str(item.get("url") or ""),
            })
        for key in ("options", "items", "choice", "from"):
            if key in value:
                found.extend(_option_references(value[key]))
    elif isinstance(value, str) and value.strip():
        found.append({"index": _slug(value), "name": value.strip().title(), "url": ""})
    unique: dict[str, dict[str, str]] = {}
    for item in found:
        unique.setdefault(item["index"], item)
    return list(unique.values())


def _maximum_choose(value: Any) -> int:
    if isinstance(value, dict):
        own = value.get("choose")
        choices = [int(own)] if isinstance(own, int) and own > 0 else []
        choices.extend(_maximum_choose(child) for child in value.values())
        return max(choices, default=0)
    if isinstance(value, list):
        return max((_maximum_choose(child) for child in value), default=0)
    return 0


def _feature_choices(feature: dict[str, Any]) -> list[dict[str, Any]]:
    specific = feature.get("feature_specific")
    if not isinstance(specific, dict):
        return []
    groups: list[dict[str, Any]] = []
    for key, raw_group in specific.items():
        if key == "invocations" and isinstance(raw_group, list):
            continue
        if not isinstance(raw_group, dict):
            continue
        options = _option_references(raw_group.get("from", raw_group))
        choose = _maximum_choose(raw_group)
        if options and choose:
            group_name = str(feature.get("name") or "Class choice")
            if len(specific) > 1:
                group_name = f"{group_name}: {key.replace('_', ' ').title()}"
            groups.append({
                "key": key,
                "name": group_name,
                "choose": choose,
                "options": options,
            })
    return groups


def _choice_entry(
    parent: dict[str, Any],
    option: dict[str, str],
    *,
    external_key: str,
    kind: str = "feature",
) -> dict[str, Any]:
    record = {
        "name": option["name"],
        "url": option.get("url", ""),
    }
    return _base_entry(
        record,
        external_key=external_key,
        kind=kind,
        summary=f"{parent.get('name', 'Class')} option",
    )


def normalize_datasets(datasets: dict[str, list[dict[str, Any]]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    missing = set(DATASET_FILES) - set(datasets)
    if missing:
        raise ValueError(f"SRD import is missing: {', '.join(sorted(missing))}")
    for name, rows in datasets.items():
        if not isinstance(rows, list) or len(rows) > MAX_RECORDS_PER_FILE:
            raise ValueError(f"SRD {name} dataset is invalid or too large")
        if any(not isinstance(row, dict) for row in rows):
            raise ValueError(f"SRD {name} dataset contains an invalid record")

    entries_by_key: dict[str, dict[str, Any]] = {}

    def remember(entry: dict[str, Any]) -> None:
        key = str(entry.get("external_key") or "")
        if not key or not entry.get("name"):
            return
        entries_by_key[key] = entry

    for record in datasets["spells"]:
        remember(_spell_entry(record))
    for record in datasets["equipment"]:
        remember(_equipment_entry(record))
    for record in datasets["magic_items"]:
        remember(_equipment_entry(record, magic=True))
    for record in datasets["features"]:
        remember(_feature_entry(record))
    for record in datasets["feats"]:
        remember(_simple_entry(record, prefix="feat", kind="feature"))
    for record in datasets["conditions"]:
        remember(_simple_entry(record, prefix="condition", kind="condition"))

    feature_by_index = {
        str(record.get("index")): record
        for record in datasets["features"]
        if record.get("index")
    }
    progressions: list[dict[str, Any]] = []
    base_levels: list[dict[str, Any]] = []
    subclass_first_levels: dict[tuple[str, str], int] = {}
    for level_row in datasets["levels"]:
        class_name = _name(level_row.get("class"))
        subclass_name = _name(level_row.get("subclass"))
        level = int(level_row.get("level") or 0)
        if not class_name or level < 1 or level > 20:
            continue
        if subclass_name:
            key = (class_name, subclass_name)
            subclass_first_levels[key] = min(level, subclass_first_levels.get(key, level))
        else:
            base_levels.append(level_row)
        for order, feature_ref in enumerate(level_row.get("features") or []):
            if not isinstance(feature_ref, dict):
                continue
            feature_index = str(feature_ref.get("index") or "")
            feature = feature_by_index.get(feature_index)
            feature_key = f"feature:{feature_index}"
            if feature is None or feature_key not in entries_by_key:
                continue
            choice_groups = _feature_choices(feature)
            if not choice_groups:
                progressions.append({
                    "external_key": feature_key,
                    "class_name": class_name,
                    "subclass": subclass_name,
                    "level": level,
                    "grant_type": "automatic",
                    "sort_order": order,
                })
                continue
            for group_index, group in enumerate(choice_groups):
                for option_index, option in enumerate(group["options"]):
                    option_key = f"feature:{option['index']}"
                    if option_key not in entries_by_key:
                        choice_scope = ":".join((
                            _slug(class_name),
                            _slug(subclass_name or "base"),
                            _feature_family_key(feature),
                            _slug(group["key"]),
                        ))
                        choice_digest = hashlib.sha256(
                            choice_scope.encode("utf-8")
                        ).hexdigest()[:16]
                        option_key = f"choice:{choice_digest}:{_slug(option['index'])}"
                        kind = "proficiency" if "proficien" in str(group["name"]).lower() else "feature"
                        remember(_choice_entry(feature, option, external_key=option_key, kind=kind))
                    progressions.append({
                        "external_key": option_key,
                        "class_name": class_name,
                        "subclass": subclass_name,
                        "level": level,
                        "grant_type": "choice",
                        "choice_group": group["name"],
                        "choice_count": group["choose"],
                        "sort_order": order * 100 + group_index * 10 + option_index,
                    })

    for subclass in datasets["subclasses"]:
        class_name = _name(subclass.get("class"))
        subclass_name = str(subclass.get("name") or "").strip()
        index = str(subclass.get("index") or "")
        level = subclass_first_levels.get((class_name, subclass_name))
        group = _SUBCLASS_CHOICE_GROUPS.get(class_name)
        if not class_name or not subclass_name or not index or not level or not group:
            continue
        key = f"subclass:{index}"
        remember(_base_entry(
            subclass,
            external_key=key,
            kind="feature",
            summary=f"{class_name} subclass",
            description=_description(subclass.get("desc")),
        ))

    invocation_parent = feature_by_index.get("eldritch-invocations")
    invocation_refs = []
    if invocation_parent:
        specific = invocation_parent.get("feature_specific")
        if isinstance(specific, dict):
            invocation_refs = _option_references(specific.get("invocations"))
    previous_invocations = 0
    for level_row in sorted(
        (row for row in base_levels if _name(row.get("class")) == "Warlock"),
        key=lambda row: int(row.get("level") or 0),
    ):
        current = int((level_row.get("class_specific") or {}).get("invocations_known") or 0)
        choose = max(0, current - previous_invocations)
        previous_invocations = current
        if not choose:
            continue
        level = int(level_row["level"])
        for order, option in enumerate(invocation_refs):
            feature = feature_by_index.get(option["index"])
            if feature is None or int(feature.get("level") or 0) > level:
                continue
            option_key = f"feature:{option['index']}"
            if option_key in entries_by_key:
                progressions.append({
                    "external_key": option_key,
                    "class_name": "Warlock",
                    "level": level,
                    "grant_type": "choice",
                    "choice_group": "Eldritch Invocations",
                    "choice_count": choose,
                    "sort_order": order,
                })

    return list(entries_by_key.values()), progressions


def _fetch_dataset(client: httpx.Client, name: str) -> list[dict[str, Any]]:
    filename = DATASET_FILES.get(name)
    if filename is None:
        raise ValueError("Unknown SRD dataset")
    url = f"{RAW_BASE}/{filename}"
    with client.stream("GET", url) as response:
        response.raise_for_status()
        advertised = int(response.headers.get("content-length", "0") or 0)
        if advertised > MAX_FILE_BYTES:
            raise ValueError(f"SRD {name} dataset exceeds the response limit")
        content = bytearray()
        for chunk in response.iter_bytes():
            content.extend(chunk)
            if len(content) > MAX_FILE_BYTES:
                raise ValueError(f"SRD {name} dataset exceeds the response limit")
    payload = json.loads(content)
    if not isinstance(payload, list):
        raise ValueError(f"SRD {name} dataset returned an invalid response")
    return payload


def refresh_dnd_2014(
    fetcher: Callable[[str], list[dict[str, Any]]] | None = None,
) -> dict[str, int]:
    """Explicitly refresh local SRD records; never called during startup."""
    if fetcher is None:
        headers = {
            "Accept": "application/json",
            "User-Agent": "LuigiWeb/1.0",
        }
        with httpx.Client(headers=headers, timeout=60, follow_redirects=False) as client:
            datasets = {
                name: _fetch_dataset(client, name)
                for name in DATASET_FILES
            }
    else:
        datasets = {name: fetcher(name) for name in DATASET_FILES}
    entries, progressions = normalize_datasets(datasets)
    result = rpg.upsert_provider_library(
        "dnd5e_2014",
        PROVIDER,
        entries,
        progressions,
    )
    return {
        **result,
        "records_received": sum(len(rows) for rows in datasets.values()),
    }