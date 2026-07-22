import sys
import threading
import time
from pydantic import BaseModel
sys.path.append('D:\\NCKH\\demo_7\\backend')
from core.scraper import run_scraping, scrape_status

class Req(BaseModel):
    start_year: int = 2023
    end_year: int = 2026
    target_url: str = 'https://tapchinghiencuuyhoc.vn'

db_config = {
    'host': 'localhost',
    'user': 'root',
    'password': '17092005Khang',
    'database': 'yhoc_corpus'
}

req = Req()
print("Starting scrape...")
t = threading.Thread(target=run_scraping, args=(req, db_config, 'test'))
t.daemon = True
t.start()

for i in range(15):
    time.sleep(1)
    print("Logs so far:", scrape_status["log_messages"])
