from fastapi import FastAPI

from . import repository, scryfall


def start(app: FastAPI) -> None:
    repository.init_db()
    repository.mark_interrupted_refreshes()
    scryfall.start_scheduler()


def stop(app: FastAPI) -> None:
    scryfall.stop_scheduler()