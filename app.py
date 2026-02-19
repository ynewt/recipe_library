import os
import json
import sqlite3
from urllib.parse import urlparse
from flask import Flask, render_template, request, jsonify, g

from extractor import extract_recipe, extract_from_image

app = Flask(__name__)
DB_PATH = os.path.join(os.path.dirname(__file__), "recipes.db")


def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(DB_PATH)
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA foreign_keys = ON")
    return g.db


@app.teardown_appcontext
def close_db(exc):
    db = g.pop("db", None)
    if db:
        db.close()


def init_db():
    db = sqlite3.connect(DB_PATH)
    db.executescript("""
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL UNIQUE
        );

        CREATE TABLE IF NOT EXISTS recipes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT NOT NULL,
            ingredients TEXT NOT NULL DEFAULT '[]',
            steps TEXT NOT NULL DEFAULT '[]',
            source_url TEXT,
            image_url TEXT,
            user_id INTEGER REFERENCES users(id),
            created_at TEXT NOT NULL DEFAULT (datetime('now'))
        );

        CREATE TABLE IF NOT EXISTS tags (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL UNIQUE
        );

        CREATE TABLE IF NOT EXISTS recipe_tags (
            recipe_id INTEGER NOT NULL,
            tag_id INTEGER NOT NULL,
            PRIMARY KEY (recipe_id, tag_id),
            FOREIGN KEY (recipe_id) REFERENCES recipes(id) ON DELETE CASCADE,
            FOREIGN KEY (tag_id) REFERENCES tags(id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS credentials (
            domain TEXT PRIMARY KEY,
            username TEXT NOT NULL,
            password TEXT NOT NULL
        );
    """)

    # Add user_id column if missing (migration for existing DB)
    cols = [row[1] for row in db.execute("PRAGMA table_info(recipes)").fetchall()]
    if "user_id" not in cols:
        db.execute("ALTER TABLE recipes ADD COLUMN user_id INTEGER REFERENCES users(id)")

    # Seed default user and assign orphan recipes
    db.execute("INSERT OR IGNORE INTO users (name) VALUES (?)", ("Yulia",))
    db.commit()
    yulia_id = db.execute("SELECT id FROM users WHERE name = ?", ("Yulia",)).fetchone()[0]
    db.execute("UPDATE recipes SET user_id = ? WHERE user_id IS NULL", (yulia_id,))

    # Remove old default tags and seed new ones
    db.execute("DELETE FROM tags WHERE name NOT IN (SELECT DISTINCT t.name FROM tags t JOIN recipe_tags rt ON rt.tag_id = t.id)")
    default_tags = ["Vegan", "Vegetarian", "Dessert", "Meal"]
    for tag in default_tags:
        db.execute("INSERT OR IGNORE INTO tags (name) VALUES (?)", (tag,))
    db.commit()
    db.close()


def recipe_to_dict(row, db):
    """Convert a recipe row to dict with tags."""
    recipe_id = row["id"]
    tag_rows = db.execute("""
        SELECT t.id, t.name FROM tags t
        JOIN recipe_tags rt ON rt.tag_id = t.id
        WHERE rt.recipe_id = ?
        ORDER BY t.name
    """, (recipe_id,)).fetchall()

    return {
        "id": row["id"],
        "title": row["title"],
        "ingredients": json.loads(row["ingredients"]),
        "steps": json.loads(row["steps"]),
        "source_url": row["source_url"],
        "image_url": row["image_url"],
        "user_id": row["user_id"],
        "created_at": row["created_at"],
        "tags": [{"id": t["id"], "name": t["name"]} for t in tag_rows],
    }


# ---- Pages ----

@app.route("/")
def index():
    return render_template("index.html")


# ---- Recipe CRUD ----

@app.route("/api/recipes")
def list_recipes():
    db = get_db()
    tag_filter = request.args.get("tag")
    user_id = request.args.get("user_id", type=int)

    if tag_filter and user_id:
        rows = db.execute("""
            SELECT DISTINCT r.* FROM recipes r
            JOIN recipe_tags rt ON rt.recipe_id = r.id
            JOIN tags t ON t.id = rt.tag_id
            WHERE t.name = ? AND r.user_id = ?
            ORDER BY r.created_at DESC
        """, (tag_filter, user_id)).fetchall()
    elif tag_filter:
        rows = db.execute("""
            SELECT DISTINCT r.* FROM recipes r
            JOIN recipe_tags rt ON rt.recipe_id = r.id
            JOIN tags t ON t.id = rt.tag_id
            WHERE t.name = ?
            ORDER BY r.created_at DESC
        """, (tag_filter,)).fetchall()
    elif user_id:
        rows = db.execute("SELECT * FROM recipes WHERE user_id = ? ORDER BY created_at DESC", (user_id,)).fetchall()
    else:
        rows = db.execute("SELECT * FROM recipes ORDER BY created_at DESC").fetchall()
    return jsonify([recipe_to_dict(r, db) for r in rows])


@app.route("/api/recipes/<int:recipe_id>")
def get_recipe(recipe_id):
    db = get_db()
    row = db.execute("SELECT * FROM recipes WHERE id = ?", (recipe_id,)).fetchone()
    if not row:
        return jsonify({"error": "Not found"}), 404
    return jsonify(recipe_to_dict(row, db))


@app.route("/api/recipes", methods=["POST"])
def add_recipe():
    """Add a recipe by URL extraction or manual entry."""
    db = get_db()
    data = request.json
    url = data.get("url", "").strip()

    if url:
        # Check for stored credentials for this domain
        domain = urlparse(url).hostname or ""
        cred_row = db.execute("SELECT * FROM credentials WHERE domain = ?", (domain,)).fetchone()
        credentials = None
        if cred_row:
            credentials = {"username": cred_row["username"], "password": cred_row["password"]}

        # Also check if credentials were passed directly
        if data.get("username"):
            credentials = {"username": data["username"], "password": data.get("password", "")}
            # Store for later
            db.execute(
                "INSERT OR REPLACE INTO credentials (domain, username, password) VALUES (?, ?, ?)",
                (domain, data["username"], data.get("password", "")),
            )
            db.commit()

        try:
            recipe_data = extract_recipe(url, credentials)
        except RuntimeError as e:
            return jsonify({"error": str(e)}), 400
        except Exception as e:
            return jsonify({"error": f"Unexpected error: {str(e)[:300]}"}), 500

        cursor = db.execute(
            "INSERT INTO recipes (title, ingredients, steps, source_url, image_url, user_id) VALUES (?, ?, ?, ?, ?, ?)",
            (
                recipe_data["title"],
                json.dumps(recipe_data["ingredients"]),
                json.dumps(recipe_data["steps"]),
                recipe_data["source_url"],
                recipe_data.get("image_url", ""),
                data.get("user_id"),
            ),
        )
    else:
        # Manual entry
        title = data.get("title", "").strip()
        if not title:
            return jsonify({"error": "Title or URL required"}), 400
        cursor = db.execute(
            "INSERT INTO recipes (title, ingredients, steps, source_url, image_url, user_id) VALUES (?, ?, ?, ?, ?, ?)",
            (
                title,
                json.dumps(data.get("ingredients", [])),
                json.dumps(data.get("steps", [])),
                "",
                "",
                data.get("user_id"),
            ),
        )

    db.commit()
    row = db.execute("SELECT * FROM recipes WHERE id = ?", (cursor.lastrowid,)).fetchone()
    return jsonify(recipe_to_dict(row, db)), 201


@app.route("/api/recipes/from-image", methods=["POST"])
def add_recipe_from_image():
    """Add a recipe by uploading a screenshot/image."""
    db = get_db()

    if "image" not in request.files:
        return jsonify({"error": "No image uploaded"}), 400

    file = request.files["image"]
    if file.filename == "":
        return jsonify({"error": "No image selected"}), 400

    title = request.form.get("title", "").strip() or "Untitled Recipe"

    try:
        recipe_data = extract_from_image(file)
    except RuntimeError as e:
        return jsonify({"error": str(e)}), 400

    user_id = request.form.get("user_id", type=int)
    cursor = db.execute(
        "INSERT INTO recipes (title, ingredients, steps, source_url, image_url, user_id) VALUES (?, ?, ?, ?, ?, ?)",
        (
            title,
            json.dumps(recipe_data["ingredients"]),
            json.dumps(recipe_data["steps"]),
            "",
            "",
            user_id,
        ),
    )
    db.commit()
    row = db.execute("SELECT * FROM recipes WHERE id = ?", (cursor.lastrowid,)).fetchone()
    return jsonify(recipe_to_dict(row, db)), 201


@app.route("/api/recipes/<int:recipe_id>", methods=["PATCH"])
def update_recipe(recipe_id):
    db = get_db()
    data = request.json
    row = db.execute("SELECT * FROM recipes WHERE id = ?", (recipe_id,)).fetchone()
    if not row:
        return jsonify({"error": "Not found"}), 404

    if "title" in data:
        db.execute("UPDATE recipes SET title = ? WHERE id = ?", (data["title"], recipe_id))
    if "ingredients" in data:
        db.execute("UPDATE recipes SET ingredients = ? WHERE id = ?", (json.dumps(data["ingredients"]), recipe_id))
    if "steps" in data:
        db.execute("UPDATE recipes SET steps = ? WHERE id = ?", (json.dumps(data["steps"]), recipe_id))
    db.commit()

    row = db.execute("SELECT * FROM recipes WHERE id = ?", (recipe_id,)).fetchone()
    return jsonify(recipe_to_dict(row, db))


@app.route("/api/recipes/<int:recipe_id>", methods=["DELETE"])
def delete_recipe(recipe_id):
    db = get_db()
    db.execute("DELETE FROM recipes WHERE id = ?", (recipe_id,))
    db.commit()
    return "", 204


# ---- Tags ----

@app.route("/api/tags")
def list_tags():
    db = get_db()
    rows = db.execute("SELECT * FROM tags ORDER BY name").fetchall()
    return jsonify([{"id": r["id"], "name": r["name"]} for r in rows])


@app.route("/api/tags", methods=["POST"])
def create_tag():
    db = get_db()
    name = request.json.get("name", "").strip()
    if not name:
        return jsonify({"error": "Name required"}), 400
    try:
        cursor = db.execute("INSERT INTO tags (name) VALUES (?)", (name,))
        db.commit()
        return jsonify({"id": cursor.lastrowid, "name": name}), 201
    except sqlite3.IntegrityError:
        row = db.execute("SELECT * FROM tags WHERE name = ?", (name,)).fetchone()
        return jsonify({"id": row["id"], "name": row["name"]})


@app.route("/api/tags/<int:tag_id>", methods=["DELETE"])
def delete_tag(tag_id):
    db = get_db()
    db.execute("DELETE FROM recipe_tags WHERE tag_id = ?", (tag_id,))
    db.execute("DELETE FROM tags WHERE id = ?", (tag_id,))
    db.commit()
    return "", 204


@app.route("/api/recipes/<int:recipe_id>/tags", methods=["POST"])
def add_tag_to_recipe(recipe_id):
    db = get_db()
    tag_id = request.json.get("tag_id")
    if not tag_id:
        return jsonify({"error": "tag_id required"}), 400
    try:
        db.execute("INSERT INTO recipe_tags (recipe_id, tag_id) VALUES (?, ?)", (recipe_id, tag_id))
        db.commit()
    except sqlite3.IntegrityError:
        pass
    return jsonify({"ok": True})


@app.route("/api/recipes/<int:recipe_id>/tags/<int:tag_id>", methods=["DELETE"])
def remove_tag_from_recipe(recipe_id, tag_id):
    db = get_db()
    db.execute("DELETE FROM recipe_tags WHERE recipe_id = ? AND tag_id = ?", (recipe_id, tag_id))
    db.commit()
    return "", 204


# ---- Users ----

@app.route("/api/users")
def list_users():
    db = get_db()
    rows = db.execute("SELECT * FROM users ORDER BY name").fetchall()
    return jsonify([{"id": r["id"], "name": r["name"]} for r in rows])


@app.route("/api/users", methods=["POST"])
def create_user():
    db = get_db()
    name = request.json.get("name", "").strip()
    if not name:
        return jsonify({"error": "Name required"}), 400
    try:
        cursor = db.execute("INSERT INTO users (name) VALUES (?)", (name,))
        db.commit()
        return jsonify({"id": cursor.lastrowid, "name": name}), 201
    except sqlite3.IntegrityError:
        row = db.execute("SELECT * FROM users WHERE name = ?", (name,)).fetchone()
        return jsonify({"id": row["id"], "name": row["name"]})


@app.route("/api/users/<int:user_id>", methods=["DELETE"])
def delete_user(user_id):
    db = get_db()
    db.execute("DELETE FROM recipes WHERE user_id = ?", (user_id,))
    db.execute("DELETE FROM users WHERE id = ?", (user_id,))
    db.commit()
    return "", 204


# ---- Credentials ----

@app.route("/api/credentials")
def list_credentials():
    db = get_db()
    rows = db.execute("SELECT domain, username FROM credentials ORDER BY domain").fetchall()
    return jsonify([{"domain": r["domain"], "username": r["username"]} for r in rows])


@app.route("/api/credentials/<domain>", methods=["DELETE"])
def delete_credential(domain):
    db = get_db()
    db.execute("DELETE FROM credentials WHERE domain = ?", (domain,))
    db.commit()
    return "", 204


init_db()

if __name__ == "__main__":
    app.run(debug=True, port=5003)
