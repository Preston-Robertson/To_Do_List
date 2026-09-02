import os
import sqlite3
import tempfile
import unittest
from contextlib import closing
from unittest.mock import patch

from luigi_web import rpg


class RpgRepositoryTests(unittest.TestCase):
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

    def test_system_defaults_and_sheet_math(self) -> None:
        dnd = rpg.create_character("Example ranger", "dnd5e_2014", level=5)
        dnd_sheet = rpg.sheet_view(dnd["id"])
        self.assertEqual(dnd_sheet["state"]["abilities"]["strength"], 10)
        self.assertEqual(dnd_sheet["proficiency_bonus"], 3)

        pf2 = rpg.create_character("Example thaumaturge", "pf2e", level=4)
        pf2_sheet = rpg.sheet_view(pf2["id"])
        self.assertEqual(pf2_sheet["state"]["abilities"]["strength"], 0)
        self.assertIsNone(pf2_sheet["proficiency_bonus"])
        self.assertEqual(len(pf2_sheet["saves"]), 3)

    def test_cloned_level_state_is_an_independent_snapshot(self) -> None:
        character = rpg.create_character("Example wizard", "dnd5e_2014")
        first = rpg.get_state(character["active_state_id"])
        assert first is not None
        rpg.update_state(
            first["id"],
            hp_current=7,
            hp_max=12,
            class_name="Wizard 1",
            abilities={**first["abilities"], "intelligence": 16},
            skills={"arcana": {"rank": 1, "misc": 0}},
        )
        first_entry = rpg.add_entry(
            first["id"],
            kind="spell",
            name="Example spell",
            rank=1,
            prepared=True,
            current_uses=2,
            max_uses=2,
            reset_on="long_rest",
            source_url="https://example.com/rules/example-spell",
        )

        second = rpg.create_state(
            character["id"],
            level=2,
            label="Level 2 - School chosen",
            clone_from_id=first["id"],
        )
        cloned_entries = rpg.list_entries(second["id"])

        self.assertTrue(second["is_active"])
        self.assertFalse(rpg.get_state(first["id"])["is_active"])
        self.assertEqual(second["abilities"]["intelligence"], 16)
        self.assertEqual(len(cloned_entries), 1)
        self.assertNotEqual(cloned_entries[0]["id"], first_entry["id"])

        rpg.update_state(second["id"], hp_current=14, hp_max=14, class_name="Wizard 2")
        rpg.update_entry(cloned_entries[0]["id"], name="Improved example spell")

        unchanged_first = rpg.get_state(first["id"])
        self.assertEqual(unchanged_first["hp_current"], 7)
        self.assertEqual(unchanged_first["class_name"], "Wizard 1")
        self.assertEqual(rpg.get_entry(first_entry["id"])["name"], "Example spell")

    def test_state_ownership_and_last_state_are_enforced(self) -> None:
        first_character = rpg.create_character("First", "dnd5e_2014")
        second_character = rpg.create_character("Second", "pf2e")
        with self.assertRaisesRegex(ValueError, "not found"):
            rpg.create_state(
                second_character["id"],
                level=2,
                clone_from_id=first_character["active_state_id"],
            )
        with self.assertRaisesRegex(ValueError, "at least one"):
            rpg.delete_state(first_character["id"], first_character["active_state_id"])

    def test_entries_validate_links_and_track_uses(self) -> None:
        character = rpg.create_character("Example cleric", "pf2e")
        state_id = character["active_state_id"]
        resource = rpg.add_entry(
            state_id,
            kind="resource",
            name="Example pool",
            current_uses=2,
            max_uses=3,
        )
        self.assertEqual(rpg.set_entry_uses(resource["id"], 1)["current_uses"], 1)
        with self.assertRaisesRegex(ValueError, "cannot exceed"):
            rpg.update_entry(resource["id"], current_uses=4)
        with self.assertRaisesRegex(ValueError, "http or https"):
            rpg.add_entry(
                state_id,
                kind="feature",
                name="Unsafe link",
                source_url="javascript:alert(1)",
            )

    def test_archive_filter_and_active_state_replacement(self) -> None:
        character = rpg.create_character("Example fighter", "dnd5e_2014")
        first_state_id = character["active_state_id"]
        second = rpg.create_state(
            character["id"],
            level=2,
            clone_from_id=first_state_id,
        )
        rpg.delete_state(character["id"], second["id"])
        self.assertTrue(rpg.get_state(first_state_id)["is_active"])

        rpg.set_character_archived(character["id"], True)
        self.assertEqual(rpg.list_characters(), [])
        self.assertEqual(len(rpg.list_characters(include_archived=True)), 1)

    def test_schema_v1_migrates_without_losing_character_data(self) -> None:
        character = rpg.create_character("Migration example", "dnd5e_2014")
        with closing(sqlite3.connect(rpg.db_path())) as connection:
            connection.execute("PRAGMA user_version=1")
            connection.execute("DROP TABLE class_progressions")
            connection.execute("DROP TABLE library_entries")
            connection.commit()

        rpg.init_db()

        self.assertEqual(rpg.get_character(character["id"])["name"], "Migration example")
        with closing(sqlite3.connect(rpg.db_path())) as connection:
            self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], 4)

    def test_schema_v2_adds_library_replacement_family(self) -> None:
        with closing(sqlite3.connect(rpg.db_path())) as connection:
            connection.execute("ALTER TABLE library_entries DROP COLUMN replacement_family")
            connection.execute("PRAGMA user_version=2")
            connection.commit()

        rpg.init_db()

        with closing(sqlite3.connect(rpg.db_path())) as connection:
            columns = {
                row[1]
                for row in connection.execute("PRAGMA table_info(library_entries)").fetchall()
            }
            self.assertIn("replacement_family", columns)
            self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], 4)

    def test_schema_v3_adds_progression_provider(self) -> None:
        with closing(sqlite3.connect(rpg.db_path())) as connection:
            connection.execute("ALTER TABLE class_progressions DROP COLUMN provider")
            connection.execute("PRAGMA user_version=3")
            connection.commit()

        rpg.init_db()

        with closing(sqlite3.connect(rpg.db_path())) as connection:
            columns = {
                row[1]
                for row in connection.execute("PRAGMA table_info(class_progressions)").fetchall()
            }
            self.assertIn("provider", columns)
            self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], 4)

    def test_library_copy_is_system_scoped_and_independent(self) -> None:
        template = rpg.create_library_entry(
            "dnd5e_2014",
            kind="spell",
            name="Example library spell",
            summary="Synthetic summary",
            rank=2,
            max_uses=3,
            source_url="https://example.com/srd/example-spell",
            source_title="Example SRD",
            license_name="CC BY 4.0",
        )
        dnd = rpg.create_character("Library wizard", "dnd5e_2014")
        pf2 = rpg.create_character("Library thaumaturge", "pf2e")

        copied = rpg.add_library_entries_to_state(
            dnd["active_state_id"],
            [template["id"]],
        )[0]

        self.assertEqual(copied["current_uses"], 3)
        self.assertEqual(copied["library_entry_id"], template["id"])
        rpg.update_entry(copied["id"], name="Character-specific spell")
        self.assertEqual(rpg.get_library_entry(template["id"])["name"], "Example library spell")
        with self.assertRaisesRegex(ValueError, "game system"):
            rpg.add_library_entries_to_state(pf2["active_state_id"], [template["id"]])

    def test_library_replacement_family_updates_only_new_snapshot(self) -> None:
        first = rpg.create_library_entry(
            "dnd5e_2014",
            kind="feature",
            name="Example die (d6)",
            replacement_family="example-die",
        )
        upgraded = rpg.create_library_entry(
            "dnd5e_2014",
            kind="feature",
            name="Example die (d8)",
            replacement_family="example-die",
        )
        character = rpg.create_character("Upgrade example", "dnd5e_2014")
        first_state_id = character["active_state_id"]
        rpg.add_library_entries_to_state(first_state_id, [first["id"]])
        second = rpg.create_state(
            character["id"],
            level=2,
            clone_from_id=first_state_id,
        )

        rpg.add_library_entries_to_state(second["id"], [upgraded["id"]])

        self.assertEqual(
            [entry["name"] for entry in rpg.list_entries(first_state_id)],
            ["Example die (d6)"],
        )
        self.assertEqual(
            [entry["name"] for entry in rpg.list_entries(second["id"])],
            ["Example die (d8)"],
        )

    def test_provider_upsert_rolls_back_invalid_progression(self) -> None:
        with self.assertRaisesRegex(ValueError, "unknown library entry"):
            rpg.upsert_provider_library(
                "dnd5e_2014",
                "synthetic-provider",
                [{
                    "external_key": "feature:valid",
                    "kind": "feature",
                    "name": "Valid entry",
                }],
                [{
                    "external_key": "feature:missing",
                    "class_name": "Example class",
                    "level": 1,
                    "grant_type": "automatic",
                }],
            )

        self.assertEqual(
            rpg.list_library_entries("dnd5e_2014", include_archived=True),
            [],
        )

    def test_guided_level_up_adds_automatic_and_selected_choices(self) -> None:
        automatic = rpg.create_library_entry(
            "dnd5e_2014", kind="feature", name="Automatic feature"
        )
        option_a = rpg.create_library_entry(
            "dnd5e_2014", kind="feature", name="Option A"
        )
        option_b = rpg.create_library_entry(
            "dnd5e_2014", kind="feature", name="Option B"
        )
        later = rpg.create_library_entry(
            "dnd5e_2014", kind="feature", name="Later feature"
        )
        rpg.add_class_progression(
            automatic["id"],
            class_name="Example class",
            level=2,
            grant_type="automatic",
        )
        for option in (option_a, option_b):
            rpg.add_class_progression(
                option["id"],
                class_name="Example class",
                level=2,
                grant_type="choice",
                choice_group="Example choice",
                choice_count=1,
            )
        rpg.add_class_progression(
            later["id"],
            class_name="Example class",
            level=3,
            grant_type="automatic",
        )
        character = rpg.create_character("Guided example", "dnd5e_2014")
        first_state_id = character["active_state_id"]

        third = rpg.create_guided_state(
            character["id"],
            source_state_id=first_state_id,
            class_name="Example class",
            target_level=3,
            selected_library_ids=[option_b["id"]],
        )

        names = {entry["name"] for entry in rpg.list_entries(third["id"])}
        self.assertEqual(names, {"Automatic feature", "Option B", "Later feature"})
        self.assertEqual(third["class_name"], "Example class 3")
        self.assertFalse(rpg.get_state(first_state_id)["is_active"])

    def test_guided_level_up_rejects_choice_without_partial_state(self) -> None:
        option = rpg.create_library_entry(
            "dnd5e_2014", kind="feature", name="Required option"
        )
        rpg.add_class_progression(
            option["id"],
            class_name="Choice class",
            level=2,
            grant_type="choice",
            choice_group="Required choice",
            choice_count=1,
        )
        character = rpg.create_character("Choice example", "dnd5e_2014")
        before = rpg.list_states(character["id"])

        with self.assertRaisesRegex(ValueError, "Required choice"):
            rpg.create_guided_state(
                character["id"],
                source_state_id=character["active_state_id"],
                class_name="Choice class",
                target_level=2,
                selected_library_ids=[],
            )

        self.assertEqual(rpg.list_states(character["id"]), before)
        self.assertTrue(rpg.get_state(character["active_state_id"])["is_active"])

    def test_guided_level_up_rejects_reused_choice(self) -> None:
        option_a = rpg.create_library_entry(
            "dnd5e_2014", kind="feature", name="Existing option"
        )
        option_b = rpg.create_library_entry(
            "dnd5e_2014", kind="feature", name="New option"
        )
        for option in (option_a, option_b):
            rpg.add_class_progression(
                option["id"],
                class_name="Choice class",
                level=2,
                grant_type="choice",
                choice_group="Example choice",
                choice_count=1,
            )
        character = rpg.create_character("Reuse example", "dnd5e_2014")
        rpg.add_library_entries_to_state(character["active_state_id"], [option_a["id"]])

        with self.assertRaisesRegex(ValueError, "new option"):
            rpg.create_guided_state(
                character["id"],
                source_state_id=character["active_state_id"],
                class_name="Choice class",
                target_level=2,
                selected_library_ids=[option_a["id"]],
            )

        state = rpg.create_guided_state(
            character["id"],
            source_state_id=character["active_state_id"],
            class_name="Choice class",
            target_level=2,
            selected_library_ids=[option_b["id"]],
        )
        self.assertEqual(
            {entry["name"] for entry in rpg.list_entries(state["id"])},
            {"Existing option", "New option"},
        )

    def test_guided_level_up_distinguishes_repeated_choice_pools(self) -> None:
        options = [
            rpg.create_library_entry(
                "dnd5e_2014", kind="feature", name=f"Repeated option {label}"
            )
            for label in ("A", "B", "C")
        ]
        progression_ids: dict[tuple[int, int], int] = {}
        for level in (2, 3):
            for option in options:
                progression = rpg.add_class_progression(
                    option["id"],
                    class_name="Repeated class",
                    level=level,
                    grant_type="choice",
                    choice_group="Repeated pool",
                    choice_count=1,
                )
                progression_ids[(level, option["id"])] = progression["id"]
        character = rpg.create_character("Repeated pool example", "dnd5e_2014")

        with self.assertRaisesRegex(ValueError, "new option"):
            rpg.create_guided_state(
                character["id"],
                source_state_id=character["active_state_id"],
                class_name="Repeated class",
                target_level=3,
                selected_progression_ids=[
                    progression_ids[(2, options[0]["id"])],
                    progression_ids[(3, options[0]["id"])],
                ],
            )

        state = rpg.create_guided_state(
            character["id"],
            source_state_id=character["active_state_id"],
            class_name="Repeated class",
            target_level=3,
            selected_progression_ids=[
                progression_ids[(2, options[0]["id"])],
                progression_ids[(3, options[1]["id"])],
            ],
        )
        self.assertEqual(
            {entry["name"] for entry in rpg.list_entries(state["id"])},
            {"Repeated option A", "Repeated option B"},
        )

    def test_guided_level_up_filters_and_records_subclass(self) -> None:
        first_option = rpg.create_library_entry(
            "dnd5e_2014", kind="feature", name="First subclass feature"
        )
        other_option = rpg.create_library_entry(
            "dnd5e_2014", kind="feature", name="Other subclass feature"
        )
        for entry, subclass in ((first_option, "First path"), (other_option, "Other path")):
            rpg.add_class_progression(
                entry["id"],
                class_name="Example class",
                subclass=subclass,
                level=2,
                grant_type="automatic",
            )
        character = rpg.create_character("Subclass example", "dnd5e_2014")

        state = rpg.create_guided_state(
            character["id"],
            source_state_id=character["active_state_id"],
            class_name="Example class",
            subclass="First path",
            target_level=2,
        )

        self.assertEqual(state["subclass"], "First path")
        self.assertEqual(
            [entry["name"] for entry in rpg.list_entries(state["id"])],
            ["First subclass feature"],
        )
        self.assertEqual(
            rpg.list_library_subclasses("dnd5e_2014", "Example class"),
            ["First path", "Other path"],
        )
        self.assertEqual(
            rpg.list_library_subclasses(
                "dnd5e_2014",
                "Example class",
                through_level=1,
            ),
            [],
        )
        with self.assertRaisesRegex(ValueError, "Choose a subclass"):
            rpg.create_guided_state(
                character["id"],
                source_state_id=character["active_state_id"],
                class_name="Example class",
                target_level=2,
            )
        with self.assertRaisesRegex(ValueError, "cannot change"):
            rpg.create_guided_state(
                character["id"],
                source_state_id=state["id"],
                class_name="Example class",
                subclass="Other path",
                target_level=3,
            )


if __name__ == "__main__":
    unittest.main()