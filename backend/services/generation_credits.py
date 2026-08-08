"""Whose key writes this post — and, if it is ours, whether we can afford it.

The product is bring-your-own-keys. UX phase 6 adds one exception: an account
with no key of its own gets a few posts on the application's key, so that the
question "paste an API key" arrives when somebody has already seen the product
work rather than on the way in.

That exception is the only place in the product where a stranger can spend our
money, so almost everything here is about refusing, and the order of the
refusals is itself a guard: anything that will be refused is refused *before* it
costs anything, and the allowance is claimed *before* the model is called.

Three rules hold the shape:

**All or nothing.** A generation runs entirely on the user's credentials or
entirely on ours. Mixing them — their cheap text key, our image key — would let
anybody with a five-dollar OpenRouter account mint pictures on our bill forever.

**On our key, our models.** `user.text_model` and the per-post override are
ignored on the free path; the models come from the platform defaults. Otherwise
choosing an expensive model in Settings would be a choice about our spending,
made by someone who does not pay the bill.

**Having a key means using it.** An account that holds any text-capable key of
its own never lands on the free path, even when it has not finished choosing a
model. Such an account gets today's "choose a provider and model" refusal, which
is true and fixable by them; spending our allowance on it would be neither.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from config import Settings
from models.database import User as UserModel
from services import app_spend, free_generation
from services.user_settings import resolve_ai_choice

#: Every credential that can pay for a text generation. Read off the effective
#: settings, which since UX phase 6.0 contain the user's own keys and nothing
#: else — that is what makes "do they have a key" answerable at all.
_TEXT_KEY_FIELDS = ("openrouter_api_key", "openai_api_key",
                    "anthropic_api_key", "google_api_key")

#: Same refusal text as the rest of the product for "you have not set this up".
_NO_MODEL = "No text model selected. Choose a provider and model in Account → AI models."


def no_key_detail(provider: Optional[str]) -> str:
    """The refusal for "you named this provider and never pasted its key".

    One sentence in one place because two routes and this module all have to say
    it, and a user who reads "choose a provider and model" while looking at a
    screen where both are chosen learns nothing about what to do next. Found
    exactly that way: on prod, minutes after the phase-6 deploy.
    """
    named = f" for {provider}" if provider else ""
    return f"No API key{named}. Add it in Account → AI models."


@dataclass(frozen=True)
class GenerationCreds:
    """Everything the route needs to build an engine and know who is paying."""

    settings: Settings
    #: Whose AI choice to resolve against — the user, or None for the platform's
    #: own defaults. `resolve_ai_choice(None, …)` already means exactly that, so
    #: the free path needs no separate resolver.
    actor: Optional[UserModel]
    text_provider: Optional[str]
    text_model: Optional[str]
    image_model: Optional[str]
    on_our_key: bool


def has_own_text_key(effective: Settings) -> bool:
    return any(getattr(effective, field, "") for field in _TEXT_KEY_FIELDS)


def free_allowance(user: UserModel, effective: Settings,
                   base: Settings) -> Optional[dict]:
    """What to tell this account about free posts, or None if the subject does
    not apply to them.

    Public because the interface has to answer the same question the resolver
    answers, and answer it identically: a counter that says "3 left" next to a
    button the server would refuse — or a wall in front of somebody the server
    would have served — is worse than no counter at all. So both read this.

    None, not zero, in three cases that are not "you have run out": the desktop
    owner, an account paying with its own key, and a deployment with no
    application key, where nothing free was ever on offer.
    """
    if user.is_local or has_own_text_key(effective):
        return None
    _provider, our_model, our_key = resolve_ai_choice(None, base, "text")
    if not (our_key and our_model):
        return None
    return {"remaining": free_generation.remaining(user),
            "limit": free_generation.FREE_POST_LIMIT}


def _own(user: UserModel, effective: Settings, *,
         text_model_override: Optional[str] = None,
         image_model_override: Optional[str] = None) -> GenerationCreds:
    provider, model, _key = resolve_ai_choice(user, effective, "text")
    _iprovider, image_model, _ikey = resolve_ai_choice(user, effective, "image")
    return GenerationCreds(
        settings=effective,
        actor=user,
        text_provider=provider,
        text_model=text_model_override or model,
        image_model=image_model_override or image_model,
        on_our_key=False,
    )


async def claim_generation_credentials(
    db: AsyncSession,
    user: UserModel,
    *,
    effective: Settings,
    base: Settings,
    text_model_override: Optional[str] = None,
    image_model_override: Optional[str] = None,
) -> GenerationCreds:
    """Decide who pays, claiming a free generation if the answer is "we do".

    Named `claim` rather than `resolve` because it has a side effect on the way
    out: the allowance is decremented and committed before the caller has made
    a single network call. A counter incremented after a successful generation
    never rises when the process dies mid-call or when two requests arrive
    together — and each of those ends with us paying twice.

    Raises HTTPException on every refusal, in the order they must happen.
    """
    # 1. Their own key, if they have one. This is the ordinary path and it is
    #    unchanged: their provider, their models, their bill, no counter.
    if has_own_text_key(effective):
        return _own(user, effective,
                    text_model_override=text_model_override,
                    image_model_override=image_model_override)

    # The desktop owner reads keys straight from .env and is not a tenant of
    # anything — there the platform and the user are the same person.
    if user.is_local:
        return _own(user, effective,
                    text_model_override=text_model_override,
                    image_model_override=image_model_override)

    # 2. No key of theirs and none of ours — nothing to spend either way, and
    #    the sentence has to name whichever half is actually missing. An account
    #    that picked a provider and a model needs a key; one that picked nothing
    #    cannot paste a key for a provider it has not chosen.
    their_provider, their_model, _their_key = resolve_ai_choice(user, effective, "text")
    our_provider, our_model, our_key = resolve_ai_choice(None, base, "text")
    if not (our_key and our_model):
        if their_provider and their_model:
            raise HTTPException(status_code=400,
                                detail=no_key_detail(their_provider))
        raise HTTPException(status_code=400, detail=_NO_MODEL)

    # 3. Their allowance, checked before our ceiling: this refusal is about them
    #    and is fixed by adding a key, which is the sentence we want them to read.
    if free_generation.remaining(user) <= 0:
        raise HTTPException(
            status_code=409,
            detail=("You've used your free posts. Add your own AI key in "
                    "Account → AI models to keep going."))

    # 4. Our own daily ceiling. Deliberately after the per-account check and
    #    before the reservation: a day we have capped must not quietly eat
    #    somebody's remaining free post. This refusal is about us, and says so.
    if await app_spend.flush_and_total(db) >= base.app_daily_spend_usd:
        raise HTTPException(
            status_code=503,
            detail=("Free generations are paused until tomorrow. Adding your own "
                    "AI key in Account → AI models works right now."))

    # 5. Claim it. A False here is the race the counter exists for: two requests
    #    passed the check above at the same time and only one may proceed.
    if not await free_generation.reserve(db, user):
        raise HTTPException(
            status_code=409,
            detail=("You've used your free posts. Add your own AI key in "
                    "Account → AI models to keep going."))

    return GenerationCreds(
        settings=base,
        actor=None,
        text_provider=our_provider,
        text_model=our_model,
        image_model=resolve_ai_choice(None, base, "image")[1],
        on_our_key=True,
    )
