from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
import urllib3
import os
from dotenv import load_dotenv

from api import routes as api_routes
from core.ner_dict import load_ner_dictionary

load_dotenv()
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# ── Khởi tạo từ điển NER ngay khi app start ───────────────────
load_ner_dictionary()

# ── Cấu hình DB ───────────────────────────────────────────────
db_config = {
    "user":     "root",
    "password": os.getenv("DB_PASSWORD", "17092005Khang"),
    "host":     "localhost",
    "database": "yhoc_corpus", # fixed typo yhoc_corpuss -> yhoc_corpus
    "charset":  "utf8mb4",
}

OUTPUT_FOLDER = "Kho_Ngu_Lieu_Txt"
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

# ── Entry point ───────────────────────────────────────────────
if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="127.0.0.1", port=8000, reload=False, access_log=False)
