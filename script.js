/* ============================================================
   MedNLP Studio — Main Script v4.0
   Supports: Dashboard, Crawl UI, Dictionary UI, Labeling UI
============================================================ */

const API_BASE = "http://localhost:8000/api";

// ============================================================
// STATE
// ============================================================
let pollingInterval = null;
let isScraping      = false;
let autoScroll      = true;

let currentArticlesData = [];
let currentArticleId    = null;
let currentArticleFilter = "all";
let isNerActive         = false;

let dictData = [];
let dictEditMode = false;
let editingId    = null; // index in dictData

// Color mapping for entity types
const ENTITY_COLORS = {
  DISEASE:   { cls: "disease",   label: "Bệnh lý",    icon: "🟢" },
  SYMPTOM:   { cls: "symptom",   label: "Triệu chứng",icon: "🔵" },
  TREATMENT: { cls: "treatment", label: "Điều trị",   icon: "🟣" },
  LAB_TEST:  { cls: "labtest",   label: "Xét nghiệm", icon: "🟡" },
  IMAGING:   { cls: "imaging",   label: "Hình ảnh",   icon: "🩵" },
  TRAD_MED:  { cls: "tradmed",   label: "Đông y",     icon: "🟠" },
};

function entityClass(type) {
  return (ENTITY_COLORS[type] || { cls: "other" }).cls;
}
function entityLabel(type) {
  return (ENTITY_COLORS[type] || { label: type }).label;
}

// ============================================================
// NAVIGATION
// ============================================================
const SCREENS = {
  dashboard:    { title: "Dashboard",           sub: "Tổng quan hệ thống" },
  crawl:        { title: "Thu thập dữ liệu",    sub: "Crawler & nhật ký" },
  dictionary:   { title: "Từ điển Y khoa",      sub: "Quản lý thuật ngữ y tế" },
  labeling:     { title: "Gán nhãn văn bản",    sub: "Xử lý & gán nhãn NER" },
  pdf:          { title: "Trích xuất PDF",       sub: "Phân tích bài báo y học tự động" },
  "split-pdf":  { title: "Tách nội dung PDF",    sub: "Tách bài báo y học thành file văn bản" },
};

function switchScreen(name) {
  // Deactivate all
  document.querySelectorAll(".screen").forEach(s => s.classList.remove("active"));
  document.querySelectorAll(".nav-item").forEach(n => n.classList.remove("active"));

  // Activate target
  const screenEl = document.getElementById(`screen-${name}`);
  if (screenEl) screenEl.classList.add("active");
  const navEl = document.querySelector(`[data-screen="${name}"]`);
  if (navEl) navEl.classList.add("active");

  // Update topbar
  const info = SCREENS[name] || {};
  document.getElementById("topbarTitle").textContent = info.title || name;
  document.getElementById("topbarSub").textContent   = info.sub || "";

  // Persist current screen in URL hash (survives page reload)
  location.hash = name;

  // Lazy load
  if (name === "dashboard")  loadDashboard();
  if (name === "dictionary") loadDictionary();
  if (name === "labeling")   loadData();
}


// ============================================================
// CLOCK
// ============================================================
function updateClock() {
  const now = new Date();
  const str = now.toLocaleDateString("vi-VN", { weekday: "short", day: "2-digit", month: "2-digit", year: "numeric" })
            + "  " + now.toLocaleTimeString("vi-VN");
  document.getElementById("topbarClock").textContent = str;
}
setInterval(updateClock, 1000);
updateClock();

// ============================================================
// SERVER STATUS CHECK
// ============================================================
async function checkServerStatus() {
  const dot  = document.getElementById("statusDot");
  const text = document.getElementById("statusText");
  dot.className = "status-dot checking";
  text.textContent = "Đang kết nối...";
  try {
    const res = await fetch(`${API_BASE}/status`, { signal: AbortSignal.timeout(3000) });
    if (res.ok) {
      dot.className = "status-dot online";
      text.textContent = "Máy chủ hoạt động";
    } else throw new Error();
  } catch {
    dot.className = "status-dot offline";
    text.textContent = "Mất kết nối";
  }
}

// ============================================================
// TOAST NOTIFICATIONS
// ============================================================
function showToast(msg, type = "info", duration = 3000) {
  const container = document.getElementById("toastContainer");
  const icons = { success: "✅", error: "❌", info: "ℹ️" };
  const toast = document.createElement("div");
  toast.className = `toast ${type}`;
  toast.innerHTML = `<span>${icons[type] || "ℹ️"}</span><span>${msg}</span>`;
  container.appendChild(toast);
  setTimeout(() => {
    toast.style.animation = "toastOut 0.3s ease forwards";
    setTimeout(() => toast.remove(), 300);
  }, duration);
}

// ============================================================
// DASHBOARD
// ============================================================
async function loadDashboard() {
  await Promise.all([
    loadKPIs(),
    loadRecentArticles(),
  ]);
}

async function loadKPIs() {
  try {
    const articles = await fetch(`${API_BASE}/articles`).then(r => r.json());
    
    // We can fetch concepts just for the KPI total count
    const concepts = await fetch(`${API_BASE}/top-concepts?limit=100`).then(r => r.json());

    const labeled = articles.filter(a => a.highlighted_html).length;

    animateCount("kpi-articles", articles.length);
    animateCount("kpi-labeled",  labeled);
    animateCount("kpi-concepts", concepts.length > 0
      ? concepts.reduce((s, c) => s + (c.frequency || 0), 0) : 0);

    document.getElementById("kpi-articles-sub").textContent =
      `${labeled} đã gán nhãn / ${articles.length} tổng`;

    // Dictionary size — load separately
    loadDictSize();

    currentArticlesData = articles;
  } catch(e) {
    console.warn("KPI load error:", e);
  }
}

async function loadDictSize() {
  try {
    // Try to get dict from API or local
    const dict = await loadDictData();
    animateCount("kpi-dict", dict.length);
    document.getElementById("kpi-dict-sub").textContent = `${dict.length} thuật ngữ y tế`;
  } catch {
    document.getElementById("kpi-dict").textContent = "—";
  }
}

function animateCount(id, target) {
  const el = document.getElementById(id);
  if (!el) return;
  const duration = 800;
  const start = Date.now();
  const startVal = 0;
  function step() {
    const p = Math.min((Date.now() - start) / duration, 1);
    const eased = 1 - Math.pow(1 - p, 3);
    el.textContent = Math.round(startVal + (target - startVal) * eased).toLocaleString("vi-VN");
    if (p < 1) requestAnimationFrame(step);
  }
  requestAnimationFrame(step);
}

let currentConceptFilter = "";
let allConceptsCache = [];

async function loadTopConcepts(label) {
  currentConceptFilter = label;
  const container = document.getElementById("barChartContainer");
  try {
    const url = label
      ? `${API_BASE}/top-concepts?limit=10&label=${encodeURIComponent(label)}`
      : `${API_BASE}/top-concepts?limit=10`;
    const data = await fetch(url).then(r => r.json());
    allConceptsCache = data;
    renderBarChart(data);
  } catch {
    container.innerHTML = `<div class="chart-empty">Không thể tải dữ liệu</div>`;
  }
}

function filterConcepts(label) {
  document.querySelectorAll(".filter-btn").forEach(b => b.classList.remove("active"));
  const map = { "": "filter-all", "DISEASE": "filter-disease", "SYMPTOM": "filter-symptom" };
  document.getElementById(map[label] || "filter-all")?.classList.add("active");
  loadTopConcepts(label);
}

function renderBarChart(data) {
  const container = document.getElementById("barChartContainer");
  if (!data || data.length === 0) {
    container.innerHTML = `<div class="chart-empty">Không có dữ liệu</div>`;
    return;
  }
  const max = Math.max(...data.map(d => d.frequency || 0));
  container.innerHTML = data.map((d, i) => {
    const pct = max > 0 ? (d.frequency / max) * 100 : 0;
    const col = ENTITY_COLORS[d.concept_type] || { cls: "other" };
    return `
      <div class="bar-row" style="animation-delay:${i * 0.05}s">
        <div class="bar-label" title="${d.concept_name}">${d.concept_name}</div>
        <div class="bar-track">
          <div class="bar-fill" style="width:${pct}%; background: ${barColor(d.concept_type)};"></div>
        </div>
        <div class="bar-count">${d.frequency}</div>
      </div>`;
  }).join("");
}

function barColor(type) {
  const colors = {
    DISEASE:   "linear-gradient(90deg,#4ade80,#86efac)",
    SYMPTOM:   "linear-gradient(90deg,#818cf8,#a5b4fc)",
    TREATMENT: "linear-gradient(90deg,#c084fc,#d8b4fe)",
    LAB_TEST:  "linear-gradient(90deg,#fbbf24,#fde68a)",
    IMAGING:   "linear-gradient(90deg,#2dd4bf,#99f6e4)",
    TRAD_MED:  "linear-gradient(90deg,#fb923c,#fdba74)",
  };
  return colors[type] || "linear-gradient(90deg, var(--primary), var(--accent))";
}

function buildDonut(concepts) {
  const canvas = document.getElementById("donutCanvas");
  if (!canvas) return;
  const ctx = canvas.getContext("2d");
  const W = 200, H = 200, R = 80, r = 50;

  // Count by type
  const counts = {};
  concepts.forEach(c => {
    counts[c.concept_type] = (counts[c.concept_type] || 0) + (c.frequency || 1);
  });

  const palette = {
    DISEASE:   "#4ade80",
    SYMPTOM:   "#818cf8",
    TREATMENT: "#c084fc",
    LAB_TEST:  "#fbbf24",
    IMAGING:   "#2dd4bf",
    TRAD_MED:  "#fb923c",
  };

  const entries = Object.entries(counts);
  const total   = entries.reduce((s, [,v]) => s + v, 0);

  ctx.clearRect(0, 0, W, H);

  if (total === 0) {
    ctx.fillStyle = "#1e2840";
    ctx.beginPath(); ctx.arc(W/2, H/2, R, 0, Math.PI*2); ctx.fill();
    ctx.clearRect(W/2-r, H/2-r, r*2, r*2);
    ctx.beginPath(); ctx.arc(W/2, H/2, r, 0, Math.PI*2); ctx.fill();
    return;
  }

  let startAngle = -Math.PI / 2;
  entries.forEach(([type, count]) => {
    const angle = (count / total) * Math.PI * 2;
    ctx.beginPath();
    ctx.moveTo(W/2, H/2);
    ctx.arc(W/2, H/2, R, startAngle, startAngle + angle);
    ctx.closePath();
    ctx.fillStyle = palette[type] || "#64748b";
    ctx.fill();
    startAngle += angle;
  });

  // Center hole
  ctx.beginPath(); ctx.arc(W/2, H/2, r, 0, Math.PI*2);
  ctx.fillStyle = "#141b2d"; ctx.fill();

  // Legend
  const legend = document.getElementById("donutLegend");
  legend.innerHTML = entries.slice(0, 6).map(([type, count]) => {
    const pct = ((count / total) * 100).toFixed(1);
    return `
      <div class="legend-row">
        <div class="legend-dot" style="background:${palette[type] || "#64748b"}"></div>
        <span class="legend-row-label">${entityLabel(type)}</span>
        <span class="legend-row-pct">${pct}%</span>
      </div>`;
  }).join("");
}

async function loadRecentArticles() {
  const el = document.getElementById("recentArticles");
  try {
    const data = await fetch(`${API_BASE}/articles`).then(r => r.json());
    const recent = data.slice(0, 8);
    if (recent.length === 0) {
      el.innerHTML = `<div class="list-placeholder">Chưa có bài báo nào</div>`;
      return;
    }
    el.innerHTML = recent.map((a, i) => {
      const isLabeled = !!a.highlighted_html;
      return `
        <div class="recent-item" onclick="switchScreen('labeling'); setTimeout(()=>selectArticle(${a.id}),300)">
          <div class="recent-num">${i + 1}</div>
          <div class="recent-info">
            <div class="recent-title">${a.title || "Không có tiêu đề"}</div>
            <div class="recent-meta">${a.publication_year || "—"} · ${(a.authors || "").split(",")[0]}</div>
          </div>
          <span class="recent-badge ${isLabeled ? "badge-labeled" : "badge-unlabeled"}">
            ${isLabeled ? "Đã gán nhãn" : "Chưa xử lý"}
          </span>
        </div>`;
    }).join("");
  } catch {
    el.innerHTML = `<div class="list-placeholder">Không thể tải dữ liệu</div>`;
  }
}

// ============================================================
// CRAWL UI
// ============================================================
function setUrl(url) {
  document.getElementById("targetUrl").value = url;
}

function clearLog() {
  document.getElementById("logTerminal").innerHTML =
    `<div class="log-placeholder"><span class="log-cursor">█</span> Log đã được xóa.</div>`;
}

function toggleAutoScroll() {
  autoScroll = !autoScroll;
  document.getElementById("autoScrollBtn").textContent = autoScroll ? "↓ Tự cuộn" : "↑ Tắt cuộn";
}

function appendLog(msg, type = "") {
  const term = document.getElementById("logTerminal");
  const placeholder = term.querySelector(".log-placeholder");
  if (placeholder) placeholder.remove();

  const now  = new Date().toLocaleTimeString("vi-VN");
  const line = document.createElement("div");
  line.className = `log-line ${type}`;

  // Color based on content heuristics
  let lineType = type;
  if (!lineType) {
    if (/lỗi|error|fail|exception/i.test(msg)) lineType = "error";
    else if (/tải file pdf thành công|thành công|saved|lưu|✅|done/i.test(msg))  lineType = "success";
    else if (/cảnh báo|warn|skip|bỏ qua|trùng/i.test(msg)) lineType = "warning";
    else if (/bắt đầu|start|kết nối|connect|đang/i.test(msg)) lineType = "info";
  }
  line.className = `log-line ${lineType}`;
  line.innerHTML = `<span class="log-ts">[${now}]</span> ${msg}`;
  term.appendChild(line);

  if (autoScroll) term.scrollTop = term.scrollHeight;
}

function updateProgressUI(s) {
  const saved   = s.success || 0;
  const dup     = s.duplicates || 0;
  const skip    = s.skipped || 0;
  const done    = saved + dup + skip;
  // Giả sử mỗi năm có khoảng 100 bài để chạy phần trăm tương đối, hoặc đơn giản để 100% nếu không biết tổng
  const total   = s.total_urls || done; 
  const pct     = total > 0 ? Math.min(100, Math.round((done / total) * 100)) : 0;

  document.getElementById("prog-total").textContent = s.total_urls || total || 0;
  document.getElementById("prog-saved").textContent = saved;
  document.getElementById("prog-dup").textContent   = dup;
  document.getElementById("prog-skip").textContent  = skip;
  document.getElementById("progFill").style.width   = `${pct}%`;
  document.getElementById("progPct").textContent    = `${pct}%`;

  if (s.current_url) {
    document.getElementById("progressMsg").textContent = `Đang xử lý: ${s.current_url}`;
    appendLog(`🌐 Đang cào: ${s.current_url}`);
  }

  const badge = document.getElementById("crawlStateBadge");
  if (s.running) {
    badge.textContent = "⚙️ Đang chạy";
    badge.className   = "crawl-state-badge running";
    document.getElementById("crawl-badge").style.display = "";
  } else if (s.error) {
    badge.textContent = "❌ Lỗi";
    badge.className   = "crawl-state-badge error";
    document.getElementById("crawl-badge").style.display = "none";
  } else if (s.done) {
    badge.textContent = "✅ Hoàn thành";
    badge.className   = "crawl-state-badge done";
    document.getElementById("crawl-badge").style.display = "none";
  } else {
    badge.textContent = "Chờ";
    badge.className   = "crawl-state-badge";
    document.getElementById("crawl-badge").style.display = "none";
  }
}

function _showSummaryBox(summary) {
  const box = document.getElementById("summaryBox");
  document.getElementById("sum-success").textContent = summary.success    || 0;
  document.getElementById("sum-dup").textContent     = summary.duplicates || 0;
  document.getElementById("sum-skip").textContent    = summary.skipped    || 0;
  box.style.display = "block";
}

async function startScraping() {
  const btn  = document.getElementById("btnScrape");
  const stop = document.getElementById("btnStopScrape");
  const url   = document.getElementById("targetUrl").value.trim();
  const sYear = parseInt(document.getElementById("startYear").value);
  const eYear = parseInt(document.getElementById("endYear").value);

  if (!url)          { showToast("Vui lòng nhập URL tên miền đích!", "error"); return; }
  if (isNaN(sYear) || isNaN(eYear)) { showToast("Vui lòng nhập năm hợp lệ!", "error"); return; }
  if (sYear > eYear) { showToast("Năm bắt đầu phải ≤ năm kết thúc!", "error"); return; }

  isScraping = true;
  btn.disabled = true;
  btn.innerHTML = `<span class="btn-icon">⚙️</span> Đang khởi động...`;
  stop.style.display = "";
  document.getElementById("summaryBox").style.display = "none";

  appendLog(`🚀 Bắt đầu thu thập: ${url} (${sYear}–${eYear})`, "info");
  updateProgressUI({ running: true });

  try {
    const res = await fetch(`${API_BASE}/scrape`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ target_url: url, start_year: sYear, end_year: eYear })
    });

    if (res.status === 409) {
      const err = await res.json();
      appendLog(`⚠️ ${err.detail}`, "warning");
      showToast(err.detail, "error");
      resetScrapeBtn();
      return;
    }

    if (!res.ok) throw new Error("Lỗi máy chủ");

    appendLog("✅ Lệnh đã được gửi, crawler đang chạy nền...", "success");

    let lastLogCount = 0;
    if (pollingInterval) clearInterval(pollingInterval);
    pollingInterval = setInterval(async () => {
      try {
        const s = await fetch(`${API_BASE}/status`).then(r => r.json());
        updateProgressUI(s);

        if (s.log_messages && s.log_messages.length > lastLogCount) {
          const newLogs = s.log_messages.slice(lastLogCount);
          newLogs.forEach(msg => appendLog(msg));
          lastLogCount = s.log_messages.length;
        }

        if (!s.running) {
          clearInterval(pollingInterval); pollingInterval = null;
          if (s.error) {
            appendLog(`❌ Lỗi: ${s.error}`, "error");
            showToast("Crawler gặp lỗi!", "error");
          } else if (s.done) {
            appendLog("✅ Thu thập hoàn thành! Đang làm mới dữ liệu...", "success");
            showToast("Thu thập dữ liệu hoàn thành!", "success");
            _showSummaryBox(s.summary || {});
            // Làm mới số liệu trên thanh tiến độ và nhật ký — KHÔNG chuyển màn hình
            updateProgressUI(s);
            // Chờ 1.5s rồi reload lại dữ liệu bài báo ngay tại trang crawl
            setTimeout(() => {
              loadData();         // cập nhật danh sách bài báo
              loadDashboard();    // cập nhật thống kê nền
              appendLog(`📊 Tổng kết: ✅ ${s.summary?.success||0} lưu thành công · 🔁 ${s.summary?.duplicates||0} trùng · ⏭ ${s.summary?.skipped||0} bỏ qua`, "info");
            }, 1500);
          }
          resetScrapeBtn();
        }
      } catch {}
    }, 2000);

  } catch (err) {
    appendLog(`❌ Lỗi kết nối: ${err.message}`, "error");
    showToast("Không thể kết nối đến máy chủ", "error");
    resetScrapeBtn();
  }
}

async function stopScraping() {
  clearInterval(pollingInterval); pollingInterval = null;
  appendLog("⏹ Đang yêu cầu máy chủ dừng thu thập...", "warning");
  try {
    await fetch(`${API_BASE}/scrape/stop`, { method: "POST" });
  } catch (e) {}
  resetScrapeBtn();
}

function resetScrapeBtn() {
  isScraping = false;
  const btn  = document.getElementById("btnScrape");
  const stop = document.getElementById("btnStopScrape");
  btn.disabled = false;
  btn.innerHTML = `<span class="btn-icon">🚀</span> Bắt đầu thu thập`;
  stop.style.display = "none";
}

// ============================================================
// DICTIONARY UI
// ============================================================
async function loadDictData() {
  try {
    const res = await fetch(`${API_BASE}/dictionary`);
    if (res.ok) return await res.json();
  } catch {}
  // Fallback — return cached
  return dictData;
}

async function loadDictionary() {
  const tbody = document.getElementById("dictTableBody");
  tbody.innerHTML = `<tr><td colspan="5" class="table-empty">Đang tải...</td></tr>`;
  try {
    const data = await loadDictData();
    dictData = Array.isArray(data) ? data : [];
    renderDictTable(dictData);
  } catch {
    tbody.innerHTML = `<tr><td colspan="5" class="table-empty">Không thể tải từ điển</td></tr>`;
  }
}

function renderDictTable(data) {
  const tbody = document.getElementById("dictTableBody");
  document.getElementById("dictCount").textContent = `${data.length} thuật ngữ`;

  if (data.length === 0) {
    tbody.innerHTML = `<tr><td colspan="5" class="table-empty">Chưa có thuật ngữ nào. Hãy thêm thuật ngữ đầu tiên!</td></tr>`;
    return;
  }

  tbody.innerHTML = data.map((item, idx) => {
    const type = item.type || "DISEASE";
    const col  = ENTITY_COLORS[type] || { cls: "other", label: type };
    return `
      <tr>
        <td style="color:var(--text-3);font-weight:600">${idx + 1}</td>
        <td>
          <div style="font-weight:600;margin-bottom:2px">${item.term || "—"}</div>
          ${item.note ? `<div style="font-size:11px;color:var(--text-3)">${item.note}</div>` : ""}
        </td>
        <td><span class="type-badge type-${col.cls}">${col.icon || ""} ${col.label}</span></td>
        <td style="color:var(--accent);font-family:monospace;font-size:12px">${item.code || "—"}</td>
        <td>
          <button class="dict-action-btn edit" onclick="editDictEntry(${idx})" title="Chỉnh sửa">✏️</button>
          <button class="dict-action-btn delete" onclick="deleteDictEntry(${idx})" title="Xóa">🗑️</button>
        </td>
      </tr>`;
  }).join("");
}

function filterDict() {
  const q    = (document.getElementById("dictSearch").value || "").toLowerCase();
  const type = document.getElementById("dictFilterType").value;
  const filtered = dictData.filter(item => {
    const matchQ    = !q || (item.term || "").toLowerCase().includes(q) || (item.code || "").toLowerCase().includes(q);
    const matchType = !type || item.type === type;
    return matchQ && matchType;
  });
  renderDictTable(filtered);
}

function editDictEntry(idx) {
  const item = dictData[idx];
  if (!item) return;
  document.getElementById("dictTerm").value  = item.term  || "";
  document.getElementById("dictType").value  = item.type  || "DISEASE";
  document.getElementById("dictCode").value  = item.code  || "";
  document.getElementById("dictNote").value  = item.note  || "";
  document.getElementById("dictEditId").value = idx;
  document.getElementById("dictFormTitle").textContent = "Chỉnh sửa thuật ngữ";
  document.getElementById("dictCancelBtn").style.display = "";
  document.getElementById("dictSaveBtn").innerHTML = "<span>✏️</span> Cập nhật thuật ngữ";
  dictEditMode = true;
  editingId    = idx;
  document.getElementById("dictTerm").focus();
}

function cancelDictEdit() {
  dictEditMode = false;
  editingId    = null;
  document.getElementById("dictTerm").value  = "";
  document.getElementById("dictType").value  = "DISEASE";
  document.getElementById("dictCode").value  = "";
  document.getElementById("dictNote").value  = "";
  document.getElementById("dictEditId").value = "";
  document.getElementById("dictFormTitle").textContent = "Thêm thuật ngữ";
  document.getElementById("dictCancelBtn").style.display = "none";
  document.getElementById("dictSaveBtn").innerHTML = "<span>💾</span> Lưu thuật ngữ";
}

async function saveDictEntry() {
  const term = document.getElementById("dictTerm").value.trim();
  const type = document.getElementById("dictType").value;
  const code = document.getElementById("dictCode").value.trim();
  const note = document.getElementById("dictNote").value.trim();

  if (!term) { showToast("Vui lòng nhập thuật ngữ!", "error"); return; }

  const entry = { term, type, code, note };

  if (dictEditMode && editingId !== null) {
    dictData[editingId] = entry;
    showToast(`Đã cập nhật: "${term}"`, "success");
  } else {
    const exists = dictData.some(d => d.term.toLowerCase() === term.toLowerCase());
    if (exists) { showToast("Thuật ngữ đã tồn tại trong từ điển!", "error"); return; }
    dictData.unshift(entry);
    showToast(`Đã thêm: "${term}"`, "success");
  }

  // Try to save to server
  try {
    await fetch(`${API_BASE}/save-to-dictionary`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ matched_concepts: [{ name: term, type, code }] })
    });
  } catch {}

  cancelDictEdit();
  renderDictTable(dictData);
}

function deleteDictEntry(idx) {
  const item = dictData[idx];
  if (!item) return;
  if (!confirm(`Xóa thuật ngữ "${item.term}"?`)) return;
  dictData.splice(idx, 1);
  renderDictTable(dictData);
  showToast(`Đã xóa: "${item.term}"`, "info");
}

function exportDict() {
  const blob = new Blob([JSON.stringify(dictData, null, 2)], { type: "application/json" });
  const a    = document.createElement("a");
  a.href     = URL.createObjectURL(blob);
  a.download = "tu_dien_y_khoa.json";
  a.click();
  showToast("Đã xuất từ điển!", "success");
}

function importDict(event) {
  const file = event.target.files[0];
  if (!file) return;
  const reader = new FileReader();
  reader.onload = e => {
    try {
      const imported = JSON.parse(e.target.result);
      if (!Array.isArray(imported)) throw new Error("Dữ liệu không đúng định dạng");
      let added = 0;
      imported.forEach(item => {
        if (item.term && !dictData.some(d => d.term.toLowerCase() === item.term.toLowerCase())) {
          dictData.push(item);
          added++;
        }
      });
      renderDictTable(dictData);
      showToast(`Đã nhập ${added} thuật ngữ mới!`, "success");
    } catch(err) {
      showToast("Lỗi đọc file: " + err.message, "error");
    }
  };
  reader.readAsText(file, "utf-8");
  event.target.value = "";
}

// ============================================================
// LABELING UI
// ============================================================
let articleFilter = "all";

function setArticleFilter(filter, btn) {
  articleFilter = filter;
  document.querySelectorAll(".filter-pill").forEach(b => b.classList.remove("active"));
  btn.classList.add("active");
  renderArticleList();
}

async function loadData() {
  const query = (document.getElementById("searchInput")?.value || "").trim();
  const scroll = document.getElementById("articleListScroll");
  if (scroll) scroll.innerHTML = `<div class="list-placeholder">Đang tải...</div>`;
  try {
    const url  = query
      ? `${API_BASE}/articles?q=${encodeURIComponent(query)}`
      : `${API_BASE}/articles`;
    const data = await fetch(url).then(r => r.json());
    currentArticlesData = Array.isArray(data) ? data : [];
    renderArticleList();
  } catch {
    if (scroll) scroll.innerHTML = `<div class="list-placeholder">Lỗi kết nối máy chủ</div>`;
  }
}

function renderArticleList() {
  const scroll = document.getElementById("articleListScroll");
  const info   = document.getElementById("pageInfo");
  if (!scroll) return;

  let data = currentArticlesData;
  if (articleFilter === "labeled")   data = data.filter(a =>  a.highlighted_html);
  if (articleFilter === "unlabeled") data = data.filter(a => !a.highlighted_html);

  if (info) info.textContent = `${data.length} bài báo`;

  if (data.length === 0) {
    scroll.innerHTML = `<div class="list-placeholder">Không tìm thấy bài báo</div>`;
    return;
  }

  scroll.innerHTML = data.map(a => {
    const isLabeled = !!a.highlighted_html;
    const isActive  = a.id === currentArticleId;
    return `
      <div class="article-list-item ${isActive ? "active" : ""}" onclick="selectArticle(${a.id})">
        <div class="ali-title">${a.title || "Không có tiêu đề"}</div>
        <div class="ali-authors">${a.authors || "Không rõ tác giả"}</div>
      </div>`;
  }).join("");
}

function selectArticle(id) {
  currentArticleId = id;
  isNerActive      = false;
  renderArticleList(); // update active state

  const article = currentArticlesData.find(a => a.id === id);
  if (!article) return;

  // Show viewer
  document.getElementById("viewerPlaceholder").style.display = "none";
  document.getElementById("viewerContent").style.display     = "flex";
  document.getElementById("viewerContent").style.flexDirection = "column";

  // Meta
  document.getElementById("articleTitle").textContent   = article.title   || "Không có tiêu đề";
  document.getElementById("articleYear").textContent    = article.publication_year || "—";
  document.getElementById("articleAuthors").textContent = article.authors  || "Không rõ tác giả";

  // Text
  const textBody = document.getElementById("textBody");
  textBody.innerHTML = article.abstract
    ? article.abstract.replace(/\n/g, "<br>")
    : "<em style='color:var(--text-3)'>Không có nội dung tóm tắt</em>";

  // NER state
  const btnNer   = document.getElementById("btnNer");
  const btnSave  = document.getElementById("btnSaveNer");
  const nerStatus = document.getElementById("nerStatus");
  const entPanel  = document.getElementById("entitiesPanel");
  const legend    = document.getElementById("entityLegend");

  btnNer.className   = "btn-ner";
  btnNer.innerHTML   = "<span>🏷️</span> Bật gán nhãn";
  btnSave.style.display = "none";
  nerStatus.textContent = "";
  entPanel.style.display = "none";
  legend.style.display   = "none";

  // If already analyzed — show
  if (article.highlighted_html) {
    isNerActive = true;
    btnNer.className = "btn-ner active";
    btnNer.innerHTML = "<span>🏷️</span> Ẩn gán nhãn";
    textBody.innerHTML = article.highlighted_html;
    nerStatus.textContent = "✅ Đã gán nhãn";
    legend.style.display  = "flex";
    btnSave.style.display = "";
    if (article.matched_concepts?.length) {
      renderEntities(article.matched_concepts);
      entPanel.style.display = "";
    }
  }
}

async function toggleNer() {
  const article = currentArticlesData.find(a => a.id === currentArticleId);
  if (!article) return;

  const btnNer    = document.getElementById("btnNer");
  const nerStatus = document.getElementById("nerStatus");

  if (isNerActive) {
    // Turn off — show raw text
    isNerActive = false;
    btnNer.className = "btn-ner";
    btnNer.innerHTML = "<span>🏷️</span> Bật gán nhãn";
    document.getElementById("textBody").innerHTML =
      article.abstract ? article.abstract.replace(/\n/g, "<br>") : "";
    document.getElementById("btnNer").classList.remove("active");
  document.getElementById("btnNer").innerHTML = `<span>🏷️</span> Bật gán nhãn`;
  document.getElementById("btnSaveNer").style.display = "none";
  document.getElementById("btnAiNer").style.display = "inline-flex"; // Hiển thị nút AI
  document.getElementById("aiPanel").style.display = "none"; // Ẩn panel AI cũ
    nerStatus.textContent = "";
    return;
  }

  // Call NER
  btnNer.disabled  = true;
  btnNer.className = "btn-ner loading";
  btnNer.innerHTML = "<span>⚙️</span> Đang phân tích...";
  nerStatus.textContent = "Đang gán nhãn...";

  try {
    const res = await fetch(`${API_BASE}/highlight-text`, {
      method:  "POST",
      headers: { "Content-Type": "application/json" },
      body:    JSON.stringify({
        text:                article.abstract || "",
        threshold:           100,
        enable_tone_restore: true,
        enable_noun_phrase:  true,
      })
    });
    const result = await res.json();

    if (res.ok) {
      article.highlighted_html = result.highlighted_html;
      article.matched_concepts = result.matched_concepts || [];
      article.ner_note         = result.note || "";
      isNerActive = true;

      document.getElementById("textBody").innerHTML = result.highlighted_html;
      document.getElementById("entityLegend").style.display = "flex";
      btnNer.className = "btn-ner active";
      btnNer.innerHTML = "<span>🏷️</span> Ẩn gán nhãn";
      document.getElementById("btnSaveNer").style.display = "";

      const count = (result.matched_concepts || []).length;
      nerStatus.textContent = count > 0
        ? `✅ Tìm thấy ${count} thực thể`
        : "ℹ️ Không tìm thấy thực thể y tế";

      // Preprocessing log
      const preprocLog = result.preprocessing_log;
      _renderPreprocLog(preprocLog, 'preprocPanel', 'preprocBody');

      if (count > 0) {
        renderEntities(result.matched_concepts);
        document.getElementById("entitiesPanel").style.display = "";
      } else {
        document.getElementById("entitiesPanel").style.display = "none";
      }
    } else {
      showToast("Lỗi phân tích NER: " + (result.detail || "Không xác định"), "error");
      nerStatus.textContent = "";
    }
  } catch (err) {
    showToast("Lỗi kết nối máy chủ NER", "error");
    nerStatus.textContent = "";
    console.error(err);
  } finally {
    btnNer.disabled = false;
  }
}


function renderEntities(concepts) {
  const grid = document.getElementById("entitiesGrid");
  if (!concepts || concepts.length === 0) {
    grid.innerHTML = `<span style="color:var(--text-3);font-size:12px">Không có thực thể</span>`;
    return;
  }
  const seen = new Set();
  grid.innerHTML = concepts
    .filter(c => { const k = (c.name||"").toLowerCase(); if (seen.has(k)) return false; seen.add(k); return true; })
    .map(c => {
      const cls = entityClass(c.type || "DISEASE");
      const matchedBy = c.matched_by || 'exact';
      const badgeCls  = matchedBy === 'noun_phrase' ? 'matched-noun-phrase' : 'matched-exact';
      const badgeLbl  = matchedBy === 'noun_phrase' ? 'NP' : 'EM';
      const codeHtml  = c.code ? `<span class="entity-code">${c.code}</span>` : '';
      return `<div class="entity-tag ${cls}" title="${c.code || ''}">
        ${c.name}
        ${codeHtml}
        <span class="matched-badge ${badgeCls}">${badgeLbl}</span>
      </div>`;
    }).join("");
}


async function saveCurrentHighlight() {
  const article = currentArticlesData.find(a => a.id === currentArticleId);
  if (!article || !article.highlighted_html) {
    showToast("Chưa có dữ liệu để lưu!", "error");
    return;
  }
  const btn = document.getElementById("btnSaveNer");
  btn.disabled = true;
  btn.innerHTML = "<span>⏳</span> Đang lưu...";

  try {
    const res = await fetch(`${API_BASE}/save-highlight`, {
      method:  "POST",
      headers: { "Content-Type": "application/json" },
      body:    JSON.stringify({
        article_id:       article.id,
        highlighted_html: article.highlighted_html,
        matched_concepts: article.matched_concepts || []
      })
    });
    const result = await res.json();
    if (res.ok) {
      showToast(`Đã lưu ${result.concepts_saved} thực thể!`, "success");
      renderArticleList(); // refresh labels
    } else {
      showToast("Lỗi: " + (result.detail || "Không xác định"), "error");
    }
  } catch {
    showToast("Lỗi kết nối khi lưu", "error");
  } finally {
    btn.disabled = false;
    btn.innerHTML = "<span>💾</span> Lưu kết quả";
  }
}

async function saveToDict() {
  const article = currentArticlesData.find(a => a.id === currentArticleId);
  if (!article?.matched_concepts?.length) {
    showToast("Không có thực thể để lưu vào từ điển!", "error");
    return;
  }
  try {
    const res = await fetch(`${API_BASE}/save-to-dictionary`, {
      method:  "POST",
      headers: { "Content-Type": "application/json" },
      body:    JSON.stringify({ matched_concepts: article.matched_concepts })
    });
    const result = await res.json();
    if (res.ok) {
      document.getElementById("saveDictStatus").textContent =
        `✅ Đã thêm ${result.added?.length || 0} thuật ngữ`;
      showToast(`Đã thêm ${result.added?.length || 0} thuật ngữ vào từ điển!`, "success");
    }
  } catch {
    showToast("Lỗi khi lưu vào từ điển", "error");
  }
}

// ============================================================
// INIT
// ============================================================
document.addEventListener("DOMContentLoaded", () => {
  checkServerStatus();
  setInterval(checkServerStatus, 30000);

  // Init year fields
  const currentYear = new Date().getFullYear();
  const sy = document.getElementById("startYear");
  const ey = document.getElementById("endYear");
  if (sy) { sy.value = 2020; sy.max = currentYear; }
  if (ey) { ey.value = currentYear; ey.max = currentYear; }

  // Restore saved screen from URL hash, or default to dashboard
  const savedScreen = location.hash.replace('#', '');
  if (savedScreen && (SCREENS[savedScreen] || document.getElementById(`screen-${savedScreen}`))) {
    switchScreen(savedScreen);
  } else {
    switchScreen('dashboard');
  }
});

// ============================================================
// CRAWL LOGS
// ============================================================
async function loadCrawlLogs() {
  const tbody = document.getElementById("logsTableBody");
  try {
    const res = await fetch(`${API_BASE}/crawl-logs`);
    const logs = await res.json();
    if (!logs || logs.length === 0) {
      tbody.innerHTML = `<tr><td colspan="6" class="table-empty">Chưa có dữ liệu nhật ký thu thập.</td></tr>`;
      return;
    }

    tbody.innerHTML = logs.map(log => {
      const date = new Date(log.crawl_date).toLocaleDateString("vi-VN");
      return `
        <tr>
          <td>${date}</td>
          <td class="text-ellipsis" title="${log.target_url}">${log.target_url || "—"}</td>
          <td style="text-align:center;">${log.total_urls}</td>
          <td style="text-align:center; color:var(--success); font-weight:500;">${log.success_count}</td>
          <td style="text-align:center; color:var(--warning); font-weight:500;">${log.duplicate_count}</td>
          <td style="text-align:center; color:var(--danger); font-weight:500;">${log.error_count}</td>
        </tr>
      `;
    }).join("");
  } catch (e) {
    tbody.innerHTML = `<tr><td colspan="6" class="table-empty">Lỗi khi tải nhật ký: ${e.message}</td></tr>`;
  }
}

// Add loadCrawlLogs call to switchScreen if screen == "logs"
const oldSwitchScreen = switchScreen;
switchScreen = function(screenId) {
  oldSwitchScreen(screenId);
  if (screenId === "logs") {
    loadCrawlLogs();
  }
};

// ============================================================
// SPLIT PDF
// ============================================================

let _splitPdfFile = null;

function pdfDragOver(e) {
  e.preventDefault();
  document.getElementById('splitPdfDropzone').classList.add('dragging');
}
function pdfDragLeave(e) {
  e.preventDefault();
  document.getElementById('splitPdfDropzone').classList.remove('dragging');
}
function splitPdfDrop(e) {
  e.preventDefault();
  pdfDragLeave(e);
  const files = e.dataTransfer?.files;
  if (files && files[0]) _setSplitPdfFile(files[0]);
}
function splitPdfSelected(e) {
  const f = e.target.files[0];
  if (f) _setSplitPdfFile(f);
}

function _setSplitPdfFile(file) {
  if (!file.name.toLowerCase().endsWith('.pdf')) {
    showToast('Chỉ hỗ trợ file PDF!', 'error'); return;
  }
  _splitPdfFile = file;
  document.getElementById('splitPdfName').textContent = file.name;
  document.getElementById('splitPdfSize').textContent = _fmtSize(file.size);
  document.getElementById('splitPdfInfo').style.display = 'flex';
  document.getElementById('btnSplitPdf').disabled = false;
  document.getElementById('splitPdfResultPanel').style.display = 'none';
  document.getElementById('splitPdfEmpty').style.display = 'block';
}

function clearSplitPdf() {
  _splitPdfFile = null;
  document.getElementById('splitPdfInput').value = '';
  document.getElementById('splitPdfInfo').style.display = 'none';
  document.getElementById('btnSplitPdf').disabled = true;
  document.getElementById('splitPdfResultPanel').style.display = 'none';
  document.getElementById('splitPdfEmpty').style.display = 'block';
}

function _fmtSize(bytes) {
  if (bytes < 1024) return bytes + ' B';
  if (bytes < 1048576) return (bytes / 1024).toFixed(1) + ' KB';
  return (bytes / 1048576).toFixed(1) + ' MB';
}

async function executeSplitPdf() {
  if (!_splitPdfFile) { showToast('Chưa chọn file PDF', 'warning'); return; }

  const btn = document.getElementById('btnSplitPdf');
  const oldHtml = btn.innerHTML;
  btn.disabled = true;
  btn.innerHTML = '<span>⏳ Đang tách file...</span>';

  try {
    const formData = new FormData();
    formData.append('file', _splitPdfFile);

    const res = await fetch(`${API_BASE}/extract-pdf`, {
      method: 'POST',
      body: formData,
    });

    if (!res.ok) {
      const err = await res.json().catch(() => ({}));
      throw new Error(err.detail || `HTTP ${res.status}`);
    }

    const data = await res.json();
    showToast('Tách PDF thành công!', 'success');
    
    // Show results
    document.getElementById('splitPdfEmpty').style.display = 'none';
    document.getElementById('splitPdfResultPanel').style.display = 'block';
    document.getElementById('splitPdfMessage').textContent = data.message;
    
    const filesList = document.getElementById('splitPdfFilesList');
    filesList.innerHTML = data.files_created.map(f => `<li>📄 ${f}</li>`).join("");

  } catch (e) {
    showToast(`Lỗi: ${e.message}`, 'error');
  } finally {
    btn.disabled = false;
    btn.innerHTML = oldHtml;
  }
}

// Render preprocessing log panel
function _renderPreprocLog(log, panelId, bodyId) {
  const panel = document.getElementById(panelId);
  const body  = document.getElementById(bodyId);
  if (!panel || !body) return;

  if (!log || (!log.tone_restore?.length && !log.np_count && !log.exact_count)) {
    panel.style.display = 'none';
    return;
  }

  let html = '';

  // Tone restore changes
  if (log.tone_restore && log.tone_restore.length > 0) {
    html += '<div style="margin-bottom:6px;"><strong>Sửa dấu tiếng Việt:</strong></div>';
    log.tone_restore.forEach(c => {
      html += `<div class="preproc-item">
        <span class="preproc-before">${c.original}</span>
        <span class="preproc-arrow">→</span>
        <span class="preproc-after">${c.corrected}</span>
      </div>`;
    });
  } else {
    html += '<div style="color:var(--text-3);font-size:11px;">✓ Văn bản đã có đầy đủ dấu</div>';
  }

  // Stats
  if (log.exact_count !== undefined || log.np_count !== undefined) {
    html += `<div class="preproc-stat" style="margin-top:8px;">`;
    if (log.exact_count !== undefined) {
      html += `<span class="preproc-stat-item">
        <span class="preproc-stat-dot" style="background:#10b981;"></span>
        <span>${log.exact_count} exact match</span>
      </span>`;
    }
    if (log.np_count !== undefined) {
      html += `<span class="preproc-stat-item">
        <span class="preproc-stat-dot" style="background:#6366f1;"></span>
        <span>${log.np_count} noun phrase</span>
      </span>`;
    }
    html += '</div>';
  }

  body.innerHTML = html;
  panel.style.display = '';
}

// Render entities grid with matched_by badge
function _renderEntitiesGrid(concepts, gridId) {
  const grid = document.getElementById(gridId);
  if (!grid) return;

  if (!concepts || concepts.length === 0) {
    grid.innerHTML = '<div style="color:var(--text-3);font-size:13px;padding:8px 0;">Không tìm thấy thực thể y tế</div>';
    return;
  }

  const TYPE_MAP = {
    'Bệnh Lý':                'disease',
    'Triệu Chứng':            'symptom',
    'Điều Trị':               'treatment',
    'Xét Nghiệm/Cận Lâm Sàng': 'labtest',
    'Chẩn Đoán Hình Ảnh':    'imaging',
    'Đông Y / YHCT':          'tradmed',
    'Tiến Trình Bệnh Lý':    'symptom',
    'DISEASE':                'disease',
    'SYMPTOM':                'symptom',
    'TREATMENT':              'treatment',
  };

  grid.innerHTML = concepts.map(c => {
    const cls = TYPE_MAP[c.type] || 'other';
    const matchedBy = c.matched_by || 'exact';
    const badgeCls  = matchedBy === 'noun_phrase' ? 'matched-noun-phrase' : 'matched-exact';
    const badgeLbl  = matchedBy === 'noun_phrase' ? 'NP'                  : 'EM';
    const codeHtml  = c.code ? `<span class="entity-code">${c.code}</span>` : '';
    return `<div class="entity-tag ${cls}">
      ${c.name}
      ${codeHtml}
      <span class="matched-badge ${badgeCls}">${badgeLbl}</span>
    </div>`;
  }).join('');
}
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
