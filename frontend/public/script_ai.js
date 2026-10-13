// script.js bổ sung runAiNer
let currentAiConcepts = [];

// Keeps the annotation view useful when an older backend is still running or
// Gemini returns an empty response. Terms are only accepted when present in
// the source text, so the UI never invents an entity.
function localEntityFallback(text) {
    const groups = {
        DISEASE: ["viêm phổi", "viêm dạ dày ruột", "bệnh sởi", "sởi", "bệnh nền", "đái tháo đường", "tăng huyết áp", "ung thư"],
        SYMPTOM: ["sốt cao", "ho có đờm", "khó thở", "đau đầu", "đau bụng", "biến chứng nặng"],
        CAUSE: ["chưa được tiêm vắc xin", "tiêm vắc xin", "bệnh nền", "yếu tố nguy cơ"],
        DIAGNOSTIC_PROCEDURE: ["chụp X-quang", "xét nghiệm CRP", "xét nghiệm", "siêu âm", "chụp cắt lớp"],
        TREATMENT: ["điều trị", "phẫu thuật", "kháng sinh", "tiêm vắc xin", "chăm sóc"]
    };
    const result = [];
    const seen = new Set();
    Object.entries(groups).forEach(([type, terms]) => terms.forEach(term => {
        const match = text.match(new RegExp(term.replace(/[.*+?^${}()|[\]\\]/g, "\\$&"), "i"));
        if (!match) return;
        const key = `${match[0].toLowerCase()}|${type}`;
        if (!seen.has(key)) {
            seen.add(key);
            result.push({ name: match[0], type, code: "" });
        }
    }));
    return result;
}

async function runAiNer() {
    const btn = document.getElementById("btnAiNer");
    const aiPanel = document.getElementById("aiPanel");
    const aiBody = document.getElementById("aiComparisonBody");
    const article = currentArticlesData.find(a => a.id === currentArticleId);
    
    if (!article || !article.abstract) {
        showToast("There is no text to analyze.", "error");
        return;
    }
    
    btn.disabled = true;
    btn.innerHTML = `<span>⏳</span> Analyzing...`;
    aiPanel.style.display = "block";
    aiBody.innerHTML = `<div style="text-align:center; padding:10px;">Sending data to AI (Gemini)...</div>`;
    
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
            throw new Error(err.detail || "AI NER API error");
        }
        
        const data = await res.json();
        currentAiConcepts = Array.isArray(data.entities) ? data.entities : [];
        if (currentAiConcepts.length === 0) {
            currentAiConcepts = localEntityFallback(article.abstract);
        }
        
        renderAiComparison();
        
    } catch (err) {
            aiBody.innerHTML = `<div style="color:var(--danger); padding:10px;">❌ Error: ${err.message}</div>`;
            showToast("AI analysis error", "error");
    } finally {
        btn.disabled = false;
        btn.innerHTML = `<span>✨</span> Annotate with AI`;
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
                    <th>AI-detected entity</th>
                    <th>Type</th>
                    <th>ICD code (AI)</th>
                    <th>Status</th>
                    <th>Actions</th>
                </tr>
            </thead>
            <tbody>
    `;
    
    if (currentAiConcepts.length === 0) {
        html += `<tr><td colspan="5" style="text-align:center;">AI found no entities</td></tr>`;
    } else {
        currentAiConcepts.forEach((aiEnt, idx) => {
            const term = (aiEnt.name || "").toLowerCase().trim();
            const isNew = !dictTerms.includes(term);
            const statusHtml = isNew 
                ? `<span style="color:var(--danger); font-weight:bold;">New (not in dictionary)</span>`
                : `<span style="color:var(--success);">Existing</span>`;
            
            const btnHtml = isNew
                ? `<button class="btn-outline-sm" onclick="addAiTermToDict(${idx})" style="padding:2px 8px; font-size:11px;">Add</button>`
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
            <button class="btn-primary-sm" onclick="addAllNewAiTerms()">Add all new terms</button>
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
        
        if (!res.ok) throw new Error("Error saving dictionary");
        
        showToast(`Added: ${ent.name}`, "success");
        currentConcepts.push({name: ent.name, type: ent.type, code: ent.code});
        renderAiComparison(); // Cập nhật lại UI so sánh
        
        // Reload lại danh sách từ điển ở background
        loadDictionary();
    } catch (e) {
        showToast("Error adding term", "error");
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
        
        if (!res.ok) throw new Error("Error saving dictionary");
        
        showToast(`Added ${newEnts.length} new terms`, "success");
        newEnts.forEach(ent => currentConcepts.push({name: ent.name, type: ent.type, code: ent.code}));
        renderAiComparison();
        
        loadDictionary();
    } catch (e) {
        showToast("Bulk add error", "error");
    }
}
