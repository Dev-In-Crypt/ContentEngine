import asyncio
import io
import json
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from PIL import Image

from models.schemas import ImageSource, PostFormat, Platform, TemplateStyle
from services.caption_generator import GeneratedCaption
from services.content_engine import ContentEngine, GeneratedPost, _num_slides
from services.image_router import SlideImageConfig
from services.brand_engine import PillowBrandEngine, BrandConfig
from services.exporter import TemplateExporter


def make_jpeg(color="blue") -> bytes:
    img = Image.new("RGB", (800, 800), color)
    buf = io.BytesIO()
    img.save(buf, format="JPEG")
    return buf.getvalue()


FAKE_CAPTION = GeneratedCaption(
    caption="Full caption about AI trends.",
    hashtags=["#AI", "#Tech"],
    cta="Follow for more!",
    hook="AI is changing everything.",
    image_search_queries=["futuristic AI", "technology abstract"],
    image_gen_prompts=["A neural network visualization"],
    alt_text="An abstract AI image.",
    seo_keywords=["ai trends", "tech tips"],
    slide_overlays=[
        "AI is changing everything.",
        "Robots write code now.",
        "Adapt or fall behind.",
        "Tools that save you hours.",
        "Start with the smallest task.",
    ],
)


def make_engine(image_source=ImageSource.STOCK):
    caption_gen = AsyncMock()
    caption_gen.generate.return_value = FAKE_CAPTION

    image_router = AsyncMock()
    image_router.fetch_image.return_value = make_jpeg()

    brand_engine = PillowBrandEngine(BrandConfig())
    exporter = TemplateExporter()

    engine = ContentEngine(caption_gen, image_router, brand_engine, exporter)
    return engine, caption_gen, image_router


@pytest.mark.asyncio
async def test_generate_single_post():
    engine, cap_gen, img_router = make_engine()
    post = await engine.generate_post(topic="AI trends", format=PostFormat.SINGLE)

    assert isinstance(post, GeneratedPost)
    assert post.topic == "AI trends"
    assert post.format == PostFormat.SINGLE
    assert post.caption == FAKE_CAPTION.caption
    assert post.hashtags == FAKE_CAPTION.hashtags
    assert len(post.slides) == 1
    assert post.slides[0].image_source == ImageSource.STOCK
    assert len(post.id) > 0


@pytest.mark.asyncio
async def test_generate_carousel_3_slides():
    engine, cap_gen, img_router = make_engine()
    post = await engine.generate_post(topic="Top 5 tips", format=PostFormat.CAROUSEL_3)
    assert len(post.slides) == 3
    assert img_router.fetch_image.await_count == 3


@pytest.mark.asyncio
async def test_generate_carousel_5_slides():
    engine, _, img_router = make_engine()
    post = await engine.generate_post(topic="Trends", format=PostFormat.CAROUSEL_5)
    assert len(post.slides) == 5
    assert img_router.fetch_image.await_count == 5


@pytest.mark.asyncio
async def test_text_only_produces_no_slides_but_keeps_caption():
    """A text-only post (X) has a caption but zero slides — no image is fetched.
    Mutation guard: don't force num=0 → slides get built and image_router is called."""
    engine, cap_gen, img_router = make_engine()
    post = await engine.generate_post(
        topic="A hot take", format=PostFormat.SINGLE,
        platform=Platform.X, text_only=True,
    )
    assert post.slides == []
    assert img_router.fetch_image.await_count == 0     # nothing fetched/branded
    assert post.caption == FAKE_CAPTION.caption          # caption still written
    cap_gen.generate.assert_awaited_once()


@pytest.mark.asyncio
async def test_generate_no_branding_keeps_raw_image():
    engine, _, img_router = make_engine()
    img_router.fetch_image.return_value = b"raw-bytes"
    post = await engine.generate_post(
        topic="test", format=PostFormat.SINGLE, apply_branding=False
    )
    assert post.slides[0].image_bytes == b"raw-bytes"


@pytest.mark.asyncio
async def test_generate_with_branding_transforms_image():
    engine, _, img_router = make_engine()
    post = await engine.generate_post(
        topic="test", format=PostFormat.SINGLE, apply_branding=True
    )
    # Branded image should be JPEG bytes, not the raw mock
    assert post.slides[0].image_bytes != img_router.fetch_image.return_value


@pytest.mark.asyncio
async def test_generate_passes_custom_slide_configs():
    engine, _, img_router = make_engine()
    configs = [
        SlideImageConfig(slide_number=1, image_source=ImageSource.AI_GEN, gen_prompt="space"),
        SlideImageConfig(slide_number=2, image_source=ImageSource.STOCK, search_query="earth"),
    ]
    post = await engine.generate_post(
        topic="Space", format=PostFormat.CAROUSEL_3,
        slide_configs=configs,
        apply_branding=False,
    )
    # Only 2 custom configs provided for carousel_3 → engine takes min(configs, num)
    assert len(post.slides) == 2


@pytest.mark.asyncio
async def test_export_template_returns_zip():
    engine, _, _ = make_engine()
    post = await engine.generate_post(topic="AI", format=PostFormat.SINGLE)
    zip_bytes = await engine.export_template(post)
    import zipfile
    import io
    assert zipfile.is_zipfile(io.BytesIO(zip_bytes))


@pytest.mark.asyncio
async def test_caption_gen_called_with_correct_params():
    engine, cap_gen, _ = make_engine()
    await engine.generate_post(
        topic="Fitness tips",
        format=PostFormat.CAROUSEL_3,
        text_model="openai/gpt-4o",
        tone="casual",
        niche="Health",
        target_audience="Millennials",
    )
    cap_gen.generate.assert_awaited_once()
    call_kwargs = cap_gen.generate.call_args.kwargs
    assert call_kwargs["topic"] == "Fitness tips"
    assert call_kwargs["num_slides"] == 3
    assert call_kwargs["text_model"] == "openai/gpt-4o"
    assert call_kwargs["tone"] == "casual"
    assert call_kwargs["niche"] == "Health"


@pytest.mark.asyncio
async def test_x_style_forwarded_to_caption_gen():
    """The composer's X style must reach the caption generator. Mutation guard:
    drop the x_style pass-through → the kwarg is missing and this fails."""
    from models.schemas import XStyle
    engine, cap_gen, _ = make_engine()
    await engine.generate_post(
        topic="AI", format=PostFormat.SINGLE, platform=Platform.X,
        text_only=True, x_style=XStyle.HOT_TAKE,
    )
    assert cap_gen.generate.call_args.kwargs["x_style"] == XStyle.HOT_TAKE


@pytest.mark.asyncio
async def test_seo_keywords_and_platform_propagate():
    engine, _, _ = make_engine()
    post = await engine.generate_post(
        topic="AI", format=PostFormat.SINGLE, platform=Platform.LINKEDIN,
    )
    assert post.seo_keywords == FAKE_CAPTION.seo_keywords
    assert post.platform == Platform.LINKEDIN


@pytest.mark.asyncio
async def test_branded_card_routes_to_create_branded_card():
    engine, _, _ = make_engine()
    engine.brand_engine = MagicMock()
    engine.brand_engine.create_branded_card.return_value = b"card"
    await engine.generate_post(
        topic="AI", format=PostFormat.SINGLE,
        template_style=TemplateStyle.BRANDED_CARD,
    )
    engine.brand_engine.create_branded_card.assert_called()


@pytest.mark.asyncio
async def test_carousel_slides_use_per_slide_overlay_and_hide_niche_box():
    """Slide 1 shows niche box + hook; slides 2..N show ONLY their slide_overlay."""
    engine, _, _ = make_engine()
    engine.brand_engine = MagicMock()
    engine.brand_engine.create_branded_card.return_value = b"card"
    await engine.generate_post(
        topic="AI", format=PostFormat.CAROUSEL_3,
        template_style=TemplateStyle.BRANDED_CARD,
        niche="Tech",
    )
    calls = engine.brand_engine.create_branded_card.call_args_list
    assert len(calls) == 3

    by_slide = {}
    for c in calls:
        # find the SlideImageConfig-related slide number — easiest via page_number kwarg
        kwargs = c.kwargs
        by_slide[kwargs["page_number"]] = kwargs

    # Slide 1: niche box + hook (overlay[0])
    assert by_slide[1]["show_niche_box"] is True
    assert by_slide[1]["niche_text"] == "Tech"
    assert by_slide[1]["description_text"] == FAKE_CAPTION.slide_overlays[0]

    # Slide 2: no niche box, overlay[1]
    assert by_slide[2]["show_niche_box"] is False
    assert by_slide[2]["niche_text"] == ""
    assert by_slide[2]["description_text"] == FAKE_CAPTION.slide_overlays[1]
    assert "Slide 2" not in by_slide[2]["description_text"]

    # Slide 3: no niche box, overlay[2]
    assert by_slide[3]["show_niche_box"] is False
    assert by_slide[3]["description_text"] == FAKE_CAPTION.slide_overlays[2]


@pytest.mark.asyncio
async def test_square_routes_to_legacy_methods():
    engine, _, _ = make_engine()
    engine.brand_engine = MagicMock()
    engine.brand_engine.apply_brand.return_value = b"square"
    await engine.generate_post(
        topic="AI", format=PostFormat.SINGLE,
        template_style=TemplateStyle.SQUARE,
    )
    engine.brand_engine.apply_brand.assert_called()
    engine.brand_engine.create_branded_card.assert_not_called()


@pytest.mark.asyncio
async def test_render_params_persisted_per_slide():
    """Each slide records render_params so per-slide regenerate can re-brand it."""
    engine, _, _ = make_engine()
    post = await engine.generate_post(
        topic="AI", format=PostFormat.CAROUSEL_3,
        template_style=TemplateStyle.BRANDED_CARD,
        niche="Tech", niche_box_color="#0076cb", show_logo=False,
    )
    assert all(s.render_params for s in post.slides)
    s1 = next(s for s in post.slides if s.slide_number == 1)
    s2 = next(s for s in post.slides if s.slide_number == 2)
    assert s1.render_params["show_niche_box"] is True
    assert s1.render_params["niche_text"] == "Tech"
    assert s1.render_params["overlay_text"] == FAKE_CAPTION.slide_overlays[0]
    assert s2.render_params["show_niche_box"] is False
    assert s2.render_params["overlay_text"] == FAKE_CAPTION.slide_overlays[1]
    assert s1.render_params["show_logo"] is False
    assert s1.render_params["niche_box_color"] == "#0076cb"


def test_rebrand_slide_bytes_passes_through_when_no_render_params():
    """The replace helper skips re-branding when there are no stored params."""
    from api.routes.posts import _rebrand_slide_bytes
    from services.brand_engine import BrandConfig, PillowBrandEngine
    engine = PillowBrandEngine(BrandConfig())
    raw = b"raw-bytes"
    assert _rebrand_slide_bytes(raw, None, engine) is raw
    assert _rebrand_slide_bytes(raw, {}, engine) is raw
    # Non-branded_card style also passes through:
    assert _rebrand_slide_bytes(raw, {"template_style": "square"}, engine) is raw


def test_rebrand_slide_bytes_uses_stored_params():
    """When render_params describe a branded_card, we re-run create_branded_card."""
    from api.routes.posts import _rebrand_slide_bytes
    from unittest.mock import MagicMock
    engine = MagicMock()
    engine.create_branded_card.return_value = b"new-branded"
    params = {
        "template_style": "branded_card",
        "niche_text": "Running", "overlay_text": "Run!",
        "niche_box_color": "#ff751f", "show_logo": False,
        "show_niche_box": True, "page_number": 1, "total_slides": 3,
    }
    out = _rebrand_slide_bytes(b"raw", params, engine)
    assert out == b"new-branded"
    engine.create_branded_card.assert_called_once()
    kwargs = engine.create_branded_card.call_args.kwargs
    assert kwargs["niche_text"] == "Running"
    assert kwargs["description_text"] == "Run!"
    assert kwargs["show_niche_box"] is True


@pytest.mark.asyncio
async def test_progress_callback_invoked():
    engine, _, _ = make_engine()
    messages = []

    async def progress(msg, **kw):
        messages.append(msg)

    await engine.generate_post(topic="AI", format=PostFormat.SINGLE, progress=progress)
    assert len(messages) >= 2


@pytest.mark.asyncio
async def test_progress_reports_one_stage_per_image():
    """A carousel spends most of its wall-clock fetching and branding pictures.
    Reporting that as one line means the longest part of the wait looks frozen."""
    engine, _, _ = make_engine()
    seen = []

    async def progress(msg, **kw):
        seen.append((msg, kw.get("step"), kw.get("total")))

    await engine.generate_post(topic="AI", format=PostFormat.CAROUSEL_3,
                               progress=progress)
    per_image = [m for m, _s, _t in seen if "Image" in m]
    assert len(per_image) == 3
    # Every stage is numbered, and the total is the same throughout — a counter
    # that changes mid-run reads as the finish line moving.
    totals = {t for _m, _s, t in seen}
    assert totals == {len(seen)}
    assert [s for _m, s, _t in seen] == list(range(1, len(seen) + 1))


@pytest.mark.asyncio
async def test_a_text_only_post_reports_no_image_stages():
    """num is already 0 for text-only, so the count follows for free — but a
    hardcoded stage list would promise pictures that are never coming."""
    engine, _, _ = make_engine()
    seen = []

    async def progress(msg, **kw):
        seen.append(msg)

    await engine.generate_post(topic="AI", format=PostFormat.SINGLE,
                               text_only=True, progress=progress)
    assert not [m for m in seen if "Image" in m]


def test_num_slides():
    assert _num_slides(PostFormat.SINGLE) == 1
    assert _num_slides(PostFormat.CAROUSEL_3) == 3
    assert _num_slides(PostFormat.CAROUSEL_5) == 5
    assert _num_slides(PostFormat.CAROUSEL_10) == 10
    assert _num_slides(PostFormat.INFOGRAPHIC) == 1


def test_build_default_slide_configs_stock():
    configs = ContentEngine._build_default_slide_configs(
        num=3,
        image_source=ImageSource.STOCK,
        search_queries=["AI", "tech", "future"],
        gen_prompts=[],
        image_model=None,
    )
    assert len(configs) == 3
    assert configs[0].slide_number == 1
    assert configs[0].search_query == "AI"
    assert configs[2].search_query == "future"


def test_build_default_slide_configs_fallback_query():
    configs = ContentEngine._build_default_slide_configs(
        num=3,
        image_source=ImageSource.STOCK,
        search_queries=["only-one"],
        gen_prompts=[],
        image_model=None,
    )
    assert configs[0].search_query == "only-one"
    assert configs[1].search_query == "slide 2"
    assert configs[2].search_query == "slide 3"


# ── A3/A4: PIL off the event loop, gather doesn't abandon siblings ──────────

@pytest.mark.asyncio
async def test_branding_runs_off_the_event_loop():
    """PIL rendering must go through asyncio.to_thread so it can't block the loop
    (and the SSE progress stream) during a slow LANCZOS resize."""
    engine, _, _ = make_engine()
    with patch("services.content_engine.asyncio.to_thread", wraps=asyncio.to_thread) as spy:
        await engine.generate_post(topic="AI trends", format=PostFormat.SINGLE)
    assert spy.await_count >= 1


@pytest.mark.asyncio
async def test_one_slide_failure_lets_siblings_finish_before_raising():
    """With gather(return_exceptions=True) the whole batch is awaited, so a slow
    sibling completes before the failure surfaces. Plain gather would raise on
    slide 1 immediately and leave slides 2 and 3 orphaned mid-flight."""
    engine, _, img_router = make_engine()

    completed: set[int] = set()

    async def flaky(cfg):
        if cfg.slide_number == 1:
            raise RuntimeError("slide 1 boom")
        await asyncio.sleep(0.05)      # still running when slide 1 fails
        completed.add(cfg.slide_number)
        return make_jpeg()

    img_router.fetch_image.side_effect = flaky
    with pytest.raises(RuntimeError, match="slide 1 boom"):
        await engine.generate_post(topic="Trends", format=PostFormat.CAROUSEL_3)

    # Both siblings ran to completion rather than being abandoned when slide 1 blew up.
    assert completed == {2, 3}


@pytest.mark.asyncio
async def test_no_niche_means_no_niche_box_text():
    """The box used to fall back to the whole topic, which rendered as an unreadable
    strip across the photo. Empty niche → empty label → brand_engine skips the box."""
    engine, _, _ = make_engine()
    engine.brand_engine = MagicMock()
    engine.brand_engine.create_branded_card.return_value = b"card"
    post = await engine.generate_post(
        topic="How to start strength training after 40 without getting injured",
        format=PostFormat.SINGLE,
        template_style=TemplateStyle.BRANDED_CARD,
        niche=None,
    )
    kwargs = engine.brand_engine.create_branded_card.call_args_list[0].kwargs
    assert kwargs["niche_text"] == ""
    assert post.slides[0].render_params["niche_text"] == ""


# ── the user's own photos (PART XXVII) ──────────────────────────────────────

@pytest.mark.asyncio
async def test_uploaded_photos_are_handed_out_in_slide_order():
    engine, _, img_router = make_engine()
    await engine.generate_post(
        topic="Meal prep", format=PostFormat.CAROUSEL_3,
        default_image_source=ImageSource.UPLOAD,
        upload_ids=["aaa", "bbb", "ccc"],
    )
    configs = [c.args[0] for c in img_router.fetch_image.call_args_list]
    by_slide = {c.slide_number: c for c in configs}
    assert [by_slide[i].upload_id for i in (1, 2, 3)] == ["aaa", "bbb", "ccc"]
    assert all(c.image_source == ImageSource.UPLOAD for c in configs)


@pytest.mark.asyncio
async def test_a_failed_upload_is_not_silently_replaced_by_stock():
    """Swapping in a stock photo would publish an image the user never chose."""
    from services.image_router import ImageFetchError
    engine, _, img_router = make_engine()
    img_router.fetch_image.side_effect = ImageFetchError("file gone")

    with pytest.raises(ImageFetchError):
        await engine.generate_post(
            topic="Meal prep", format=PostFormat.SINGLE,
            default_image_source=ImageSource.UPLOAD, upload_ids=["aaa"],
        )
    # One attempt only — no second call with a stock fallback config.
    assert img_router.fetch_image.call_count == 1


@pytest.mark.asyncio
async def test_stock_still_falls_back_when_a_fetch_fails():
    """Regression: the no-fallback rule is for uploads only."""
    from services.image_router import ImageFetchError
    engine, _, img_router = make_engine()
    img_router.fetch_image.side_effect = [ImageFetchError("rate limited"), make_jpeg()]

    post = await engine.generate_post(topic="AI trends", format=PostFormat.SINGLE)

    assert len(post.slides) == 1
    assert img_router.fetch_image.call_count == 2
    assert img_router.fetch_image.call_args_list[1].args[0].image_source == ImageSource.STOCK
