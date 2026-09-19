"""Offline Trading Cards baseline; synthetic databases are always removed."""
from __future__ import annotations

import argparse
from collections import Counter
from contextlib import contextmanager
import json
import os
from pathlib import Path
import re
import sqlite3
from statistics import median
import sys
import tempfile
from time import perf_counter
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


_SQL_STRING = r"'(?:[^']|'')*'"
_SCHEMA_PROJECTION = r'(?:1|\*|(?:[a-z_][a-z_0-9]*|"[a-z_][a-z_0-9]*")(?:\s*,\s*(?:[a-z_][a-z_0-9]*|"[a-z_][a-z_0-9]*"))*)'
_SCHEMA_SELECT = re.compile(
    rf"SELECT\s+{_SCHEMA_PROJECTION}\s+FROM\s+(?:main\.)?"
    rf"(?:sqlite_master(?:\s+WHERE\s+type\s*=\s*'table'\s+AND\s+name\s*=\s*{_SQL_STRING})?"
    rf"|pragma_table_info\s*\(\s*{_SQL_STRING}\s*\))\s*;?",
    re.IGNORECASE,
)
_TABLE_INFO = re.compile(
    r"PRAGMA\s+(?:main\.)?table_info\s*\(\s*(?:[a-z_][a-z_0-9]*|'(?:[^']|'')*'|\"[^\"]*\")\s*\)\s*;?",
    re.IGNORECASE,
)


@contextmanager
def synthetic_database(catalog_size=100_000, holdings=5_000):
    if catalog_size < 120 or not 1 <= holdings <= catalog_size:
        raise ValueError("Use at least 120 catalog cards and 1..catalog_size holdings")
    with tempfile.TemporaryDirectory(prefix="luigi-cards-benchmark-") as temporary:
        database = Path(temporary) / "cards.db"
        with (
            patch.dict(os.environ, {
                **{key: os.environ[key] for key in ("SYSTEMROOT", "SYSTEMDRIVE", "WINDIR") if key in os.environ},
                "LUIGI_WEB_CARDS_DB": str(database),
                "LUIGI_WEB_DATA_DIR": temporary,
                "LUIGI_WEB_CARDS_REFRESH_HOURS": "0",
                "LUIGI_WEB_CARDS_DRAWIO_URL": "",
                "TEMP": temporary,
                "TMP": temporary,
                "SQLITE_TMPDIR": temporary,
                "APPDATA": temporary,
                "LOCALAPPDATA": temporary,
            }, clear=True),
            patch("socket.socket.connect", side_effect=RuntimeError("Benchmark network blocked")),
            patch("socket.create_connection", side_effect=RuntimeError("Benchmark network blocked")),
            patch("socket.getaddrinfo", side_effect=RuntimeError("Benchmark DNS blocked")),
        ):
            from luigi_web.modules.cards import repository as cards

            cards.init_db()
            with cards.transaction() as connection:
                for start in range(1, catalog_size + 1, 2_000):
                    batch = []
                    for index in range(start, min(start + 2_000, catalog_size + 1)):
                        identity = (index - 1) // 2
                        batch.append((
                            index, "mtg", f"synthetic-{index}", "manual",
                            f"Example Card {identity:06d}", f"T{index % 100:02d}",
                            "Synthetic Set", str(index), "common", "Artifact", "{2}", 2,
                            100 + index % 40, 150 + index % 40,
                            json.dumps({"oracle_id": f"synthetic-oracle-{identity}",
                                        "lang": "en", "layout": "normal", "digital": False,
                                        "released_at": "2026-01-01", "games": ["paper"],
                                        "legalities": {"commander": "legal"}}),
                        ))
                    connection.executemany(
                        "INSERT INTO cards (id, game_code, external_id, source, name, set_code, "
                        "set_name, collector_number, rarity, type_line, mana_cost, cmc, "
                        "price_usd_minor, price_usd_foil_minor, raw_json) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", batch,
                    )
                connection.executemany(
                    "INSERT INTO collection (card_id, qty, acquired_qty, acquired_price_minor, acquired_currency) "
                    "VALUES (?, 2, 2, 75, 'USD')",
                    ((1 + index * catalog_size // holdings,) for index in range(holdings)),
                )
                connection.execute(
                    "INSERT INTO collection_lots (collection_id, original_card_id, holding_card_id, "
                    "qty, remaining_qty, priced_qty, remaining_priced_qty, foil, condition, "
                    "unit_price_minor, currency, price_source) "
                    "SELECT id, card_id, card_id, qty, qty, acquired_qty, acquired_qty, foil, condition, "
                    "acquired_price_minor, acquired_currency, 'entered' FROM collection"
                )
                connection.execute("INSERT INTO decks (id, game_code, name) VALUES (1, 'mtg', 'Example Deck')")
                connection.executemany(
                    "INSERT INTO deck_cards (deck_id, card_id, qty, category, board) VALUES (1, ?, 1, ?, 'main')",
                    ((index, f"Example category {(index - 1) // 40 + 1}") for index in range(1, 121)),
                )
            yield cards, database


def trace_operation(cards, operation):
    statements = []
    plans = []
    planned_statements = set()
    connections = 0
    vm_steps = 0
    original_connect = cards._connect

    def progress():
        nonlocal vm_steps
        vm_steps += 100
        return 0

    @contextmanager
    def traced_connect():
        nonlocal connections
        connections += 1
        connection_statements = []

        def record(statement):
            statements.append(statement)
            connection_statements.append(statement)

        with original_connect() as connection:
            connection.set_trace_callback(record)
            connection.set_progress_handler(progress, 100)
            try:
                yield connection
            finally:
                connection.set_trace_callback(None)
                connection.set_progress_handler(None, 0)
            for sql in connection_statements:
                statement = " ".join(sql.split())
                if not statement.upper().startswith(("SELECT ", "WITH ")) or statement in planned_statements:
                    continue
                plans.append({
                    "query": statement,
                    "kind": "schema" if _SCHEMA_SELECT.fullmatch(statement) else "domain",
                    "plan": [row["detail"] for row in connection.execute("EXPLAIN QUERY PLAN " + statement)],
                })
                planned_statements.add(statement)

    with patch.object(cards, "_connect", traced_connect):
        result = operation()
    normalized = [" ".join(statement.split()) for statement in statements]
    selects = [statement for statement in normalized if statement.upper().startswith(("SELECT ", "WITH "))]
    schema_selects = [statement for statement in selects if _SCHEMA_SELECT.fullmatch(statement)]
    return result, {
        "connections": connections,
        "statements": len(statements),
        "select_statements": len(selects),
        "schema_select_statements": len(schema_selects),
        "domain_select_statements": len(selects) - len(schema_selects),
        "schema_checks": sum(statement.upper() == "PRAGMA USER_VERSION" for statement in normalized),
        "table_info_checks": sum(bool(_TABLE_INFO.fullmatch(statement)) for statement in normalized),
        "statement_kinds": dict(Counter(statement.split()[0].upper() for statement in normalized)),
        "vm_steps_approx": vm_steps,
        "query_plans": plans,
    }


def measure(cards, operation, samples):
    operation()
    elapsed = []
    for _sample in range(samples):
        start = perf_counter()
        operation()
        elapsed.append((perf_counter() - start) * 1000)
    result, trace = trace_operation(cards, operation)
    size = len(result) if isinstance(result, (list, str)) else len(
        result.get("rows", result.get("items", result.get("boards", [])))
    )
    return {
        "p50_ms": round(median(elapsed), 3),
        "min_ms": round(min(elapsed), 3),
        "max_ms": round(max(elapsed), 3),
        "samples": samples,
        "returned_rows_or_characters": size,
        "result_totals": (result.get("totals", result.get("counts", {})) if isinstance(result, dict) else {}),
        **trace,
    }


def run_benchmark(catalog_size=100_000, holdings=5_000, samples=5):
    if not 1 <= samples <= 15:
        raise ValueError("Use 1..15 samples")
    seed_started = perf_counter()
    with synthetic_database(catalog_size, holdings) as (cards, database):
        seed_ms = (perf_counter() - seed_started) * 1000
        from luigi_web.modules.cards import analysis, routes

        def render_deck_state():
            state = routes._deck_state("mtg", 1)
            return routes.templates.get_template("cards/partials/deck_cards.html").render(
                current_game="mtg", refresh_summary=False, **state,
            )

        operations = {
            "list_deck_cards": lambda: cards.list_deck_cards(1),
            "list_collection_500": lambda: cards.list_collection("mtg"),
            "list_collection_1000": lambda: cards.list_collection("mtg", limit=1000),
            "catalog_default": lambda: cards.browse_catalog("mtg"),
            "catalog_search": lambda: cards.browse_catalog("mtg", query="Example Card 0499"),
            "catalog_unique_cards": lambda: cards.browse_catalog("mtg", filters={"unique": "cards"}),
            "render_deck_state": render_deck_state,
            "build_checklist_exact": lambda: analysis.build_checklist("mtg", 1, match_mode="exact"),
            "build_checklist_equivalent": lambda: analysis.build_checklist("mtg", 1, match_mode="any"),
            "deck_analysis": lambda: analysis.deck_analysis("mtg", 1),
        }
        report = {
            "dataset": {"catalog": catalog_size, "holdings": holdings, "deck_rows": 120, "category_rows": 40,
                        "purchase_lots": holdings, "printings_per_oracle": 2},
            "python": sys.version.split()[0], "sqlite": sqlite3.sqlite_version,
            "seed_ms": round(seed_ms, 3), "network_blocked": True,
            "measurement": "Warm calls including repository init_db; tracing is a separate untimed call. "
                           "Connection setup PRAGMAs and commit are excluded from trace counts. "
                           "Total SELECTs include narrowly recognized schema SELECTs; all others are domain reads. "
                           "Query plans use the original connections after disabling trace and progress callbacks. "
                           "Checklist rows count items; analysis rows count boards and result_totals counts copies. "
                           "VM steps are counted in batches of 100, not a precise opcode total.",
            "operations": {name: measure(cards, operation, samples) for name, operation in operations.items()},
            "host_imported": "luigi_web.application" in sys.modules,
        }
    report["temporary_database_removed"] = not database.parent.exists()
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--catalog-size", type=int, default=100_000)
    parser.add_argument("--holdings", type=int, default=5_000)
    parser.add_argument("--samples", type=int, default=5)
    arguments = parser.parse_args()
    print(json.dumps(run_benchmark(arguments.catalog_size, arguments.holdings, arguments.samples), indent=2))


if __name__ == "__main__":
    main()