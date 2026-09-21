"""Finance routes share the same separate unlock and isolated storage."""
from fastapi import APIRouter

from .overview_routes import router as overview_router
from .planning_routes import router as planning_router
from .routes import router as records_router

router = APIRouter()
router.include_router(overview_router)
router.include_router(planning_router)
router.include_router(records_router)