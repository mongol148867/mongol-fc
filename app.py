import os
import random
import sqlite3
import string
from datetime import datetime, timedelta
from pathlib import Path

from flask import Flask, abort, render_template, request, send_from_directory, url_for
from werkzeug.utils import secure_filename

app = Flask(__name__)
DATA_DIR = Path(os.environ.get("DATA_DIR", "./data")).resolve()
UPLOAD_DIR = DATA_DIR / "uploads"
DB_PATH = DATA_DIR / "files.db"
DATA_DIR.mkdir(parents=True, exist_ok=True)
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
MAX_UPLOAD_MB = int(os.environ.get("MAX_UPLOAD_MB", "100"))
app.config["MAX_CONTENT_LENGTH"] = MAX_UPLOAD_MB * 1024 * 1024
LANGUAGES = {"ru", "en", "bg"}


def get_lang():
    lang = request.args.get("lang", "ru").lower()
    return lang if lang in LANGUAGES else "ru"


def connect_db():
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    return con


def init_db():
    with connect_db() as con:
        con.execute("""CREATE TABLE IF NOT EXISTS files (
            code TEXT PRIMARY KEY,
            stored_name TEXT NOT NULL,
            original_name TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            expires_at TEXT
        )""")
        # Migrate databases created by earlier versions.
        columns = {row[1] for row in con.execute("PRAGMA table_info(files)").fetchall()}
        if "expires_at" not in columns:
            con.execute("ALTER TABLE files ADD COLUMN expires_at TEXT")
            # Existing uploads get a 30-day grace period after this update.
            con.execute("UPDATE files SET expires_at = ? WHERE expires_at IS NULL",
                        ((datetime.utcnow() + timedelta(days=30)).isoformat(timespec="seconds"),))


def cleanup_expired():
    now = datetime.utcnow().isoformat(timespec="seconds")
    with connect_db() as con:
        rows = con.execute("SELECT stored_name FROM files WHERE expires_at IS NOT NULL AND expires_at <= ?", (now,)).fetchall()
        con.execute("DELETE FROM files WHERE expires_at IS NOT NULL AND expires_at <= ?", (now,))
    for row in rows:
        try:
            (UPLOAD_DIR / row["stored_name"]).unlink(missing_ok=True)
        except OSError:
            pass


init_db()


@app.get("/")
def index():
    cleanup_expired()
    return render_template("index.html", max_upload_mb=MAX_UPLOAD_MB, lang=get_lang())


@app.post("/upload")
def upload():
    cleanup_expired()
    lang = request.form.get("lang", "ru").lower()
    if lang not in LANGUAGES:
        lang = "ru"
    uploaded = request.files.get("file")
    if not uploaded or not uploaded.filename:
        return render_template("index.html", error_key="choose_file", max_upload_mb=MAX_UPLOAD_MB, lang=lang), 400

    try:
        expires_days = int(request.form.get("expires_days", "7"))
    except (TypeError, ValueError):
        expires_days = 7
    expires_days = max(1, min(30, expires_days))

    original_name = uploaded.filename
    safe_name = secure_filename(original_name) or "file"
    suffix = Path(safe_name).suffix[:20]
    stored_name = "".join(random.choices(string.ascii_letters + string.digits, k=24)) + suffix
    destination = UPLOAD_DIR / stored_name
    uploaded.save(destination)
    expires_at = (datetime.utcnow() + timedelta(days=expires_days)).isoformat(timespec="seconds")

    try:
        with connect_db() as con:
            for _ in range(100):
                code = "".join(random.choices(string.ascii_letters + string.digits, k=5))
                try:
                    con.execute("INSERT INTO files (code, stored_name, original_name, expires_at) VALUES (?, ?, ?, ?)",
                                (code, stored_name, original_name, expires_at))
                    break
                except sqlite3.IntegrityError:
                    continue
            else:
                destination.unlink(missing_ok=True)
                return "Could not create a short code. Please try again.", 503
    except Exception:
        destination.unlink(missing_ok=True)
        raise

    page_url = url_for("download_page", code=code, lang=lang, _external=True)
    return render_template("index.html", code=code, download_url=page_url,
                           expires_days=expires_days, max_upload_mb=MAX_UPLOAD_MB, lang=lang)


@app.get("/<code>")
def download_page(code):
    if len(code) != 5 or not code.isascii() or not code.isalnum():
        abort(404)
    cleanup_expired()
    with connect_db() as con:
        row = con.execute("SELECT * FROM files WHERE code = ?", (code,)).fetchone()
    if row is None or not (UPLOAD_DIR / row["stored_name"]).is_file():
        return render_template("download.html", missing=True, lang=get_lang()), 404
    try:
        expires = datetime.fromisoformat(row["expires_at"]) if row["expires_at"] else None
    except ValueError:
        expires = None
    return render_template("download.html", file=row, code=code, lang=get_lang(),
                           expires_at=expires.strftime("%Y-%m-%d %H:%M UTC") if expires else "")


@app.get("/get/<code>")
def get_file(code):
    if len(code) != 5 or not code.isascii() or not code.isalnum():
        abort(404)
    cleanup_expired()
    with connect_db() as con:
        row = con.execute("SELECT * FROM files WHERE code = ?", (code,)).fetchone()
    if row is None:
        return render_template("download.html", missing=True, lang=get_lang()), 404
    path = UPLOAD_DIR / row["stored_name"]
    if not path.is_file():
        abort(404)
    return send_from_directory(UPLOAD_DIR, row["stored_name"], as_attachment=True, download_name=row["original_name"])


@app.errorhandler(413)
def too_large(_error):
    return render_template("index.html", error_key="too_large", max_upload_mb=MAX_UPLOAD_MB, lang=get_lang()), 413


@app.errorhandler(404)
def not_found(_error):
    return render_template("download.html", missing=True, lang=get_lang()), 404


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "5000")))
