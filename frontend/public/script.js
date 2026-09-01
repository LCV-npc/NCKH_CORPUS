/* ============================================================
   MedNLP Studio — Main Script v4.0
   Supports: Dashboard, Crawl UI, Dictionary UI, Labeling UI
============================================================ */

const API_BASE = "/api";

// Authentication is enforced by the FastAPI backend.  The browser only keeps
// an opaque, short-lived session token; passwords and password hashes are
// never exposed to this script.
let authToken = sessionStorage.getItem("mednlp.sessionToken") || "";
let currentAuthUser = null;
const nativeFetch = window.fetch.bind(window);

function installAuthenticatedFetch() {
  if (window.__mednlpAuthenticatedFetchInstalled) return;
  window.__mednlpAuthenticatedFetchInstalled = true;

  window.fetch = (input, init = {}) => {
    const url = typeof input === "string" ? input : (input && input.url) || "";
    const needsToken = authToken && (url.startsWith("/api/") || url.includes("/api/"))
      && !url.includes("/api/auth/login")
      && !url.includes("/api/auth/register");
    if (!needsToken) return nativeFetch(input, init);

    const headers = new Headers(init.headers || (input instanceof Request ? input.headers : undefined));
    headers.set("Authorization", `Bearer ${authToken}`);
    return nativeFetch(input, { ...init, headers });
  };
}

function setApplicationVisible(visible) {
  const sidebar = document.getElementById("sidebar");
  const main = document.querySelector(".main-content");
  const authShell = document.getElementById("authShell");
  document.body.classList.toggle("auth-mode", !visible);
  if (sidebar) sidebar.hidden = !visible;
  if (main) main.hidden = !visible;
  if (authShell) authShell.hidden = visible;
}

function showAuthRoute(mode = "login") {
  const login = mode !== "register";
  setApplicationVisible(false);
  document.getElementById("loginFormWrap").hidden = !login;
  document.getElementById("registerFormWrap").hidden = login;
  if (location.pathname !== `/${login ? "login" : "register"}`) {
    history.pushState({}, "", login ? "/login" : "/register");
  }
  return false;
}

function showAuthError(targetId, message = "") {
  const target = document.getElementById(targetId);
  if (target) target.textContent = message;
}

async function loadAuthenticatedUser() {
  if (!authToken) return null;
  const response = await nativeFetch(`${API_BASE}/auth/me`, {
    headers: { Authorization: `Bearer ${authToken}` },
  });
  if (!response.ok) {
    sessionStorage.removeItem("mednlp.sessionToken");
    authToken = "";
    return null;
  }
  const payload = await response.json();
  return payload.user || null;
}

function renderAuthenticatedIdentity(user) {
  const identity = document.getElementById("userIdentity");
  document.getElementById("currentUserName").textContent = user.name;
  document.getElementById("currentUserRole").textContent = user.role === "admin" ? "Admin" : "Expert";
  identity.hidden = false;
}

async function logoutCurrentUser() {
  try {
    if (authToken) await nativeFetch(`${API_BASE}/auth/logout`, {
      method: "POST", headers: { Authorization: `Bearer ${authToken}` },
    });
  } finally {
    sessionStorage.removeItem("mednlp.sessionToken");
    authToken = "";
    currentAuthUser = null;
    history.replaceState({}, "", "/login");
    showAuthRoute("login");
  }
}

function bindAuthForms() {
  document.getElementById("loginForm").addEventListener("submit", async (event) => {
    event.preventDefault();
    showAuthError("loginError");
    const email = document.getElementById("loginEmail").value.trim();
    const password = document.getElementById("loginPassword").value;
    try {
      const response = await nativeFetch(`${API_BASE}/auth/login`, {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ email, password }),
      });
      const payload = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(payload.detail || "Unable to login.");
      authToken = payload.token;
      if (!authToken) throw new Error("Máy chủ không trả về phiên đăng nhập hợp lệ.");
      sessionStorage.setItem("mednlp.sessionToken", authToken);
      const target = payload.user.role === "expert" ? "/expert/dashboard" : "/";
      location.assign(target);
    } catch (error) {
      showAuthError("loginError", error.message);
    }
  });

  document.getElementById("registerForm").addEventListener("submit", async (event) => {
    event.preventDefault();
    showAuthError("registerError");
    const name = document.getElementById("registerName").value.trim();
    const email = document.getElementById("registerEmail").value.trim();
    const password = document.getElementById("registerPassword").value;
    const confirmPassword = document.getElementById("registerConfirmPassword").value;
    if (password !== confirmPassword) {
      showAuthError("registerError", "Confirm Password must match Password.");
      return;
    }
    try {
      const response = await nativeFetch(`${API_BASE}/auth/register`, {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ fullName: name, email, password, confirmPassword }),
      });
      const payload = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(payload.detail || "Unable to register.");
      document.getElementById("registerSuccess").textContent = payload.message;
      setTimeout(() => showAuthRoute("login"), 700);
    } catch (error) {
      showAuthError("registerError", error.message);
    }
  });
}

// ============================================================
// STATE
// ============================================================
let pollingInterval = null;
let isScraping      = false;
let autoScroll      = true;
let tamanhJobId = null;
let tamanhPollingInterval = null;
let tamanhLogCount = 0;

let currentArticlesData = [];
let currentArticleId    = null;
let currentArticleFilter = "all";
let isNerActive         = false;

let aiLabelArticlesData = [];
let currentAiArticleId  = null;
let currentAiFilter     = "all";
let currentAiLabelResult = null;
let currentAiLabelResultArticleId = null;

// Color mapping for entity types
const ENTITY_COLORS = {
  DISEASE:   { cls: "disease",   label: "Bệnh lý",    icon: "🟢" },
  SYMPTOM:   { cls: "symptom",   label: "Triệu chứng",icon: "🔵" },
  TREATMENT: { cls: "treatment", label: "Điều trị",   icon: "🟣" },
  LAB_TEST:  { cls: "labtest",   label: "Xét nghiệm", icon: "🟡" },
  IMAGING:   { cls: "imaging",   label: "Hình ảnh",   icon: "🩵" },
  TRAD_MED:  { cls: "tradmed",   label: "Đông y",     icon: "🟠" },
};

function escapeHtml(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

function buildEntityTooltip(item, fallbackCategory) {
  const category = item.dictionary_type || fallbackCategory;
  let tooltip = category;
  if (item.code) tooltip += ` | Mã: ${item.code}`;
  if (item.label_vn && item.label_vn.toLowerCase() !== item.term.toLowerCase()) {
    tooltip += ` | ${item.label_vn}`;
  }
  return tooltip;
}

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
  "tamanh-crawler": { title: "Crawler hỏi đáp y khoa", sub: "Tâm Anh Hospital — Q&A công khai" },
  labeling:     { title: "Gán nhãn văn bản",    sub: "Xử lý & gán nhãn NER" },
  "ai-label":   { title: "AI Gán nhãn",         sub: "Gán nhãn thực thể y khoa bằng AI" },
  logs:         { title: "Nhật ký thu thập",    sub: "Lịch sử thu thập" },
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
  if (name === "labeling")   loadData();
  if (name === "ai-label")   loadAiLabelData();
  if (name === "logs")       loadCrawlLogs();
}

// ============================================================
// TÂM ANH MEDICAL Q&A CRAWLER
// ============================================================
function setTamanhSource() {
  const source = document.getElementById("tamanhSource").value;
  if (source) document.getElementById("tamanhSourceUrl").value = source;
}

function appendTamanhLog(message) {
  const terminal = document.getElementById("tamanhLogTerminal");
  if (!terminal) return;
  const placeholder = terminal.querySelector(".log-placeholder");
  if (placeholder) placeholder.remove();
  const line = document.createElement("div");
  line.className = "log-line";
  line.textContent = message;
  terminal.appendChild(line);
  if (autoScroll) terminal.scrollTop = terminal.scrollHeight;
}

function updateTamanhStatus(status) {
  document.getElementById("tamanhCategories").textContent = `${status.categories_processed || 0} / ${status.categories_found || 0}`;
  document.getElementById("tamanhPages").textContent = status.pages_processed || 0;
  document.getElementById("tamanhQuestions").textContent = status.questions_found || 0;
  document.getElementById("tamanhFiles").textContent = status.files_created || 0;
  document.getElementById("tamanhDuplicates").textContent = status.duplicates_skipped || 0;
  document.getElementById("tamanhErrors").textContent = status.errors || 0;
  const badge = document.getElementById("tamanhStateBadge");
  badge.textContent = status.status || "Chờ";
  badge.className = "crawl-state-badge";
  if (["RUNNING", "QUEUED"].includes(status.status)) badge.classList.add("running");
  if (["COMPLETED", "STOPPED"].includes(status.status)) badge.classList.add("done");
  if (status.status === "ERROR") badge.classList.add("error");
  document.getElementById("tamanhProgressMsg").textContent = status.current_category
    ? `Đang xử lý: ${status.current_category}${status.current_url ? ` — ${status.current_url}` : ""}`
    : (status.message || "Đang chuẩn bị crawler...");
}

async function startTamanhCrawler() {
  const sourceUrl = document.getElementById("tamanhSourceUrl").value.trim();
  const startYear = Number(document.getElementById("tamanhStartYear").value);
  const endYear = Number(document.getElementById("tamanhEndYear").value);
  if (!sourceUrl || !Number.isInteger(startYear) || !Number.isInteger(endYear) || startYear > endYear) {
    showToast("Vui lòng nhập URL và khoảng năm hợp lệ.", "error");
    return;
  }
  const startButton = document.getElementById("btnStartTamanh");
  startButton.disabled = true;
  updateTamanhStatus({ status: "QUEUED", message: "Đang gửi yêu cầu khởi động crawler..." });
  appendTamanhLog("Đang gửi yêu cầu khởi động tới backend...");
  try {
    const response = await fetch(`${API_BASE}/crawler/tamanh/start`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ sourceUrl, startYear, endYear }),
    });
    const contentType = response.headers.get("content-type") || "";
    const payload = contentType.includes("application/json")
      ? await response.json()
      : { detail: `Backend trả về HTTP ${response.status} thay vì JSON.` };
    if (!response.ok) throw new Error(payload.detail || "Không thể khởi động crawler.");
    tamanhJobId = payload.jobId;
    tamanhLogCount = 0;
    document.getElementById("tamanhLogTerminal").innerHTML = "";
    document.getElementById("btnStopTamanh").style.display = "";
    appendTamanhLog("Đã khởi động crawler Tâm Anh.");
    pollTamanhStatus();
  } catch (error) {
    const message = `Không thể khởi động crawler: ${error.message}. Hãy kiểm tra/restart backend ở cổng 8000.`;
    updateTamanhStatus({ status: "ERROR", message, errors: 1 });
    appendTamanhLog(`ERROR: ${message}`);
    showToast(message, "error", 7000);
    startButton.disabled = false;
  }
}

function pollTamanhStatus() {
  if (tamanhPollingInterval) clearInterval(tamanhPollingInterval);
  const poll = async () => {
    if (!tamanhJobId) return;
    try {
      const response = await fetch(`${API_BASE}/crawler/tamanh/status/${tamanhJobId}`);
      const contentType = response.headers.get("content-type") || "";
      const status = contentType.includes("application/json")
        ? await response.json()
        : { detail: `Backend trả về HTTP ${response.status} thay vì JSON.` };
      if (!response.ok) throw new Error(status.detail || "Không đọc được trạng thái crawler.");
      updateTamanhStatus(status);
      (status.log_messages || []).slice(tamanhLogCount).forEach(appendTamanhLog);
      tamanhLogCount = (status.log_messages || []).length;
      if (!["QUEUED", "RUNNING"].includes(status.status)) {
        clearInterval(tamanhPollingInterval); tamanhPollingInterval = null;
        document.getElementById("btnStartTamanh").disabled = false;
        document.getElementById("btnStopTamanh").style.display = "none";
        showToast(status.message || status.status, status.status === "ERROR" ? "error" : "success");
      }
    } catch (error) {
      const message = `Lỗi đọc trạng thái: ${error.message}`;
      updateTamanhStatus({ status: "ERROR", message, errors: 1 });
      appendTamanhLog(message);
      clearInterval(tamanhPollingInterval); tamanhPollingInterval = null;
      document.getElementById("btnStartTamanh").disabled = false;
      document.getElementById("btnStopTamanh").style.display = "none";
    }
  };
  poll();
  tamanhPollingInterval = setInterval(poll, 1500);
}

async function stopTamanhCrawler() {
  if (!tamanhJobId) return;
  try {
    await fetch(`${API_BASE}/crawler/tamanh/stop/${tamanhJobId}`, { method: "POST" });
    appendTamanhLog("Đã gửi yêu cầu dừng crawler.");
  } catch {
    const message = "Không thể gửi yêu cầu dừng crawler.";
    appendTamanhLog(`ERROR: ${message}`);
    showToast(message, "error");
  }
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
let serverHealthFailures = 0;
const SERVER_HEALTH_TIMEOUT_MS = 8000;
const SERVER_HEALTH_FAILURE_LIMIT = 2;

async function checkServerStatus() {
  const dot  = document.getElementById("statusDot");
  const text = document.getElementById("statusText");
  if (serverHealthFailures === 0) {
    dot.className = "status-dot checking";
    text.textContent = "Đang kết nối...";
  }
  try {
    // Do not use crawler progress as the connectivity probe: a scraper's
    // transient state must not make a healthy API appear offline.
    const healthResponse = await fetch(`${API_BASE}/health`, {
      signal: AbortSignal.timeout(SERVER_HEALTH_TIMEOUT_MS),
    });
    let status = null;
    if (healthResponse.ok) {
      // Nếu crawl đang chạy trên server mà frontend chưa polling → tự resume.
      // Status is optional; it must not affect the online indicator.
      try {
        const statusResponse = await fetch(`${API_BASE}/status`, {
          signal: AbortSignal.timeout(SERVER_HEALTH_TIMEOUT_MS),
        });
        status = statusResponse.ok ? await statusResponse.json() : null;
      } catch {
        // The health probe above already proved the API is reachable.
      }
    } else if (healthResponse.status === 404) {
      // Compatibility for a backend process that has not restarted after the
      // health route was added. Its established status endpoint is enough.
      const legacyResponse = await fetch(`${API_BASE}/status`, {
        signal: AbortSignal.timeout(SERVER_HEALTH_TIMEOUT_MS),
      });
      if (!legacyResponse.ok) throw new Error(`HTTP ${legacyResponse.status}`);
      status = await legacyResponse.json();
    } else {
      throw new Error(`HTTP ${healthResponse.status}`);
    }

    serverHealthFailures = 0;
    dot.className = "status-dot online";
    text.textContent = "Máy chủ hoạt động";
    if (status?.running && !pollingInterval) {
      appendLog("🔄 Phát hiện crawler đang chạy — tự động kết nối lại...", "info");
      document.getElementById("btnScrape").disabled = true;
      document.getElementById("btnStopScrape").style.display = "";
      _startPolling();
    }
  } catch {
    serverHealthFailures += 1;
    if (serverHealthFailures >= SERVER_HEALTH_FAILURE_LIMIT) {
      dot.className = "status-dot offline";
      text.textContent = "Mất kết nối";
    } else {
      dot.className = "status-dot checking";
      text.textContent = "Đang kết nối lại...";
    }
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
    loadTopConcepts(""),
    loadRecentArticles(),
  ]);
}

async function loadKPIs() {
  try {
    const [articles, concepts] = await Promise.all([
      fetch(`${API_BASE}/articles`).then(r => r.json()),
      fetch(`${API_BASE}/top-concepts?limit=100`).then(r => r.json()),
    ]);

    // API list trả về is_labeled (0/1), không trả về highlighted_html
    const labeled = articles.filter(a => a.is_labeled || a.highlighted_html).length;

    animateCount("kpi-articles", articles.length);
    animateCount("kpi-labeled",  labeled);
    animateCount("kpi-concepts", concepts.length > 0
      ? concepts.reduce((s, c) => s + (c.frequency || 0), 0) : 0);

    document.getElementById("kpi-articles-sub").textContent =
      `${labeled} đã gán nhãn / ${articles.length} tổng`;

    // Draw donut
    buildDonut(concepts);

    currentArticlesData = articles;
  } catch(e) {
    console.warn("KPI load error:", e);
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

const displayedCrawlLogMessages = new Set();

function clearLog() {
  displayedCrawlLogMessages.clear();
  document.getElementById("logTerminal").innerHTML =
    `<div class="log-placeholder"><span class="log-cursor">█</span> Log đã được xóa.</div>`;
}

function toggleAutoScroll() {
  autoScroll = !autoScroll;
  document.getElementById("autoScrollBtn").textContent = autoScroll ? "↓ Tự cuộn" : "↑ Tắt cuộn";
}

function appendLog(msg, type = "") {
  const term = document.getElementById("logTerminal");
  const logKey = String(msg || "").trim();
  // Status polling can return the same message more than once. Keep one
  // visible record of an identical message in the current terminal session.
  if (displayedCrawlLogMessages.has(logKey)) return;
  displayedCrawlLogMessages.add(logKey);
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


  if (s.current_url) {
    document.getElementById("progressMsg").textContent = `Đang xử lý: ${s.current_url}`;
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
  document.getElementById("sum-language-rejected").textContent =
    (summary.rejected_english || 0) + (summary.rejected_mixed || 0) + (summary.rejected_no_text || 0);
  document.getElementById("sum-quarantined").textContent = summary.quarantined || 0;
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
      // Nếu đang chạy rồi → tự động bắt đầu polling để hiển thị log
      _startPolling();
      return;
    }

    if (!res.ok) throw new Error("Lỗi máy chủ");

    appendLog("✅ Lệnh đã được gửi, crawler đang chạy nền...", "success");
    _startPolling();

  } catch (err) {
    appendLog(`❌ Lỗi kết nối: ${err.message}`, "error");
    showToast("Không thể kết nối đến máy chủ", "error");
    resetScrapeBtn();
  }
}

/** Bắt đầu polling vòng lặp lấy log từ backend mỗi 2s */
function _startPolling() {
  let lastLogCount = 0;
  if (pollingInterval) clearInterval(pollingInterval);
  pollingInterval = setInterval(async () => {
    try {
      const s = await fetch(`${API_BASE}/status`).then(r => r.json());

      // Cập nhật số liệu tiến độ
      updateProgressUI(s);

      // Append các log mới
      if (s.log_messages && s.log_messages.length > lastLogCount) {
        const newLogs = s.log_messages.slice(lastLogCount);
        newLogs.forEach(msg => appendLog(msg));
        lastLogCount = s.log_messages.length;
      }

      // Chỉ dừng polling khi done=true (crawl thực sự kết thúc)
      if (s.done) {
        clearInterval(pollingInterval); pollingInterval = null;
        if (s.error) {
          appendLog(`❌ Lỗi: ${s.error}`, "error");
          showToast("Crawler gặp lỗi!", "error");
        } else {
          appendLog("✅ Thu thập hoàn thành! Đang làm mới dữ liệu...", "success");
          showToast("Thu thập dữ liệu hoàn thành!", "success");
          _showSummaryBox(s.summary || {});
          updateProgressUI(s);
          setTimeout(() => {
            loadData();
            loadDashboard();
            loadCrawlLogs();
            appendLog(`📊 Tổng kết: ✅ ${s.summary?.success||0} lưu · 🔁 ${s.summary?.duplicates||0} trùng · ⏭ ${s.summary?.skipped||0} bỏ qua`, "info");
          }, 1500);
        }
        resetScrapeBtn();
      } else if (s.running) {
        // Đảm bảo nút Stop hiển thị khi đang chạy
        document.getElementById("btnScrape").disabled = true;
        document.getElementById("btnStopScrape").style.display = "";
      }
    } catch {}
  }, 2000);
}

function stopScraping() {
  fetch(`${API_BASE}/scrape/stop`, { method: 'POST' }).catch(() => {});
  clearInterval(pollingInterval); pollingInterval = null;
  isScraping = false;
  appendLog("⏹ Đã gửi lệnh dừng crawler", "warning");
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
  if (currentArticlesData && currentArticlesData.length > 0) {
    renderArticleList();
    return;
  }
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
  if (articleFilter === "labeled")   data = data.filter(a =>  a.is_labeled || a.highlighted_html);
  if (articleFilter === "unlabeled") data = data.filter(a => !a.is_labeled && !a.highlighted_html);

  if (info) info.textContent = `${data.length} bài báo`;

  if (data.length === 0) {
    scroll.innerHTML = `<div class="list-placeholder">Không tìm thấy bài báo</div>`;
    return;
  }

  scroll.innerHTML = data.map(a => {
    const isLabeled = a.is_labeled || !!a.highlighted_html;
    const isActive  = a.id === currentArticleId;
    return `
      <div class="article-list-item ${isActive ? "active" : ""}" onclick="selectArticle(${a.id})">
        <div class="ali-title">${a.title || "Không có tiêu đề"}</div>
        <div class="ali-authors">${a.authors || "Không rõ tác giả"}</div>
        <div class="ali-meta">
          <span class="ali-dot ${isLabeled ? "labeled" : "unlabeled"}"></span>
          <span>${a.publication_year || "—"}</span>
          <span>·</span>
          <span>${isLabeled ? "Đã gán nhãn" : "Chưa xử lý"}</span>
        </div>
      </div>`;
  }).join("");
}

async function selectArticle(id) {
  currentArticleId = id;
  isNerActive      = false;
  renderArticleList(); // update active state

  let article = currentArticlesData.find(a => a.id === id);
  if (!article) return;

  // Show viewer
  document.getElementById("viewerPlaceholder").style.display = "none";
  document.getElementById("viewerContent").style.display     = "flex";
  document.getElementById("viewerContent").style.flexDirection = "column";

  const textBody = document.getElementById("textBody");

  // Fetch full details if not yet loaded (list API only returns metadata, no abstract)
  if (!article._loaded) {
    textBody.innerHTML = `<em style='color:var(--text-3)'>⏳ Đang tải nội dung...</em>`;
    try {
      const detail = await fetch(`${API_BASE}/articles/${id}`).then(r => r.json());
      if (detail) {
        article.abstract = detail.abstract || null;
        article.highlighted_html = detail.highlighted_html || null;
        article.matched_concepts = detail.matched_concepts || [];
        article._loaded = true;
      }
    } catch(e) {
      console.error("Error fetching detail:", e);
      textBody.innerHTML = `<em style='color:var(--danger)'>Lỗi tải nội dung bài báo</em>`;
      return;
    }
  }

  // Meta
  document.getElementById("articleTitle").textContent   = article.title   || "Không có tiêu đề";
  document.getElementById("articleYear").textContent    = article.publication_year || "—";
  document.getElementById("articleAuthors").textContent = article.authors  || "Không rõ tác giả";

  // NER state UI reset
  const btnNer   = document.getElementById("btnNer");
  const btnSave  = document.getElementById("btnSaveNer");
  const nerStatus = document.getElementById("nerStatus");
  const entPanel  = document.getElementById("entitiesPanel");
  const legend    = document.getElementById("entityLegend");

  btnNer.className   = "btn-ner";
  btnNer.textContent = "Bật gán nhãn";
  btnSave.style.display = "none";
  nerStatus.textContent = "";
  entPanel.style.display = "none";
  legend.style.display   = "none";

  // If already analyzed — show
  if (article.highlighted_html) {
    isNerActive = true;
    btnNer.className = "btn-ner active";
    btnNer.textContent = "Ẩn gán nhãn";
    textBody.innerHTML = article.highlighted_html;
    nerStatus.textContent = "✅ Đã gán nhãn";
    legend.style.display  = "flex";
    btnSave.style.display = "";
    if (article.matched_concepts?.length) {
      renderEntities(article.matched_concepts);
      entPanel.style.display = "";
    }
  } else {
    // Show raw text
    textBody.innerHTML = article.abstract ? article.abstract.replace(/\n/g, "<br>") : "<em>Không có nội dung tóm tắt.</em>";
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
    btnNer.textContent = "Bật gán nhãn";
    document.getElementById("textBody").innerHTML =
      article.abstract ? article.abstract.replace(/\n/g, "<br>") : "";
    document.getElementById("btnNer").classList.remove("active");
  document.getElementById("btnNer").textContent = "Bật gán nhãn";
  document.getElementById("btnSaveNer").style.display = "none";
  document.getElementById("btnAiNer").style.display = "inline-flex"; // Hiển thị nút AI
  document.getElementById("aiPanel").style.display = "none"; // Ẩn panel AI cũ
    nerStatus.textContent = "";
    return;
  }

  // Call NER
  btnNer.disabled  = true;
  btnNer.className = "btn-ner loading";
  btnNer.textContent = "Đang phân tích...";
  nerStatus.textContent = "Đang gán nhãn...";

  try {
    const res = await fetch(`${API_BASE}/highlight-text`, {
      method:  "POST",
      headers: { "Content-Type": "application/json" },
      body:    JSON.stringify({
        text:                article.abstract || "",
        threshold:           100,
        enable_tone_restore: false,
        enable_noun_phrase:  false,
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
      btnNer.textContent = "Ẩn gán nhãn";
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
    btn.textContent = "Lưu kết quả";
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
// CRAWL LOGS
// ============================================================
async function loadCrawlLogs() {
  const tbody = document.getElementById("logsTableBody");
  try {
    const res = await fetch(`${API_BASE}/crawl-logs`);
    const logs = await res.json();
    if (!logs || logs.length === 0) {
      tbody.innerHTML = `<tr><td colspan="7" class="table-empty">Chưa có dữ liệu nhật ký thu thập.</td></tr>`;
      return;
    }

    tbody.innerHTML = logs.map(log => {
      const date = new Date(log.crawl_date).toLocaleDateString("vi-VN");
      let statusHtml = "";
      return `
        <tr>
          <td>${date}</td>
          <td class="text-ellipsis" title="${log.target_url}">${log.target_url || "—"}</td>
          <td style="text-align:center; font-weight:500;">${log.start_year || "—"}</td>
          <td style="text-align:center; font-weight:500;">${log.end_year || "—"}</td>
          <td style="text-align:center;">${log.total_urls}</td>
          <td style="text-align:center; color:var(--success); font-weight:500;">${log.success_count}</td>
          <td style="text-align:center; color:var(--warning); font-weight:500;">${log.duplicate_count}</td>
          <td style="text-align:center; color:var(--danger); font-weight:500;">${log.error_count}</td>
        </tr>
      `;
    }).join("");
  } catch (e) {
    tbody.innerHTML = `<tr><td colspan="7" class="table-empty">Lỗi khi tải nhật ký: ${e.message}</td></tr>`;
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

let _splitPdfFiles = [];
let _splitPdfRunning = false;
let _splitPdfStopRequested = false;
let _splitPdfAbortController = null;

function stopSplitPdf() {
  if (!_splitPdfRunning) return;

  _splitPdfStopRequested = true;
  document.getElementById('btnStopSplitPdf').disabled = true;
  document.getElementById('splitPdfMessage').textContent =
    'Đang dừng… File đang xử lý sẽ không được chờ nữa; các file chưa bắt đầu sẽ không được gửi.';
  // Abort lets the browser leave a synchronous request immediately. The
  // backend may still finish the one file it already received, but no further
  // PDF from this batch is submitted after the stop request.
  _splitPdfAbortController?.abort();
}

function openPdfFolderPicker(event) {
  event?.preventDefault();
  event?.stopPropagation();
  document.getElementById('splitPdfInput').click();
  return false;
}

function openPdfFilePicker(event) {
  event?.preventDefault();
  event?.stopPropagation();
  document.getElementById('splitPdfFileInput').click();
  return false;
}

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
  if (files && files.length > 0) _setSplitPdfFiles(files);
}
function splitPdfSelected(e) {
  const files = e.target.files;
  if (files && files.length > 0) _setSplitPdfFiles(files);
}

function _setSplitPdfFiles(files) {
  if (_splitPdfRunning) {
    showToast('Không thể thay đổi danh sách khi đang tách. Hãy bấm Dừng tách trước.', 'warning');
    return;
  }
  _splitPdfFiles = [];
  let totalSize = 0;
  for (let i = 0; i < files.length; i++) {
    if (files[i].name.toLowerCase().endsWith('.pdf')) {
      _splitPdfFiles.push(files[i]);
      totalSize += files[i].size;
    }
  }

  if (_splitPdfFiles.length === 0) {
    showToast('Không tìm thấy file PDF nào trong thư mục!', 'error'); 
    return;
  }
  
  document.getElementById('splitPdfName').textContent = `Đã chọn ${_splitPdfFiles.length} file PDF`;
  document.getElementById('splitPdfSize').textContent = `Tổng dung lượng: ${_fmtSize(totalSize)}`;
  const preview = _splitPdfFiles.slice(0, 5)
    .map(file => file.webkitRelativePath || file.name)
    .join(' • ');
  document.getElementById('splitPdfPreview').textContent = preview
    ? `${preview}${_splitPdfFiles.length > 5 ? ` • và ${_splitPdfFiles.length - 5} file khác` : ''}`
    : '';
  document.getElementById('splitPdfInfo').style.display = 'flex';
  document.getElementById('btnSplitPdf').disabled = false;
  document.getElementById('splitPdfResultPanel').style.display = 'none';
  document.getElementById('splitPdfEmpty').style.display = 'block';
}

function clearSplitPdf() {
  if (_splitPdfRunning) {
    stopSplitPdf();
    return;
  }
  _splitPdfFiles = [];
  document.getElementById('splitPdfInput').value = '';
  document.getElementById('splitPdfFileInput').value = '';
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
  if (_splitPdfFiles.length === 0) { showToast('Chưa chọn file PDF', 'warning'); return; }
  if (_splitPdfRunning) return;

  const btn = document.getElementById('btnSplitPdf');
  const stopBtn = document.getElementById('btnStopSplitPdf');
  const oldHtml = btn.innerHTML;
  const filesToProcess = [..._splitPdfFiles];
  _splitPdfRunning = true;
  _splitPdfStopRequested = false;
  btn.disabled = true;
  stopBtn.disabled = false;
  stopBtn.style.display = 'block';

  document.getElementById('splitPdfEmpty').style.display = 'none';
  document.getElementById('splitPdfResultPanel').style.display = 'block';
  
  const filesList = document.getElementById('splitPdfFilesList');
  filesList.innerHTML = '';
  
  let successCount = 0;
  let duplicateCount = 0;
  let stopped = false;

  for (let i = 0; i < filesToProcess.length; i++) {
    if (_splitPdfStopRequested) {
      stopped = true;
      break;
    }
    const file = filesToProcess[i];
    btn.innerHTML = `<span>⏳ Gemini đang trích xuất ${i+1}/${filesToProcess.length}...</span>`;
    document.getElementById('splitPdfMessage').textContent = `Đang dựng layout và gọi Gemini cho file ${i+1}/${filesToProcess.length}... (${file.name})`;
    
    try {
      const formData = new FormData();
      formData.append('file', file);
      formData.append('relative_path', file.webkitRelativePath || file.name);

      _splitPdfAbortController = new AbortController();
      const res = await fetch(`${API_BASE}/extract-pdf`, {
        method: 'POST',
        body: formData,
        signal: _splitPdfAbortController.signal,
      });

      if (!res.ok) {
        const err = await res.json().catch(() => ({}));
        throw new Error(err.detail || `HTTP ${res.status}`);
      }

      const data = await res.json();
      if (data.duplicate) {
        duplicateCount++;
        const existingPath = escapeHtml(data.outputDirectory || data.duplicateOf?.outputDirectory || 'kho PDF đã tách');
        filesList.innerHTML += `<li><div style="margin-top:15px; margin-bottom:5px; color:var(--text-2);"><strong style="color:var(--warning); font-size:15px;">↪ ${escapeHtml(file.webkitRelativePath || file.name)}</strong> — Đã bỏ qua vì nội dung PDF trùng (SHA-256); không tạo file TXT/JSON mới.<div style="margin:4px 0 0 20px; font-size:12px;">Bản đã có: ${existingPath}</div></div></li>`;
        continue;
      }
      successCount++;

      const validation = data.validation || {};
      const validationBadge = validation.ok
        ? `<span style="margin-left:10px; color:var(--success); font-size:12px;">✓ Đã đối chiếu ${validation.section_count || 0} section với PDF nguồn</span>`
        : `<span style="margin-left:10px; color:var(--warning); font-size:12px;">⚠ Có ${(validation.issues || []).length} cảnh báo kiểm chứng</span>`;
      const article = data.article || {};
      const extraction = data.extraction || {};
      const extractionInfo = extraction.method
        ? `<div><strong>Trích xuất:</strong> ${escapeHtml(extraction.provider || 'LLM')} · ${escapeHtml(extraction.model || '')} · prompt ${escapeHtml(extraction.prompt_version || '')} · ${Number(article.sections || 0)} section</div>`
        : '';
      const articleInfo = article.title || article.authors || article.abstract
        ? `<div style="margin:6px 0 10px 20px; font-size:12px; color:var(--text-2);">
             <div><strong>Tiêu đề:</strong> ${escapeHtml(article.title || 'Không nhận diện được')}</div>
             <div><strong>Tác giả:</strong> ${escapeHtml(article.authors || 'Không nhận diện được')}</div>
             ${extractionInfo}
             <div title="${escapeHtml(data.outputDirectory || '')}"><strong>Lưu tại:</strong> ${escapeHtml(data.outputDirectory || '')}</div>
             <div title="${escapeHtml(data.structuredDocumentFile || '')}"><strong>Master JSON:</strong> ${escapeHtml(data.structuredDocumentFile || 'Không có')}</div>
           </div>`
        : '';
      const fileHeader = `<li><div style="margin-top:15px; margin-bottom: 5px;"><strong style="color:var(--primary); font-size:15px;">📄 ${escapeHtml(file.webkitRelativePath || file.name)}</strong>${validationBadge}</div>${articleInfo}`;
      // abstract.txt is the corpus-facing abstract. Its section JSON is kept
      // for provenance in Master JSON, but must not appear as a second
      // duplicate abstract card in the UI.
      const displayFiles = data.files_created.filter((f) =>
        typeof f === 'string' || !(f.label === 'abstract' && f.json_file_path)
      );
      const innerList = displayFiles.map((f) => {
        if (typeof f === 'string') return `<div style="margin-left:20px; font-size:13px; margin-bottom:4px;">- ${f}</div>`;
        return `
          <div style="margin-left:20px; border: 1px solid var(--border); border-radius: 6px; margin-bottom: 10px; overflow: hidden;">
            <div style="background: var(--bg-soft); padding: 8px 14px; border-bottom: 1px solid var(--border); display: flex; justify-content: space-between; align-items: center; cursor: pointer;" onclick="const content = this.nextElementSibling; content.style.display = content.style.display === 'none' ? 'block' : 'none';">
              <strong style="color: var(--primary); font-size: 13px;">📑 ${f.section_name}${f.canonical_type ? ` · ${f.canonical_type}` : ''}</strong>
              <span style="font-size: 12px; color: var(--text-3); max-width: 300px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis;" title="${f.file_path}">${f.file_path.split(/[\\/\\\\]/).pop()}</span>
            </div>
            <div style="padding: 14px; font-size: 12px; color: var(--text-2); background: white; display: none; line-height: 1.5;">
              ${f.content_preview ? f.content_preview.replace(/\\n/g, '<br>') : 'Không có nội dung preview'}
            </div>
          </div>
        `;
      }).join("");
      
      filesList.innerHTML += fileHeader + innerList + "</li>";

    } catch (e) {
      if (_splitPdfStopRequested || e?.name === 'AbortError') {
        stopped = true;
        filesList.innerHTML += `<li><div style="margin-top:15px; margin-bottom:5px; color:var(--warning);"><strong>⏹ ${escapeHtml(file.webkitRelativePath || file.name)}</strong> — Đã dừng theo yêu cầu; không chờ kết quả file này.</div></li>`;
        break;
      }
      filesList.innerHTML += `<li><div style="margin-top:15px; margin-bottom: 5px;"><strong style="color:var(--danger); font-size:15px;">❌ ${file.name}</strong> - Lỗi: ${e.message}</div></li>`;
    } finally {
      _splitPdfAbortController = null;
    }
  }

  stopped = stopped || _splitPdfStopRequested;
  if (stopped) {
    document.getElementById('splitPdfMessage').textContent = `Đã dừng: Tách mới ${successCount} file, bỏ qua ${duplicateCount} file trùng. Các file còn lại không được gửi.`;
    showToast('Đã dừng tách PDF. Các kết quả đã hoàn tất vẫn được giữ lại.', 'info');
  } else {
    document.getElementById('splitPdfMessage').textContent = `Hoàn tất: Tách mới ${successCount} file, bỏ qua ${duplicateCount} file trùng.`;
    showToast(`Tách mới ${successCount} file; bỏ qua ${duplicateCount} file trùng.`, duplicateCount ? 'info' : 'success');
  }
  
  _splitPdfRunning = false;
  _splitPdfStopRequested = false;
  stopBtn.style.display = 'none';
  stopBtn.disabled = false;
  btn.disabled = false;
  btn.innerHTML = oldHtml;
}

// Render preprocessing log panel
function _renderPreprocLog(log, panelId, bodyId) {
  const panel = document.getElementById(panelId);
  const body  = document.getElementById(bodyId);
  if (!panel || !body) return;

  if (!log || !Array.isArray(log) || log.length === 0) {
    panel.style.display = 'none';
    return;
  }

  panel.style.display = 'block';
  
  let html = '<div class="preproc-timeline">';
  
  log.forEach(step => {
    html += `
      <div class="preproc-step">
        <div class="preproc-step-header">
          <span class="preproc-step-icon">⚡</span>
          <span class="preproc-step-title">${step.step}</span>
        </div>
        <div class="preproc-step-desc">${step.description}</div>
    `;
    
    if (step.changes && step.changes.length > 0) {
      html += `<div class="preproc-changes-list">`;
      step.changes.forEach(change => {
        const parts = change.split(' → ');
        if (parts.length === 2) {
          // Highlight before/after
          html += `
            <div class="preproc-item">
              <span class="preproc-before">${parts[0].trim()}</span>
              <span class="preproc-arrow">→</span>
              <span class="preproc-after">${parts[1].trim()}</span>
            </div>
          `;
        } else {
          html += `<div class="preproc-item" style="font-size:11px">${change}</div>`;
        }
      });
      html += `</div>`;
    }
    
    html += `</div>`;
  });
  
  html += '</div>';
  body.innerHTML = html;
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

// ============================================================
// AI LABEL UI
// ============================================================

async function loadAiLabelData() {
  if (aiLabelArticlesData && aiLabelArticlesData.length > 0) return;
  const listEl = document.getElementById("aiArticleListScroll");
  try {
    const res = await fetch(`${API_BASE}/articles`);
    aiLabelArticlesData = await res.json();
    if (aiLabelArticlesData.length > 0) {
      currentAiArticleId = aiLabelArticlesData[0].id;
    }
    renderAiArticleList();
    if (currentAiArticleId) selectAiArticle(currentAiArticleId);
  } catch (e) {
    listEl.innerHTML = `<div style="padding:20px; color:var(--danger)">Lỗi tải dữ liệu: ${e.message}</div>`;
  }
}

function filterAiArticles() {
  const query = document.getElementById("aiSearchInput").value.toLowerCase();
  const pills = document.querySelectorAll("#aiFilterPills .filter-pill");
  pills.forEach(p => {
    if (p.classList.contains("active")) {
      currentAiFilter = p.dataset.filter;
    }
  });

  const filtered = aiLabelArticlesData.filter(a => {
    const text = (a.title + " " + a.authors + " " + a.abstract).toLowerCase();
    if (!text.includes(query)) return false;

    if (currentAiFilter === "labeled") return (a.matched_concepts && a.matched_concepts.length > 0) || a.highlighted_html || a._aiSaved;
    if (currentAiFilter === "unlabeled") return !(a.matched_concepts && a.matched_concepts.length > 0) && !a.highlighted_html && !a._aiSaved;
    return true;
  });

  const listEl = document.getElementById("aiArticleListScroll");
  listEl.innerHTML = filtered.map(a => {
    const isActive = a.id === currentAiArticleId ? "active" : "";
    const isLabeled = (a.matched_concepts && a.matched_concepts.length > 0) || a.highlighted_html || a._aiSaved;
    return `
      <div class="article-list-item ${isActive}" onclick="selectAiArticle(${a.id})">
        <div class="ali-title">${a.title || "Không có tiêu đề"}</div>
        <div class="ali-meta">
          <span class="ali-dot ${isLabeled ? "labeled" : "unlabeled"}"></span>
          <span>${a.publication_year || "—"}</span>
          <span>·</span>
          <span>${isLabeled ? "Đã gán nhãn" : "Chưa xử lý"}</span>
        </div>
      </div>
    `;
  }).join("");
}

// Add listener to filter pills
document.querySelectorAll("#aiFilterPills .filter-pill").forEach(p => {
  p.addEventListener("click", function() {
    document.querySelectorAll("#aiFilterPills .filter-pill").forEach(el => el.classList.remove("active"));
    this.classList.add("active");
    filterAiArticles();
  });
});

function renderAiArticleList() {
  filterAiArticles();
}

async function selectAiArticle(id) {
  currentAiArticleId = id;
  currentAiLabelResult = null;
  currentAiLabelResultArticleId = null;
  renderAiArticleList(); // highlight active card

  const article = aiLabelArticlesData.find(a => a.id === id);
  if (!article) return;

  document.getElementById("aiMetaTitle").textContent = article.title || "Chưa có tiêu đề";
  document.getElementById("aiMetaYear").textContent  = article.publication_year || "N/A";
  document.getElementById("aiMetaAuthors").textContent = article.authors || "Không rõ tác giả";

  document.getElementById("btnRunAiNer").disabled = false;
  document.getElementById("btnRunAiNer").textContent = "Gán nhãn bằng AI";
  const saveButton = document.getElementById("btnSaveAiLabel");
  saveButton.disabled = true;
  saveButton.textContent = "Lưu kết quả AI";
  
  const aiTextBodyArea = document.getElementById("aiTextBodyArea");
  if (!article._loaded) {
    aiTextBodyArea.innerHTML = `<em style='color:var(--text-3)'>⏳ Đang tải nội dung...</em>`;
    try {
      const detail = await fetch(`${API_BASE}/articles/${id}`).then(r => r.json());
      if (detail) {
        article.abstract = detail.abstract || null;
        article.highlighted_html = detail.highlighted_html || null;
        article._loaded = true;
      }
    } catch (e) {
      console.error("Error fetching detail for AI:", e);
      aiTextBodyArea.innerHTML = `<em style='color:var(--danger)'>Lỗi tải nội dung bài báo</em>`;
      return;
    }
  }

  aiTextBodyArea.innerHTML = article.abstract
    ? escapeHtml(article.abstract).replace(/\n/g, "<br>")
    : "<em style='color:var(--text-3)'>Bài báo này không có nội dung tóm tắt trong cơ sở dữ liệu.</em>";
  
  document.getElementById("aiEntitiesList").innerHTML = `<div class="empty-state">Chưa phân tích (Bấm Gán nhãn bằng AI)</div>`;
  document.getElementById("aiTotalEntitiesCount").textContent = "0";
}

async function runAiLabel() {
  const article = aiLabelArticlesData.find(a => a.id === currentAiArticleId);
  if (!article || !article.abstract) {
    showToast("Không có văn bản để phân tích", "error");
    return;
  }

  const btn = document.getElementById("btnRunAiNer");
  const saveButton = document.getElementById("btnSaveAiLabel");
  currentAiLabelResult = null;
  currentAiLabelResultArticleId = null;
  saveButton.disabled = true;
  saveButton.textContent = "Lưu kết quả AI";
  btn.disabled = true;
  btn.textContent = "Đang phân tích...";
  document.getElementById("aiEntitiesList").innerHTML = `<div class="empty-state">Đang gọi Gemini AI...</div>`;

  try {
    const res = await fetch(`${API_BASE}/ai-label`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ text: article.abstract })
    });

    const payload = await res.json().catch(() => ({}));
    if (!res.ok) {
      throw new Error(payload.detail || `Lỗi gọi API AI (HTTP ${res.status})`);
    }

    const data = payload;
    currentAiLabelResult = data;
    currentAiLabelResultArticleId = article.id;
    
    let totalCount = 0;

    const catMapping = {
      "Bệnh lý": { color: "#4ade80", key: "DISEASE" },
      "Triệu chứng": { color: "#818cf8", key: "SYMPTOM" },
      "Điều trị": { color: "#c084fc", key: "TREATMENT" },
      "Xét nghiệm": { color: "#fbbf24", key: "LAB_TEST" },
      "Hình ảnh": { color: "#2dd4bf", key: "IMAGING" },
      "Sinh lý": { color: "#fb923c", key: "PHYSIOLOGY" }
    };

    if(!ENTITY_COLORS["PHYSIOLOGY"]) {
      ENTITY_COLORS["PHYSIOLOGY"] = { cls: "physiology", label: "Sinh lý", icon: "" };
    }
    if (!document.getElementById("physiology-style")) {
      const style = document.createElement('style');
      style.id = "physiology-style";
      style.innerHTML = `.entity-tag.physiology { background-color: rgba(251,146,60,0.15); border-color: rgba(251,146,60,0.4); color: #c2410c; }
      mark.ner-physiology { background: #f9a8d4; color: var(--text); font-weight: 600; border-radius: 4px; padding: 2px 5px; cursor: help; box-shadow: 0 1px 2px rgba(0,0,0,0.05); border: 1px solid rgba(0,0,0,0.08); }`;
      document.head.appendChild(style);
    }

    let entitiesHtml = "";
    const matches = [];

    for (const [vnCat, termsList] of Object.entries(data)) {
      const mapping = catMapping[vnCat];
      if (!mapping || !Array.isArray(termsList)) continue;

      for (const rawItem of termsList) {
        const item = typeof rawItem === "string"
          ? { term: rawItem, code: "", label_vn: "", spans: [] }
          : rawItem;
        if (!item?.term) continue;

        totalCount += 1;
        const markClass = mapping.key.toLowerCase().replace("_", "");
        const tooltip = buildEntityTooltip(item, vnCat);
        const codeHtml = item.code
          ? `<span class="entity-code">${escapeHtml(item.code)}</span>`
          : "";
        const sourceLabel = item.source === "ai+dictionary" ? "AI + Từ điển" : "AI";
        entitiesHtml += `<div class="entity-tag ${mapping.key.toLowerCase()}" title="${escapeHtml(tooltip)}">
          <span>${escapeHtml(item.term)}</span>
          ${codeHtml}
          <span class="matched-badge matched-exact">${sourceLabel}</span>
        </div>`;

        const spans = Array.isArray(item.spans) ? item.spans : [];
        for (const span of spans) {
          const start = Number(span.start);
          const end = Number(span.end);
          if (!Number.isInteger(start) || !Number.isInteger(end) || start < 0 || end <= start) continue;
          const surface = article.abstract.slice(start, end);
          if (surface.toLocaleLowerCase("vi") !== item.term.toLocaleLowerCase("vi")) continue;
          matches.push({ start, end, text: surface, markClass, tooltip });
        }
      }
    }

    // Giữ span dài nhất khi nhiều thực thể chồng lấn.
    matches.sort((a, b) => b.text.length - a.text.length || a.start - b.start);
    const finalMatches = [];
    for (const m of matches) {
      const overlaps = finalMatches.some(
        accepted => m.start < accepted.end && m.end > accepted.start
      );
      if (!overlaps) finalMatches.push(m);
    }

    // Dựng HTML từ văn bản nguồn và offset đã được backend xác thực.
    finalMatches.sort((a, b) => a.start - b.start);
    const pieces = [];
    let cursor = 0;
    for (const m of finalMatches) {
      pieces.push(escapeHtml(article.abstract.slice(cursor, m.start)));
      pieces.push(
        `<mark class="ner-${m.markClass}" title="${escapeHtml(m.tooltip)}">${escapeHtml(m.text)}</mark>`
      );
      cursor = m.end;
    }
    pieces.push(escapeHtml(article.abstract.slice(cursor)));
    document.getElementById("aiTextBodyArea").innerHTML = pieces.join("").replace(/\n/g, "<br>");

    if (totalCount > 0) {
        document.getElementById("aiTotalEntitiesCount").textContent = totalCount;
        document.getElementById("aiEntitiesList").innerHTML = entitiesHtml;
        document.getElementById("btnSaveAiLabel").disabled = false;
    } else {
        document.getElementById("aiTotalEntitiesCount").textContent = "0";
        document.getElementById("aiEntitiesList").innerHTML = `<div class="empty-state">AI không tìm thấy thực thể nào</div>`;
    }
    
  } catch (e) {
    document.getElementById("aiEntitiesList").innerHTML = `<div class="empty-state" style="color:var(--danger)">Lỗi: ${e.message}</div>`;
    showToast("Lỗi phân tích AI", "error");
  } finally {
    btn.disabled = false;
    btn.textContent = "Gán nhãn bằng AI";
  }
}

async function saveAiLabelResult() {
  const article = aiLabelArticlesData.find(a => a.id === currentAiArticleId);
  if (!article || currentAiLabelResultArticleId !== article.id || !currentAiLabelResult) {
    showToast("Hãy chạy gán nhãn AI cho bài báo này trước khi lưu.", "error");
    return;
  }

  const button = document.getElementById("btnSaveAiLabel");
  button.disabled = true;
  button.textContent = "Đang lưu...";
  try {
    const response = await fetch(`${API_BASE}/ai-label/save`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ articleId: article.id, labels: currentAiLabelResult }),
    });
    const payload = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(payload.detail || `Lỗi lưu kết quả AI (HTTP ${response.status})`);

    article._aiSaved = true;
    button.textContent = payload.duplicate ? "Đã lưu trước đó" : "Đã lưu kết quả AI";
    renderAiArticleList();
    showToast(payload.message || "Kết quả AI đã được lưu.", "success");
  } catch (error) {
    button.disabled = false;
    button.textContent = "Lưu kết quả AI";
    showToast(error.message, "error");
  }
}

window.onerror = function(msg, url, lineNo, columnNo, error) { console.error(msg + ' at line ' + lineNo); return false; };

// ============================================================
// KHỜI TẠO KHI TRANG LOAD — auto-resume nếu crawl đang chạy
// ============================================================
document.addEventListener("DOMContentLoaded", async () => {
  bindAuthForms();
  installAuthenticatedFetch();

  try {
    currentAuthUser = await loadAuthenticatedUser();
  } catch {
    currentAuthUser = null;
  }

  if (!currentAuthUser) {
    showAuthRoute(location.pathname === "/register" ? "register" : "login");
    return;
  }

  setApplicationVisible(true);
  renderAuthenticatedIdentity(currentAuthUser);
  initializeReviewWorkspace(currentAuthUser);

  // Experts never initialise the current administration tools. Their routes
  // use only the protected review APIs.
  if (currentAuthUser.role === "expert") return;

  await checkServerStatus();
  const currentYear = new Date().getFullYear();
  const sy = document.getElementById("startYear");
  const ey = document.getElementById("endYear");
  if (sy) { sy.value = 2020; sy.max = currentYear; }
  if (ey) { ey.value = currentYear; ey.max = currentYear; }

  const hash = location.hash.replace("#", "");
  if (hash && document.getElementById(`screen-${hash}`)) switchScreen(hash);
  else switchScreen("dashboard");
  setInterval(checkServerStatus, 10000);
});
