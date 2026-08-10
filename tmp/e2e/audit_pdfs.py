import json
import re
import sys
from pathlib import Path

import pymupdf
import requests


ROOT = Path(__file__).resolve().parents[2]
BACKEND = ROOT / "backend"
sys.path.insert(0, str(BACKEND))
from pdf_extractor import extract_from_pdf_path  # noqa: E402


def inspect_pdf(path, deep=False):
    raw = path.read_bytes()
    record = {
        "path": str(path),
        "magic_pdf": raw.startswith(b"%PDF"),
        "bytes": len(raw),
        "pages": 0,
        "tables": 0,
        "images": 0,
    }
    with pymupdf.open(stream=raw, filetype="pdf") as document:
        record["pages"] = document.page_count
        if deep:
            for page in document:
                record["images"] += len(page.get_images(full=True))
                try:
                    record["tables"] += len(page.find_tables().tables)
                except Exception:
                    pass
    return record


def main():
    pdf_root = BACKEND / "Văn_Bản_Y_Tế_PDF" / "tapchiyhcd.vn"
    pdfs = sorted(pdf_root.rglob("*.pdf"))
    inspected = []
    valid = []
    invalid = []
    for path in pdfs:
        try:
            item = inspect_pdf(path)
            inspected.append(item)
            if item["magic_pdf"] and item["pages"] > 0:
                valid.append(item)
            else:
                invalid.append(item)
        except Exception as exc:
            invalid.append({"path": str(path), "error": str(exc), "bytes": path.stat().st_size})

    table_samples = []
    for item in valid[:30]:
        deep_item = inspect_pdf(Path(item["path"]), deep=True)
        if deep_item["tables"] > 0:
            table_samples.append(deep_item)
        if len(table_samples) == 3:
            break
    samples = table_samples or valid[:3]
    extraction = []
    boilerplate_re = re.compile(r"received|copyright|creative commons|downloaded from", re.I)
    fake_re = re.compile(r"_(?:table|figure|zenodo|abstract_\d+)\.txt$", re.I)
    for item in samples:
        path = Path(item["path"])
        with path.open("rb") as handle:
            response = requests.post(
                "http://127.0.0.1:8000/api/extract-pdf",
                files={"file": (path.name, handle, "application/pdf")},
                timeout=180,
            )
        payload = response.json() if response.headers.get("content-type", "").startswith("application/json") else {"raw": response.text[:500]}
        direct = extract_from_pdf_path(str(path))
        created_entries = payload.get("files_created", [])
        created = [Path(entry.get("file_path", "")) if isinstance(entry, dict) else Path(entry) for entry in created_entries]
        created = [file if file.is_absolute() else BACKEND / file for file in created]
        existing = [file for file in created if file.exists()]
        txt_texts = [file.read_text(encoding="utf-8", errors="replace") for file in existing]
        extraction.append({
            "pdf": str(path),
            "http": response.status_code,
            "pages": item["pages"],
            "tables_in_source": item["tables"],
            "images_in_source": item["images"],
            "sections": [(section["label"], len(section["content"])) for section in direct.get("sections", [])],
            "validation": payload.get("validation"),
            "files_created": [str(file) for file in created],
            "files_exist": len(existing),
            "fake_files": [str(file) for file in existing if fake_re.search(file.name)],
            "boilerplate_files": [str(existing[index]) for index, text in enumerate(txt_texts) if boilerplate_re.search(text)],
            "extract_error": direct.get("error"),
        })

    language_dirs = sorted(
        str(path.relative_to(pdf_root)) for path in pdf_root.glob("English") if path.is_dir()
    ) + sorted(str(path.relative_to(pdf_root)) for path in pdf_root.glob("Vietnamese") if path.is_dir())
    output = {
        "pdf_count": len(pdfs),
        "valid_count": len(valid),
        "invalid_count": len(invalid),
        "invalid_examples": invalid[:10],
        "expected_language_dirs_present": language_dirs,
        "year_dirs_at_site_root": sorted(path.name for path in pdf_root.iterdir() if path.is_dir()),
        "samples": extraction,
    }
    print(json.dumps(output, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
