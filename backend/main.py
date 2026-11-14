from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
import urllib3
import os
from config.env import BACKEND_ROOT, ENV_FILE, load_backend_env

load_backend_env()

DB_PASSWORD = os.getenv("DB_PASSWORD", "")
if not DB_PASSWORD:
    raise RuntimeError(
        f"Thiếu DB_PASSWORD. Hãy thêm DB_PASSWORD=<mật khẩu MySQL> vào {ENV_FILE} "
        "rồi khởi động lại backend."
    )

from api import routes as api_routes
from core.auth import ensure_auth_review_schema
from core.language_audit import ensure_language_audit_schema
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# Dictionary NER is loaded lazily on the first dictionary-backed request.
# A failed first load is reported as HTTP 503 instead of preventing the app
# from starting or silently producing zero entities.

# ── Cấu hình DB ───────────────────────────────────────────────
db_config = {
    "user":     os.getenv("DB_USER", "root"),
    "password": DB_PASSWORD,
    "host":     os.getenv("DB_HOST", "127.0.0.1"),
    "database": os.getenv("DB_NAME", "yhoc_corpus"),
    "charset":  "utf8mb4",
}

OUTPUT_FOLDER = str(BACKEND_ROOT / "Kho_Ngu_Lieu_Txt")
os.makedirs(OUTPUT_FOLDER, exist_ok=True)

# ── Khởi tạo app ──────────────────────────────────────────────
app = FastAPI(title="NER Y Học Tiếng Việt", version="2.0")

allowed_origins = [
    value.strip()
    for value in os.getenv(
        "CORS_ALLOWED_ORIGINS",
        "http://localhost:5173,http://127.0.0.1:5173",
    ).split(",")
    if value.strip()
]

app.add_middleware(
    CORSMiddleware,
    allow_origins=allowed_origins,
    allow_credentials=False,
    allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type"],
)


@app.middleware("http")
async def add_security_headers(request, call_next):
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
    if request.url.path.startswith("/api/"):
        response.headers["Cache-Control"] = "no-store"
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; base-uri 'self'; object-src 'none'; frame-ancestors 'none'; form-action 'self'; "
        "img-src 'self' data:; style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
        "font-src 'self' https://fonts.gstatic.com; script-src 'self' 'unsafe-inline'; connect-src 'self'"
    )
    return response

# ── Khởi tạo router và include vào app ────────────────────────
api_routes.init_router(db_config, OUTPUT_FOLDER)
app.include_router(api_routes.router)

# ── Migration: đảm bảo crawl_logs có cột status ──────────────
try:
    import mysql.connector
    ensure_auth_review_schema(db_config)
    ensure_language_audit_schema(db_config)
    _conn = mysql.connector.connect(**db_config)
    _cur = _conn.cursor()
    _cur.execute(
        "SELECT COUNT(*) FROM information_schema.columns "
        "WHERE table_schema=%s AND table_name='crawl_logs' AND column_name='status'",
        (db_config["database"],)
    )
    if _cur.fetchone()[0] == 0:
        _cur.execute("ALTER TABLE crawl_logs ADD COLUMN status VARCHAR(20) DEFAULT 'completed'")
        _conn.commit()
    _cur.close()
    _conn.close()
except Exception as exc:
    raise RuntimeError(
        "Không thể khởi tạo MySQL. Hãy kiểm tra DB_USER, DB_PASSWORD, DB_HOST, "
        "DB_NAME và bảo đảm dịch vụ MySQL đang chạy."
    ) from exc

# ── Entry point ───────────────────────────────────────────────
if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="127.0.0.1", port=8000, reload=False, access_log=False)
