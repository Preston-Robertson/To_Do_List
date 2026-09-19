"""Synthetic, offline Steam provider and snapshot regressions."""
from collections import OrderedDict
from datetime import datetime, timedelta
import json
import os
from tempfile import TemporaryDirectory
import traceback
from typing import Any
import unittest
from unittest.mock import Mock, patch

from luigi_web.modules.media import steam


APP_ID = "123456789"
FAKE_ENV = {
    "LUIGI_WEB_STEAM_API_KEY": "synthetic-api-key",
    "LUIGI_WEB_STEAM_ID": "00000000000000001",
}


def owned(minutes: Any = 120, **fields):
    return {"response": {"games": [{
        "appid": int(APP_ID), "name": "Example game", "playtime_forever": minutes, **fields,
    }]}}


def achievements(*values):
    return {"playerstats": {"success": True, "gameName": "Example game", "achievements": [
        {"apiname": f"Example achievement {index}", "achieved": value}
        for index, value in enumerate(values)
    ]}}


class SteamTestCase(unittest.TestCase):
    def setUp(self):
        self.directory = self.enterContext(TemporaryDirectory(prefix="luigi-steam-test-"))
        self.enterContext(patch.dict(os.environ, {**FAKE_ENV, "LUIGI_WEB_DATA_DIR": self.directory}, clear=True))


@unittest.skipIf(steam.httpx is None, "HTTPX is not installed")
class SteamProviderTests(SteamTestCase):
    def setUp(self):
        super().setUp()
        self.client_type = self.enterContext(patch.object(steam.httpx, "Client"))
        self.client = self.client_type.return_value.__enter__.return_value

    def responses(self, playtime, progress):
        def response(payload):
            if isinstance(payload, Exception):
                return payload
            result = Mock()
            result.json.return_value = payload
            return result
        self.client.get.side_effect = [response(playtime), response(progress)]

    def test_private_empty_is_unknown_but_real_zero_is_known(self):
        for payload in ({}, {"response": {}}, {"response": {"games": []}}):
            with self.subTest(payload=payload):
                self.responses(payload, achievements(0))
                result = steam._fetch(APP_ID)
                self.assertIsNone(result["hours_played"])
                self.assertTrue(result["playtime_unavailable"])
        self.responses(owned(0, playtime_2weeks=0), achievements(0))
        result = steam._fetch(APP_ID)
        self.assertEqual(result["hours_played"], 0.0)
        self.assertEqual(result["hours_recent"], 0.0)
        self.assertFalse(result["playtime_unavailable"])

    def test_actual_app_id_not_first_game_controls_hours(self):
        payload = owned(90)
        payload["response"]["games"].insert(0, {"appid": 987654321, "playtime_forever": 600})
        self.responses(payload, achievements(1, 0))
        self.assertEqual(steam._fetch(APP_ID)["hours_played"], 1.5)
        self.responses({"response": {"games": [payload["response"]["games"][0]]}}, achievements(0))
        self.assertIsNone(steam._fetch(APP_ID)["hours_played"])

    def test_duplicate_app_entries_are_not_trusted(self):
        payload = owned()
        payload["response"]["games"] *= 2
        self.responses(payload, achievements(0))
        self.assertIsNone(steam._fetch(APP_ID)["hours_played"])

    def test_missing_achievements_are_unknown_and_not_complete(self):
        for payload in ({}, {"playerstats": {}}, {"playerstats": {"success": True}},
                        {"playerstats": {"success": False, "achievements": []}}):
            with self.subTest(payload=payload):
                self.responses(owned(), payload)
                result = steam._fetch(APP_ID)
                self.assertIsNone(result["achievements_unlocked"])
                self.assertIsNone(result["achievements_total"])
                self.assertIsNone(result["achievement_percent"])
                self.assertTrue(result["achievements_unavailable"])
                self.assertFalse(result["complete"])

    def test_explicit_empty_achievements_are_known_zero_not_complete(self):
        self.responses(owned(), achievements())
        result = steam._fetch(APP_ID)
        self.assertEqual(result["achievements_total"], 0)
        self.assertFalse(result["achievements_unavailable"])
        self.assertFalse(result["complete"])

    def test_achievement_progress_and_next_list(self):
        self.responses(owned(135), achievements(1, 0, 0, 0, 0, 0, 0))
        result = steam._fetch(APP_ID)
        self.assertEqual(result["hours_played"], 2.2)
        self.assertEqual(result["achievements_unlocked"], 1)
        self.assertEqual(result["achievements_total"], 7)
        self.assertEqual(result["achievement_percent"], 14)
        self.assertEqual(len(result["next_achievements"]), 5)
        self.assertFalse(result["complete"])
        self.responses(owned(), achievements(1, 1))
        self.assertTrue(steam._fetch(APP_ID)["complete"])

    def test_partial_failures_keep_the_other_part(self):
        error = RuntimeError("Synthetic provider failure")
        self.responses(error, achievements(1, 0))
        result = steam._fetch(APP_ID)
        self.assertIsNone(result["hours_played"])
        self.assertEqual(result["achievements_total"], 2)
        self.responses(owned(), error)
        result = steam._fetch(APP_ID)
        self.assertEqual(result["hours_played"], 2.0)
        self.assertIsNone(result["achievements_total"])

    def test_http_error_does_not_trust_a_success_shaped_body(self):
        denied = Mock()
        denied.raise_for_status.side_effect = RuntimeError("Synthetic denial")
        denied.json.return_value = owned(0)
        valid = Mock()
        valid.json.return_value = achievements(0)
        self.client.get.side_effect = [denied, valid]
        self.assertIsNone(steam._fetch(APP_ID)["hours_played"])
        denied.json.assert_not_called()

    def test_invalid_minutes_are_unknown_not_clamped_to_zero(self):
        for minutes in (None, -1, float("nan"), float("inf"), -float("inf"), True,
                        "0", {}, [], 10 ** 400):
            with self.subTest(minutes=minutes):
                self.responses(owned(minutes, playtime_2weeks=minutes), achievements(0))
                result = steam._fetch(APP_ID)
                self.assertIsNone(result["hours_played"])
                self.assertIsNone(result["hours_recent"])

    def test_malformed_playtime_schemas_stay_unknown(self):
        for payload in (None, [], "invalid", {"response": []}, {"response": {"games": {}}},
                        {"response": {"games": [None, {}, {"appid": APP_ID}]}}):
            with self.subTest(payload=payload):
                self.responses(payload, achievements(0))
                self.assertIsNone(steam._fetch(APP_ID)["hours_played"])

    def test_malformed_achievement_schemas_stay_unknown(self):
        payloads = [None, [], {"playerstats": []}, {"playerstats": {"success": 1, "achievements": []}}]
        for entries in (None, {}, [None], [{}], [{"achieved": "0"}], [{"achieved": True}],
                        [{"achieved": -1}], [{"achieved": float("nan")}], [{"achieved": 2}]):
            payloads.append({"playerstats": {"success": True, "achievements": entries}})
        for payload in payloads:
            with self.subTest(payload=payload):
                self.responses(owned(), payload)
                result = steam._fetch(APP_ID)
                self.assertIsNone(result["achievements_total"])
                self.assertFalse(result["complete"])

    def test_both_unknown_and_provider_errors_are_generic(self):
        secret_error = RuntimeError(f"{steam._OWNED_URL}?key={FAKE_ENV['LUIGI_WEB_STEAM_API_KEY']}"
                                    f"&steamid={FAKE_ENV['LUIGI_WEB_STEAM_ID']}")
        for parts in (({}, {}), (secret_error, secret_error)):
            with self.subTest(parts="unavailable"):
                self.responses(*parts)
                with self.assertRaisesRegex(RuntimeError, "^Steam statistics are unavailable\\.$") as caught:
                    steam._fetch(APP_ID)
                rendered = "".join(traceback.format_exception(caught.exception))
                for secret in (*FAKE_ENV.values(), steam._OWNED_URL):
                    self.assertNotIn(secret, rendered)

    def test_missing_configuration_does_not_create_client(self):
        for missing in FAKE_ENV:
            with self.subTest(missing=missing), patch.dict(os.environ, {missing: " "}):
                with self.assertRaisesRegex(RuntimeError, "^Steam statistics are unavailable\\.$"):
                    steam._fetch(APP_ID)
        self.client_type.assert_not_called()

    def test_invalid_app_ids_do_not_create_client(self):
        invalid_ids: tuple[Any, ...] = (
            None, True, "", "0", "-1", "1.5", "1e3", "https://example.invalid", "4294967296", "\u0661",
        )
        for app_id in invalid_ids:
            with self.subTest(app_id=app_id), self.assertRaises(ValueError):
                steam._fetch(app_id)
        self.client_type.assert_not_called()

    def test_fixed_urls_bounded_timeout_and_no_redirects(self):
        self.responses(owned(), achievements(0))
        result = steam._fetch("0123456789")
        self.assertEqual(result["app_id"], APP_ID)
        self.assertEqual([call.args[0] for call in self.client.get.call_args_list],
                         [steam._OWNED_URL, steam._ACHIEVEMENTS_URL])
        settings = self.client_type.call_args.kwargs
        self.assertEqual(settings["timeout"], 10.0)
        self.assertIs(settings["follow_redirects"], False)
        self.assertIs(settings["trust_env"], False)
        for secret in FAKE_ENV.values():
            self.assertNotIn(secret, json.dumps(result))


class SteamOptionalDependencyTests(SteamTestCase):
    def test_missing_httpx_is_generic(self):
        with patch.object(steam, "httpx", None):
            with self.assertRaisesRegex(RuntimeError, "^Steam statistics are unavailable\\.$"):
                steam._fetch(APP_ID)


@unittest.skipIf(steam.httpx is None, "HTTPX is not installed")
class SteamTransportTests(SteamTestCase):
    def test_credentials_reach_only_transport_not_httpx_logs(self):
        urls = []

        def handle_request(_transport, request):
            urls.append(request.url)
            payload = owned() if request.url.path == "/IPlayerService/GetOwnedGames/v1/" else achievements(0)
            return steam.httpx.Response(200, json=payload)

        with patch.object(
            steam.httpx.HTTPTransport, "handle_request", handle_request,
        ), self.assertLogs("httpx", level="INFO") as logs:
            result = steam._fetch(APP_ID)
        self.assertEqual(len(urls), 2)
        for url in urls:
            self.assertEqual(url.params["key"], FAKE_ENV["LUIGI_WEB_STEAM_API_KEY"])
            self.assertEqual(url.params["steamid"], FAKE_ENV["LUIGI_WEB_STEAM_ID"])
        for secret in FAKE_ENV.values():
            self.assertNotIn(secret, "\n".join(logs.output))
            self.assertNotIn(secret, json.dumps(result))


class SteamSnapshotTests(SteamTestCase):
    def setUp(self):
        super().setUp()
        self.enterContext(patch.object(steam, "_cache", OrderedDict()))
        self.clock = self.enterContext(patch.object(steam, "monotonic", return_value=1000.0))
        self.snapshot = {
            "app_id": APP_ID,
            "name": "Example game",
            "hours_played": 2.5,
            "hours_recent": None,
            "achievements_unlocked": 0,
            "achievements_total": 1,
            "achievement_percent": 0,
            "complete": False,
            "next_achievements": [{"name": "Example achievement", "description": ""}],
            "playtime_unavailable": False,
            "achievements_unavailable": False,
        }
        self.fetch = self.enterContext(patch.object(steam, "_fetch", return_value=self.snapshot))
        if steam.httpx is not None:
            self.client_type = self.enterContext(patch.object(
                steam.httpx, "Client", side_effect=AssertionError("Unexpected HTTP client"),
            ))

    def refresh(self, profile="Example profile", title="Example game", app_id: str | int = APP_ID):
        return steam.refresh(profile, title, app_id)

    def get(self, profile="Example profile", title="Example game", app_id: str | int = APP_ID):
        return steam.get_snapshot(profile, title, app_id)

    def save(self, snapshot_id, profile="Example profile", title="Example game", app_id: str | int = APP_ID):
        return steam.saved_hours(profile, title, app_id, snapshot_id)

    def test_cache_miss_never_fetches(self):
        self.assertEqual(self.get(), {"snapshot": None, "snapshot_id": None, "fetched_at": None, "stale": True})
        self.fetch.assert_not_called()

    def test_refresh_envelope_has_opaque_id_utc_time_and_no_secrets(self):
        result = self.refresh()
        self.fetch.assert_called_once_with(APP_ID)
        self.assertEqual(set(result), {"snapshot", "snapshot_id", "fetched_at", "stale"})
        self.assertEqual(result["snapshot"], self.snapshot)
        self.assertFalse(result["stale"])
        self.assertRegex(result["snapshot_id"], r"^[A-Za-z0-9_-]{43}$")
        self.assertEqual(datetime.fromisoformat(result["fetched_at"]).utcoffset(), timedelta(0))
        for secret in FAKE_ENV.values():
            self.assertNotIn(secret, json.dumps(result))
            self.assertNotIn(secret, repr(steam._cache))
        self.assertEqual(os.listdir(self.directory), [])

    def test_cache_hits_and_stale_reads_never_fetch_or_renew_age(self):
        result = self.refresh()
        self.fetch.reset_mock()
        for age, stale in ((0, False), (299.999, False), (300, True), (599, True), (600, True), (3600, True)):
            with self.subTest(age=age):
                self.clock.return_value = 1000 + age
                cached = self.get()
                self.assertEqual(cached["snapshot_id"], result["snapshot_id"])
                self.assertEqual(cached["fetched_at"], result["fetched_at"])
                self.assertEqual(cached["stale"], stale)
        self.fetch.assert_not_called()
        if steam.httpx is not None:
            self.client_type.assert_not_called()

    def test_save_allows_known_zero_and_stale_but_unexpired_hours(self):
        for hours in (0.0, 2.5):
            with self.subTest(hours=hours):
                self.clock.return_value = 1000.0
                self.snapshot["hours_played"] = hours
                result = self.refresh()
                self.fetch.reset_mock()
                for age in (0, 300, 599.999):
                    self.clock.return_value = 1000 + age
                    saved = self.save(result["snapshot_id"])
                    self.assertIsInstance(saved, float)
                    self.assertEqual(saved, hours)
                self.fetch.assert_not_called()

    def test_save_expires_at_ten_minutes(self):
        result = self.refresh()
        self.fetch.reset_mock()
        for age in (600, 601, 3600, -1):
            with self.subTest(age=age):
                self.clock.return_value = 1000 + age
                self.get()
                with self.assertRaisesRegex(RuntimeError, "Refresh before saving"):
                    self.save(result["snapshot_id"])
        self.fetch.assert_not_called()

    def test_cross_profile_title_and_app_cannot_read_or_save(self):
        result = self.refresh()
        self.fetch.reset_mock()
        for binding in (
            {"profile": "Other profile"}, {"title": "Other game"}, {"app_id": "987654321"},
        ):
            with self.subTest(binding=binding):
                self.assertIsNone(self.get(**binding)["snapshot"])
                with self.assertRaises(RuntimeError):
                    self.save(result["snapshot_id"], **binding)
        self.fetch.assert_not_called()

    def test_both_configuration_values_bind_reads_and_saves(self):
        result = self.refresh()
        self.fetch.reset_mock()
        for name, replacement in (
            ("LUIGI_WEB_STEAM_API_KEY", "different-synthetic-key"),
            ("LUIGI_WEB_STEAM_ID", "00000000000000002"),
            ("LUIGI_WEB_STEAM_API_KEY", ""),
            ("LUIGI_WEB_STEAM_ID", ""),
        ):
            with self.subTest(name=name, replacement=replacement), patch.dict(os.environ, {name: replacement}):
                self.assertIsNone(self.get()["snapshot"])
                with self.assertRaises(RuntimeError):
                    self.save(result["snapshot_id"])
        self.assertEqual(self.save(result["snapshot_id"]), 2.5)
        self.fetch.assert_not_called()

    def test_credential_rotation_during_fetch_is_not_cached(self):
        for name in FAKE_ENV:
            with self.subTest(name=name), patch.dict(os.environ, FAKE_ENV):
                def changed_config(_app_id):
                    os.environ[name] = "different-synthetic-value"
                    return self.snapshot
                self.fetch.side_effect = changed_config
                with self.assertRaisesRegex(RuntimeError, "^Steam statistics are unavailable\\.$"):
                    self.refresh()
                self.assertEqual(len(steam._cache), 0)

    def test_missing_configuration_refresh_is_generic_without_fetch(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertIsNone(self.get()["snapshot"])
            with self.assertRaisesRegex(RuntimeError, "^Steam statistics are unavailable\\.$"):
                self.refresh()
            with self.assertRaises(RuntimeError):
                self.save("unknown-token")
        self.fetch.assert_not_called()

    def test_mismatched_provider_app_is_rejected(self):
        self.snapshot["app_id"] = "987654321"
        with self.assertRaises(RuntimeError):
            self.refresh()
        self.assertIsNone(self.get()["snapshot"])

    def test_unknown_and_invalid_cached_hours_are_not_saveable(self):
        for hours in (None, -1, float("nan"), float("inf"), True, "0", 10 ** 400):
            with self.subTest(hours=hours):
                self.snapshot["hours_played"] = hours
                result = self.refresh()
                with self.assertRaises(RuntimeError):
                    self.save(result["snapshot_id"])
        self.snapshot.update(hours_played=0.0, playtime_unavailable=True)
        result = self.refresh()
        with self.assertRaises(RuntimeError):
            self.save(result["snapshot_id"])

    def test_unavailable_achievements_do_not_block_saving_known_hours(self):
        self.snapshot.update(achievements_unlocked=None, achievements_total=None, achievements_unavailable=True)
        result = self.refresh()
        self.assertEqual(self.save(result["snapshot_id"]), 2.5)

    def test_invalid_or_wrong_token_is_not_saveable(self):
        result = self.refresh()
        for token in (None, 42, "", "unknown-token", "\u0661"):
            with self.subTest(token=token), self.assertRaises(RuntimeError):
                self.save(token)
        self.assertEqual(self.save(result["snapshot_id"]), 2.5)

    def test_explicit_refresh_replaces_id_and_invalidates_previous_token(self):
        previous = self.refresh()
        self.snapshot["hours_played"] = 3.0
        self.clock.return_value += 1
        current = self.refresh()
        self.assertNotEqual(current["snapshot_id"], previous["snapshot_id"])
        self.assertEqual(self.fetch.call_count, 2)
        with self.assertRaises(RuntimeError):
            self.save(previous["snapshot_id"])
        self.assertEqual(self.save(current["snapshot_id"]), 3.0)

    def test_failed_refresh_preserves_previous_snapshot_and_age(self):
        previous = self.refresh()
        self.clock.return_value += 300
        self.fetch.side_effect = RuntimeError(f"{steam._OWNED_URL} {FAKE_ENV['LUIGI_WEB_STEAM_API_KEY']}")
        with self.assertRaises(RuntimeError) as caught:
            self.refresh()
        self.assertEqual(str(caught.exception), "Steam statistics are unavailable.")
        cached = self.get()
        self.assertEqual(cached["snapshot_id"], previous["snapshot_id"])
        self.assertEqual(cached["fetched_at"], previous["fetched_at"])
        self.assertTrue(cached["stale"])

    def test_callers_cannot_mutate_cached_snapshot_or_nested_values(self):
        result = self.refresh()
        result["snapshot"]["hours_played"] = 999
        result["snapshot"]["next_achievements"][0]["name"] = "Changed"
        self.snapshot["hours_played"] = 888
        cached = self.get()
        self.assertEqual(cached["snapshot"]["hours_played"], 2.5)
        self.assertEqual(cached["snapshot"]["next_achievements"][0]["name"], "Example achievement")
        cached["snapshot"]["hours_played"] = 777
        self.assertEqual(self.save(result["snapshot_id"]), 2.5)

    def test_cache_is_bounded_and_evicts_least_recently_read_entry(self):
        oldest = self.refresh(title="Example game 0")
        evicted = self.refresh(title="Example game 1")
        for index in range(2, 256):
            self.refresh(title=f"Example game {index}")
        self.assertEqual(len(steam._cache), 256)
        self.get(title="Example game 0")
        self.refresh(title="Example game 256")
        self.assertEqual(len(steam._cache), 256)
        self.assertEqual(self.save(oldest["snapshot_id"], title="Example game 0"), 2.5)
        self.assertIsNone(self.get(title="Example game 1")["snapshot"])
        with self.assertRaises(RuntimeError):
            self.save(evicted["snapshot_id"], title="Example game 1")

    def test_repeated_refreshes_reuse_one_cache_slot(self):
        tokens = {self.refresh()["snapshot_id"] for _index in range(260)}
        self.assertEqual(len(tokens), 260)
        self.assertEqual(len(steam._cache), 1)

    def test_canonical_app_id_is_shared_across_all_three_operations(self):
        result = self.refresh(app_id="0123456789")
        self.assertEqual(self.get(app_id=int(APP_ID))["snapshot_id"], result["snapshot_id"])
        self.assertEqual(self.save(result["snapshot_id"], app_id=APP_ID), 2.5)

    def test_invalid_identity_fails_before_fetch(self):
        invalid_bindings: tuple[dict[str, Any], ...] = (
            {"profile": " "}, {"title": None}, {"app_id": "invalid"},
        )
        for binding in invalid_bindings:
            with self.subTest(binding=binding):
                with self.assertRaises(ValueError):
                    self.refresh(**binding)
                with self.assertRaises(ValueError):
                    self.get(**binding)
                with self.assertRaises(ValueError):
                    self.save("unknown-token", **binding)
        self.fetch.assert_not_called()


if __name__ == "__main__":
    unittest.main()