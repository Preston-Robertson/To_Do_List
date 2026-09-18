from fastapi import FastAPI

from . import repository


def start(app: FastAPI) -> None:
    repository.init_db()