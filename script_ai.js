// script.js bổ sung runAiNer
let currentAiConcepts = [];

async function runAiNer() {
    const btn = document.getElementById("btnAiNer");
    const aiPanel = document.getElementById("aiPanel");
    const aiBody = document.getElementById("aiComparisonBody");
    const article = currentArticlesData.find(a => a.id === currentArticleId);
    
    if (!article || !article.abstract) {
        showToast("Không có văn bản để phân tích", "error");
        return;
    }
    
    btn.disabled = true;
    btn.innerHTML = `<span>⏳</span> Đang phân tích...`;
    aiPanel.style.display = "block";
    aiBody.innerHTML = `<div style="text-align:center; padding:10px;">Đang gửi dữ liệu cho AI (Gemini)...</div>`;
    
    try {
        const res = await fetch(`${API_BASE}/ai-ner`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
                text: article.abstract,
                threshold: 100,
                enable_tone_restore: true,
                enable_noun_phrase: true
            })
        });
        
        if (!res.ok) {
            const err = await res.json();
            throw new Error(err.detail || "Lỗi API AI NER");
        }
        
        const data = await res.json();
        currentAiConcepts = data.entities || [];
        
        renderAiComparison();
        
    } catch (err) {
        aiBody.innerHTML = `<div style="color:var(--danger); padding:10px;">❌ Lỗi: ${err.message}</div>`;
        showToast("Lỗi phân tích AI", "error");
    } finally {
        btn.disabled = false;
        btn.innerHTML = `<span>✨</span> Gán nhãn AI`;
    }
}

function renderAiComparison() {
    const aiBody = document.getElementById("aiComparisonBody");
    
    // So sánh currentAiConcepts với currentConcepts (đã phân tích từ từ điển)
    const dictTerms = currentConcepts.map(c => (c.name || "").toLowerCase().trim());
    
    let html = `
        <table class="data-table" style="margin-top:10px;">
            <thead>
                <tr>
                    <th>Thực thể AI tìm thấy</th>
                    <th>Loại</th>
                    <th>Mã ICD (AI)</th>
                    <th>Trạng thái</th>
                    <th>Thao tác</th>
                </tr>
            </thead>
            <tbody>
    `;
    
    if (currentAiConcepts.length === 0) {
        html += `<tr><td colspan="5" style="text-align:center;">AI không tìm thấy thực thể nào</td></tr>`;
    } else {
        currentAiConcepts.forEach((aiEnt, idx) => {
            const term = (aiEnt.name || "").toLowerCase().trim();
            const isNew = !dictTerms.includes(term);
            const statusHtml = isNew 
                ? `<span style="color:var(--danger); font-weight:bold;">Mới (Chưa có trong từ điển)</span>`
                : `<span style="color:var(--success);">Đã có</span>`;
            
            const btnHtml = isNew
                ? `<button class="btn-outline-sm" onclick="addAiTermToDict(${idx})" style="padding:2px 8px; font-size:11px;">Thêm</button>`
                : ``;
                
            const typeLabel = entityLabel(aiEnt.type) || aiEnt.type;
                
            html += `
                <tr>
                    <td style="font-weight:bold;">${aiEnt.name}</td>
                    <td>${typeLabel}</td>
                    <td>${aiEnt.code || ""}</td>
                    <td>${statusHtml}</td>
                    <td>${btnHtml}</td>
                </tr>
            `;
        });
    }
    
    html += `</tbody></table>`;
    
    // Thêm nút cập nhật hàng loạt nếu có từ mới
    const hasNew = currentAiConcepts.some(c => !dictTerms.includes((c.name || "").toLowerCase().trim()));
    if (hasNew) {
        html += `<div style="margin-top:10px; text-align:right;">
            <button class="btn-primary-sm" onclick="addAllNewAiTerms()">Thêm tất cả từ mới</button>
        </div>`;
    }
    
    aiBody.innerHTML = html;
}

async function addAiTermToDict(idx) {
    const ent = currentAiConcepts[idx];
    if (!ent) return;
    
    const payload = {
        matched_concepts: [{
            name: ent.name,
            type: ent.type,
            code: ent.code || ""
        }]
    };
    
    try {
        const res = await fetch(`${API_BASE}/save-to-dictionary`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(payload)
        });
        
        if (!res.ok) throw new Error("Lỗi lưu từ điển");
        
        showToast(`Đã thêm: ${ent.name}`, "success");
        currentConcepts.push({name: ent.name, type: ent.type, code: ent.code});
        renderAiComparison(); // Cập nhật lại UI so sánh
        
        // Reload lại danh sách từ điển ở background
        loadDictionary();
    } catch (e) {
        showToast("Lỗi thêm thuật ngữ", "error");
    }
}

async function addAllNewAiTerms() {
    const dictTerms = currentConcepts.map(c => (c.name || "").toLowerCase().trim());
    const newEnts = currentAiConcepts.filter(c => !dictTerms.includes((c.name || "").toLowerCase().trim()));
    
    if (newEnts.length === 0) return;
    
    const payload = {
        matched_concepts: newEnts.map(ent => ({
            name: ent.name,
            type: ent.type,
            code: ent.code || ""
        }))
    };
    
    try {
        const res = await fetch(`${API_BASE}/save-to-dictionary`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(payload)
        });
        
        if (!res.ok) throw new Error("Lỗi lưu từ điển");
        
        showToast(`Đã thêm ${newEnts.length} thuật ngữ mới`, "success");
        newEnts.forEach(ent => currentConcepts.push({name: ent.name, type: ent.type, code: ent.code}));
        renderAiComparison();
        
        loadDictionary();
    } catch (e) {
        showToast("Lỗi thêm hàng loạt", "error");
    }
}
