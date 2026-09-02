import os
import tempfile
import unittest
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


if __name__ == "__main__":
    unittest.main()