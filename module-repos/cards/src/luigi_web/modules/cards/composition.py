"""Compose the Cards domain without duplicating its public URL prefix."""
from fastapi import APIRouter

from .analysis_routes import router as analysis_router
from .collection_routes import router as collection_router
from .routes import router as library_router
from .version_routes import router as version_router

router = APIRouter()
router.include_router(library_router)
router.include_router(analysis_router)
router.include_router(collection_router)
router.include_router(version_router)