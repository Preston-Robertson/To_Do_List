"""Authentication, CSRF, mutation, and ownership tests for Characters."""
from __future__ import annotations

import os
import tempfile
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from luigi_web import application, auth, rpg


class RpgRouteTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.env = patch.dict(os.environ, {
            "LUIGI_WEB_UI_TOKEN": "main-secret",
            "LUIGI_WEB_RPG_DB": os.path.join(self.temp_dir.name, "rpg.db"),
            "LUIGI_WEB_CARDS_REFRESH_HOURS": "0",
        })
        self.env.start()
        rpg.init_db()
        self.client = TestClient(application.app)
        self.client.cookies.set(auth.COOKIE_NAME, "main-secret")

    def tearDown(self) -> None:
        self.client.close()
        self.env.stop()
        self.temp_dir.cleanup()

    def csrf_headers(self) -> dict[str, str]:
        self.client.cookies.set(auth.CSRF_COOKIE_NAME, "csrf-value")
        return {"X-CSRF-Token": "csrf-value", "HX-Request": "true"}

    def test_routes_require_main_authentication(self) -> None:
        anonymous = TestClient(application.app)
        response = anonymous.get(
            "/characters",
            headers={"Accept": "text/html"},
            follow_redirects=False,
        )
        anonymous.close()
        self.assertEqual(response.status_code, 303)
        self.assertEqual(response.headers["location"], "/login")

    def test_mutations_require_csrf(self) -> None:
        response = self.client.post(
            "/characters",
            data={"name": "Rejected", "system_code": "pf2e", "level": 1},
        )
        self.assertEqual(response.status_code, 403)
        self.assertEqual(rpg.list_characters(), [])

    def test_pages_and_drawer_forms_render_without_caching(self) -> None:
        character = rpg.create_character("Example investigator", "pf2e", level=3)
        entry = rpg.add_entry(
            character["active_state_id"],
            kind="spell",
            name="Example cantrip",
            summary="A synthetic rules summary.",
        )
        paths = (
            "/characters",
            "/characters/new",
            f"/characters/{character['id']}",
            f"/characters/{character['id']}/edit",
            f"/characters/{character['id']}/states/new",
            f"/characters/{character['id']}/states/{character['active_state_id']}/edit",
            f"/characters/{character['id']}/states/{character['active_state_id']}/entries/new?kind=action",
            f"/characters/{character['id']}/states/{character['active_state_id']}/entries/{entry['id']}/edit",
        )
        for path in paths:
            with self.subTest(path=path):
                response = self.client.get(path)
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.headers["Cache-Control"], "no-store")
        sheet = self.client.get(f"/characters/{character['id']}")
        self.assertIn("Character level states", sheet.text)
        self.assertIn('data-rpg-tab="spells"', sheet.text)
        self.assertIn("Example cantrip", sheet.text)
        self.assertEqual(self.client.get("/static/css/rpg.css").status_code, 200)
        self.assertEqual(self.client.get("/static/js/rpg.js").status_code, 200)

    def test_new_state_suggestion_is_capped_at_level_twenty(self) -> None:
        character = rpg.create_character("Example champion", "pf2e", level=20)
        response = self.client.get(f"/characters/{character['id']}/states/new")
        self.assertEqual(response.status_code, 200)
        self.assertIn(
            'name="level" min="1" max="20" required value="20"',
            response.text,
        )

    def test_create_update_clone_and_entry_flow(self) -> None:
        headers = self.csrf_headers()
        created = self.client.post(
            "/characters",
            data={
                "name": "Example adventurer",
                "system_code": "dnd5e_2014",
                "campaign": "Example campaign",
                "level": 1,
            },
            headers=headers,
        )
        self.assertEqual(created.status_code, 204)
        self.assertEqual(created.headers["Cache-Control"], "no-store")
        self.assertEqual(created.headers["Cache-Control"], "no-store")
        character = rpg.list_characters()[0]
        state_id = character["active_state_id"]
        self.assertEqual(
            created.headers["HX-Redirect"],
            f"/characters/{character['id']}",
        )

        updated = self.client.post(
            f"/characters/{character['id']}/states/{state_id}",
            data={
                "hp_current": 8,
                "hp_max": 11,
                "ability_dexterity": 16,
                "skill_stealth_rank": 1,
                "skill_stealth_misc": 1,
            },
            headers=headers,
        )
        self.assertEqual(updated.status_code, 204)
        self.assertEqual(rpg.get_state(state_id)["abilities"]["dexterity"], 16)

        entry_response = self.client.post(
            f"/characters/{character['id']}/states/{state_id}/entries",
            data={
                "kind": "action",
                "name": "Example strike",
                "action_cost": "Action",
                "current_uses": 1,
                "max_uses": 2,
            },
            headers=headers,
        )
        self.assertEqual(entry_response.status_code, 204)
        first_entry = rpg.list_entries(state_id)[0]

        cloned = self.client.post(
            f"/characters/{character['id']}/states",
            data={
                "level": 2,
                "label": "Level 2 path",
                "clone_from_id": state_id,
            },
            headers=headers,
        )
        self.assertEqual(cloned.status_code, 204)
        second_state = rpg.list_states(character["id"])[1]
        second_entry = rpg.list_entries(second_state["id"])[0]
        self.assertNotEqual(first_entry["id"], second_entry["id"])

        uses = self.client.post(
            f"/characters/{character['id']}/states/{second_state['id']}/entries/{second_entry['id']}/uses",
            data={"current_uses": 0},
            headers=headers,
        )
        self.assertEqual(uses.status_code, 204)
        self.assertEqual(rpg.get_entry(second_entry["id"])["current_uses"], 0)
        self.assertEqual(rpg.get_entry(first_entry["id"])["current_uses"], 1)

    def test_state_and_entry_ids_are_character_scoped(self) -> None:
        headers = self.csrf_headers()
        first = rpg.create_character("First", "dnd5e_2014")
        second = rpg.create_character("Second", "pf2e")
        entry = rpg.add_entry(
            first["active_state_id"],
            kind="feature",
            name="First only",
        )

        wrong_state = self.client.post(
            f"/characters/{second['id']}/states/{first['active_state_id']}",
            data={"hp_current": 5},
            headers=headers,
        )
        wrong_entry = self.client.post(
            f"/characters/{second['id']}/states/{second['active_state_id']}/entries/{entry['id']}/delete",
            headers=headers,
        )
        self.assertEqual(wrong_state.status_code, 404)
        self.assertEqual(wrong_entry.status_code, 404)
        self.assertIsNotNone(rpg.get_entry(entry["id"]))

    def test_library_static_routes_and_explicit_srd_refresh(self) -> None:
        headers = self.csrf_headers()
        self.assertEqual(self.client.get("/characters/library").status_code, 200)
        new_form = self.client.get("/characters/library/new?system=pf2e&kind=spell")
        self.assertEqual(new_form.status_code, 200)
        self.assertIn("New library entry", new_form.text)

        with patch("luigi_web.rpg_routes.rpg_srd.refresh_dnd_2014") as refresh:
            rejected = self.client.post("/characters/library/srd/refresh")
            self.assertEqual(rejected.status_code, 403)
            refresh.assert_not_called()

            response = self.client.post(
                "/characters/library/srd/refresh",
                headers=headers,
            )
            self.assertEqual(response.status_code, 204)
            self.assertEqual(
                response.headers["HX-Redirect"],
                "/characters/library?system=dnd5e_2014&refreshed=true",
            )
            refresh.assert_called_once_with()

    def test_library_create_mapping_picker_and_guided_level_flow(self) -> None:
        headers = self.csrf_headers()
        character = rpg.create_character("Example fighter", "dnd5e_2014")
        state_id = character["active_state_id"]

        created = self.client.post(
            "/characters/library",
            data={
                "system_code": "dnd5e_2014",
                "kind": "feature",
                "name": "Local library feature",
                "summary": "Synthetic reusable entry.",
            },
            headers=headers,
        )
        self.assertEqual(created.status_code, 204)
        local_entry = next(
            entry for entry in rpg.list_library_entries("dnd5e_2014")
            if entry["name"] == "Local library feature"
        )
        self.assertEqual(
            created.headers["HX-Redirect"],
            "/characters/library?system=dnd5e_2014",
        )

        mapped = self.client.post(
            f"/characters/library/{local_entry['id']}/progressions",
            data={
                "class_name": "Example class",
                "level": 2,
                "grant_type": "automatic",
            },
            headers=headers,
        )
        self.assertEqual(mapped.status_code, 204)

        second_local = rpg.create_library_entry(
            "dnd5e_2014",
            kind="feature",
            name="Second local feature",
        )
        picker = self.client.get(
            f"/characters/{character['id']}/states/{state_id}/library?kind=feature"
        )
        self.assertEqual(picker.status_code, 200)
        self.assertIn("Local library feature", picker.text)
        self.assertIn("Second local feature", picker.text)

        added = self.client.post(
            f"/characters/{character['id']}/states/{state_id}/library",
            data={
                "kind": "feature",
                "library_entry_id": [local_entry["id"], second_local["id"]],
            },
            headers=headers,
        )
        self.assertEqual(added.status_code, 204)
        self.assertEqual(
            {entry["name"] for entry in rpg.list_entries(state_id)},
            {"Local library feature", "Second local feature"},
        )

        option_a = rpg.create_library_entry(
            "dnd5e_2014", kind="feature", name="Guided option A"
        )
        option_b = rpg.create_library_entry(
            "dnd5e_2014", kind="feature", name="Guided option B"
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
        option_b_progression = next(
            row for row in rpg.list_class_progressions(library_entry_id=option_b["id"])
            if row["class_name"] == "Example class"
        )
        level_form = self.client.get(
            f"/characters/{character['id']}/states/{state_id}/level-up"
            "?class_name=Example%20class&target_level=2"
        )
        self.assertEqual(level_form.status_code, 200)
        self.assertIn("Guided option A", level_form.text)
        self.assertIn("Local library feature", level_form.text)

        leveled = self.client.post(
            f"/characters/{character['id']}/states/{state_id}/level-up",
            data={
                "class_name": "Example class",
                "target_level": 2,
                "progression_id": [option_b_progression["id"]],
            },
            headers=headers,
        )
        self.assertEqual(leveled.status_code, 204)
        new_state = rpg.list_states(character["id"])[1]
        names = {entry["name"] for entry in rpg.list_entries(new_state["id"])}
        self.assertIn("Local library feature", names)
        self.assertIn("Guided option B", names)
        self.assertNotIn("Guided option A", names)

        pf2_entry = rpg.create_library_entry(
            "pf2e", kind="feature", name="PF2 only"
        )
        cross_system = self.client.post(
            f"/characters/{character['id']}/states/{state_id}/library",
            data={"kind": "feature", "library_entry_id": [pf2_entry["id"]]},
            headers=headers,
        )
        self.assertEqual(cross_system.status_code, 422)
        self.assertEqual(cross_system.headers["Cache-Control"], "no-store")
        self.assertEqual(
            {entry["name"] for entry in rpg.list_entries(state_id)},
            {"Local library feature", "Second local feature"},
        )


if __name__ == "__main__":
    unittest.main()