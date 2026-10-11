import os
import random
import sqlite3
import string
from pathlib import Path

from flask import Flask, abort, redirect, render_template, request, send_from_directory, url_for
from werkzeug.utils import secure_filename

app = Flask(__name__)

# Railway: attach a Volume mounted at /data and set DATA_DIR=/data.
DATA_DIR = Path(os.environ.get("DATA_DIR", "./data")).resolve()
UPLOAD_DIR = DATA_DIR / "uploads"
DB_PATH = DATA_DIR / "files.db"
DATA_DIR.mkdir(parents=True, exist_ok=True)
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)

# Set MAX_UPLOAD_MB in Railway variables if you want a different limit.
MAX_UPLOAD_MB = int(os.environ.get("MAX_UPLOAD_MB", "100"))
app.config["MAX_CONTENT_LENGTH"] = MAX_UPLOAD_MB * 1024 * 1024

# Five-character codes use Latin letters (upper/lowercase) and digits.
# This is convenient but not suitable for private/sensitive files.


def connect_db():
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    return con


def init_db():
    with connect_db() as con:
        con.execute("""
            CREATE TABLE IF NOT EXISTS files (
                code TEXT PRIMARY KEY,
                stored_name TEXT NOT NULL,
                original_name TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
        """)


init_db()


@app.get("/")
def index():
    return render_template("index.html", max_upload_mb=MAX_UPLOAD_MB)


@app.post("/upload")
def upload():
    uploaded = request.files.get("file")
    if not uploaded or not uploaded.filename:
        return render_template(
            "index.html",
            error="Выберите файл перед загрузкой.",
            max_upload_mb=MAX_UPLOAD_MB,
        ), 400

    original_name = uploaded.filename
    safe_name = secure_filename(original_name) or "file"
    # Store each upload under a random internal filename; never use user input as a path.
    suffix = Path(safe_name).suffix[:20]
    stored_name = "".join(random.choices(string.ascii_letters + string.digits, k=24)) + suffix
    destination = UPLOAD_DIR / stored_name
    uploaded.save(destination)

    try:
        with connect_db() as con:
            for _ in range(100):
                code = "".join(random.choices(string.ascii_letters + string.digits, k=5))
                try:
                    con.execute(
                        "INSERT INTO files (code, stored_name, original_name) VALUES (?, ?, ?)",
                        (code, stored_name, original_name),
                    )
                    break
                except sqlite3.IntegrityError:
                    continue
            else:
                destination.unlink(missing_ok=True)
                return "Не удалось создать короткий код. Попробуйте ещё раз.", 503
    except Exception:
        destination.unlink(missing_ok=True)
        raise

    return render_template(
        "index.html",
        code=code,
        download_url=url_for("download", code=code, _external=True),
        max_upload_mb=MAX_UPLOAD_MB,
    )


@app.get("/<code>")
def download(code):
    if len(code) != 5 or not code.isascii() or not code.isalnum():
        abort(404)

    with connect_db() as con:
        row = con.execute("SELECT * FROM files WHERE code = ?", (code,)).fetchone()

    if row is None:
        abort(404)

    path = UPLOAD_DIR / row["stored_name"]
    if not path.is_file():
        abort(404)

    return send_from_directory(
        UPLOAD_DIR,
        row["stored_name"],
        as_attachment=True,
        download_name=row["original_name"],
    )


@app.errorhandler(413)
def too_large(_error):
    return render_template(
        "index.html",
        error=f"Файл слишком большой. Максимальный размер: {MAX_UPLOAD_MB} МБ.",
        max_upload_mb=MAX_UPLOAD_MB,
    ), 413


@app.errorhandler(404)
def not_found(_error):
    return render_template(
        "index.html",
        error="Файл по этой ссылке не найден.",
        max_upload_mb=MAX_UPLOAD_MB,
    ), 404


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "5000")))
