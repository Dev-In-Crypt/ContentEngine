"""The model catalogue that drives the Account → AI models dropdowns.

Served WITHOUT the user's API key on purpose: the provider and model pickers have
to populate *before* a key is entered. The lists are curated (OpenRouter alone
exposes hundreds of models), and any model id is still accepted on save, so a newly
released model never requires a deploy.
"""
from fastapi import APIRouter, Depends

from api.deps import require_token
from models.schemas import ModelInfo
from services.ai.catalog import IMAGE, TEXT, list_providers, list_video_providers

router = APIRouter(prefix="/api/models", tags=["models"], dependencies=[Depends(require_token)])


def _flatten(kind: str) -> list[ModelInfo]:
    return [
        ModelInfo(id=m["id"], name=m["label"], provider=p["key"])
        for p in list_providers(kind) for m in p["models"]
    ]


def _flatten_video() -> list[ModelInfo]:
    return [
        ModelInfo(id=m["id"], name=m["label"], provider=p["key"])
        for p in list_video_providers() for m in p["models"]
    ]


@router.get("/providers")
async def list_ai_providers() -> dict:
    """Full catalogue, grouped by provider, for both modalities plus video."""
    return {"text": list_providers(TEXT), "image": list_providers(IMAGE),
            "video": list_video_providers()}


@router.get("/text", response_model=list[ModelInfo])
async def list_text_models() -> list[ModelInfo]:
    return _flatten(TEXT)


@router.get("/image", response_model=list[ModelInfo])
async def list_image_models() -> list[ModelInfo]:
    return _flatten(IMAGE)


@router.get("/video", response_model=list[ModelInfo])
async def list_video_models() -> list[ModelInfo]:
    return _flatten_video()
