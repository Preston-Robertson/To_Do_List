"""Authenticated routes for tabletop RPG characters and level-state sheets."""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates

from . import rpg
from .auth import require_auth
from .paths import STATIC_DIR, TEMPLATES_DIR

router = APIRouter(prefix="/characters", dependencies=[Depends(require_auth)])
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))


def _asset_version() -> str:
    paths = [STATIC_DIR / "css" / "rpg.css", STATIC_DIR / "js" / "rpg.js"]
    return str(int(max((path.stat().st_mtime for path in paths if path.exists()), default=0)))


def _ctx(request: Request, **extra: Any) -> dict[str, Any]:
    return {
        "request": request,
        "active_nav": "characters",
        "asset_version": _asset_version(),
        "systems": rpg.SYSTEMS,
        "entry_kinds": rpg.ENTRY_KINDS,
        **extra,
    }


def _render(template_name: str, context: dict[str, Any]) -> Response:
    response = templates.TemplateResponse(template_name, context)
    response.headers["Cache-Control"] = "no-store"
    response.headers["Pragma"] = "no-cache"
    return response


def _redirect(request: Request, url: str) -> Response:
    if request.headers.get("HX-Request") == "true":
        return Response(status_code=204, headers={"HX-Redirect": url})
    return RedirectResponse(url, status_code=303)


def _form_dict(form: Any) -> dict[str, Any]:
    return {str(key): value for key, value in dict(form).items()}


def _value_error(exc: ValueError) -> HTTPException:
    return HTTPException(422, str(exc))


def _require_character(character_id: int) -> dict[str, Any]:
    character = rpg.get_character(character_id)
    if character is None:
        raise HTTPException(404, "Character not found")
    return character


def _require_state(character_id: int, state_id: int) -> dict[str, Any]:
    state = rpg.get_state(state_id, character_id)
    if state is None:
        raise HTTPException(404, "Level state not found")
    return state


def _require_entry(state_id: int, entry_id: int) -> dict[str, Any]:
    entry = rpg.get_entry(entry_id, state_id)
    if entry is None:
        raise HTTPException(404, "Sheet entry not found")
    return entry


def _sheet_values(form: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
    scalar_fields = (
        "label", "level", "ancestry", "heritage", "background", "class_name",
        "subclass", "alignment", "xp_current", "xp_next", "hp_current", "hp_max",
        "temp_hp", "armor_class", "speed", "initiative", "perception", "class_dc",
        "spell_dc", "spell_attack", "inspiration", "hero_points", "focus_current",
        "focus_max", "death_successes", "death_failures", "languages",
        "proficiencies", "conditions", "notes",
    )
    values = {field: form[field] for field in scalar_fields if field in form}
    ability_fields = {
        key: form[f"ability_{key}"]
        for key in rpg.ABILITY_KEYS
        if f"ability_{key}" in form
    }
    if ability_fields:
        values["abilities"] = {**state["abilities"], **ability_fields}

    save_keys = (
        rpg.ABILITY_KEYS
        if state["system_code"] == "dnd5e_2014"
        else ("fortitude", "reflex", "will")
    )
    saves: dict[str, dict[str, Any]] = {}
    for key in save_keys:
        rank_key = f"save_{key}_rank"
        misc_key = f"save_{key}_misc"
        if rank_key in form or misc_key in form:
            current = state["saves"].get(key, {})
            saves[key] = {
                "rank": form.get(rank_key, current.get("rank", 0)),
                "misc": form.get(misc_key, current.get("misc", 0)),
            }
    if saves:
        values["saves"] = {**state["saves"], **saves}

    skill_defs = rpg.DND_SKILLS if state["system_code"] == "dnd5e_2014" else rpg.PF2_SKILLS
    skills: dict[str, dict[str, Any]] = {}
    for key, _label, _ability in skill_defs:
        rank_key = f"skill_{key}_rank"
        misc_key = f"skill_{key}_misc"
        if rank_key in form or misc_key in form:
            current = state["skills"].get(key, {})
            skills[key] = {
                "rank": form.get(rank_key, current.get("rank", 0)),
                "misc": form.get(misc_key, current.get("misc", 0)),
            }
    if skills:
        values["skills"] = {**state["skills"], **skills}
    return values


@router.get("", response_class=HTMLResponse)
def characters_page(
    request: Request,
    system: str = Query(default=""),
    archived: bool = Query(default=False),
) -> Response:
    rpg.init_db()
    try:
        characters = rpg.list_characters(
            system_code=system or None,
            include_archived=archived,
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return _render(
        "rpg/index.html",
        _ctx(
            request,
            page_title="Characters",
            characters=characters,
            system_filter=system,
            include_archived=archived,
        ),
    )


@router.get("/new", response_class=HTMLResponse)
def character_new_form(request: Request) -> Response:
    return _render(
        "rpg/partials/character_form.html",
        _ctx(request, character=None),
    )


@router.post("")
async def character_create(request: Request) -> Response:
    form = _form_dict(await request.form())
    try:
        character = rpg.create_character(
            form.get("name"),
            form.get("system_code"),
            campaign=form.get("campaign", ""),
            concept=form.get("concept", ""),
            level=form.get("level", 1),
        )
    except ValueError as exc:
        raise _value_error(exc) from exc
    return _redirect(request, f"/characters/{character['id']}")


@router.get("/{character_id}", response_class=HTMLResponse)
def character_sheet(
    request: Request,
    character_id: int,
    state: int | None = Query(default=None),
) -> Response:
    try:
        sheet = rpg.sheet_view(character_id, state)
    except ValueError as exc:
        raise HTTPException(404, str(exc)) from exc
    return _render(
        "rpg/sheet.html",
        _ctx(request, page_title=sheet["character"]["name"], sheet=sheet),
    )


@router.get("/{character_id}/edit", response_class=HTMLResponse)
def character_edit_form(request: Request, character_id: int) -> Response:
    return _render(
        "rpg/partials/character_form.html",
        _ctx(request, character=_require_character(character_id)),
    )


@router.post("/{character_id}")
async def character_update(request: Request, character_id: int) -> Response:
    _require_character(character_id)
    form = _form_dict(await request.form())
    try:
        rpg.update_character(
            character_id,
            name=form.get("name"),
            campaign=form.get("campaign", ""),
            concept=form.get("concept", ""),
        )
    except ValueError as exc:
        raise _value_error(exc) from exc
    return _redirect(request, f"/characters/{character_id}")


@router.post("/{character_id}/archive")
async def character_archive(request: Request, character_id: int) -> Response:
    _require_character(character_id)
    form = _form_dict(await request.form())
    try:
        rpg.set_character_archived(character_id, form.get("archived", "1"))
    except ValueError as exc:
        raise _value_error(exc) from exc
    return _redirect(request, "/characters")


@router.post("/{character_id}/delete")
def character_delete(request: Request, character_id: int) -> Response:
    _require_character(character_id)
    try:
        rpg.delete_character(character_id)
    except ValueError as exc:
        raise _value_error(exc) from exc
    return _redirect(request, "/characters")


@router.get("/{character_id}/states/new", response_class=HTMLResponse)
def state_new_form(request: Request, character_id: int, source: int | None = None) -> Response:
    character = _require_character(character_id)
    states = rpg.list_states(character_id)
    suggested_level = min(20, max((int(state["level"]) for state in states), default=0) + 1)
    source_id = source or character.get("active_state_id")
    if source_id is not None:
        _require_state(character_id, int(source_id))
    return _render(
        "rpg/partials/state_form.html",
        _ctx(
            request,
            character=character,
            states=states,
            source_id=source_id,
            suggested_level=suggested_level,
        ),
    )


@router.post("/{character_id}/states")
async def state_create(request: Request, character_id: int) -> Response:
    _require_character(character_id)
    form = _form_dict(await request.form())
    clone_value = form.get("clone_from_id")
    try:
        state = rpg.create_state(
            character_id,
            level=form.get("level"),
            label=form.get("label", ""),
            clone_from_id=int(clone_value) if clone_value not in (None, "") else None,
            make_active=True,
        )
    except (TypeError, ValueError) as exc:
        raise _value_error(ValueError(str(exc))) from exc
    return _redirect(request, f"/characters/{character_id}?state={state['id']}")


@router.post("/{character_id}/states/{state_id}/active")
def state_activate(request: Request, character_id: int, state_id: int) -> Response:
    _require_state(character_id, state_id)
    try:
        rpg.make_state_active(character_id, state_id)
    except ValueError as exc:
        raise _value_error(exc) from exc
    return _redirect(request, f"/characters/{character_id}?state={state_id}")


@router.post("/{character_id}/states/{state_id}/delete")
def state_delete(request: Request, character_id: int, state_id: int) -> Response:
    _require_state(character_id, state_id)
    try:
        rpg.delete_state(character_id, state_id)
    except ValueError as exc:
        raise _value_error(exc) from exc
    return _redirect(request, f"/characters/{character_id}")


@router.get("/{character_id}/states/{state_id}/edit", response_class=HTMLResponse)
def state_edit_form(request: Request, character_id: int, state_id: int) -> Response:
    sheet = rpg.sheet_view(character_id, _require_state(character_id, state_id)["id"])
    return _render(
        "rpg/partials/sheet_form.html",
        _ctx(request, sheet=sheet),
    )


@router.post("/{character_id}/states/{state_id}")
async def state_update(request: Request, character_id: int, state_id: int) -> Response:
    state = _require_state(character_id, state_id)
    form = _form_dict(await request.form())
    try:
        rpg.update_state(state_id, **_sheet_values(form, state))
    except ValueError as exc:
        raise _value_error(exc) from exc
    return _redirect(request, f"/characters/{character_id}?state={state_id}")


@router.get("/{character_id}/states/{state_id}/entries/new", response_class=HTMLResponse)
def entry_new_form(
    request: Request,
    character_id: int,
    state_id: int,
    kind: str = "feature",
) -> Response:
    _require_state(character_id, state_id)
    if kind not in rpg.ENTRY_KINDS:
        raise HTTPException(400, "Unknown sheet section")
    return _render(
        "rpg/partials/entry_form.html",
        _ctx(
            request,
            character_id=character_id,
            state_id=state_id,
            entry=None,
            selected_kind=kind,
        ),
    )


@router.post("/{character_id}/states/{state_id}/entries")
async def entry_create(request: Request, character_id: int, state_id: int) -> Response:
    _require_state(character_id, state_id)
    form = _form_dict(await request.form())
    try:
        rpg.add_entry(state_id, **form)
    except ValueError as exc:
        raise _value_error(exc) from exc
    return _redirect(request, f"/characters/{character_id}?state={state_id}")


@router.get(
    "/{character_id}/states/{state_id}/entries/{entry_id}/edit",
    response_class=HTMLResponse,
)
def entry_edit_form(
    request: Request,
    character_id: int,
    state_id: int,
    entry_id: int,
) -> Response:
    _require_state(character_id, state_id)
    entry = _require_entry(state_id, entry_id)
    return _render(
        "rpg/partials/entry_form.html",
        _ctx(
            request,
            character_id=character_id,
            state_id=state_id,
            entry=entry,
            selected_kind=entry["kind"],
        ),
    )


@router.post("/{character_id}/states/{state_id}/entries/{entry_id}")
async def entry_update(
    request: Request,
    character_id: int,
    state_id: int,
    entry_id: int,
) -> Response:
    _require_state(character_id, state_id)
    _require_entry(state_id, entry_id)
    form = _form_dict(await request.form())
    try:
        rpg.update_entry(entry_id, **form)
    except ValueError as exc:
        raise _value_error(exc) from exc
    return _redirect(request, f"/characters/{character_id}?state={state_id}")


@router.post("/{character_id}/states/{state_id}/entries/{entry_id}/uses")
async def entry_uses(
    request: Request,
    character_id: int,
    state_id: int,
    entry_id: int,
) -> Response:
    _require_state(character_id, state_id)
    _require_entry(state_id, entry_id)
    form = _form_dict(await request.form())
    try:
        rpg.set_entry_uses(entry_id, form.get("current_uses"))
    except ValueError as exc:
        raise _value_error(exc) from exc
    return _redirect(request, f"/characters/{character_id}?state={state_id}")


@router.post("/{character_id}/states/{state_id}/entries/{entry_id}/delete")
def entry_delete(
    request: Request,
    character_id: int,
    state_id: int,
    entry_id: int,
) -> Response:
    _require_state(character_id, state_id)
    _require_entry(state_id, entry_id)
    try:
        rpg.delete_entry(entry_id, state_id)
    except ValueError as exc:
        raise _value_error(exc) from exc
    return _redirect(request, f"/characters/{character_id}?state={state_id}")