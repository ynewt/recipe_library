"""Extract recipe data from URLs (web pages and videos) and images."""

import os
import re
import json
import tempfile
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup
import yt_dlp
import pytesseract
from PIL import Image, ImageFilter, ImageEnhance

# Lazy-load whisper pipeline (heavy import)
_whisper_pipe = None


def _get_whisper():
    global _whisper_pipe
    if _whisper_pipe is None:
        from transformers import pipeline
        _whisper_pipe = pipeline(
            "automatic-speech-recognition",
            model="openai/whisper-base",
            chunk_length_s=30,
            return_timestamps=True,
        )
    return _whisper_pipe

# Try recipe-scrapers
try:
    from recipe_scrapers import scrape_html
except ImportError:
    scrape_html = None


VIDEO_DOMAINS = {
    "youtube.com", "youtu.be", "www.youtube.com", "m.youtube.com",
    "vimeo.com", "www.vimeo.com",
    "facebook.com", "www.facebook.com", "fb.watch", "m.facebook.com",
    "instagram.com", "www.instagram.com",
    "tiktok.com", "www.tiktok.com",
    "twitter.com", "x.com",
    "dailymotion.com", "www.dailymotion.com",
    "twitch.tv", "www.twitch.tv",
}


def is_video_url(url):
    """Check if URL is likely a video platform."""
    domain = urlparse(url).hostname or ""
    domain = domain.lower()
    if any(domain.endswith(v) for v in VIDEO_DOMAINS):
        return True
    # Also check for common video path patterns
    path = urlparse(url).path.lower()
    if any(ext in path for ext in ["/watch", "/video", "/reel", "/shorts"]):
        return True
    return False


def extract_recipe(url, credentials=None):
    """
    Main entry point. Returns dict with:
      title, ingredients (list), steps (list), source_url, image_url
    Raises RuntimeError on failure, with "AUTH_REQUIRED" prefix if login needed.
    """
    if is_video_url(url):
        return extract_from_video(url, credentials)
    else:
        return extract_from_webpage(url)


def extract_from_image(file_storage):
    """Extract recipe from an uploaded image using OCR.

    Args:
        file_storage: a Flask FileStorage (from request.files)

    Returns:
        dict with ingredients (list) and steps (list)
    """
    try:
        img = Image.open(file_storage)
    except Exception:
        raise RuntimeError("Could not open image. Please upload a valid image file.")

    # Preprocess for better OCR: convert to grayscale, enhance contrast, sharpen
    img = img.convert("L")
    img = ImageEnhance.Contrast(img).enhance(1.5)
    img = img.filter(ImageFilter.SHARPEN)

    try:
        text = pytesseract.image_to_string(img)
    except Exception as e:
        raise RuntimeError(f"OCR failed: {str(e)[:200]}")

    if not text or len(text.strip()) < 10:
        raise RuntimeError("Could not extract any text from the image. Try a clearer photo.")

    # Parse with the structured parser first, then aggressive
    ingredients, steps = parse_recipe_text(text)

    if not ingredients:
        ingredients, steps2 = parse_recipe_text_aggressive(text)
        if not steps:
            steps = steps2

    if not ingredients:
        ingredients = _extract_ingredient_lines(text)

    if not steps:
        steps = _extract_step_lines(text)

    # If still nothing, treat every non-empty line as an ingredient
    # (common for screenshots that are just an ingredient list)
    if not ingredients:
        lines = [l.strip() for l in text.split('\n') if l.strip() and len(l.strip()) > 2]
        # Filter out obvious non-ingredient lines
        ingredients = [
            l for l in lines
            if not re.match(r'^(http|www\.|#|@|follow|subscribe)', l, re.IGNORECASE)
            and len(l) < 150
        ]

    return {
        "ingredients": ingredients[:60],
        "steps": steps[:40],
    }


def extract_from_webpage(url):
    """Extract recipe from a web page using recipe-scrapers or fallback."""
    headers = {
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                       "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
    }

    try:
        resp = requests.get(url, headers=headers, timeout=15, allow_redirects=True)
        resp.raise_for_status()
    except requests.exceptions.HTTPError as e:
        raise RuntimeError(f"Could not fetch page (HTTP {resp.status_code}). The URL may be invalid or the site may be blocking requests.")
    except requests.exceptions.ConnectionError:
        raise RuntimeError("Could not connect to the website. Please check the URL.")
    except requests.exceptions.Timeout:
        raise RuntimeError("Request timed out. The website may be slow or unreachable.")
    except requests.exceptions.RequestException as e:
        raise RuntimeError(f"Could not fetch page: {str(e)[:200]}")

    html = resp.text

    # Try recipe-scrapers first
    if scrape_html:
        try:
            scraper = scrape_html(html, org_url=url)
            return {
                "title": scraper.title() or "Untitled Recipe",
                "ingredients": scraper.ingredients() or [],
                "steps": _split_instructions(scraper.instructions() or ""),
                "source_url": url,
                "image_url": _safe_call(scraper.image) or "",
            }
        except Exception:
            pass  # Fall through to manual parsing

    # Fallback: try JSON-LD
    result = _parse_jsonld(html, url)
    if result:
        return result

    # Fallback: try scraping common recipe patterns
    result = _parse_html_heuristic(html, url)
    if result:
        return result

    raise RuntimeError("Could not extract recipe from this page. It may not contain a recognized recipe format.")


def _safe_call(fn):
    try:
        return fn()
    except Exception:
        return None


def _split_instructions(text):
    """Split instruction text into individual steps."""
    if not text:
        return []
    # Split on numbered patterns or newlines
    lines = re.split(r'\n+', text.strip())
    steps = []
    for line in lines:
        line = re.sub(r'^\s*(?:\d+[\.\)]\s*|[-*]\s*)', '', line).strip()
        if line:
            steps.append(line)
    return steps


def _parse_jsonld(html, url):
    """Extract recipe from JSON-LD structured data."""
    soup = BeautifulSoup(html, "html.parser")
    for script in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(script.string)
            recipes = _find_recipes_in_jsonld(data)
            if recipes:
                r = recipes[0]
                ingredients = r.get("recipeIngredient", [])
                instructions = _extract_jsonld_instructions(r.get("recipeInstructions", []))
                return {
                    "title": r.get("name", "Untitled Recipe"),
                    "ingredients": ingredients if isinstance(ingredients, list) else [ingredients],
                    "steps": instructions,
                    "source_url": url,
                    "image_url": _extract_image(r.get("image", "")),
                }
        except (json.JSONDecodeError, TypeError):
            continue
    return None


def _find_recipes_in_jsonld(data):
    """Recursively find Recipe objects in JSON-LD."""
    results = []
    if isinstance(data, dict):
        if data.get("@type") in ("Recipe", ["Recipe"]):
            results.append(data)
        elif "@graph" in data:
            for item in data["@graph"]:
                results.extend(_find_recipes_in_jsonld(item))
    elif isinstance(data, list):
        for item in data:
            results.extend(_find_recipes_in_jsonld(item))
    return results


def _extract_jsonld_instructions(instructions):
    """Parse recipeInstructions from JSON-LD."""
    if isinstance(instructions, str):
        return _split_instructions(instructions)
    steps = []
    if isinstance(instructions, list):
        for item in instructions:
            if isinstance(item, str):
                steps.append(item.strip())
            elif isinstance(item, dict):
                if item.get("@type") == "HowToStep":
                    steps.append(item.get("text", "").strip())
                elif item.get("@type") == "HowToSection":
                    for sub in item.get("itemListElement", []):
                        if isinstance(sub, dict):
                            steps.append(sub.get("text", "").strip())
                        elif isinstance(sub, str):
                            steps.append(sub.strip())
    return [s for s in steps if s]


def _extract_image(img):
    if isinstance(img, str):
        return img
    if isinstance(img, dict):
        return img.get("url", "")
    if isinstance(img, list) and img:
        return _extract_image(img[0])
    return ""


def _parse_html_heuristic(html, url):
    """Last-resort heuristic extraction from HTML."""
    soup = BeautifulSoup(html, "html.parser")
    title = ""
    title_tag = soup.find("h1")
    if title_tag:
        title = title_tag.get_text(strip=True)

    text = soup.get_text("\n", strip=True)
    ingredients, steps = parse_recipe_text(text)

    if ingredients or steps:
        return {
            "title": title or "Untitled Recipe",
            "ingredients": ingredients,
            "steps": steps,
            "source_url": url,
            "image_url": "",
        }
    return None


# ---- Video extraction ----

def extract_from_video(url, credentials=None):
    """Extract recipe from video using metadata, description links, subtitles, and audio."""
    ydl_opts = {
        "skip_download": True,
        "writeautomaticsub": True,
        "subtitleslangs": ["en"],
        "quiet": True,
        "no_warnings": True,
    }

    if credentials:
        if credentials.get("username"):
            ydl_opts["username"] = credentials["username"]
        if credentials.get("password"):
            ydl_opts["password"] = credentials["password"]

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)
    except yt_dlp.utils.DownloadError as e:
        err = str(e).lower()
        if any(kw in err for kw in ["login", "sign in", "authenticate", "credentials", "private"]):
            raise RuntimeError("AUTH_REQUIRED: This video requires authentication. Please provide login credentials.")
        raise RuntimeError(f"Could not extract video info: {str(e)[:300]}")
    except Exception as e:
        raise RuntimeError(f"Could not extract video info: {str(e)[:300]}")

    if not info:
        raise RuntimeError("Could not parse video metadata.")

    title = info.get("title", "Untitled Recipe")
    description = info.get("description", "")
    image_url = info.get("thumbnail", "")

    ingredients, steps = [], []

    # ---- Strategy 1: Follow links in the description to find a recipe page ----
    desc_urls = _extract_urls(description)
    for linked_url in desc_urls:
        if is_video_url(linked_url):
            continue
        try:
            result = extract_from_webpage(linked_url)
            if result and (result["ingredients"] or result["steps"]):
                # Use the video title but recipe content from the linked page
                return {
                    "title": title or result["title"],
                    "ingredients": result["ingredients"],
                    "steps": result["steps"],
                    "source_url": url,
                    "image_url": image_url or result.get("image_url", ""),
                }
        except Exception:
            continue

    # ---- Strategy 2: Parse the description text directly ----
    ingredients, steps = parse_recipe_text(description)
    if not ingredients:
        ingredients, steps2 = parse_recipe_text_aggressive(description)
        if not steps:
            steps = steps2

    # ---- Strategy 3: Parse subtitle text ----
    subtitle_text = _get_subtitles_text(info)
    if not ingredients and subtitle_text:
        ingredients, steps3 = parse_recipe_text(subtitle_text)
        if not steps:
            steps = steps3
    if not ingredients and subtitle_text:
        ingredients, steps4 = parse_recipe_text_aggressive(subtitle_text)
        if not steps:
            steps = steps4

    # ---- Strategy 4: Transcribe video audio with Whisper ----
    if not ingredients:
        try:
            transcript = _transcribe_video_audio(url, credentials)
            if transcript:
                ing_t, steps_t = parse_recipe_text(transcript)
                if not ing_t:
                    ing_t, steps_t2 = parse_recipe_text_aggressive(transcript)
                    if not steps_t:
                        steps_t = steps_t2
                if not ing_t:
                    ing_t = _extract_ingredient_lines(transcript)
                if not steps_t:
                    steps_t = _extract_step_lines(transcript)
                if ing_t:
                    ingredients = ing_t
                if not steps and steps_t:
                    steps = steps_t
        except Exception:
            pass  # Audio transcription failed, continue with what we have

    # ---- Strategy 5: Line-by-line fallback on description ----
    if not ingredients:
        ingredients = _extract_ingredient_lines(description)
    if not steps and subtitle_text:
        steps = _extract_step_lines(subtitle_text)

    return {
        "title": title,
        "ingredients": ingredients,
        "steps": steps,
        "source_url": url,
        "image_url": image_url,
    }


def _extract_urls(text):
    """Extract HTTP(S) URLs from text."""
    if not text:
        return []
    url_pattern = re.compile(r'https?://[^\s<>"\')\]]+')
    urls = url_pattern.findall(text)
    # Clean trailing punctuation
    cleaned = []
    for u in urls:
        u = u.rstrip('.,;:!?)\'"]')
        if len(u) > 10:
            cleaned.append(u)
    return cleaned


def _transcribe_video_audio(url, credentials=None):
    """Download video audio and transcribe with Whisper."""
    with tempfile.TemporaryDirectory() as tmpdir:
        audio_path = os.path.join(tmpdir, "audio")

        ydl_opts = {
            "format": "bestaudio/best",
            "outtmpl": audio_path + ".%(ext)s",
            "postprocessors": [{
                "key": "FFmpegExtractAudio",
                "preferredcodec": "wav",
                "preferredquality": "16",
            }],
            "quiet": True,
            "no_warnings": True,
        }

        if credentials:
            if credentials.get("username"):
                ydl_opts["username"] = credentials["username"]
            if credentials.get("password"):
                ydl_opts["password"] = credentials["password"]

        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            ydl.download([url])

        # Find the output audio file
        wav_file = audio_path + ".wav"
        if not os.path.exists(wav_file):
            # Try to find whatever file was created
            for f in os.listdir(tmpdir):
                if f.startswith("audio"):
                    wav_file = os.path.join(tmpdir, f)
                    break
            else:
                return ""

        # Transcribe with Whisper
        pipe = _get_whisper()
        result = pipe(wav_file)
        return result.get("text", "")


def _get_subtitles_text(info):
    """Extract subtitle text from yt-dlp info."""
    # Check for automatic or manual subtitles
    for sub_key in ("subtitles", "automatic_captions"):
        subs = info.get(sub_key, {})
        for lang in ("en", "en-US", "en-GB"):
            if lang in subs:
                for fmt in subs[lang]:
                    sub_url = fmt.get("url", "")
                    if sub_url and fmt.get("ext") in ("vtt", "srv1", "srv2", "srv3", "json3"):
                        try:
                            resp = requests.get(sub_url, timeout=10)
                            return _clean_subtitle_text(resp.text)
                        except Exception:
                            continue
    return ""


def _clean_subtitle_text(raw):
    """Clean VTT/SRT subtitle text to plain text."""
    # Remove VTT header
    raw = re.sub(r'WEBVTT.*?\n\n', '', raw, flags=re.DOTALL)
    # Remove timestamps
    raw = re.sub(r'\d{2}:\d{2}:\d{2}[\.,]\d{3}\s*-->\s*\d{2}:\d{2}:\d{2}[\.,]\d{3}.*?\n', '', raw)
    # Remove position/alignment tags
    raw = re.sub(r'<[^>]+>', '', raw)
    # Remove duplicate consecutive lines (common in auto-subs)
    lines = raw.split('\n')
    deduped = []
    prev = ""
    for line in lines:
        line = line.strip()
        if line and line != prev and not re.match(r'^\d+$', line):
            deduped.append(line)
            prev = line
    return " ".join(deduped)


# ---- Text parsing for recipe content ----

INGREDIENT_HEADERS = re.compile(
    r'(?:^|\n)\s*(?:ingredients?|what you.?ll need|you.?ll need|shopping list|for the (?:sauce|dough|filling|batter|marinade|dressing|topping|frosting|crust|glaze))\s*[:\-]?\s*\n',
    re.IGNORECASE
)

STEP_HEADERS = re.compile(
    r'(?:^|\n)\s*(?:instructions?|directions?|steps?|method|preparation|how to (?:make|cook|prepare)|procedure|to make)\s*[:\-]?\s*\n',
    re.IGNORECASE
)

SECTION_HEADERS = re.compile(
    r'(?:^|\n)\s*(?:ingredients?|instructions?|directions?|steps?|method|preparation|notes?|tips?|nutrition|serving|equipment|for the \w+)\s*[:\-]?\s*\n',
    re.IGNORECASE
)

# Common measurements and units
_UNITS = (
    r'cups?|tablespoons?|tbsps?|teaspoons?|tsps?|oz|ounces?|pounds?|lbs?|'
    r'grams?|g|kgs?|mls?|liters?|litres?|pinch(?:es)?|dash(?:es)?|handfuls?|'
    r'cloves?|cans?|bunch(?:es)?|stalks?|slices?|pieces?|sticks?|sprigs?|heads?|'
    r'pints?|quarts?|gallons?|package|pkg|bag'
)

# Common food/ingredient words
_FOODS = (
    r'salt|pepper|sugar|flour|butter|oil|garlic|onion|egg|milk|cream|cheese|'
    r'chicken|beef|pork|fish|rice|pasta|noodle|bread|tomato|potato|carrot|'
    r'lemon|lime|vinegar|sauce|broth|stock|water|wine|honey|mustard|'
    r'ginger|cumin|paprika|oregano|basil|parsley|cilantro|thyme|rosemary|'
    r'cinnamon|vanilla|baking|powder|soda|yeast|chocolate|cocoa|'
    r'avocado|spinach|lettuce|celery|mushroom|bell pepper|jalape[nñ]o|'
    r'shrimp|salmon|tuna|bacon|sausage|ham|turkey|lamb|'
    r'yogurt|sour cream|mayonnaise|ketchup|soy sauce|'
    r'almond|walnut|pecan|peanut|coconut|sesame|'
    r'banana|apple|strawberr|blueberr|raspberr|mango|pineapple|orange|'
    r'zucchini|broccoli|cauliflower|corn|bean|pea|lentil|chickpea|'
    r'tortilla|wrap|bun|pita'
)

_INGREDIENT_LINE = re.compile(
    r'(?:'
    # Pattern 1: starts with a number/fraction followed by a unit
    r'^\s*[\d½¼¾⅓⅔⅛⅜⅝⅞]+[\d/\s½¼¾⅓⅔⅛⅜⅝⅞.,]*\s*(?:' + _UNITS + r')s?\b'
    r'|'
    # Pattern 2: starts with a number/fraction (even without unit)
    r'^\s*[\d½¼¾⅓⅔⅛⅜⅝⅞]+[\d/\s½¼¾⅓⅔⅛⅜⅝⅞.,]*\s+\w+'
    r'|'
    # Pattern 3: line contains food words and is shortish (likely an ingredient)
    r'^.{0,80}(?:' + _FOODS + r')'
    r')',
    re.IGNORECASE | re.MULTILINE
)

_COOKING_VERBS = (
    r'preheat|heat|add|mix|stir|cook|bake|fry|saut[eé]|boil|simmer|'
    r'chop|dice|mince|slice|julienne|grate|shred|'
    r'combine|whisk|fold|pour|drain|season|serve|plate|'
    r'place|put|set|let|allow|leave|rest|'
    r'remove|transfer|spread|layer|arrange|'
    r'roll|knead|cover|wrap|refrigerate|chill|freeze|'
    r'marinate|brine|rub|coat|dredge|bread|'
    r'grill|roast|broil|toast|sear|brown|'
    r'blend|pur[eé]e|process|mash|crush|'
    r'garnish|top|drizzle|sprinkle|brush|glaze|'
    r'bring|reduce|deglaze|strain|'
    r'toss|flip|turn|rotate|'
    r'beat|cream|whip|knead'
)


def parse_recipe_text(text):
    """Parse recipe text to extract ingredients and steps using section headers."""
    if not text:
        return [], []

    ingredients = []
    steps = []

    # Try to find ingredients section
    ing_match = INGREDIENT_HEADERS.search(text)
    if ing_match:
        after_ing = text[ing_match.end():]
        next_section = SECTION_HEADERS.search(after_ing)
        ing_block = after_ing[:next_section.start()] if next_section else after_ing[:2000]
        ingredients = _parse_list_block(ing_block)

    # Try to find steps section
    step_match = STEP_HEADERS.search(text)
    if step_match:
        after_step = text[step_match.end():]
        next_section = SECTION_HEADERS.search(after_step)
        step_block = after_step[:next_section.start()] if next_section else after_step[:3000]
        steps = _parse_list_block(step_block)

    return ingredients, steps


def parse_recipe_text_aggressive(text):
    """Aggressive parsing without requiring section headers."""
    if not text:
        return [], []

    ingredients = _extract_ingredient_lines(text)
    steps = _extract_step_lines(text)
    return ingredients, steps


def _extract_ingredient_lines(text):
    """Extract lines that look like ingredients from any text."""
    if not text:
        return []

    lines = text.split('\n')
    ingredients = []
    seen = set()

    for line in lines:
        line = line.strip()
        # Remove bullet/number prefix
        clean = re.sub(r'^(?:\d+[\.\)]\s*|[-*•▪]\s*|[✓✔]\s*)', '', line).strip()
        if not clean or len(clean) < 3 or len(clean) > 150:
            continue
        # Skip lines that look like URLs, hashtags, or @mentions
        if re.match(r'^(?:http|www\.|#|@|follow|subscribe|like|share|comment|link)', clean, re.IGNORECASE):
            continue
        # Skip lines that are clearly section headers
        if re.match(r'^(?:ingredients?|instructions?|directions?|steps?|method|notes?|tips?)\s*[:\-]?\s*$', clean, re.IGNORECASE):
            continue

        # Check if line looks like an ingredient
        is_ingredient = False

        # Has a quantity at the start
        if re.match(r'^[\d½¼¾⅓⅔⅛⅜⅝⅞]+', clean):
            is_ingredient = True

        # Has measurement units
        if re.search(r'\b(?:' + _UNITS + r')s?\b', clean, re.IGNORECASE):
            is_ingredient = True

        # Short line with food words (likely ingredient list format)
        if len(clean) < 80 and re.search(r'\b(?:' + _FOODS + r')\w*\b', clean, re.IGNORECASE):
            # But not if it's a full sentence with cooking verbs (that's a step)
            if not re.search(r'\b(?:' + _COOKING_VERBS + r')\b', clean, re.IGNORECASE):
                is_ingredient = True

        if is_ingredient:
            key = clean.lower()
            if key not in seen:
                seen.add(key)
                ingredients.append(clean)

    return ingredients[:60]


def _extract_step_lines(text):
    """Extract lines that look like cooking steps from any text."""
    if not text:
        return []

    lines = text.split('\n')
    steps = []
    seen = set()

    for line in lines:
        line = line.strip()
        clean = re.sub(r'^(?:\d+[\.\)]\s*|[-*•▪]\s*)', '', line).strip()
        if not clean or len(clean) < 10:
            continue

        # Check if line starts with or contains a cooking verb
        if re.match(
            r'^(?:(?:first|then|next|now|finally|after that|meanwhile|while|once|when)[\s,]*)?'
            r'(?:' + _COOKING_VERBS + r')\b',
            clean, re.IGNORECASE
        ):
            key = clean.lower()
            if key not in seen:
                seen.add(key)
                steps.append(clean)
        # Also catch numbered steps like "1. Do something" or "Step 1: Do something"
        elif re.match(r'^(?:step\s*)?\d+[\.\):\s]', line, re.IGNORECASE):
            key = clean.lower()
            if key not in seen:
                seen.add(key)
                steps.append(clean)

    return steps[:40]


def _parse_list_block(block):
    """Parse a text block into list items."""
    lines = block.strip().split('\n')
    items = []
    for line in lines:
        line = line.strip()
        # Remove bullet/number prefix
        line = re.sub(r'^(?:\d+[\.\)]\s*|[-*•▪]\s*|[✓✔]\s*)', '', line).strip()
        if not line:
            continue
        # Stop if we hit something that looks like a new major section
        if re.match(r'^(?:note|tip|serving|nutrition|equipment|source|credit|adapted)s?\s*:', line, re.IGNORECASE):
            break
        if len(line) > 3:
            items.append(line)
    return items
