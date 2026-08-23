"""Runtime configuration for crawlers.

Environment variables keep operational crawler settings out of source code while
providing safe defaults for local development.
"""

from dataclasses import dataclass
import os
from pathlib import Path


BACKEND_ROOT = Path(__file__).resolve().parents[1]


@dataclass(frozen=True)
class TamanhCrawlerSettings:
    base_url: str = os.getenv("CRAWLER_TAMANH_BASE_URL", "https://tamanhhospital.vn/")
    max_retries: int = int(os.getenv("CRAWLER_TAMANH_MAX_RETRIES", "3"))
    request_delay_ms: int = int(os.getenv("CRAWLER_TAMANH_REQUEST_DELAY_MS", "1000"))
    timeout_ms: int = int(os.getenv("CRAWLER_TAMANH_TIMEOUT_MS", "15000"))
    output_dir: Path = Path(
        os.getenv("CRAWLER_TAMANH_OUTPUT_DIR", str(BACKEND_ROOT / "Kho_Ngu_Lieu_Txt" / "tamanh"))
    )
