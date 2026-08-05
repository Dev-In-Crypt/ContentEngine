from dataclasses import dataclass
from typing import Optional, Protocol

from models.schemas import ImageSource


class ImageFetchError(Exception):
    pass


# Imported lazily to avoid circular imports; used only in _fetch_stock
def _stock_error_cls():
    from services.stock import StockError
    return StockError


class CanvaClientProtocol(Protocol):
    async def export_design(self, design_id: str, format: str = "png") -> bytes: ...


@dataclass
class SlideImageConfig:
    slide_number: int
    image_source: ImageSource
    # stock
    search_query: Optional[str] = None
    stock_source: str = "auto"           # "auto" (Unsplash→Pexels) | "unsplash" | "pexels"
    # ai gen
    gen_prompt: Optional[str] = None
    gen_model: Optional[str] = None
    # canva
    canva_template_id: Optional[str] = None
    # the user's own photo, staged before generation
    upload_id: Optional[str] = None
    # carousel
    page_number: Optional[int] = None    # manual page number for branded card


class ImageRouter:
    def __init__(
        self,
        image_provider=None,   # services.ai ImageProvider (may differ from the text one)
        stock_client=None,     # StockClient
        canva_client=None,     # CanvaClient (optional)
        upload_reader=None,    # Callable[[str], bytes] over the caller's staged photos
    ):
        self.image_provider = image_provider
        self.stock = stock_client
        self.canva = canva_client
        # Injected rather than read from disk here: the router has no business
        # knowing where a tenant's uploads live, and tests hand it a dict.
        self.upload_reader = upload_reader

    async def fetch_image(
        self, config: SlideImageConfig
    ) -> tuple[bytes, Optional[dict]]:
        """Returns (image_bytes, attribution_dict_or_None).

        attribution is only populated for stock sources (Unsplash/Pexels
        licensing requires it).
        """
        match config.image_source:
            case ImageSource.STOCK:
                return await self._fetch_stock(config)
            case ImageSource.AI_GEN:
                return await self._fetch_ai(config), None
            case ImageSource.CANVA:
                return await self._fetch_canva(config), None
            case ImageSource.UPLOAD:
                return self._read_upload(config), None
            case _:
                raise ImageFetchError(f"Unknown image source: {config.image_source}")

    def _read_upload(self, config: SlideImageConfig) -> bytes:
        """The user's own photo. No attribution: they own it."""
        if not self.upload_reader:
            raise ImageFetchError("Uploads are not available in this context")
        if not config.upload_id:
            raise ImageFetchError(f"No photo attached to slide {config.slide_number}")
        try:
            return self.upload_reader(config.upload_id)
        except Exception as e:
            raise ImageFetchError(f"Uploaded photo is unavailable: {e}") from e

    async def _fetch_stock(
        self, config: SlideImageConfig
    ) -> tuple[bytes, Optional[dict]]:
        if not self.stock:
            raise ImageFetchError("No stock client configured")
        StockError = _stock_error_cls()
        query    = config.search_query or "abstract background"
        source   = config.stock_source or "auto"
        fallbacks = [query, "abstract background", "nature landscape", "minimal texture"]
        last_exc: Exception = ImageFetchError("stock fetch failed")
        for q in dict.fromkeys(fallbacks):
            try:
                data, picked = await self.stock.search_and_download(
                    query=q, orientation="squarish", source=source
                )
                return data, picked.as_attribution()
            except StockError as e:
                last_exc = e
                continue
        raise ImageFetchError(f"Stock photo search exhausted all fallbacks. Last error: {last_exc}")

    async def _fetch_ai(self, config: SlideImageConfig) -> bytes:
        if not self.image_provider:
            raise ImageFetchError(
                "No image provider configured. Choose one in Account → AI models.")
        prompt = config.gen_prompt or "A beautiful abstract image"
        model = config.gen_model or ""
        if not model:
            raise ImageFetchError(
                "No image model selected. Choose one in Account → AI models.")
        return await self.image_provider.generate_image(model=model, prompt=prompt)

    async def _fetch_canva(self, config: SlideImageConfig) -> bytes:
        if not self.canva:
            raise ImageFetchError("No Canva client configured")
        if not config.canva_template_id:
            raise ImageFetchError("canva_template_id is required for Canva image source")
        return await self.canva.export_design(config.canva_template_id, format="png")
