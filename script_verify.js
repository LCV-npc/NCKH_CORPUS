// script.js bổ sung verifyData
async function verifyData() {
    const modal = document.getElementById('verifyModal');
    const body = document.getElementById('verifyModalBody');
    modal.style.display = 'flex';
    body.innerHTML = '<div style="text-align:center; padding: 20px;">⏳ Đang kiểm chứng dữ liệu, vui lòng đợi...</div>';
    
    try {
        const res = await fetch(`${API_BASE}/verify-data`);
        if (!res.ok) throw new Error("Lỗi API verify-data");
        const data = await res.json();
        
        let html = `
            <div style="display:flex; justify-content:space-around; margin-bottom: 20px; background:var(--bg-card); padding:15px; border-radius:8px;">
                <div style="text-align:center;">
                    <div style="font-size:24px; font-weight:bold; color:var(--primary);">${data.total_articles_in_db}</div>
                    <div style="font-size:12px; color:var(--text-3);">Bài báo (DB)</div>
                </div>
                <div style="text-align:center;">
                    <div style="font-size:24px; font-weight:bold; color:var(--accent);">${data.total_pdfs_on_disk}</div>
                    <div style="font-size:12px; color:var(--text-3);">File PDF (Disk)</div>
                </div>
                <div style="text-align:center;">
                    <div style="font-size:24px; font-weight:bold; color:var(--success);">${data.total_txts_on_disk}</div>
                    <div style="font-size:12px; color:var(--text-3);">File TXT (Disk)</div>
                </div>
            </div>
        `;
        
        if (data.missing_pdfs_count > 0 || data.missing_txts_count > 0) {
            html += `<div style="color:var(--danger); font-weight:bold; margin-bottom:10px;">⚠️ Phát hiện ${data.missing_pdfs_count} bài báo thiếu PDF và ${data.missing_txts_count} bài báo thiếu TXT.</div>`;
            
            if (data.missing_pdfs.length > 0) {
                html += `<h4 style="margin-top:10px;">📄 Danh sách thiếu PDF (hiển thị tối đa 50)</h4>`;
                html += `<ul style="list-style:none; padding:0;">`;
                data.missing_pdfs.forEach(m => {
                    html += `<li style="padding:8px; border-bottom:1px solid var(--border); font-size:13px;">
                        <strong>[ID: ${m.id}] ${m.title}</strong> (${m.year})<br>
                        <span style="color:var(--danger);">${m.reason}</span>
                    </li>`;
                });
                html += `</ul>`;
            }
            
            if (data.missing_txts.length > 0) {
                html += `<h4 style="margin-top:20px;">📝 Danh sách thiếu TXT (hiển thị tối đa 50)</h4>`;
                html += `<ul style="list-style:none; padding:0;">`;
                data.missing_txts.forEach(m => {
                    html += `<li style="padding:8px; border-bottom:1px solid var(--border); font-size:13px;">
                        <strong>[ID: ${m.id}] ${m.title}</strong> (${m.year})<br>
                        <span style="color:var(--danger);">${m.reason}</span>
                    </li>`;
                });
                html += `</ul>`;
            }
        } else {
            html += `<div style="text-align:center; color:var(--success); font-weight:bold; padding:20px;">✅ Dữ liệu hoàn toàn khớp nhau! Không phát hiện file bị thiếu.</div>`;
        }
        
        body.innerHTML = html;
        
    } catch (err) {
        body.innerHTML = `<div style="color:var(--danger); text-align:center; padding:20px;">❌ Lỗi: ${err.message}</div>`;
    }
}
