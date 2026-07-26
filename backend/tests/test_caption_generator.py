import json
import pytest
from pytest_httpx import HTTPXMock
from unittest.mock import AsyncMock
from models.schemas import Platform, LengthTier
from services.caption_generator import CaptionGenerator, CaptionParseError, GeneratedCaption
from services.openrouter import OpenRouterClient

BASE = "https://openrouter.ai/api/v1"

GOOD_JSON = {
    "caption": "This is the full caption text about AI trends that is long enough.",
    "hashtags": ["#AI", "#Tech", "#Innovation"],
    "seo_keywords": ["ai trends", "tech tips", "productivity"],
    "cta": "Follow for more tips!",
    "hook": "AI is changing everything.",
    "image_search_queries": ["futuristic AI robot", "technology abstract"],
    "image_gen_prompts": ["A glowing neural network visualization"],
    "slide_overlays": [
        "AI is changing everything.",
        "Robots write code now.",
        "Adapt or get left behind.",
    ],
    "alt_text": "An abstract image representing artificial intelligence.",
}


@pytest.mark.asyncio
async def test_generate_success(httpx_mock: HTTPXMock):
    httpx_mock.add_response(
        url=f"{BASE}/chat/completions",
        json={"choices": [{"message": {"content": json.dumps(GOOD_JSON)}}]},
    )
    client = OpenRouterClient(api_key="test-key")
    gen = CaptionGenerator(client)
    result = await gen.generate(topic="AI trends", format="single", num_slides=1)

    assert isinstance(result, GeneratedCaption)
    assert result.caption == GOOD_JSON["caption"]
    assert result.hashtags == GOOD_JSON["hashtags"]
    assert result.cta == GOOD_JSON["cta"]
    assert result.hook == GOOD_JSON["hook"]
    assert len(result.image_search_queries) == 2
    assert result.alt_text == GOOD_JSON["alt_text"]
    await client.close()


def _sys_user(httpx_mock: HTTPXMock):
    """Return (system_prompt, user_prompt) from the captured OpenRouter request."""
    body = json.loads(httpx_mock.get_requests()[0].content)
    msgs = {m["role"]: m["content"] for m in body["messages"]}
    return msgs["system"], msgs["user"]


def test_json_spec_reaches_model_with_single_braces():
    """Regression: `_JSON_FORMAT` is substituted as a .format() VALUE, so its braces
    are never unescaped. Doubling them shipped a literal '{{' to the model, which
    cheap models copied verbatim and emitted invalid JSON — measured ~50% failed
    generations on X with deepseek before this was fixed."""
    from models.schemas import LengthTier, XStyle
    from services.caption_generator import (
        INSTAGRAM_SYSTEM_PROMPT, LINKEDIN_SYSTEM_PROMPT, X_SYSTEM_PROMPT,
        LENGTH_TIER_INSTRUCTIONS, X_STYLE_INSTRUCTIONS, _JSON_FORMAT, _frame_brand_voice,
    )
    for template in (INSTAGRAM_SYSTEM_PROMPT, LINKEDIN_SYSTEM_PROMPT, X_SYSTEM_PROMPT):
        rendered = template.format(
            brand_voice=_frame_brand_voice(None), tone="professional",
            length_instruction=LENGTH_TIER_INSTRUCTIONS[LengthTier.SWEET_SPOT],
            json_format=_JSON_FORMAT,
            x_style=X_STYLE_INSTRUCTIONS[XStyle.STANDARD],
        )
        assert "{{" not in rendered and "}}" not in rendered
        # and the example the model is told to copy must be parseable JSON shape
        assert '\n{\n' in rendered


@pytest.mark.asyncio
async def test_prompts_are_niche_neutral(httpx_mock: HTTPXMock):
    """The system prompt must not hardcode the old fitness/self-dev niche."""
    httpx_mock.add_response(
        url=f"{BASE}/chat/completions",
        json={"choices": [{"message": {"content": json.dumps(GOOD_JSON)}}]},
    )
    client = OpenRouterClient(api_key="test-key")
    gen = CaptionGenerator(client)
    await gen.generate(topic="Sourdough baking", format="single",
                       platform=Platform.INSTAGRAM, web_grounded=False)
    system, _user = _sys_user(httpx_mock)
    low = system.lower()
    for banned in ("fitness", "running", "marathon", "personal development", "healthy habits"):
        assert banned not in low, f"system prompt still niche-locked on {banned!r}"
    await client.close()


@pytest.mark.asyncio
async def test_niche_and_brand_reach_user_prompt(httpx_mock: HTTPXMock):
    httpx_mock.add_response(
        url=f"{BASE}/chat/completions",
        json={"choices": [{"message": {"content": json.dumps(GOOD_JSON)}}]},
    )
    client = OpenRouterClient(api_key="test-key")
    gen = CaptionGenerator(client)
    await gen.generate(topic="Sourdough baking", format="single", niche="Artisan bakery",
                       target_audience="Home bakers", brand_name="Crumb & Co",
                       web_grounded=False)
    _system, user = _sys_user(httpx_mock)
    assert "Artisan bakery" in user
    assert "Home bakers" in user
    assert "Crumb & Co" in user
    await client.close()


@pytest.mark.asyncio
async def test_regenerate_field_uses_actual_platform(httpx_mock: HTTPXMock):
    httpx_mock.add_response(
        url=f"{BASE}/chat/completions",
        json={"choices": [{"message": {"content": json.dumps({"variants": ["a", "b"]})}}]},
    )
    client = OpenRouterClient(api_key="test-key")
    gen = CaptionGenerator(client)
    await gen.regenerate_field(field="hook", topic="t", current_value="x",
                               platform=Platform.X, count=2)
    _system, user = _sys_user(httpx_mock)
    assert "valid for x" in user.lower()
    assert "valid for instagram" not in user.lower()
    await client.close()


@pytest.mark.asyncio
async def test_generate_strips_markdown_fences(httpx_mock: HTTPXMock):
    wrapped = f"```json\n{json.dumps(GOOD_JSON)}\n```"
    httpx_mock.add_response(
        url=f"{BASE}/chat/completions",
        json={"choices": [{"message": {"content": wrapped}}]},
    )
    client = OpenRouterClient(api_key="test-key")
    gen = CaptionGenerator(client)
    result = await gen.generate(topic="AI trends", format="single")
    assert result.caption == GOOD_JSON["caption"]
    await client.close()


@pytest.mark.asyncio
async def test_generate_invalid_json(httpx_mock: HTTPXMock):
    httpx_mock.add_response(
        url=f"{BASE}/chat/completions",
        json={"choices": [{"message": {"content": "This is not JSON at all."}}]},
        is_reusable=True,   # the generator retries once on a parse failure
    )
    client = OpenRouterClient(api_key="test-key")
    gen = CaptionGenerator(client)
    with pytest.raises(CaptionParseError, match="JSON"):
        await gen.generate(topic="AI trends", format="single")
    await client.close()


@pytest.mark.asyncio
async def test_generate_missing_field(httpx_mock: HTTPXMock):
    bad = dict(GOOD_JSON)
    del bad["hashtags"]
    httpx_mock.add_response(
        url=f"{BASE}/chat/completions",
        json={"choices": [{"message": {"content": json.dumps(bad)}}]},
        is_reusable=True,   # the generator retries once on a parse failure
    )
    client = OpenRouterClient(api_key="test-key")
    gen = CaptionGenerator(client)
    with pytest.raises(CaptionParseError, match="hashtags"):
        await gen.generate(topic="AI trends", format="single")
    await client.close()


def test_parse_raw_json():
    client = OpenRouterClient(api_key="key")
    gen = CaptionGenerator(client)
    result = gen._parse(json.dumps(GOOD_JSON))
    assert result.hook == GOOD_JSON["hook"]


def test_parse_code_fence():
    client = OpenRouterClient(api_key="key")
    gen = CaptionGenerator(client)
    result = gen._parse(f"```\n{json.dumps(GOOD_JSON)}\n```")
    assert result.cta == GOOD_JSON["cta"]


def test_parse_invalid_raises():
    gen = CaptionGenerator(OpenRouterClient(api_key="key"))
    with pytest.raises(CaptionParseError):
        gen._parse("not json")


def test_parse_extracts_seo_keywords():
    gen = CaptionGenerator(OpenRouterClient(api_key="key"))
    result = gen._parse(json.dumps(GOOD_JSON))
    assert result.seo_keywords == GOOD_JSON["seo_keywords"]


def test_parse_tolerates_missing_seo_keywords():
    gen = CaptionGenerator(OpenRouterClient(api_key="key"))
    payload = dict(GOOD_JSON)
    del payload["seo_keywords"]
    result = gen._parse(json.dumps(payload))
    assert result.seo_keywords == []


def test_parse_extracts_slide_overlays():
    gen = CaptionGenerator(OpenRouterClient(api_key="key"))
    result = gen._parse(json.dumps(GOOD_JSON))
    assert result.slide_overlays == GOOD_JSON["slide_overlays"]


def test_parse_missing_overlays_falls_back_to_hook():
    gen = CaptionGenerator(OpenRouterClient(api_key="key"))
    payload = dict(GOOD_JSON)
    del payload["slide_overlays"]
    result = gen._parse(json.dumps(payload))
    # When the model omits overlays we keep slide 1 working with the hook.
    assert result.slide_overlays == [payload["hook"]]


@pytest.mark.asyncio
@pytest.mark.parametrize("x_mode", ["short", "thread", "long"])
async def test_x_prompt_carries_formatting_and_style_angle(httpx_mock: HTTPXMock, x_mode):
    """Every X mode must ask for readable formatting AND inject the chosen style
    angle. Mutation guard: drop the {x_style} field / formatting lines → the
    ANGLE / emoji wording vanishes and this fails."""
    from models.schemas import XPostMode, XStyle
    payload = dict(GOOD_JSON)
    if x_mode == "thread":
        payload["thread"] = ["Hook tweet.", "Second tweet.", "Last tweet."]
    httpx_mock.add_response(
        url=f"{BASE}/chat/completions",
        json={"choices": [{"message": {"content": json.dumps(payload)}}]},
    )
    client = OpenRouterClient(api_key="test-key")
    gen = CaptionGenerator(client)
    await gen.generate(topic="Smart contract security", format="single",
                       platform=Platform.X, x_mode=XPostMode(x_mode),
                       x_style=XStyle.HOT_TAKE, web_grounded=False)
    system, _user = _sys_user(httpx_mock)
    assert "ANGLE" in system
    assert "bold, specific stance" in system            # HOT_TAKE angle injected
    assert "emoji" in system.lower()                     # formatting/emoji guidance
    await client.close()


@pytest.mark.asyncio
async def test_x_style_default_is_standard(httpx_mock: HTTPXMock):
    """Omitting x_style injects the neutral Standard angle, not a hot take."""
    from services.caption_generator import X_STYLE_INSTRUCTIONS
    from models.schemas import XStyle
    httpx_mock.add_response(
        url=f"{BASE}/chat/completions",
        json={"choices": [{"message": {"content": json.dumps(GOOD_JSON)}}]},
    )
    client = OpenRouterClient(api_key="test-key")
    gen = CaptionGenerator(client)
    await gen.generate(topic="AI", format="single", platform=Platform.X,
                       web_grounded=False)
    system, _user = _sys_user(httpx_mock)
    assert X_STYLE_INSTRUCTIONS[XStyle.STANDARD] in system
    assert "bold, specific stance" not in system         # not the hot take
    await client.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("platform", [Platform.INSTAGRAM, Platform.LINKEDIN])
async def test_generate_per_platform(httpx_mock: HTTPXMock, platform):
    httpx_mock.add_response(
        url=f"{BASE}/chat/completions",
        json={"choices": [{"message": {"content": json.dumps(GOOD_JSON)}}]},
    )
    client = OpenRouterClient(api_key="test-key")
    gen = CaptionGenerator(client)
    result = await gen.generate(topic="AI", format="single", platform=platform)
    assert result.seo_keywords == GOOD_JSON["seo_keywords"]
    # the system prompt sent must match the platform
    request = httpx_mock.get_requests()[-1]
    sent = json.loads(request.content)["messages"][0]["content"]
    if platform == Platform.LINKEDIN:
        assert "LinkedIn" in sent
    else:
        assert "Instagram" in sent
    await client.close()


@pytest.mark.asyncio
async def test_generate_appends_online_suffix_when_web_grounded(httpx_mock: HTTPXMock):
    httpx_mock.add_response(
        url=f"{BASE}/chat/completions",
        json={"choices": [{"message": {
            "content": json.dumps(GOOD_JSON),
            "annotations": [
                {"type": "url_citation", "url_citation": {"url": "https://x.example/a", "title": "Article A"}},
            ],
        }}]},
    )
    client = OpenRouterClient(api_key="k")
    gen = CaptionGenerator(client)
    result = await gen.generate(
        topic="t", format="single", text_model="anthropic/claude-sonnet-4",
        web_grounded=True,
    )
    # Outgoing model id had :online appended
    body = json.loads(httpx_mock.get_requests()[-1].content)
    assert body["model"].endswith(":online")
    # Citations parsed into GeneratedCaption.sources
    assert result.sources == [{"title": "Article A", "url": "https://x.example/a"}]
    await client.close()


@pytest.mark.asyncio
async def test_generate_no_online_suffix_when_disabled(httpx_mock: HTTPXMock):
    httpx_mock.add_response(
        url=f"{BASE}/chat/completions",
        json={"choices": [{"message": {"content": json.dumps(GOOD_JSON)}}]},
    )
    client = OpenRouterClient(api_key="k")
    gen = CaptionGenerator(client)
    await gen.generate(
        topic="t", format="single", text_model="m", web_grounded=False,
    )
    body = json.loads(httpx_mock.get_requests()[-1].content)
    assert body["model"] == "m"
    await client.close()


@pytest.mark.asyncio
async def test_deep_dive_raises_max_tokens(httpx_mock: HTTPXMock):
    httpx_mock.add_response(
        url=f"{BASE}/chat/completions",
        json={"choices": [{"message": {"content": json.dumps(GOOD_JSON)}}]},
    )
    client = OpenRouterClient(api_key="test-key")
    gen = CaptionGenerator(client)
    await gen.generate(topic="AI", format="single", length_tier=LengthTier.DEEP_DIVE)
    request = httpx_mock.get_requests()[-1]
    assert json.loads(request.content)["max_tokens"] == 3000
    await client.close()


@pytest.mark.asyncio
async def test_x_platform_uses_x_prompt(httpx_mock: HTTPXMock):
    """platform=X must send the X system prompt (250-char rule), not the IG one."""
    httpx_mock.add_response(
        url=f"{BASE}/chat/completions",
        json={"choices": [{"message": {"content": json.dumps(GOOD_JSON)}}]},
    )
    client = OpenRouterClient(api_key="test-key")
    gen = CaptionGenerator(client)
    await gen.generate(topic="Running", format="single", num_slides=1, platform=Platform.X)
    await client.close()

    body = json.loads(httpx_mock.get_requests()[0].content)
    system = body["messages"][0]["content"]
    assert "250 characters" in system
    assert "X (Twitter)" in system


# ── X post modes: short / thread / long ─────────────────────────────────────

THREAD_JSON = {
    **GOOD_JSON,
    "thread": [
        "Blends hide what a farm actually tastes like.",
        "Filter brewing pulls those notes forward instead of compressing them.",
        "Start with a washed Kenyan if you want the difference to be obvious. #coffee",
    ],
}


@pytest.mark.asyncio
async def test_thread_mode_prompt_carries_the_range_and_the_no_split_rule(httpx_mock: HTTPXMock):
    from models.schemas import XPostMode
    httpx_mock.add_response(
        url=f"{BASE}/chat/completions",
        json={"choices": [{"message": {"content": json.dumps(THREAD_JSON)}}]},
    )
    client = OpenRouterClient(api_key="k")
    gen = CaptionGenerator(client)
    await gen.generate(topic="Single origin", format="single", platform=Platform.X,
                       x_mode=XPostMode.THREAD, thread_min=3, thread_max=6,
                       web_grounded=False)
    system, _user = _sys_user(httpx_mock)
    assert "between 3 and 6 tweets" in system
    assert "250 characters or fewer" in system
    assert "NEVER split a" in system          # the coherence rule the owner asked for
    assert '"thread"' in system               # the model is shown the field
    await client.close()


@pytest.mark.asyncio
async def test_thread_mode_returns_parts(httpx_mock: HTTPXMock):
    from models.schemas import XPostMode
    httpx_mock.add_response(
        url=f"{BASE}/chat/completions",
        json={"choices": [{"message": {"content": json.dumps(THREAD_JSON)}}]},
    )
    client = OpenRouterClient(api_key="k")
    gen = CaptionGenerator(client)
    res = await gen.generate(topic="t", format="single", platform=Platform.X,
                             x_mode=XPostMode.THREAD, web_grounded=False)
    assert res.thread_parts == THREAD_JSON["thread"]
    await client.close()


@pytest.mark.asyncio
async def test_thread_parts_are_capped_at_thread_max(httpx_mock: HTTPXMock):
    from models.schemas import XPostMode
    many = {**GOOD_JSON, "thread": [f"Tweet number {i}." for i in range(9)]}
    httpx_mock.add_response(
        url=f"{BASE}/chat/completions",
        json={"choices": [{"message": {"content": json.dumps(many)}}]},
    )
    client = OpenRouterClient(api_key="k")
    gen = CaptionGenerator(client)
    res = await gen.generate(topic="t", format="single", platform=Platform.X,
                             x_mode=XPostMode.THREAD, thread_min=2, thread_max=4,
                             web_grounded=False)
    assert len(res.thread_parts) == 4
    await client.close()


@pytest.mark.asyncio
async def test_over_long_tweet_is_brought_under_the_limit(httpx_mock: HTTPXMock):
    """End-to-end of the two-stage enforcement: the model overshoots, we fix it."""
    from models.schemas import TWEET_CHAR_LIMIT, XPostMode
    over = {**GOOD_JSON, "thread": ["word " * 200, "Short one."]}
    httpx_mock.add_response(
        url=f"{BASE}/chat/completions",
        json={"choices": [{"message": {"content": json.dumps(over)}}]},
    )
    # the shortener call also hits the mocked endpoint and returns the same body,
    # which is still too long — so the deterministic cut must save it
    httpx_mock.add_response(
        url=f"{BASE}/chat/completions",
        json={"choices": [{"message": {"content": "still " * 100}}]},
    )
    client = OpenRouterClient(api_key="k")
    gen = CaptionGenerator(client)
    res = await gen.generate(topic="t", format="single", platform=Platform.X,
                             x_mode=XPostMode.THREAD, web_grounded=False)
    assert all(len(p) <= TWEET_CHAR_LIMIT for p in res.thread_parts)
    await client.close()


@pytest.mark.asyncio
async def test_long_mode_uses_the_long_prompt(httpx_mock: HTTPXMock):
    from models.schemas import XPostMode
    httpx_mock.add_response(
        url=f"{BASE}/chat/completions",
        json={"choices": [{"message": {"content": json.dumps(GOOD_JSON)}}]},
    )
    client = OpenRouterClient(api_key="k")
    gen = CaptionGenerator(client)
    res = await gen.generate(topic="t", format="single", platform=Platform.X,
                             x_mode=XPostMode.LONG, web_grounded=False)
    system, _user = _sys_user(httpx_mock)
    assert "long-form" in system.lower()
    # wording wraps across lines in the template, so check it without the newline
    assert "280-character cap" in system and "does not apply" in system
    assert res.thread_parts == []          # a long post is not a thread
    await client.close()


def test_parse_without_thread_is_unaffected():
    """Existing Instagram/LinkedIn responses have no 'thread' key — soft parse."""
    gen = CaptionGenerator(OpenRouterClient(api_key="k"))
    assert gen._parse(json.dumps(GOOD_JSON)).thread_parts == []


@pytest.mark.asyncio
async def test_short_x_caption_leaves_room_for_the_hashtags(httpx_mock: HTTPXMock):
    """"250 characters including everything": the hashtags are appended at publish,
    so caption + hashtags — not the caption alone — must fit the budget."""
    from models.schemas import TWEET_CHAR_LIMIT, XPostMode
    long_caption = dict(GOOD_JSON,
                        caption="word " * 80,                    # ~400 chars
                        hashtags=["#SleepBetter", "#HealthyHabits"])
    httpx_mock.add_response(
        url=f"{BASE}/chat/completions",
        json={"choices": [{"message": {"content": json.dumps(long_caption)}}]},
    )
    client = OpenRouterClient(api_key="test-key")
    gen = CaptionGenerator(client)
    gen.shorten_text = AsyncMock(side_effect=AssertionError("offline: use the hard cut"))
    result = await gen.generate(topic="sleep", format="single", num_slides=1,
                                platform=Platform.X, x_mode=XPostMode.SHORT)
    await client.close()

    tags = " ".join(result.hashtags)
    assert len(f"{result.caption}\n\n{tags}") <= TWEET_CHAR_LIMIT


@pytest.mark.asyncio
async def test_short_x_caption_within_budget_is_untouched(httpx_mock: HTTPXMock):
    """A caption that already fits must not be reworded or ellipsised."""
    from models.schemas import XPostMode
    httpx_mock.add_response(
        url=f"{BASE}/chat/completions",
        json={"choices": [{"message": {"content": json.dumps(GOOD_JSON)}}]},
    )
    client = OpenRouterClient(api_key="test-key")
    gen = CaptionGenerator(client)
    result = await gen.generate(topic="AI", format="single", num_slides=1,
                                platform=Platform.X, x_mode=XPostMode.SHORT)
    await client.close()
    assert result.caption == GOOD_JSON["caption"]


def test_short_x_prompt_keeps_hashtags_out_of_the_caption():
    """publisher_flow appends the hashtags field to the caption. If the prompt also
    asks for them inline, every short X post ships its hashtags twice."""
    from services.caption_generator import X_SYSTEM_PROMPT
    assert "WITHOUT hashtags" in X_SYSTEM_PROMPT
    assert "hashtags included" not in X_SYSTEM_PROMPT


def test_all_x_prompts_ban_markdown():
    """X publishes markdown literally — a grounded model's [link](url) would ship
    with the brackets showing."""
    from services.caption_generator import (
        X_LONG_SYSTEM_PROMPT, X_SYSTEM_PROMPT, X_THREAD_SYSTEM_PROMPT,
    )
    for prompt in (X_SYSTEM_PROMPT, X_THREAD_SYSTEM_PROMPT, X_LONG_SYSTEM_PROMPT):
        assert "X renders no markdown" in prompt


def test_thread_and_long_prompts_keep_hashtags_out_of_the_text():
    """Same rule as the short post: the hashtags field is appended at publish time."""
    from services.caption_generator import X_LONG_SYSTEM_PROMPT, X_THREAD_SYSTEM_PROMPT
    assert "NO hashtags anywhere in the tweets" in X_THREAD_SYSTEM_PROMPT
    assert "Hashtags only in the LAST tweet" not in X_THREAD_SYSTEM_PROMPT
    assert "NO hashtags in the post text" in X_LONG_SYSTEM_PROMPT
    assert "1-2 hashtags at the very end only" not in X_LONG_SYSTEM_PROMPT


@pytest.mark.asyncio
async def test_x_markdown_is_flattened_before_publishing(httpx_mock: HTTPXMock):
    from models.schemas import XPostMode
    body = dict(GOOD_JSON, caption="Per [a JAMA study](https://jama.org/x), walking wins.")
    httpx_mock.add_response(
        url=f"{BASE}/chat/completions",
        json={"choices": [{"message": {"content": json.dumps(body)}}]},
    )
    client = OpenRouterClient(api_key="test-key")
    gen = CaptionGenerator(client)
    result = await gen.generate(topic="walking", format="single", num_slides=1,
                                platform=Platform.X, x_mode=XPostMode.SHORT)
    await client.close()
    assert "[" not in result.caption and "](" not in result.caption
    assert "https://jama.org/x" in result.caption      # the source stays checkable


@pytest.mark.asyncio
async def test_last_tweet_reserves_room_for_the_hashtags(httpx_mock: HTTPXMock):
    """The tags are appended to the last tweet at publish; if it already fills 250,
    publishing would have to trim text the user approved in the preview."""
    from models.schemas import TWEET_CHAR_LIMIT, XPostMode
    body = dict(GOOD_JSON,
                hashtags=["#Walking", "#HealthyHabits"],
                thread=["Hook tweet.", "Middle tweet.", "word " * 49])   # ~245 chars
    httpx_mock.add_response(
        url=f"{BASE}/chat/completions",
        json={"choices": [{"message": {"content": json.dumps(body)}}]},
    )
    client = OpenRouterClient(api_key="test-key")
    gen = CaptionGenerator(client)
    gen.shorten_text = AsyncMock(side_effect=AssertionError("offline: use the hard cut"))
    result = await gen.generate(topic="walking", format="single", num_slides=1,
                                platform=Platform.X, x_mode=XPostMode.THREAD)
    await client.close()

    tags = " ".join(result.hashtags)
    assert len(f"{result.thread_parts[-1]}\n\n{tags}") <= TWEET_CHAR_LIMIT
    assert result.thread_parts[0] == "Hook tweet."      # only the last one is squeezed


# ── models wrap their JSON in prose; that must not fail the whole generation ──

def test_extract_json_from_prose_and_fence():
    """Live failure: deepseek answered with a preamble, a ```json fence and a
    bullet-point summary. The reply was fine; only our parser refused it."""
    from services.caption_generator import extract_json
    raw = (
        "Here's a concise thread for your running topic:\n\n"
        '```json\n{"caption": "text", "thread": ["one", "two"]}\n```\n\n'
        "Key features:\n1. Provocative hook\n2. Ends with an engagement prompt"
    )
    assert json.loads(extract_json(raw))["thread"] == ["one", "two"]


def test_extract_json_without_a_fence():
    from services.caption_generator import extract_json
    raw = 'Sure! {"caption": "hi", "hook": "yo"} Hope that helps.'
    assert json.loads(extract_json(raw))["caption"] == "hi"


def test_extract_json_keeps_braces_inside_strings():
    """A caption may legitimately contain a brace; it must not end the object."""
    from services.caption_generator import extract_json
    # An UNBALANCED brace inside the string: without string-awareness it closes
    # the object early and the rest of the JSON is lost.
    raw = 'Note: {"caption": "a stray } brace and \\"quotes\\"", "hook": "h"} done'
    data = json.loads(extract_json(raw))
    assert data["caption"] == 'a stray } brace and "quotes"'
    assert data["hook"] == "h"


def test_extract_json_leaves_clean_json_alone():
    from services.caption_generator import extract_json
    raw = json.dumps(GOOD_JSON)
    assert json.loads(extract_json(raw)) == GOOD_JSON


@pytest.mark.asyncio
async def test_generate_survives_a_chatty_model(httpx_mock: HTTPXMock):
    chatty = ("Sure, here you go!\n\n```json\n" + json.dumps(GOOD_JSON) +
              "\n```\n\nLet me know if you want a different angle.")
    httpx_mock.add_response(
        url=f"{BASE}/chat/completions",
        json={"choices": [{"message": {"content": chatty}}]},
    )
    client = OpenRouterClient(api_key="test-key")
    gen = CaptionGenerator(client)
    result = await gen.generate(topic="AI", format="single", num_slides=1)
    await client.close()
    assert result.caption == GOOD_JSON["caption"]


# ── polish applies to every platform, not just X (PART XXVI) ────────────────

@pytest.mark.asyncio
async def test_instagram_caption_is_cleaned_of_markdown_dashes_and_link_dump(
        httpx_mock: HTTPXMock):
    """Straight from a live carousel run: bold markers, an em dash and a trailing
    block of source links, all of which Instagram publishes literally."""
    body = dict(
        GOOD_JSON,
        caption=("Meal prep doesn't have to be overwhelming.\n\n"
                 "1. **Cook grains in bulk**: rice, quinoa, or pasta—pick your favorite.\n\n"
                 "[grabguides.com](https://grabguides.com/blog/2-hour-sunday-meal-prep)"),
        hook="**Hate cooking?** Try this system.",
        slide_overlays=["**Cook grains** in bulk.", "Prep proteins—it is quick."],
        cta="Save this—try it Sunday.",
    )
    httpx_mock.add_response(
        url=f"{BASE}/chat/completions",
        json={"choices": [{"message": {"content": json.dumps(body)}}]},
    )
    client = OpenRouterClient(api_key="test-key")
    gen = CaptionGenerator(client)
    result = await gen.generate(topic="meal prep", format="carousel_3", num_slides=2,
                                platform=Platform.INSTAGRAM)
    await client.close()

    assert "**" not in result.caption
    assert "grabguides" not in result.caption          # trailing link dump gone
    assert "—" not in result.caption
    assert result.caption.endswith("pick your favorite.")
    assert result.hook == "Hate cooking? Try this system."
    assert result.cta == "Save this, try it Sunday."
    assert result.slide_overlays == ["Cook grains in bulk.", "Prep proteins, it is quick."]


def test_instagram_and_linkedin_prompts_ban_markdown():
    from services.caption_generator import INSTAGRAM_SYSTEM_PROMPT, LINKEDIN_SYSTEM_PROMPT
    assert "renders no markdown" in INSTAGRAM_SYSTEM_PROMPT
    assert "Do NOT paste source links" in INSTAGRAM_SYSTEM_PROMPT
    assert "renders no markdown" in LINKEDIN_SYSTEM_PROMPT


@pytest.mark.asyncio
async def test_variations_come_back_polished(httpx_mock: HTTPXMock):
    """A variation lands in the field we just cleaned — it has to be clean too."""
    httpx_mock.add_response(
        url=f"{BASE}/chat/completions",
        json={"choices": [{"message": {"content": json.dumps(
            {"variants": ["**Bold** idea—for you.", "Plain idea."]})}}]},
    )
    client = OpenRouterClient(api_key="test-key")
    gen = CaptionGenerator(client)
    out = await gen.regenerate_field("caption", topic="t", current_value="c",
                                     platform=Platform.INSTAGRAM)
    await client.close()
    assert out == ["Bold idea, for you.", "Plain idea."]


def test_all_prompts_forbid_fabricated_statistics():
    """A brand can't defend a number the model rounded in its own favour."""
    from services.caption_generator import (
        INSTAGRAM_SYSTEM_PROMPT, LINKEDIN_SYSTEM_PROMPT,
        X_SYSTEM_PROMPT, X_THREAD_SYSTEM_PROMPT, X_LONG_SYSTEM_PROMPT,
    )
    for prompt in (INSTAGRAM_SYSTEM_PROMPT, LINKEDIN_SYSTEM_PROMPT,
                   X_SYSTEM_PROMPT, X_THREAD_SYSTEM_PROMPT, X_LONG_SYSTEM_PROMPT):
        assert "Do not invent statistics" in prompt


# ── lenient JSON parsing + one auto-retry (PART XXIX) ───────────────────────

def test_loads_lenient_leaves_valid_json_untouched():
    from services.caption_generator import loads_lenient
    assert loads_lenient(json.dumps(GOOD_JSON)) == GOOD_JSON


def test_loads_lenient_repairs_a_hashtag_missing_its_quote():
    """The exact live failure: a hashtag with no opening quote inside the array."""
    from services.caption_generator import loads_lenient
    broken = json.dumps(GOOD_JSON).replace('"#AI"', '#AI"')
    obj = loads_lenient(broken)
    assert isinstance(obj, dict)
    assert obj["caption"] == GOOD_JSON["caption"]    # required fields survive
    assert obj["hook"] == GOOD_JSON["hook"]


def test_loads_lenient_repairs_a_trailing_comma():
    from services.caption_generator import loads_lenient
    assert loads_lenient('{"a": 1, "b": 2,}') == {"a": 1, "b": 2}


@pytest.mark.asyncio
async def test_generate_rejects_hopeless_output(httpx_mock: HTTPXMock):
    """Repair never raises; a reply with no object at all must still fail loudly,
    not ship an empty post."""
    httpx_mock.add_response(
        url=f"{BASE}/chat/completions",
        json={"choices": [{"message": {"content": "Sorry, I can't help with that."}}]},
        is_reusable=True,   # retry hits the mock a second time
    )
    client = OpenRouterClient(api_key="test-key")
    gen = CaptionGenerator(client)
    with pytest.raises(CaptionParseError):
        await gen.generate(topic="AI", format="single", num_slides=1)
    await client.close()


@pytest.mark.asyncio
async def test_generate_retries_once_on_broken_json():
    """Broken JSON on the first sample, valid on the second → one post, two calls."""
    from unittest.mock import AsyncMock
    incomplete = dict(GOOD_JSON)
    del incomplete["caption"]   # repair can't invent a missing required field
    provider = AsyncMock()
    provider.generate_text = AsyncMock(
        side_effect=[(json.dumps(incomplete), []), (json.dumps(GOOD_JSON), [])])
    gen = CaptionGenerator(provider)

    result = await gen.generate(topic="AI", format="single", num_slides=1)

    assert result.caption == GOOD_JSON["caption"]
    assert provider.generate_text.await_count == 2


@pytest.mark.asyncio
async def test_generate_gives_up_after_one_retry():
    """Broken both times → error, and exactly two calls (no infinite loop)."""
    from unittest.mock import AsyncMock
    provider = AsyncMock()
    provider.generate_text = AsyncMock(return_value=("not json at all", []))
    gen = CaptionGenerator(provider)

    with pytest.raises(CaptionParseError):
        await gen.generate(topic="AI", format="single", num_slides=1)
    assert provider.generate_text.await_count == 2


@pytest.mark.asyncio
async def test_generate_repairs_broken_json_without_a_retry():
    """A repairable reply (a hashtag missing its quote) parses on the first sample —
    _parse must route through the repairer, so no second generation is spent."""
    from unittest.mock import AsyncMock
    broken = json.dumps(GOOD_JSON).replace('"#AI"', '#AI"')   # missing opening quote
    provider = AsyncMock()
    provider.generate_text = AsyncMock(return_value=(broken, []))
    gen = CaptionGenerator(provider)

    result = await gen.generate(topic="AI", format="single", num_slides=1)

    assert result.caption == GOOD_JSON["caption"]
    assert provider.generate_text.await_count == 1     # repaired in place, no retry


def test_x_short_states_one_length_and_it_is_the_real_limit():
    """The truncated drafts users saw were a prompt conflict, not a bad cutter:
    X_SYSTEM_PROMPT asked for ~200 characters while the injected length tier asked
    for "~150-400 characters total", so the model followed the looser number and
    overshot into the trimmer. A single X post must state exactly one budget.

    Mutation guard: hand the generic tier back to X/SHORT and "150-400" reappears.
    """
    from models.schemas import TWEET_CHAR_LIMIT, LengthTier, XPostMode
    from services.caption_generator import _length_instruction

    x_short = _length_instruction(Platform.X, XPostMode.SHORT, LengthTier.SWEET_SPOT)
    assert str(TWEET_CHAR_LIMIT) in x_short
    assert "150-400" not in x_short

    # Other platforms, and X threads/long posts, keep the tier guidance.
    for platform, mode in ((Platform.INSTAGRAM, XPostMode.SHORT),
                           (Platform.X, XPostMode.THREAD),
                           (Platform.X, XPostMode.LONG)):
        assert "150-400" in _length_instruction(platform, mode, LengthTier.SWEET_SPOT)
