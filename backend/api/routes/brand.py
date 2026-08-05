"""Reading a brand off its own website — the endpoint behind "paste a link".

One request in place of a form with eight fields. It does three things in
order, and the order is the design: read the page, fetch the logo, guess the
niche. Each step is optional to the ones after it, so a site that declares no
icon, or a tenant with no AI key, still gets back everything that did work.

Nothing is saved. The response is a proposal the user edits and accepts —
which is also why the logo comes back as a data URL rather than a stored file:
storing it before anyone has agreed to it would leave orphans on disk every
time somebody pastes a link and changes their mind.

The URL is typed by a person, so the fetch goes through services/url_guard.py
and its uniform refusal message comes through untouched: this route answers
callers who could otherwise use it to map our internal network.
"""
from __future__ import annotations

import base64
import logging
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request

from api.deps import get_current_user, get_effective_settings, get_text_provider
from api.ratelimit import limiter
from config import Settings
from models.database import User as UserModel
from models.schemas import BrandExtractRequest, BrandExtractResponse
from services.brand_extract import (
    BrandExtractError, extract_brand, fetch_brand_logo, guess_niche,
)
from services.user_settings import resolve_ai_choice

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/brand", tags=["brand"])


@router.post("/extract", response_model=BrandExtractResponse)
@limiter.limit("10/minute;60/hour")
async def extract_brand_from_url(
    request: Request,
    body: BrandExtractRequest,
    user: Annotated[UserModel, Depends(get_current_user)],
    settings: Annotated[Settings, Depends(get_effective_settings)],
    text_provider: Annotated[object, Depends(get_text_provider)],
) -> BrandExtractResponse:
    try:
        info = await extract_brand(body.url, ssl_verify=settings.ssl_verify)
    except BrandExtractError as e:
        # 400, not 502: the address came from the caller, and the message is
        # the guard's uniform one — no detail about why an address was refused.
        raise HTTPException(status_code=400, detail=str(e)) from e

    await fetch_brand_logo(info, ssl_verify=settings.ssl_verify)

    # The niche is the only part of this that needs an AI key. A tenant who
    # hasn't chosen a model yet still gets the name, the colours and the logo.
    _provider, model, _key = resolve_ai_choice(user, settings, "text")
    guess = await guess_niche(
        text_provider, name=info.name, description=info.description,
        text_model=model,
    ) if text_provider is not None and model else None

    logo_data_url = None
    if info.logo is not None:
        data, mime = info.logo
        logo_data_url = f"data:{mime};base64,{base64.b64encode(data).decode()}"

    return BrandExtractResponse(
        source_url=info.source_url,
        name=info.name,
        description=info.description,
        niche=guess.niche if guess else "",
        target_audience=guess.target_audience if guess else "",
        colors=info.colors,
        logo_data_url=logo_data_url,
    )
