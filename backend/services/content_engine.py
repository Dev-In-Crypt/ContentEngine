import asyncio
import uuid
from dataclasses import dataclass, field
from typing import Optional
from collections.abc import Awaitable, Callable

from models.schemas import XPostMode, XStyle, ImageSource, PostFormat, Platform, LengthTier, TemplateStyle
from services.caption_generator import CaptionGenerator, GeneratedCaption
from services.image_router import ImageRouter, SlideImageConfig, ImageFetchError
from services.openrouter import OpenRouterError
from services.stock import StockError
from services.brand_engine import PillowBrandEngine
from services.exporter import TemplateExporter


@dataclass
class GeneratedSlide:
    slide_number: int
    image_bytes: bytes
    image_source: ImageSource
    search_query: Optional[str] = None
    gen_prompt: Optional[str] = None
    attribution: Optional[dict] = None   # {source, author_name, author_profile_url, source_link}
    render_params: Optional[dict] = None  # niche_text, overlay_text, show_niche_box, niche_box_color, show_logo, page_number, total_slides, template_style
    raw_bytes: Optional[bytes] = None    # unbranded background, persisted to disk for /overlay editing


@dataclass
class GeneratedPost:
    id: str
    topic: str
    format: PostFormat
    caption: str
    hashtags: list[str]
    cta: str
    hook: str
    alt_text: str
    slides: list[GeneratedSlide]
    text_model_used: str
    image_model_used: Optional[str]
    seo_keywords: list[str] = field(default_factory=list)
    thread_parts: list[str] = field(default_factory=list)   # X thread tweets, in order
    platform: Platform = Platform.INSTAGRAM
    sources: list[dict] = field(default_factory=list)   # [{title,url}] web-grounded citations

    @property
    def images(self) -> list[bytes]:
        return [s.image_bytes for s in self.slides]


#: `progress(message, step=..., total=...)`. The counters are keyword-only and
#: optional so a caller that only wants the text keeps working — the SSE route
#: passes them straight through, and the SPA ignores keys it does not know, which
#: is what makes this safe to deploy while an old page is still open.
ProgressFn = Callable[..., Awaitable[None]]


def _num_slides(format: PostFormat) -> int:
    mapping = {
        PostFormat.SINGLE: 1,
        PostFormat.CAROUSEL_3: 3,
        PostFormat.CAROUSEL_5: 5,
        PostFormat.CAROUSEL_10: 10,
        PostFormat.INFOGRAPHIC: 1,
    }
    return mapping.get(format, 1)


class ContentEngine:
    def __init__(
        self,
        caption_generator: CaptionGenerator,
        image_router: ImageRouter,
        brand_engine: PillowBrandEngine,
        exporter: TemplateExporter,
    ):
        self.caption_gen = caption_generator
        self.image_router = image_router
        self.brand_engine = brand_engine
        self.exporter = exporter

    async def generate_post(
        self,
        topic: str,
        format: PostFormat,
        text_model: str = "",
        image_model: Optional[str] = None,
        default_image_source: ImageSource = ImageSource.STOCK,
        text_only: bool = False,
        upload_ids: Optional[list[str]] = None,
        slide_configs: Optional[list[SlideImageConfig]] = None,
        tone: str = "professional",
        niche: Optional[str] = None,
        target_audience: Optional[str] = None,
        additional_instructions: Optional[str] = None,
        apply_branding: bool = True,
        platform: Platform = Platform.INSTAGRAM,
        length_tier: LengthTier = LengthTier.SWEET_SPOT,
        template_style: TemplateStyle = TemplateStyle.BRANDED_CARD,
        niche_box_color: Optional[str] = None,
        show_logo: bool = True,
        brand_voice: Optional[str] = None,
        brand_name: Optional[str] = None,
        x_mode: XPostMode = XPostMode.SHORT,
        x_style: XStyle = XStyle.STANDARD,
        thread_min: int = 3,
        thread_max: int = 7,
        web_grounded: bool = True,
        progress: Optional[ProgressFn] = None,
    ) -> GeneratedPost:
        # Text-only (X): a pure-text post with no image. Still write the caption,
        # but build zero slides (num=0 → steps 2-3 produce nothing).
        num = 0 if text_only else _num_slides(format)

        # Caption, then one line per picture, then branding, then the route's own
        # save. Derived from `num` rather than written down, so a text-only X post
        # promises no images and a ten-slide carousel promises ten.
        total = 2 + num
        done = 0

        async def step(message: str) -> None:
            nonlocal done
            done += 1
            if progress:
                await progress(message, step=done, total=total)

        # 1. Generate caption + prompts
        await step("Writing the caption")
        caption_data: GeneratedCaption = await self.caption_gen.generate(
            topic=topic,
            format=format.value,
            num_slides=num,
            text_model=text_model,
            tone=tone,
            niche=niche,
            target_audience=target_audience,
            additional_instructions=additional_instructions,
            brand_voice=brand_voice,
            brand_name=brand_name,
            platform=platform,
            length_tier=length_tier,
            x_mode=x_mode,
            x_style=x_style,
            thread_min=thread_min,
            thread_max=thread_max,
            # Live web search is a per-call surcharge on whoever holds the key,
            # and only OpenRouter offers it — which is also what the application's
            # own key is. Default on, as it has always been; the free-generation
            # path (UX phase 6.2) turns it off, because a trial post is not worth
            # buying search for.
            web_grounded=web_grounded,
        )

        # 2. Build per-slide configs if not supplied
        if not slide_configs:
            slide_configs = self._build_default_slide_configs(
                num=num,
                image_source=default_image_source,
                search_queries=caption_data.image_search_queries,
                gen_prompts=caption_data.image_gen_prompts,
                image_model=image_model,
                upload_ids=upload_ids or [],
            )

        # 3. Fetch + brand all slides in parallel
        #
        # The tasks run under gather, so these lines arrive out of order — fine
        # for a checklist, and the alternative (reporting one line for the whole
        # step) leaves the longest part of the wait looking frozen.
        tasks = [
            self._fetch_and_brand(
                cfg, num, format, caption_data, apply_branding, topic,
                template_style, niche_box_color, show_logo, niche,
                on_done=step,
            )
            for cfg in slide_configs[:num]
        ]
        # return_exceptions so one slide's failure doesn't abandon its siblings
        # mid-flight ("Task exception was never retrieved"); surface the first.
        results = await asyncio.gather(*tasks, return_exceptions=True)
        for r in results:
            if isinstance(r, BaseException):
                raise r
        slides = sorted(results, key=lambda s: s.slide_number)
        await step("Applying your brand")

        return GeneratedPost(
            id=str(uuid.uuid4()),
            topic=topic,
            format=format,
            # For a thread the caption is every tweet joined, so export, pillar
            # classification and the feed keep working on one string; the tweets
            # themselves travel in thread_parts and drive the preview.
            caption=("\n\n".join(caption_data.thread_parts)
                     if caption_data.thread_parts else caption_data.caption),
            thread_parts=caption_data.thread_parts,
            hashtags=caption_data.hashtags,
            cta=caption_data.cta,
            hook=caption_data.hook,
            alt_text=caption_data.alt_text,
            slides=slides,
            text_model_used=text_model,
            image_model_used=image_model,
            seo_keywords=caption_data.seo_keywords,
            platform=platform,
            sources=caption_data.sources,
        )

    async def _fetch_and_brand(
        self,
        cfg: SlideImageConfig,
        num: int,
        format: PostFormat,
        caption_data: GeneratedCaption,
        apply_branding: bool,
        topic: str,
        template_style: TemplateStyle = TemplateStyle.BRANDED_CARD,
        niche_box_color: Optional[str] = None,
        show_logo: bool = True,
        niche: Optional[str] = None,
        on_done: Optional[Callable[[str], Awaitable[None]]] = None,
    ) -> GeneratedSlide:
        attribution: Optional[dict] = None
        try:
            result = await self.image_router.fetch_image(cfg)
        except (ImageFetchError, OpenRouterError, StockError):
            # A user's own photo is a deliberate choice — quietly swapping a stock
            # picture in when it fails would publish something they never picked.
            if cfg.image_source == ImageSource.UPLOAD:
                raise
            fallback = SlideImageConfig(
                slide_number=cfg.slide_number,
                image_source=ImageSource.STOCK,
                search_query=cfg.search_query or topic,
                page_number=cfg.page_number,
            )
            result = await self.image_router.fetch_image(fallback)
            cfg = fallback

        if isinstance(result, tuple):
            raw_bytes, attribution = result
        else:
            raw_bytes = result   # backward-compat for AI/Canva paths

        render_params: Optional[dict] = None
        if not apply_branding:
            branded = raw_bytes
        elif template_style == TemplateStyle.BRANDED_CARD:
            is_first = cfg.slide_number == 1
            # Slide 1 carries the niche box + hook; slides 2..N show only their own
            # unique overlay sentence (no niche box, no "Slide N" placeholders).
            overlays = caption_data.slide_overlays or []
            idx = cfg.slide_number - 1
            if is_first:
                overlay_text = overlays[0] if overlays else caption_data.hook
            else:
                overlay_text = overlays[idx] if idx < len(overlays) else ""
            page_num = cfg.page_number if cfg.page_number is not None else (
                cfg.slide_number if num > 1 else None
            )
            render_params = {
                "template_style": "branded_card",
                # No fallback to the topic: the box is a short label ("Fitness"),
                # and a whole topic sentence in it renders as an unreadable strip
                # across the photo. No niche → no box; the user can still add one
                # per slide in the overlay editor.
                "niche_text": (niche or "").strip() if is_first else "",
                "overlay_text": overlay_text,
                "show_niche_box": is_first,
                "niche_box_color": niche_box_color,
                "show_logo": show_logo,
                "page_number": page_num,
                "total_slides": num if num > 1 else None,
            }
            # PIL rendering is CPU-bound and synchronous; off-load it so it
            # doesn't freeze the event loop (and the SSE progress stream) while a
            # 1080x1350 LANCZOS resize runs.
            branded = await asyncio.to_thread(
                self.brand_engine.create_branded_card,
                background_image=raw_bytes,
                niche_text=render_params["niche_text"],
                description_text=overlay_text,
                niche_box_color=niche_box_color,
                show_logo=show_logo,
                show_niche_box=is_first,
                page_number=page_num,
                total_slides=num if num > 1 else None,
            )
        elif format in (PostFormat.SINGLE, PostFormat.INFOGRAPHIC):
            branded = await asyncio.to_thread(
                self.brand_engine.apply_brand,
                raw_bytes,
                text_overlay=caption_data.hook,
                subtitle=caption_data.cta,
            )
        else:
            heading = caption_data.hook if cfg.slide_number == 1 else f"Slide {cfg.slide_number}"
            body = caption_data.cta if cfg.slide_number == num else ""
            branded = await asyncio.to_thread(
                self.brand_engine.create_carousel_slide,
                slide_number=cfg.slide_number,
                total_slides=num,
                heading=heading,
                body_text=body,
                background_image=raw_bytes,
            )

        if on_done:
            await on_done(f"Image {cfg.slide_number} of {num} ready")

        return GeneratedSlide(
            slide_number=cfg.slide_number,
            image_bytes=branded,
            image_source=cfg.image_source,
            search_query=cfg.search_query,
            gen_prompt=cfg.gen_prompt,
            attribution=attribution,
            render_params=render_params,
            raw_bytes=raw_bytes,
        )

    async def export_template(self, post: GeneratedPost) -> bytes:
        return await self.exporter.export_package(
            images=post.images,
            caption=post.caption,
            hashtags=post.hashtags,
            post_name=post.topic[:50],
        )

    @staticmethod
    def _build_default_slide_configs(
        num: int,
        image_source: ImageSource,
        search_queries: list[str],
        gen_prompts: list[str],
        image_model: Optional[str],
        upload_ids: Optional[list[str]] = None,
    ) -> list[SlideImageConfig]:
        uploads = upload_ids or []
        configs = []
        for i in range(1, num + 1):
            idx = i - 1
            configs.append(SlideImageConfig(
                slide_number=i,
                image_source=image_source,
                search_query=search_queries[idx] if idx < len(search_queries) else f"slide {i}",
                gen_prompt=gen_prompts[idx] if idx < len(gen_prompts) else None,
                gen_model=image_model,
                # Photos are handed out in the order the user picked them.
                upload_id=uploads[idx] if idx < len(uploads) else None,
            ))
        return configs
