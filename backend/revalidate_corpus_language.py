"""Re-check existing PDFs before they are treated as Vietnamese corpus data.

Default mode is a non-destructive dry run.  Add ``--apply`` to move rejected
PDFs into the configured quarantine directory.  A JSON report is always
written, so the operation is auditable and reversible from the source copy.
"""

from __future__ import annotations

import argparse
import json
import os
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv

from config.language_filter import VietnameseCorpusSettings
from core.language_audit import LanguageAuditRepository, ensure_language_audit_schema, quarantine_pdf
from core.language_validation import AdmissionDecision, assess_metadata, decide_admission
from pdf_extractor import extract_from_pdf_path


load_dotenv()


def _db_config() -> dict | None:
    password = os.getenv("DB_PASSWORD")
    if not password:
        return None
    return {
        "user": os.getenv("DB_USER", "root"),
        "password": password,
        "host": os.getenv("DB_HOST", "127.0.0.1"),
        "database": os.getenv("DB_NAME", "yhoc_corpus"),
        "charset": "utf8mb4",
    }


def revalidate(root: Path, apply: bool = False, record_database: bool = True) -> dict:
    settings = VietnameseCorpusSettings()
    root = root.resolve()
    audit = None
    if record_database and (db_config := _db_config()):
        ensure_language_audit_schema(db_config)
        audit = LanguageAuditRepository(db_config)

    report: dict = {
        "generatedAt": datetime.now(timezone.utc).isoformat(),
        "root": str(root), "mode": "apply" if apply else "dry-run",
        "databaseAudit": bool(audit), "summary": {"accepted": 0, "rejected": 0, "errors": 0},
        "items": [],
    }
    for path in root.rglob("*.pdf"):
        relative = path.resolve().relative_to(root)
        if "candidates" in relative.parts or "quarantine" in relative.parts:
            continue
        result = extract_from_pdf_path(str(path))
        metadata = assess_metadata(result.get("title", ""), result.get("abstract", ""), settings=settings)
        if result.get("error"):
            decision = AdmissionDecision("REJECTED_NO_TEXT", "PDF_EXTRACTION_FAILED", metadata, None)
        else:
            decision = decide_admission(metadata, result.get("body") or result.get("full_text", ""), settings)
        item = {
            "file": str(relative), "title": result.get("title", ""),
            "status": decision.status, "reason": decision.reason,
            "metadata": metadata.as_dict(), "pdf": decision.pdf.as_dict() if decision.pdf else None,
        }
        try:
            stored_path = str(path)
            if not decision.accepted and apply:
                moved = quarantine_pdf(path, decision.status, relative.parts[:-1], settings)
                stored_path = str(moved)
                item["quarantinePath"] = stored_path
            if audit:
                audit.record(
                    source_url=f"file://{relative.as_posix()}", title=result.get("title", ""),
                    pdf_url=None, decision=decision, file_path=stored_path,
                )
            report["summary"]["accepted" if decision.accepted else "rejected"] += 1
        except Exception as exc:
            item["auditError"] = str(exc)
            report["summary"]["errors"] += 1
        report["items"].append(item)
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description="Revalidate language of existing medical PDFs.")
    parser.add_argument("--root", default="Văn_Bản_Y_Tế_PDF", help="PDF root (default: backend/Văn_Bản_Y_Tế_PDF)")
    parser.add_argument("--apply", action="store_true", help="Move rejected files into quarantine.")
    parser.add_argument("--no-db", action="store_true", help="Do not write audit records to MySQL.")
    parser.add_argument("--report", default="", help="Optional explicit JSON report path.")
    args = parser.parse_args()
    report = revalidate(Path(args.root), apply=args.apply, record_database=not args.no_db)
    settings = VietnameseCorpusSettings()
    report_path = Path(args.report) if args.report else (
        settings.audit_report_dir / f"language_revalidation_{datetime.now():%Y%m%d_%H%M%S}.json"
    )
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"summary": report["summary"], "report": str(report_path)}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
