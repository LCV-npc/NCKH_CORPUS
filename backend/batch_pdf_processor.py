import os
from datetime import datetime
from pathlib import Path
from config.language_filter import VietnameseCorpusSettings
from core.language_audit import quarantine_pdf
from core.language_validation import assess_metadata, decide_admission, select_pdf_text_for_language
from pdf_extractor import extract_from_pdf_path
from core.text_normalize import normalize_vietnamese_tones

def run_batch():
    pdf_dir = os.path.abspath("Văn_Bản_Y_Tế_PDF")
    txt_dir = os.path.abspath("Kho_Ngu_Lieu_Txt") # Output folder
    log_file = "pdf_processing_log.txt"
    
    if not os.path.exists(pdf_dir):
        print(f"Thư mục {pdf_dir} không tồn tại!")
        return

    os.makedirs(txt_dir, exist_ok=True)
    
    pdfs = []
    for root, dirs, files in os.walk(pdf_dir):
        # Candidates and quarantine are evidence stores, never corpus inputs.
        dirs[:] = [name for name in dirs if name not in {"candidates", "quarantine"}]
        for f in files:
            if f.lower().endswith(".pdf"):
                pdfs.append(os.path.join(root, f))
                
    total = len(pdfs)
    success = 0
    failed = 0
    rejected = 0
    settings = VietnameseCorpusSettings()
    
    print(f"Bắt đầu xử lý {total} file PDF từ 2020-2025...")
    
    with open(log_file, "w", encoding="utf-8") as f_log:
        f_log.write(f"--- NHẬT KÝ XỬ LÝ PDF HÀNG LOẠT ({datetime.now().strftime('%Y-%m-%d %H:%M:%S')}) ---\n")
        f_log.write(f"Tổng số file cần xử lý: {total}\n\n")
        
        for idx, pdf_path in enumerate(pdfs, 1):
            filename = os.path.basename(pdf_path)
            # Giả định đường dẫn có cấu trúc: .../năm/...
            # Lọc từ 2020 đến 2025 (nếu có thư mục năm)
            
            try:
                result = extract_from_pdf_path(pdf_path)
                if result.get("error"):
                    failed += 1
                    f_log.write(f"[{idx}/{total}] ❌ LỖI: {filename} - {result['error']}\n")
                    continue
                    
                abstract = result.get("abstract", "")
                body = result.get("body", "")
                language_text = select_pdf_text_for_language(body, result.get("full_text", ""))

                # Assess raw extracted text before tone normalization or any final write.
                decision = decide_admission(
                    assess_metadata(result.get("title", ""), abstract, settings=settings),
                    language_text,
                    settings,
                )
                if not decision.accepted:
                    relative = Path(pdf_path).resolve().relative_to(Path(pdf_dir).resolve())
                    quarantine_path = quarantine_pdf(
                        pdf_path, decision.status, relative.parts[:-1], settings
                    )
                    rejected += 1
                    f_log.write(
                        f"[{idx}/{total}] ⛔ LOẠI: {filename} -> {decision.status} "
                        f"({decision.reason}); quarantine={quarantine_path}\n"
                    )
                    continue
                
                # Làm sạch và chuẩn hóa
                abstract = normalize_vietnamese_tones(abstract)
                body = normalize_vietnamese_tones(language_text)
                title = normalize_vietnamese_tones(result.get("title", ""))
                authors = normalize_vietnamese_tones(result.get("authors", ""))
                
                out_folder = os.path.join(txt_dir, "Vietnamese", "Batch_Extracted")
                os.makedirs(out_folder, exist_ok=True)
                
                # Tên file TXT
                safe_title = filename.replace(".pdf", "")
                txt_name = f"{datetime.now().strftime('%d%m%Y')}_BATCH_{safe_title}.txt"
                txt_path = os.path.join(out_folder, txt_name)
                
                with open(txt_path, "w", encoding="utf-8-sig") as f_out:
                    f_out.write(f"TIÊU ĐỀ: {title}\n")
                    f_out.write(f"TÁC GIẢ: {authors}\n")
                    f_out.write("-" * 40 + "\n")
                    f_out.write(f"TÓM TẮT:\n{abstract}\n")
                    f_out.write("-" * 40 + "\n")
                    f_out.write(f"NỘI DUNG CHÍNH:\n{body}\n")
                    
                success += 1
                f_log.write(f"[{idx}/{total}] ✅ THÀNH CÔNG: {filename} -> Vietnamese (Độ dài: {len(body)} ký tự)\n")
                print(f"Đã xử lý {idx}/{total}: {filename}")
                
            except Exception as e:
                failed += 1
                f_log.write(f"[{idx}/{total}] ❌ LỖI NGOẠI LỆ: {filename} - {str(e)}\n")
                
        f_log.write(f"\n--- TỔNG KẾT ---\n")
        f_log.write(f"Thành công: {success}\n")
        f_log.write(f"Bị loại/quarantine: {rejected}\n")
        f_log.write(f"Thất bại: {failed}\n")
        
    print(f"\nHoàn tất! Thành công: {success}, bị loại: {rejected}, thất bại: {failed}. Đã lưu nhật ký vào {log_file}")

if __name__ == "__main__":
    run_batch()
