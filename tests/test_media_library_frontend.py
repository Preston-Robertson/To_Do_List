from copy import deepcopy
import hashlib
import json
from pathlib import Path
import re
import struct
import unittest
import zlib

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.testclient import TestClient
from jinja2 import Environment, FileSystemLoader, select_autoescape


ROOT = Path(__file__).resolve().parents[1]
MEDIA = ROOT / "luigi_web" / "modules" / "media"
CORE = ROOT / "luigi_web" / "core"


def synthetic_state(section="shows"):
    active = "playing" if section == "games" else "watching"
    statuses = ["backlog", active, "paused" if section == "games" else "on_hold",
                "completed", "dropped"]
    if section == "games":
        statuses.insert(-1, "achievements")
    return {
        "section": section, "profile": "", "profiles": ["Example profile"],
        "statuses": statuses, "status_labels": {status: status.title() for status in statuses},
        "items": [{"key": str(index) * 64, "version": "a" * 64,
                   "section": section, "profile": "Example profile",
                   "title": f"Example title {index}", "status": status,
                   "priority": 3, "rating": None, "platform": "Example platform",
                   "genre": "Example genre", "tags": ["Example tag"], "notes": "",
                   "cover_url": "", "link": "", "source": "manual",
                   "external_id": "", "current_season": 1, "current_episode": 2,
                   "total_episodes": 12, "hours_played": "", "date_added": "2026-09-01",
                   "date_started": "", "date_completed": "", "last_played": ""}
                  for index, status in enumerate(statuses, 1)],
        "updated_at": "2026-09-19T12:00:00Z", "cached": False,
    }


def render_library(section="shows", state=None, disabled=False):
    templates = Environment(loader=FileSystemLoader([MEDIA / "templates", CORE / "templates"]),
                            autoescape=select_autoescape())
    return templates.get_template("library.html").render(
        section=section, page_title=section.title(), active_nav=section,
        disabled_reason="Unavailable" if disabled else None,
        media_seed=state if state is not None else synthetic_state(section),
        landing_path="/home", navigation_groups=[], module_count=1,
        module_enabled=lambda name: False, shell_asset_version="synthetic",
        asset_version="synthetic",
    )


class MediaLibraryTemplateTests(unittest.TestCase):
    def test_tabs_have_distinct_live_panels_and_continue_default(self):
        html = render_library()
        for view in ("continue", "board", "list"):
            self.assertIn(f'aria-controls="library-{view}"', html)
            self.assertIn(f'id="library-{view}"', html)
        self.assertRegex(html, r'id="library-tab-continue"[^>]+aria-selected="true"')
        self.assertEqual(html.count('role="tabpanel"'), 3)

    def test_legacy_add_only_and_local_assets(self):
        for section in ("games", "shows"):
            html = render_library(section)
            self.assertIn(f'hx-get="/gnw/{section}/new"', html)
            self.assertIn('hx-target="#modal-body"', html)
            self.assertIn(f'/media/insights?section={section}', html)
            self.assertIn('/module-assets/media/library.js?v=synthetic" defer', html)
            self.assertIn('/module-assets/media/library.css?v=synthetic', html)
            self.assertNotIn("hx-post=", html)

    def test_seed_escapes_untrusted_titles(self):
        state = synthetic_state()
        state["items"][0]["title"] = "</script><script>unsafe()</script>"
        html = render_library(state=state)
        encoded = re.search(r'id="library-seed">(.*?)</script>', html, re.S).group(1)
        self.assertEqual(json.loads(encoded), state)
        self.assertNotIn(state["items"][0]["title"], html)

    def test_disabled_state_has_no_write_controls_or_raw_error(self):
        html = render_library(disabled=True)
        self.assertIn('data-disabled="true"', html)
        self.assertNotIn('id="library-add"', html)
        self.assertNotIn('id="library-filters"', html)

    def test_native_dialogs_and_icons_are_local(self):
        html = render_library()
        for name in ("detail", "choice", "confirm"):
            self.assertIn(f'<dialog id="library-{name}"', html)
            self.assertIn(f'aria-labelledby="library-{name}-title"', html)
        for icon in re.findall(r"/static/icons/lucide/([\w-]+)\.svg", html):
            self.assertTrue((CORE / "static" / "icons" / "lucide" / f"{icon}.svg").is_file(), icon)

    def test_secondary_filters_are_in_a_native_disclosure(self):
        html = render_library()
        self.assertIn('<details class="library-filter-more" id="library-filter-more">', html)
        self.assertIn('id="library-filter-label">Filters', html)
        for name in ("query", "profile", "status", "platform", "genre", "tag", "priority", "rating", "sort"):
            self.assertIn(f'name="{name}"', html)


def synthetic_cover():
    width, height = 120, 160
    pixels = b"".join(b"\x00" + b"".join(
        bytes((36 + horizontal, 70 + vertical // 2, 140 if horizontal < 60 else 80))
        for horizontal in range(width)) for vertical in range(height))

    def chunk(kind, content):
        return struct.pack("!I", len(content)) + kind + content + struct.pack("!I", zlib.crc32(kind + content))

    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack("!2I5B", width, height, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(pixels)) + chunk(b"IEND", b""))


def synthetic_media_app():
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    states = {section: synthetic_state(section) for section in ("games", "shows")}
    undo_records = {}
    activity = {}
    runs = {}
    controls = {"mode": "normal", "block_reads": False}
    calls = []
    app.mount("/static", StaticFiles(directory=CORE / "static"))
    app.mount("/module-assets/media", StaticFiles(directory=MEDIA / "static"))

    def response(value, status=200):
        return JSONResponse(value, status_code=status, headers={"Cache-Control": "no-store"})

    def find(section, profile, title):
        return next((item for item in states[section]["items"]
                     if item["profile"] == profile and item["title"] == title), None)

    def current(section, profile):
        state = deepcopy(states[section])
        state["profile"] = profile
        state["items"] = [item for item in state["items"] if not profile or item["profile"] == profile]
        return state

    @app.get("/__fixture__/cover.png")
    def cover_image():
        return Response(synthetic_cover(), media_type="image/png")

    @app.post("/__fixture__/control")
    async def control(request: Request):
        controls.update(await request.json())
        return {"ok": True}

    @app.get("/__fixture__/calls")
    def recorded_calls():
        return calls

    @app.get("/fixture/{section}", response_class=HTMLResponse)
    def page(section: str, request: Request):
        if section not in states:
            return HTMLResponse("Not found", status_code=404)
        for item in states[section]["items"]:
            item["cover_url"] = str(request.base_url) + "__fixture__/cover.png"
        if section == "games":
            states[section]["items"][1].update(source="steam", external_id="123", link="https://example.invalid/catalog")
        return HTMLResponse(render_library(section, states[section]), headers={"Cache-Control": "no-store"})

    @app.get("/media/{section}/data")
    def data(section: str, profile: str = ""):
        calls.append({"endpoint": "data", "profile": profile})
        if controls["block_reads"]:
            return response({"detail": "Unavailable"}, 503)
        return response(current(section, profile))

    @app.post("/media/{section}/refresh")
    async def refresh(section: str, request: Request):
        form = await request.form()
        if controls["block_reads"]:
            return response({"detail": "Unavailable"}, 503)
        return response(current(section, str(form.get("profile", ""))))

    @app.get("/media/{section}/detail")
    def detail(section: str, profile: str, title: str):
        item = find(section, profile, title)
        if item is None:
            return response({"detail": "Not found"}, 404)
        return response({"item": item, "activity": activity.get(item["key"], []),
                         "runs": runs.get(item["key"], []), "history_warning": None})

    def steam_snapshot():
        return {"snapshot": {"hours_played": 7.5, "hours_recent": None,
                             "achievements_unlocked": None, "achievements_total": 10,
                             "achievement_percent": None, "complete": False,
                             "next_achievements": [{"name": "Example achievement", "description": "Example goal"}],
                             "app_id": "123", "name": "Example title 2"},
                "snapshot_id": "synthetic-snapshot", "fetched_at": "2026-09-19T12:00:00Z", "stale": True}

    @app.get("/media/games/steam")
    def steam():
        return response(steam_snapshot())

    @app.post("/media/games/steam/refresh")
    def refresh_steam():
        return response({**steam_snapshot(), "stale": False})

    @app.post("/media/{section}/pick")
    async def pick(section: str, request: Request):
        form = dict(await request.form())
        calls.append({"endpoint": "pick", **form})
        keys = json.loads(str(form["keys"]))
        item = next((item for item in states[section]["items"] if item["key"] in keys), None)
        return response({"item": item})

    @app.post("/media/{section}/{endpoint:path}")
    async def write(section: str, endpoint: str, request: Request):
        form = dict(await request.form())
        calls.append({"endpoint": endpoint, **form})
        if controls["mode"] == "reject":
            return response({"detail": "Invalid change"}, 422)
        if endpoint == "undo":
            record = undo_records.pop(str(form.get("token")), None)
            if record is None:
                return response({"detail": "Expired"}, 409)
            item = find(section, record["profile"], record["title"])
            item.update(record)
        else:
            item = find(section, form.get("profile"), form.get("title"))
            if item is None or item["version"] != form.get("expected_version"):
                return response({"detail": "Conflict"}, 409)
            if controls["mode"] == "conflict":
                item["version"] = hashlib.sha256(f"conflict-{len(calls)}".encode()).hexdigest()
                return response({"detail": "Conflict"}, 409)
            previous = deepcopy(item)
            if endpoint == "episode":
                item["current_episode"] += 1
            elif endpoint == "change":
                for field, value in json.loads(str(form["fields"])).items():
                    if field in {"priority", "rating", "current_season", "current_episode", "total_episodes"}:
                        value = int(value) if value != "" else None
                    if field == "tags":
                        value = [part.strip() for part in value.split(",") if part.strip()]
                    item[field] = value
            elif endpoint == "runs":
                item["status"] = "playing" if section == "games" else "watching"
                if section == "shows":
                    item.update(current_season=1, current_episode=0)
                records = runs.setdefault(item["key"], [])
                records.append({"id": str(len(records) + 1), "number": len(records) + 1,
                                "started_at": "2026-09-19", "completed_at": None, "status": "active", "origin": "web"})
            elif endpoint == "steam/save" and form.get("snapshot_id") == "synthetic-snapshot":
                item["hours_played"] = "7.5"
            else:
                return response({"detail": "Invalid change"}, 400)
            token = f"synthetic-undo-{len(calls)}"
            undo_records[token] = previous
            activity.setdefault(item["key"], []).append({"id": str(len(calls)), "kind": endpoint,
                "occurred_at": "2026-09-19T12:00:00Z", "changes": {field: {"before": previous.get(field), "after": value}
                    for field, value in item.items() if value != previous.get(field)}})
        item["version"] = hashlib.sha256(f"confirmed-{len(calls)}".encode()).hexdigest()
        if controls["mode"] == "uncertain":
            return response({"detail": "Unavailable"}, 503)
        return response({"ok": True, "item": item, "undo_token": token if endpoint != "undo" else None,
                         "undo_ttl_ms": 12000, "history_warning": None})

    return app


class MediaLibraryClientContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.script = (MEDIA / "static" / "library.js").read_text(encoding="utf-8")
        cls.styles = (MEDIA / "static" / "library.css").read_text(encoding="utf-8")

    def test_only_confirmed_mutation_replies_replace_items(self):
        mutation = self.script.split("async function mutate(", 1)[1].split("async function performUndo", 1)[0]
        self.assertLess(mutation.index("await request(endpoint, payload)"), mutation.index("replaceConfirmed(reply.item, key)"))
        self.assertLess(mutation.index("reply.ok !== true"), mutation.index("replaceConfirmed(reply.item, key)"))
        self.assertIn("expected_version: item.version", mutation)
        self.assertIn("pending || loading || uncertain", mutation)
        self.assertNotIn("location.reload", self.script)
        self.assertNotIn("current_episode++", self.script)

    def test_uncertain_writes_require_recovery_and_keep_drafts(self):
        self.assertIn("await loadState(state.profile, false, true)", self.script)
        self.assertIn('request(refresh || recovery ? "refresh"', self.script)
        self.assertIn("if (recovery) uncertain = false", self.script)
        self.assertIn("if (!(name in changed)) form.elements[name].value = value", self.script)
        self.assertIn("conflict.dataset.conflict = name", self.script)
        self.assertIn("Your draft is retained.", self.script)
        self.assertIn("new AbortController()", self.script)
        self.assertIn("clearTimeout(timeout)", self.script)
        self.assertIn('button("Reload library"', self.script)
        self.assertIn("Your draft is kept.", self.script)
        self.assertNotIn("drafts.clear()", self.script)

    def test_storage_is_preferences_only_and_scoped(self):
        storage = self.script.split("localStorage.setItem", 1)[1].split("catch", 1)[0]
        for allowed in ("filters:", "view", "excludeRecent:", "saved:"):
            self.assertIn(allowed, storage)
        for forbidden in ("items", "snapshot", "drafts", "notes", "title"):
            self.assertNotIn(forbidden, storage)
        self.assertIn("${section}.${encodeURIComponent(profile)}", self.script)
        self.assertNotIn("sessionStorage", self.script)

    def test_picker_uses_filtered_keys_and_leaves_filters_alone(self):
        picker = self.script.split("async function pick()", 1)[1].split('byId("save-view")', 1)[0]
        self.assertIn("filteredItems().map((item) => item.key)", picker)
        self.assertIn('exclude_recent: byId("exclude-recent").checked ? "1" : "0"', picker)
        self.assertNotIn("applyFilters", picker)

    def test_safe_dom_and_external_urls(self):
        self.assertIn('["http:", "https:"].includes(parsed.protocol)', self.script)
        self.assertIn("!parsed.username && !parsed.password", self.script)
        self.assertIn("safeURL(item.cover_url)", self.script)
        self.assertIn("safeURL(item.link)", self.script)
        self.assertNotIn("innerHTML", self.script)
        self.assertNotIn("insertAdjacentHTML", self.script)
        self.assertIn('content.cloneNode(true)', self.script)
        self.assertNotIn("assistant", self.script.lower())

    def test_episode_and_replay_are_explicit(self):
        self.assertIn('mutate("episode", item.key)', self.script)
        self.assertIn('finishedStatuses.has(item.status)', self.script)
        self.assertIn('button("Start new run"', self.script)
        self.assertIn('mutate("runs", key)', self.script)
        self.assertIn("Earlier history and original completion dates will be kept", self.script)
        self.assertIn("Recorded web activity", self.script)

    def test_steam_unknowns_are_not_zero_and_save_is_explicit(self):
        self.assertIn('number(snapshot[field]) >= 0 ? snapshot[field] : "Unknown"', self.script)
        self.assertIn('button("Save Steam hours"', self.script)
        self.assertIn('{snapshot_id: data.snapshot_id}', self.script)
        self.assertNotIn("snapshot.complete", self.script)
        self.assertNotIn("snapshot.achievement_percent === 100", self.script)

    def test_responsive_layout_and_accessibility(self):
        self.assertIn("min-height: 44px", self.styles)
        self.assertIn("minmax(min(100%, 280px)", self.styles)
        self.assertIn("aspect-ratio: 3 / 4", self.styles)
        self.assertIn("prefers-reduced-motion", self.styles)
        self.assertNotIn("overflow-x: auto", self.styles)
        self.assertIn('["ArrowLeft", "ArrowRight", "Home", "End"]', self.script)
        self.assertIn("dialog.showModal()", self.script)
        self.assertIn("dialog.returnFocus.focus()", self.script)
        self.assertIn('if (event.key !== "Escape") return', self.script)


class MediaLibraryFixtureTests(unittest.TestCase):
    def test_fixture_serves_only_synthetic_state_and_local_assets(self):
        with TestClient(synthetic_media_app()) as client:
            for section in ("games", "shows"):
                page = client.get(f"/fixture/{section}")
                self.assertEqual(page.status_code, 200)
                self.assertEqual(page.headers["cache-control"], "no-store")
                self.assertIn("Example title", page.text)
            for asset in ("library.js", "library.css"):
                self.assertEqual(client.get(f"/module-assets/media/{asset}").status_code, 200)
            cover = client.get("/__fixture__/cover.png")
            self.assertEqual(cover.headers["content-type"], "image/png")
            self.assertTrue(cover.content.startswith(b"\x89PNG"))

    def test_fixture_episode_undo_and_version_conflict(self):
        with TestClient(synthetic_media_app()) as client:
            item = client.get("/media/shows/data").json()["items"][1]
            identity = {name: item[name] for name in ("profile", "title")}
            identity["expected_version"] = item["version"]
            reply = client.post("/media/shows/episode", data=identity).json()
            self.assertEqual(reply["item"]["current_episode"], 3)
            self.assertEqual(reply["item"]["current_season"], 1)
            self.assertEqual(reply["item"]["status"], "watching")
            self.assertEqual(client.post("/media/shows/episode", data=identity).status_code, 409)
            undone = client.post("/media/shows/undo", data={"token": reply["undo_token"]}).json()
            self.assertEqual(undone["item"]["current_episode"], 2)

    def test_fixture_can_model_committed_but_uncertain_response(self):
        with TestClient(synthetic_media_app()) as client:
            item = client.get("/media/shows/data").json()["items"][1]
            client.post("/__fixture__/control", json={"mode": "uncertain", "block_reads": True})
            response = client.post("/media/shows/episode", data={"profile": item["profile"],
                "title": item["title"], "expected_version": item["version"]})
            self.assertEqual(response.status_code, 503)
            self.assertEqual(client.get("/media/shows/data").status_code, 503)
            client.post("/__fixture__/control", json={"mode": "normal", "block_reads": False})
            self.assertEqual(client.get("/media/shows/data").json()["items"][1]["current_episode"], 3)


if __name__ == "__main__":
    unittest.main()