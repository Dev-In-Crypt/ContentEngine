import pytest
from pydantic import ValidationError
from models.schemas import (
    GenerateRequest, PostFormat, ImageSource, SlideConfig,
    CaptionUpdate, ScheduleRequest, BrandConfigSchema,
    Platform, LengthTier, TemplateStyle, PostPreview, PostStatus,
    SlidePreview, NICHE_BOX_PALETTE, EditClipSpec, EditVideoRequest,
)
from datetime import datetime, timezone


def test_generate_request_defaults():
    req = GenerateRequest(topic="AI trends", format=PostFormat.SINGLE)
    # text_model defaults to None — the route falls back to settings.default_text_model
    assert req.text_model is None
    assert req.image_model is None
    assert req.apply_branding is True
    assert req.brand_engine == "pillow"
    assert req.tone == "professional"


def test_generate_request_topic_too_short():
    with pytest.raises(ValidationError):
        GenerateRequest(topic="AI", format=PostFormat.SINGLE)


def test_generate_request_with_slides():
    req = GenerateRequest(
        topic="Marketing tips",
        format=PostFormat.CAROUSEL_3,
        slides=[
            SlideConfig(slide_number=1, image_source=ImageSource.STOCK, search_query="marketing"),
            SlideConfig(slide_number=2, image_source=ImageSource.AI_GEN, gen_prompt="abstract art"),
        ],
    )
    assert len(req.slides) == 2
    assert req.slides[0].image_source == ImageSource.STOCK


def test_caption_update_all_none():
    update = CaptionUpdate()
    assert update.caption is None
    assert update.hashtags is None


def test_caption_update_partial():
    update = CaptionUpdate(caption="New caption text")
    assert update.caption == "New caption text"
    assert update.hashtags is None


def test_schedule_request():
    dt = datetime(2026, 6, 1, 12, 0, 0, tzinfo=timezone.utc)
    req = ScheduleRequest(publish_at=dt)
    assert req.publish_at == dt


def test_brand_config_defaults():
    cfg = BrandConfigSchema(name="My Brand")
    assert cfg.primary_color == "#2E75B6"
    assert cfg.logo_position == "bottom_right"
    assert cfg.logo_scale == 0.15
    assert cfg.padding == 40


def test_post_format_values():
    assert PostFormat.CAROUSEL_3 == "carousel_3"
    assert PostFormat.SINGLE == "single"


def test_image_source_values():
    assert ImageSource.STOCK == "stock"
    assert ImageSource.AI_GEN == "ai_gen"
    assert ImageSource.CANVA == "canva"


def test_new_enum_defaults():
    req = GenerateRequest(topic="AI trends", format=PostFormat.SINGLE)
    assert req.platform == Platform.INSTAGRAM
    assert req.length_tier == LengthTier.SWEET_SPOT
    assert req.template_style == TemplateStyle.BRANDED_CARD
    assert req.show_logo is True
    assert req.niche_box_color is None


def test_niche_box_color_valid():
    req = GenerateRequest(topic="AI trends", format=PostFormat.SINGLE, niche_box_color="#FF751F")
    assert req.niche_box_color == "#ff751f"  # normalized to lowercase


def test_niche_box_color_accepts_any_hex():
    """Colours are per-tenant — the palette is only a set of suggested swatches."""
    req = GenerateRequest(topic="AI trends", format=PostFormat.SINGLE, niche_box_color="#AbCdEf")
    assert req.niche_box_color == "#abcdef"


def test_niche_box_color_malformed_rejected():
    for bad in ("royalblue", "#fff", "ff751f", "#gggggg"):
        with pytest.raises(ValidationError):
            GenerateRequest(topic="AI trends", format=PostFormat.SINGLE, niche_box_color=bad)


def test_slide_config_page_number():
    s = SlideConfig(slide_number=1, image_source=ImageSource.STOCK, page_number=3)
    assert s.page_number == 3


def test_post_preview_keeps_seo_separate_from_hashtags():
    preview = PostPreview(
        id="1", topic="t", format=PostFormat.SINGLE, status=PostStatus.PREVIEW,
        caption="c", hashtags=["#a", "#b"], seo_keywords=["kw one", "kw two"],
        cta="x", hook="h", platform=Platform.INSTAGRAM,
        slides=[SlidePreview(slide_number=1, image_url="/x", image_source=ImageSource.STOCK,
                             width=1080, height=1350)],
        text_model_used="m", image_model_used=None,
        created_at=datetime.now(timezone.utc),
    )
    assert preview.hashtags == ["#a", "#b"]
    assert preview.seo_keywords == ["kw one", "kw two"]


def test_caption_update_seo_keywords():
    update = CaptionUpdate(seo_keywords=["a", "b"])
    assert update.seo_keywords == ["a", "b"]


def test_brand_config_schema_new_fields():
    cfg = BrandConfigSchema(name="My Brand")
    assert cfg.template_style == "branded_card"
    assert cfg.niche_box_color == "#ff751f"
    assert cfg.description_box_alpha == 0.79
    assert cfg.niche_box_palette == NICHE_BOX_PALETTE


def test_edit_video_request_defaults():
    req = EditVideoRequest(clips=[EditClipSpec(asset_id="a1")])
    assert req.transitions is False
    assert req.voiceover is False
    assert req.clips[0].trim_start_sec == 0.0
    assert req.clips[0].trim_end_sec is None


def test_edit_video_request_transitions_needs_two_clips():
    with pytest.raises(ValidationError, match="transitions requires at least 2 clips"):
        EditVideoRequest(clips=[EditClipSpec(asset_id="a1")], transitions=True)


def test_edit_video_request_transitions_ok_with_two_clips():
    req = EditVideoRequest(
        clips=[EditClipSpec(asset_id="a1"), EditClipSpec(asset_id="a2")],
        transitions=True,
    )
    assert req.transitions is True


def test_edit_video_request_requires_at_least_one_clip():
    with pytest.raises(ValidationError):
        EditVideoRequest(clips=[])


def test_edit_clip_spec_trim_end_must_be_positive():
    with pytest.raises(ValidationError):
        EditClipSpec(asset_id="a1", trim_end_sec=0.0)
