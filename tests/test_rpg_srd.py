from __future__ import annotations

import os
import tempfile
import unittest
from unittest.mock import patch

import httpx

from luigi_web import rpg, rpg_srd


def _datasets() -> dict[str, list[dict]]:
    return {
        "spells": [{
            "index": "example-spell",
            "name": "Example Spell",
            "level": 1,
            "school": {"name": "Evocation"},
            "casting_time": "1 action",
            "range": "30 feet",
            "duration": "Instantaneous",
            "desc": ["Synthetic spell description."],
            "url": "/api/2014/spells/example-spell",
        }],
        "equipment": [{
            "index": "example-sword",
            "name": "Example Sword",
            "equipment_category": {"name": "Weapon"},
            "damage": {"damage_dice": "1d8", "damage_type": {"name": "Slashing"}},
            "cost": {"quantity": 10, "unit": "gp"},
            "weight": 3,
            "url": "/api/2014/equipment/example-sword",
        }],
        "magic_items": [{
            "index": "example-charm",
            "name": "Example Charm",
            "equipment_category": {"name": "Wondrous Item"},
            "rarity": {"name": "Uncommon"},
            "desc": ["Synthetic item description."],
            "url": "/api/2014/magic-items/example-charm",
        }],
        "features": [
            {
                "index": "fighter-style",
                "name": "Fighting Style",
                "class": {"name": "Fighter"},
                "level": 2,
                "desc": ["Choose a synthetic style."],
                "feature_specific": {
                    "subfeature_options": {
                        "choose": 1,
                        "from": {"options": [
                            {"item": {
                                "index": "style-a",
                                "name": "Fighting Style: Example A",
                                "url": "/api/2014/features/style-a",
                            }},
                            {"item": {
                                "index": "style-b",
                                "name": "Fighting Style: Example B",
                                "url": "/api/2014/features/style-b",
                            }},
                        ]},
                    },
                },
                "url": "/api/2014/features/fighter-style",
            },
            {
                "index": "style-a",
                "name": "Fighting Style: Example A",
                "class": {"name": "Fighter"},
                "level": 2,
                "parent": {"index": "fighter-style"},
                "desc": ["Synthetic option A."],
                "url": "/api/2014/features/style-a",
            },
            {
                "index": "style-b",
                "name": "Fighting Style: Example B",
                "class": {"name": "Fighter"},
                "level": 2,
                "parent": {"index": "fighter-style"},
                "desc": ["Synthetic option B."],
                "url": "/api/2014/features/style-b",
            },
            {
                "index": "action-surge-example",
                "name": "Example Surge",
                "class": {"name": "Fighter"},
                "level": 2,
                "desc": ["Synthetic automatic feature."],
                "url": "/api/2014/features/action-surge-example",
            },
            {
                "index": "example-champion-feature",
                "name": "Example Champion Feature",
                "class": {"name": "Fighter"},
                "subclass": {"name": "Example Champion"},
                "level": 3,
                "desc": ["Synthetic subclass feature."],
                "url": "/api/2014/features/example-champion-feature",
            },
        ],
        "levels": [
            {
                "level": 2,
                "class": {"name": "Fighter"},
                "features": [
                    {"index": "fighter-style"},
                    {"index": "action-surge-example"},
                ],
            },
            {
                "level": 3,
                "class": {"name": "Fighter"},
                "subclass": {"name": "Example Champion"},
                "features": [{"index": "example-champion-feature"}],
            },
        ],
        "subclasses": [{
            "index": "example-champion",
            "name": "Example Champion",
            "class": {"name": "Fighter"},
            "desc": ["Synthetic subclass description."],
            "url": "/api/2014/subclasses/example-champion",
        }],
        "feats": [{
            "index": "example-feat",
            "name": "Example Feat",
            "desc": ["Synthetic feat description."],
            "url": "/api/2014/feats/example-feat",
        }],
        "conditions": [{
            "index": "example-condition",
            "name": "Example Condition",
            "desc": ["Synthetic condition description."],
            "url": "/api/2014/conditions/example-condition",
        }],
    }


class RpgSrdImportTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.env = patch.dict(
            os.environ,
            {"LUIGI_WEB_RPG_DB": os.path.join(self.temp_dir.name, "rpg.db")},
        )
        self.env.start()
        rpg.init_db()

    def tearDown(self) -> None:
        self.env.stop()
        self.temp_dir.cleanup()

    def test_normalizer_builds_reusable_records_and_class_choices(self) -> None:
        entries, progressions = rpg_srd.normalize_datasets(_datasets())
        by_key = {entry["external_key"]: entry for entry in entries}

        self.assertEqual(by_key["spell:example-spell"]["rank"], 1)
        self.assertEqual(by_key["spell:example-spell"]["license_name"], "CC BY 4.0")
        self.assertIn("1d8 slashing", by_key["equipment:example-sword"]["summary"])
        self.assertIn("subclass:example-champion", by_key)

        style_choices = [
            row for row in progressions
            if row.get("choice_group") == "Fighting Style"
        ]
        self.assertEqual(len(style_choices), 2)
        self.assertTrue(all(row["choice_count"] == 1 for row in style_choices))
        automatic = [
            row for row in progressions
            if row["external_key"] == "feature:action-surge-example"
        ]
        self.assertEqual(automatic[0]["grant_type"], "automatic")
        self.assertFalse(any(
            row["external_key"] == "subclass:example-champion"
            for row in progressions
        ))
        subclass_grant = next(
            row for row in progressions
            if row["external_key"] == "feature:example-champion-feature"
        )
        self.assertEqual(subclass_grant["subclass"], "Example Champion")

    def test_refresh_is_idempotent_and_populates_level_plan(self) -> None:
        datasets = _datasets()
        first = rpg_srd.refresh_dnd_2014(lambda name: datasets[name])
        second = rpg_srd.refresh_dnd_2014(lambda name: datasets[name])

        self.assertGreater(first["entries_inserted"], 0)
        self.assertEqual(second["entries_inserted"], 0)
        self.assertEqual(second["entries_updated"], first["entries_inserted"])
        self.assertIn("Fighter", rpg.list_library_classes("dnd5e_2014"))

        plan = rpg.level_up_plan(
            "dnd5e_2014",
            "Fighter",
            from_level=1,
            target_level=2,
        )
        self.assertEqual(plan["automatic_count"], 1)
        self.assertEqual(plan["choice_group_count"], 1)
        self.assertEqual(len(plan["levels"][0]["choices"][0]["options"]), 2)
        subclass_plan = rpg.level_up_plan(
            "dnd5e_2014",
            "Fighter",
            subclass="Example Champion",
            from_level=2,
            target_level=3,
        )
        self.assertEqual(
            [row["name"] for row in subclass_plan["levels"][0]["automatic"]],
            ["Example Champion Feature"],
        )

    def test_refresh_reconciles_only_provider_owned_records(self) -> None:
        datasets = _datasets()
        manual = rpg.create_library_entry(
            "dnd5e_2014",
            kind="feature",
            name="Manual feature",
        )
        rpg_srd.refresh_dnd_2014(lambda name: datasets[name])

        datasets["features"] = [
            row for row in datasets["features"]
            if row["index"] != "action-surge-example"
        ]
        datasets["levels"][0]["features"] = [
            row for row in datasets["levels"][0]["features"]
            if row["index"] != "action-surge-example"
        ]
        rpg_srd.refresh_dnd_2014(lambda name: datasets[name])

        archived = rpg.list_library_entries(
            "dnd5e_2014",
            include_archived=True,
        )
        stale = next(row for row in archived if row["external_key"] == "feature:action-surge-example")
        self.assertTrue(stale["archived"])
        self.assertIsNotNone(rpg.get_library_entry(manual["id"]))
        self.assertFalse(rpg.get_library_entry(manual["id"])["archived"])
        plan = rpg.level_up_plan(
            "dnd5e_2014",
            "Fighter",
            from_level=1,
            target_level=2,
        )
        self.assertEqual(plan["automatic_count"], 0)

    def test_refresh_preserves_manual_mapping_on_imported_entry(self) -> None:
        datasets = _datasets()
        rpg_srd.refresh_dnd_2014(lambda name: datasets[name])
        imported = next(
            entry for entry in rpg.list_library_entries("dnd5e_2014")
            if entry["external_key"] == "feature:style-a"
        )
        rpg.add_class_progression(
            imported["id"],
            class_name="Manual class",
            level=4,
            grant_type="automatic",
        )

        rpg_srd.refresh_dnd_2014(lambda name: datasets[name])

        mappings = rpg.list_class_progressions(library_entry_id=imported["id"])
        manual = next(row for row in mappings if row["class_name"] == "Manual class")
        self.assertEqual(manual["provider"], "")
        self.assertTrue(any(row["provider"] == rpg_srd.PROVIDER for row in mappings))

    def test_bounded_downloader_uses_fixed_dataset_url(self) -> None:
        requested_urls: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            requested_urls.append(str(request.url))
            return httpx.Response(
                200,
                json=[{"index": "example", "name": "Example"}],
                request=request,
            )

        with httpx.Client(
            transport=httpx.MockTransport(handler),
            follow_redirects=False,
        ) as client:
            rows = rpg_srd._fetch_dataset(client, "feats")

        self.assertEqual(rows[0]["name"], "Example")
        self.assertEqual(
            requested_urls,
            [f"{rpg_srd.RAW_BASE}/{rpg_srd.DATASET_FILES['feats']}"],
        )

    def test_bounded_downloader_rejects_oversized_response(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                content=b"x" * (rpg_srd.MAX_FILE_BYTES + 1),
                request=request,
            )

        with httpx.Client(transport=httpx.MockTransport(handler)) as client:
            with self.assertRaisesRegex(ValueError, "response limit"):
                rpg_srd._fetch_dataset(client, "conditions")

    def test_recurring_text_choices_reuse_stable_library_entries(self) -> None:
        datasets = _datasets()
        for level in (1, 2):
            index = f"favored-example-{level}"
            datasets["features"].append({
                "index": index,
                "name": f"Favored Example ({level} {'type' if level == 1 else 'types'})",
                "class": {"name": "Ranger"},
                "level": level,
                "desc": ["Choose a synthetic type."],
                "feature_specific": {
                    "type_options": {
                        "choose": 1,
                        "from": {"options": ["alpha", "beta"]},
                    },
                },
                "url": f"/api/2014/features/{index}",
            })
            datasets["levels"].append({
                "level": level,
                "class": {"name": "Ranger"},
                "features": [{"index": index}],
            })

        entries, progressions = rpg_srd.normalize_datasets(datasets)
        alpha_rows = [
            row for row in progressions
            if row["class_name"] == "Ranger"
            and row["external_key"].startswith("choice:")
            and next(
                entry["name"] for entry in entries
                if entry["external_key"] == row["external_key"]
            ) == "Alpha"
        ]

        self.assertEqual(len(alpha_rows), 2)
        self.assertEqual(len({row["external_key"] for row in alpha_rows}), 1)


if __name__ == "__main__":
    unittest.main()