# Quy trinh van hanh thi nghiem NER 100 bai

## 1. Chuan bi

1. Bao dam MySQL dang chay va bang `articles` co cac bai `id=1..100`.
2. Cau hinh `DB_USER`, `DB_PASSWORD`, `DB_HOST`, `DB_NAME`, `GEMINI_API_KEY` va `GEMINI_MODEL` trong `backend/.env`.
3. Cai dependency backend: `pip install -r backend/requirements.txt`.
4. Cai frontend: vao thu muc `frontend`, chay `npm install`.

Khong can Vertex endpoint cho ba cau hinh hien tai. Hai cau hinh fine-tuned chi duoc mo sau khi co endpoint.

## 2. Khoi dong he thong

Backend:

```powershell
cd backend
python main.py
```

Frontend (terminal khac):

```powershell
cd frontend
npm run dev
```

Dang nhap bang tai khoan Admin, mo man hinh **Danh gia AI**, nhap ghi chu thay doi va bam **Chay 100 bai**. Admin co the dung, tiep tuc va promote checkpoint. Expert va Reviewer chi xem duoc ket qua.

## 3. Chay bang CLI

```powershell
python ner_finetuning/scripts/run_db_experiment.py --limit 2 --note "Smoke 2"
python ner_finetuning/scripts/run_db_experiment.py --limit 10 --note "Pilot 10"
python ner_finetuning/scripts/run_db_experiment.py --limit 100 --note "Full 100" --promote
```

Tiep tuc mot run bi dung:

```powershell
python ner_finetuning/scripts/run_db_experiment.py --resume RUN_ID --promote
```

Ke thua ket qua hop le tu run cha (chi ke thua khi checksum input va prompt/mapping trung khop):

```powershell
python ner_finetuning/scripts/run_db_experiment.py --limit 100 --parent RUN_ID --note "Repair" --promote
```

## 4. Artifact

Moi checkpoint bat bien nam tai `text/runs/<run_id>/`. Ban duoc promote nam tai:

- `text/current_ai/test.txt`
- `text/vietbioner/test.txt`
- `text/vimedner/test.txt`

Moi thu muc con co `predictions.jsonl`, `article_index.jsonl`, `conversion_issues.jsonl` va 100 cap BRAT trong `articles/`. Snapshot input, prompt, few-shot, mapping, dictionary, model va Git commit nam trong checkpoint.

`test.txt` la prediction, khong phai gold. Khong bao cao Precision/Recall/F1 tu ba file nay khi chua co gold doc lap.

## 5. Tao gold doc lap

1. Expert lay van ban goc qua `GET /api/ner-gold/articles/{article_id}` va gan nhan ma khong xem prediction.
2. Expert luu draft qua `POST /api/ner-gold/articles/{article_id}`.
3. Reviewer xem danh sach tai `GET /api/ner-gold/review`, giai quyet bat dong va finalize qua `POST /api/ner-gold/review/{article_id}/finalize`.
4. Gold duoc xuat tai `text/gold/test.txt`, `text/gold/gold.jsonl` va `text/gold/manifest.json`.

Khi gold du 100 bai, run hoan tat tiep theo tu dong tao `metrics.json`, `metrics.csv`, `report.md`, `errors.jsonl`, `errors.csv` va bao cao tung bai.

## 6. Nguyen tac nghien cuu

- Chi sua prompt, mapping, regex va logic tren dev.
- Moi thay doi tao run moi, co ghi chu va `parent_run_id`.
- Chon ban tot nhat theo exact macro-F1 tren dev, sau do Admin promote thu cong.
- Khoa model, prompt, code va dictionary truoc khi chay test gold cuoi.
- Test gold cuoi chi chay mot lan de lay so lieu bao cao.

