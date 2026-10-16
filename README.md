# Recipe Library

A personal recipe library that can pull recipes in from websites, cooking videos and screenshots. It's a Flask app with a JSON API, and it stores everything in a local SQLite database (`recipes.db`).

## Features

- **Three ways to add a recipe:**
  - **From URL:** paste a link to a recipe webpage or a cooking video (YouTube, Instagram, TikTok and similar).
  - **From Image:** upload a screenshot or photo of a recipe and it's read with OCR.
  - **Type It In:** enter the title, ingredients and steps by hand.
- **Edit** any recipe's title, ingredients and steps after it's saved.
- **Tags:** comes with Vegan, Vegetarian, Dessert and Meal. You can create, assign and delete your own in *Manage Tags*.
- **Search** across titles, ingredients and tags. Separate terms with commas to match recipes that contain all of them (e.g. `mushrooms, tofu`).
- **Multiple users:** each recipe belongs to a user, and you can switch between users' libraries. The selected user is remembered in the browser.
- **Saved logins:** if a video site needs you to sign in, you can enter a username and password, which are saved for that site.

## How recipe extraction works

The logic is in `extractor.py`.

- **Webpages:** first looks for structured recipe data (schema.org JSON-LD) in the page. If there isn't any, it falls back to parsing the HTML. Pages are fetched with `curl_cffi` impersonating Chrome when it's installed, which gets past some bot blocking.
- **Videos:** uses `yt-dlp` to read the video's title, description and subtitles without downloading the video, and follows recipe links in the description. If that isn't enough, it transcribes the audio with OpenAI's Whisper (`openai/whisper-base` via Hugging Face `transformers`). Whisper loads the first time it's needed, which can be slow.
- **Images:** cleans up the image (grayscale, higher contrast, sharpened) and runs Tesseract OCR on it. It then splits the text into ingredients and steps.

## Running it

Requires Python 3. Image upload also needs the [Tesseract](https://github.com/tesseract-ocr/tesseract) OCR engine (`brew install tesseract` on macOS).

```bash
python -m venv venv
source venv/bin/activate
pip install -r requirements.txt
python app.py
```

Then open http://localhost:5003. The database is created, or updated to the latest structure, automatically on startup.

## API

| Method | Endpoint | Description |
|---|---|---|
| `GET` | `/api/recipes` | List recipes (optional `?tag=` and `?user_id=` filters) |
| `GET` | `/api/recipes/<id>` | Get one recipe |
| `POST` | `/api/recipes` | Add from a `url`, or manually with `title`, `ingredients` and `steps` |
| `POST` | `/api/recipes/from-image` | Add from an uploaded image (`image` form field) |
| `PATCH` | `/api/recipes/<id>` | Update `title`, `ingredients` or `steps` |
| `DELETE` | `/api/recipes/<id>` | Delete a recipe |
| `GET` / `POST` | `/api/tags` | List or create tags |
| `DELETE` | `/api/tags/<id>` | Delete a tag |
| `POST` | `/api/recipes/<id>/tags` | Add a tag to a recipe (`tag_id`) |
| `DELETE` | `/api/recipes/<id>/tags/<tag_id>` | Remove a tag from a recipe |
| `GET` / `POST` | `/api/users` | List or create users |
| `DELETE` | `/api/users/<id>` | Delete a user and their recipes |
| `GET` | `/api/credentials` | List saved logins (site and username only) |
| `DELETE` | `/api/credentials/<domain>` | Delete a saved login |

## Files

| File | Purpose |
|---|---|
| `app.py` | Flask app, database setup and API on port 5003 |
| `extractor.py` | Gets recipes out of webpages, videos and images |
| `templates/index.html` | The UI: HTML, CSS and JavaScript |
| `recipes.db` | SQLite database holding recipes, tags, users and saved logins |
| `requirements.txt` | Python dependencies |

> **Note:** saved logins are stored as plain text in `recipes.db`, and that file is committed to this public repository. Don't commit `recipes.db` after saving a login.
