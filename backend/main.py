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
from core.ner_dict import load_ner_dictionary
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# ── Khởi tạo từ điển NER ngay khi app start ───────────────────
load_ner_dictionary()

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

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"], allow_credentials=True,
    allow_methods=["*"], allow_headers=["*"],
)

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
