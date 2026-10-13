/* Corpus review workspace. Data is always loaded from authenticated APIs. */

const REVIEW_STATUS_TEXT = {
  CORRECT: "Correct",
  INCORRECT: "Incorrect",
  NEEDS_REVISION: "Needs Revision",
};

let reviewActiveView = "dashboard";
let reviewPage = 1;
let reviewerDetailDocument = null;
let reviewerDetailComparison = null;
let reviewerDetailMode = "text";
const reviewerDecisionState = {};
let expertCurrentLabelSource = "icd10"; // 'icd10' | 'ai' — nguồn nhãn của bài đang review
const expertAnnotationState = {};

function reviewEscape(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;").replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;").replaceAll('"', "&quot;").replaceAll("'", "&#039;");
}

function reviewDate(value) {
  if (!value) return "—";
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? String(value) : date.toLocaleString("vi-VN");
}

function statusBadge(value) {
  const raw = String(value || "Pending");
  const normalized = raw.toLowerCase().replaceAll("_", "-").replaceAll(" ", "-");
  const label = REVIEW_STATUS_TEXT[raw] || (raw === "Reviewed" ? "Reviewed" : raw === "Pending" ? "Pending" : raw);
  return `<span class="status-badge status-${reviewEscape(normalized)}">${reviewEscape(label)}</span>`;
}

async function reviewRequest(path, options = {}) {
  const requestOptions = { cache: "no-store", ...options };
  const response = await fetch(`/api${path}`, requestOptions);
  const payload = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(payload.detail || `HTTP ${response.status}`);
  return payload;
}

function ensureReviewScreen(id) {
  let screen = document.getElementById(id);
  if (!screen) {
    screen = document.createElement("section");
    screen.id = id;
    screen.className = "screen";
    document.querySelector(".main-content").appendChild(screen);
  }
  return screen;
}

function setReviewHeader(title, sub) {
  document.getElementById("topbarTitle").textContent = title;
  document.getElementById("topbarSub").textContent = sub;
}

function initializeReviewWorkspace(user) {
  if (user.role === "expert") {
    initializeExpertWorkspace();
  } else if (user.role === "reviewer") {
    initializeReviewerWorkspace();
  } else {
    initializeAdminReviewLinks();
    if (location.hash === "#labeling-sessions") showAdminReviewView("sessions");
  }
}

function initializeReviewerWorkspace() {
  const nav = document.querySelector(".sidebar-nav");
  nav.innerHTML = `
    <div class="nav-section-label">Reviewer</div>
    <a class="nav-item" data-review-view="reviewer-dashboard" onclick="showReviewerView('dashboard')"><span class="nav-label">Dashboard</span></a>
    <a class="nav-item" data-review-view="reviewer-reviews" onclick="showReviewerView('reviews')"><span class="nav-label">Review Dashboard</span></a>
    <a class="nav-item" data-screen="ai-evaluation" onclick="switchScreen('ai-evaluation')"><span class="nav-label">AI Evaluation</span></a>
  `;
  window.addEventListener("popstate", () => {
    const documentId = reviewerReviewIdFromPath(location.pathname);
    if (documentId) openReviewerReviewDocument(documentId, false);
    else showReviewerView(reviewerViewFromPath(location.pathname) || "dashboard", false);
  });
  const documentId = reviewerReviewIdFromPath(location.pathname);
  if (documentId) openReviewerReviewDocument(documentId, false);
  else showReviewerView(reviewerViewFromPath(location.pathname) || "dashboard", false);
}

function reviewerViewFromPath(path) {
  if (path === "/reviewer/reviews") return "reviews";
  if (path === "/reviewer" || path === "/reviewer/dashboard") return "dashboard";
  return null;
}

function reviewerReviewIdFromPath(path) {
  const match = /^\/reviewer\/review\/(\d+)$/.exec(path);
  return match ? Number(match[1]) : null;
}

function reviewerPath(view) {
  return view === "reviews" ? "/reviewer/reviews" : "/reviewer/dashboard";
}

function showReviewerView(view, push = true) {
  if (push && location.pathname !== reviewerPath(view)) history.pushState({}, "", reviewerPath(view));
  document.querySelectorAll(".screen").forEach(item => item.classList.remove("active"));
  document.querySelectorAll("[data-review-view]").forEach(item => item.classList.toggle("active", item.dataset.reviewView === `reviewer-${view}`));
  const screen = ensureReviewScreen("screen-reviewer-workspace");
  screen.classList.add("active");
  if (view === "reviews") return renderReviewerReviews(screen);
  return renderReviewerDashboard(screen);
}

async function renderReviewerDashboard(screen) {
  setReviewHeader("Reviewer Dashboard", "Overview of expert review results");
  screen.innerHTML = '<div class="review-workspace review-loading">Loading dashboard...</div>';
  try {
    const stats = await reviewRequest("/reviewer/dashboard");
    screen.innerHTML = `<div class="review-workspace">
      <div class="review-kpis">
        ${reviewKpi("Total Reviews", stats.totalReviews)}
        ${reviewKpi("Active Experts", stats.activeExperts)}
        ${reviewKpi("Reviewed Documents", stats.reviewedDocuments)}
        ${reviewKpi("Correct", stats.correctReviews)}
        ${reviewKpi("Incorrect", stats.incorrectReviews)}
        ${reviewKpi("Needs Revision", stats.needsRevision)}
      </div>
      <div class="review-panel"><h3>Review Dashboard</h3><p>Reviewers can monitor expert assessments and review outcomes across the system.</p></div>
    </div>`;
  } catch (error) {
    screen.innerHTML = `<div class="review-workspace review-error">Unable to load dashboard: ${reviewEscape(error.message)}</div>`;
  }
}

async function renderReviewerReviews(screen) {
  setReviewHeader("Review Dashboard", "Review results submitted by experts");
  screen.innerHTML = `<div class="review-workspace"><div class="review-panel">
    <div class="review-filter"><input id="reviewerQuery" placeholder="Search articles or experts"><select id="reviewerStatus"><option value="">All statuses</option><option value="CORRECT">Correct</option><option value="INCORRECT">Incorrect</option><option value="NEEDS_REVISION">Needs Revision</option></select><button class="review-action" onclick="loadReviewerReviews()">Search</button></div>
    <div id="reviewerReviewsArea" class="review-loading">Loading results...</div>
  </div></div>`;
  await loadReviewerReviews();
}

async function loadReviewerReviews() {
  const area = document.getElementById("reviewerReviewsArea");
  if (!area) return;
  area.innerHTML = '<div class="review-loading">Loading results...</div>';
  try {
    const params = new URLSearchParams({ page_size: "100" });
    const q = document.getElementById("reviewerQuery")?.value.trim() || "";
    const status = document.getElementById("reviewerStatus")?.value || "";
    if (q) params.set("q", q);
    if (status) params.set("review_status", status);
    const data = await reviewRequest(`/reviewer/reviews?${params}`);
    const rows = (data.items || []).map(item => `<tr>
      <td>${Number(item.id)}</td><td class="document-cell">${reviewEscape(item.document_title)}<div class="muted">#${Number(item.document_id)}</div></td>
      <td>${reviewEscape(item.expert_name)}<div class="muted">${reviewEscape(item.expert_email)}</div></td>
      <td>${statusBadge(item.review_status)}</td><td>${reviewEscape([item.suggested_icd10_code, item.suggested_icd10_label].filter(Boolean).join(" - ") || "—")}</td>
      <td>${reviewEscape(item.comment)}</td><td>${reviewDate(item.created_at)}</td>
      <td><button class="review-action" onclick="openReviewerReviewDocument(${Number(item.document_id)})">Xem</button></td>
    </tr>`).join("");
    area.innerHTML = rows
      ? `<div class="review-table-wrap"><table class="review-table"><thead><tr><th>ID</th><th>Document</th><th>Expert</th><th>Result</th><th>Suggested Code</th><th>Comment</th><th>Date</th><th></th></tr></thead><tbody>${rows}</tbody></table></div>`
      : '<div class="review-empty">No review results.</div>';
  } catch (error) {
    area.innerHTML = `<div class="review-error">Unable to load reviews: ${reviewEscape(error.message)}</div>`;
  }
}

function initializeExpertWorkspace() {
  const nav = document.querySelector(".sidebar-nav");
  nav.innerHTML = `
    <div class="nav-section-label">Chuyên gia</div>
    <button class="nav-collapse-toggle" type="button" onclick="toggleDocumentSidebar()" title="Thu gọn/mở rộng thanh tài liệu">⇔ Tài liệu</button>
    <a class="nav-item" data-review-view="dashboard" onclick="showExpertView('dashboard')"><span class="nav-label">Dashboard</span></a>
    <a class="nav-item" data-review-view="icd10" onclick="showExpertView('icd10')"><span class="nav-label">ICD-10 Labeled</span></a>
    <a class="nav-item" data-review-view="ai-labeled" onclick="showExpertView('ai-labeled')"><span class="nav-label">AI Labeled</span></a>
    <a class="nav-item" data-review-view="reviewed" onclick="showExpertView('reviewed')"><span class="nav-label">Reviewed</span></a>
    <a class="nav-item" data-screen="ai-evaluation" onclick="switchScreen('ai-evaluation')"><span class="nav-label">Đánh giá AI</span></a>
    <a class="nav-item" onclick="logoutCurrentUser()"><span class="nav-label">Logout</span></a>
  `;
  window.addEventListener("popstate", () => {
    const documentId = expertReviewIdFromPath(location.pathname);
    if (documentId) openExpertReview(documentId, false);
    else {
      const view = expertViewFromPath(location.pathname);
      if (view) showExpertView(view, false);
    }
  });
  const documentId = expertReviewIdFromPath(location.pathname);
  if (documentId) openExpertReview(documentId, false);
  else showExpertView(expertViewFromPath(location.pathname) || "dashboard", false);
}

function toggleDocumentSidebar() {
  document.body.classList.toggle("documents-sidebar-collapsed");
}

function expertViewFromPath(path) {
  const map = { "/expert": "dashboard", "/expert/dashboard": "dashboard", "/expert/icd10": "icd10", "/expert/ai-labeled": "ai-labeled", "/expert/reviewed": "reviewed" };
  return map[path] || null;
}

function expertReviewIdFromPath(path) {
  const match = /^\/expert\/review\/(\d+)$/.exec(path);
  return match ? Number(match[1]) : null;
}

function expertPath(view) {
  return { dashboard: "/expert/dashboard", icd10: "/expert/icd10", "ai-labeled": "/expert/ai-labeled", reviewed: "/expert/reviewed" }[view] || "/expert/dashboard";
}

function showExpertView(view, push = true) {
  reviewActiveView = view;
  reviewPage = 1;
  if (push && location.pathname !== expertPath(view)) history.pushState({}, "", expertPath(view));
  document.querySelectorAll(".screen").forEach(item => item.classList.remove("active"));
  document.querySelectorAll("[data-review-view]").forEach(item => item.classList.toggle("active", item.dataset.reviewView === view));
  const screen = ensureReviewScreen("screen-expert-workspace");
  screen.classList.add("active");
  if (view === "dashboard") return renderExpertDashboard(screen);
  if (view === "reviewed") return renderReviewedDocuments(screen);
  return renderExpertDocumentList(screen, view);
}

async function renderExpertDashboard(screen) {
  setReviewHeader("Expert Dashboard", "Các văn bản được phân công để đánh giá");
  screen.innerHTML = `<div class="review-workspace"><div class="review-loading">Loading dashboard...</div></div>`;
  try {
    const stats = await reviewRequest("/expert/dashboard");
    screen.innerHTML = `<div class="review-workspace">
      <div class="review-kpis">
        ${reviewKpi("ICD-10 Labeled Documents", stats.icd10LabeledDocuments)}
        ${reviewKpi("AI Labeled Documents", stats.aiLabeledDocuments)}
        ${reviewKpi("Reviewed", stats.reviewed)}
        ${reviewKpi("Pending Review", stats.pendingReview)}
      </div>
      <div class="review-panel"><h3>Hướng dẫn</h3><p>Chỉ những văn bản đã có mã ICD-10 hoặc kết quả AI được lưu mới xuất hiện trong danh sách review. Các văn bản chưa gán nhãn không được gửi từ backend đến tài khoản Expert.</p></div>
    </div>`;
  } catch (error) {
    screen.innerHTML = `<div class="review-workspace review-error">Unable to load dashboard: ${reviewEscape(error.message)}</div>`;
  }
}

function reviewKpi(label, value) {
  return `<article class="review-kpi"><div class="review-kpi-label">${reviewEscape(label)}</div><div class="review-kpi-value">${Number(value || 0).toLocaleString("vi-VN")}</div></article>`;
}

function listEndpoint(view) {
  return view === "icd10" ? "/expert/documents/icd10" : "/expert/documents/ai-labeled";
}

function listTitle(view) {
  return view === "icd10" ? "ICD-10 Labeled Documents" : "AI Labeled Documents";
}

async function renderExpertDocumentList(screen, view) {
  setReviewHeader(listTitle(view), "Tìm kiếm và đánh giá văn bản corpus");
  screen.innerHTML = `<div class="review-workspace"><div class="review-panel">
    <h3>${listTitle(view)}</h3>
    <div class="review-filter">
      <input id="reviewQuery" placeholder="Tìm theo tiêu đề hoặc tác giả">
      ${view === "icd10" ? '<input id="reviewIcd" placeholder="Lọc mã ICD-10">' : '<span></span>'}
      <select id="reviewStatusFilter"><option value="">Tất cả trạng thái</option><option value="pending">Pending</option><option value="reviewed">Reviewed</option></select>
      <button class="review-action" onclick="loadExpertDocumentPage('${view}', 1)">Tìm</button>
    </div>
    <div id="reviewTableArea" class="review-loading">Loading documents...</div>
  </div></div>`;
  await loadExpertDocumentPage(view, 1);
}

async function loadExpertDocumentPage(view, page) {
  reviewPage = page;
  const area = document.getElementById("reviewTableArea");
  if (!area) return;
  area.innerHTML = `<div class="review-loading">Loading documents...</div>`;
  const params = new URLSearchParams({ page: String(page), page_size: "20" });
  const query = document.getElementById("reviewQuery")?.value.trim() || "";
  const status = document.getElementById("reviewStatusFilter")?.value || "";
  const icd = document.getElementById("reviewIcd")?.value.trim() || "";
  if (query) params.set("q", query);
  if (status) params.set("review_status", status);
  if (icd) params.set("icd", icd);
  try {
    params.set("_refresh", String(Date.now()));
    const data = await reviewRequest(`${listEndpoint(view)}?${params}`);
    area.innerHTML = renderDocumentTable(data, view);
  } catch (error) {
    area.innerHTML = `<div class="review-error">Unable to load documents: ${reviewEscape(error.message)}</div>`;
  }
}

function renderDocumentTable(data, view) {
  const items = data.items || [];
  if (!items.length) return `<div class="review-empty">No documents available for review.</div>`;
  const heading = view === "icd10"
    ? "<th>ICD-10 Code</th><th>ICD-10 Label</th>"
    : "<th>AI Prediction</th><th>Confidence</th>";
  const rows = items.map(item => {
    const middle = view === "icd10"
      ? `<td>${reviewEscape(item.icd10_codes || "—")}</td><td>${reviewEscape(item.icd10_labels || "—")}</td>`
      : `<td>${reviewEscape([item.primary_icd10_code, item.primary_icd10_label].filter(Boolean).join(" - ") || "AI entities saved (no ICD-10 code)")}</td><td>${item.confidence == null ? "Không có dữ liệu" : `${Math.round(Number(item.confidence) * 100)}%`}</td>`;
    const src = view === "icd10" ? "icd10" : "ai";
    return `<tr><td>${item.id}</td><td class="document-cell">${reviewEscape(item.title || "Không có tiêu đề")}<div class="muted">${reviewEscape(item.authors || "")}</div></td>${middle}<td>${statusBadge(item.reviewStatus)}</td><td><button class="review-action" onclick="openExpertReview(${Number(item.id)}, true, '${src}')">Review</button></td></tr>`;
  }).join("");
  return `<div class="review-table-wrap"><table class="review-table"><thead><tr><th>ID</th><th>Document</th>${heading}<th>Review Status</th><th>Action</th></tr></thead><tbody>${rows}</tbody></table></div>${renderPager(data, `loadExpertDocumentPage('${view}', __PAGE__)`)}`;
}

function renderPager(data, callback) {
  const page = Number(data.page || 1), size = Number(data.pageSize || 20), total = Number(data.total || 0);
  const pages = Math.max(1, Math.ceil(total / size));
  return `<div class="review-pagination"><span>Trang ${page}/${pages} · ${total} văn bản</span><button ${page <= 1 ? "disabled" : ""} onclick="${callback.replace("__PAGE__", page - 1)}">Trước</button><button ${page >= pages ? "disabled" : ""} onclick="${callback.replace("__PAGE__", page + 1)}">Sau</button></div>`;
}

async function renderReviewedDocuments(screen) {
  setReviewHeader("Reviewed Documents", "Lịch sử review của bạn");
  screen.innerHTML = `<div class="review-workspace"><div class="review-panel"><h3>Reviewed</h3><div class="review-filter"><input id="reviewedQuery" placeholder="Tìm theo tiêu đề"><span></span><span></span><button class="review-action" onclick="loadReviewedPage(1)">Tìm</button></div><div id="reviewTableArea" class="review-loading">Loading reviews...</div></div></div>`;
  await loadReviewedPage(1);
}

async function loadReviewedPage(page) {
  const area = document.getElementById("reviewTableArea");
  if (!area) return;
  const params = new URLSearchParams({ page: String(page), page_size: "20" });
  const q = document.getElementById("reviewedQuery")?.value.trim() || "";
  if (q) params.set("q", q);
  try {
    const data = await reviewRequest(`/expert/documents/reviewed?${params}`);
    const rows = (data.items || []).map(item => `<tr><td class="document-cell">${reviewEscape(item.title)}</td><td>${reviewEscape([item.suggested_icd10_code, item.suggested_icd10_label].filter(Boolean).join(" - ") || "—")}</td><td>${statusBadge(item.review_status)}</td><td>${reviewEscape(item.comment)}</td><td>${reviewDate(item.reviewed_at)}</td><td><button class="review-action" onclick="openExpertReview(${Number(item.id)})">View Review</button></td></tr>`).join("");
    area.innerHTML = rows ? `<div class="review-table-wrap"><table class="review-table"><thead><tr><th>Document</th><th>Label</th><th>Review Result</th><th>Comment</th><th>Reviewed At</th><th>Action</th></tr></thead><tbody>${rows}</tbody></table></div>${renderPager(data, "loadReviewedPage(__PAGE__)")}` : '<div class="review-empty">No reviewed documents yet.</div>';
  } catch (error) { area.innerHTML = `<div class="review-error">Unable to load reviews: ${reviewEscape(error.message)}</div>`; }
}

async function openExpertReview(documentId, push = true, labelSource = "icd10") {
  expertCurrentLabelSource = labelSource || "icd10";
  if (push && location.pathname !== `/expert/review/${documentId}`) {
    history.pushState({}, "", `/expert/review/${documentId}`);
  }
  const screen = ensureReviewScreen("screen-expert-workspace");
  document.querySelectorAll(".screen").forEach(item => item.classList.remove("active"));
  screen.classList.add("active");
  setReviewHeader("Document Review", `Văn bản #${documentId}`);
  screen.innerHTML = '<div class="review-workspace review-loading">Loading document...</div>';
  try {
    const document = await reviewRequest(`/expert/documents/${documentId}`);
    screen.innerHTML = renderReviewPage(document, true);
    removeReviewHistoryPanel(screen);
  } catch (error) { screen.innerHTML = `<div class="review-workspace review-error">Unable to load document: ${reviewEscape(error.message)}</div>`; }
}

const AI_LABEL_CATEGORY_STYLE = {
  "Bệnh lý": "disease",
  "Triệu chứng": "symptom",
  "Điều trị": "treatment",
  "Xét nghiệm": "labtest",
  "Hình ảnh": "imaging",
  "Sinh lý": "physiology",
  Disease: "disease",
  Symptom: "symptom",
  Cause: "cause",
  Diagnostic: "diagnostic",
  Treatment: "treatment",
  DISEASE: "disease",
  SYMPTOM: "symptom",
  CAUSE: "cause",
  DIAGNOSTIC_PROCEDURE: "diagnostic",
  TREATMENT: "treatment",
};

const AI_LABEL_CATEGORY_NAME = {
  "Bệnh lý": "Disease",
  "Triệu chứng": "Symptom",
  "Điều trị": "Treatment",
  "Xét nghiệm": "Diagnostic",
  "Hình ảnh": "Diagnostic",
  "Sinh lý": "Cause",
  Disease: "Disease",
  Symptom: "Symptom",
  Cause: "Cause",
  Diagnostic: "Diagnostic",
  Treatment: "Treatment",
  DISEASE: "Disease",
  SYMPTOM: "Symptom",
  CAUSE: "Cause",
  DIAGNOSTIC_PROCEDURE: "Diagnostic",
  TREATMENT: "Treatment",
};

function aiResultEntries(payload) {
  const entries = [];
  if (!payload || typeof payload !== "object") return entries;
  for (const [category, values] of Object.entries(payload)) {
    if (!Array.isArray(values)) continue;
    for (const raw of values) {
      const item = typeof raw === "string" ? { term: raw, spans: [] } : raw;
      if (!item || typeof item !== "object" || !String(item.term || "").trim()) continue;
      entries.push({ category: AI_LABEL_CATEGORY_NAME[category] || category, item, style: AI_LABEL_CATEGORY_STYLE[category] || "other" });
    }
  }
  return entries;
}

function normalizeAiCode(value) {
  return String(value || "").trim().toLowerCase().replace(/[^a-z0-9]/g, "");
}

function normalizeAiLabel(value) {
  return String(value || "").trim().toLocaleLowerCase("vi-VN").replace(/\s+/g, " ");
}

function aiEntryMatchesCurrentLabel(entry, currentLabels) {
  const item = entry.item || {};
  const current = Array.isArray(currentLabels) ? currentLabels : [];
  const currentCodes = new Set(
    current.map(label => normalizeAiCode(label.code)).filter(Boolean),
  );
  const itemCode = normalizeAiCode(item.code || item.icd10_code || item.icd10Code);
  if (itemCode && currentCodes.has(itemCode)) return true;

  // A term match is intentionally limited to disease entities. Other AI
  // categories may legitimately share a word with an ICD-10 label.
  if (entry.category !== "Bệnh lý") return false;
  const currentLabelsByName = new Set(
    current.map(label => normalizeAiLabel(label.label)).filter(Boolean),
  );
  return [
    item.term,
    item.text,
    item.label,
    item.label_vn,
    item.canonical_label,
  ].some(value => {
    const normalized = normalizeAiLabel(value);
    return normalized && currentLabelsByName.has(normalized);
  });
}

function filterAiPayloadByCurrentLabels(payload, currentLabels) {
  if (!payload || typeof payload !== "object") return {};
  return Object.fromEntries(
    Object.entries(payload).map(([category, values]) => [
      category,
      Array.isArray(values)
        ? values.filter(raw => !aiEntryMatchesCurrentLabel(
          { category, item: typeof raw === "string" ? { term: raw } : raw },
          currentLabels,
        ))
        : values,
    ]),
  );
}

function aiPrimaryMatchesCurrentLabel(ai, currentLabels) {
  if (!ai) return false;
  return aiEntryMatchesCurrentLabel(
    {
      category: "Bệnh lý",
      item: {
        code: ai.primary_icd10_code,
        label: ai.primary_icd10_label,
        term: ai.primary_icd10_label,
      },
    },
    currentLabels,
  );
}

function renderAiText(text, payload) {
  const source = String(text || "Không có abstract được lưu trong database.");
  const candidates = [];
  for (const entry of aiResultEntries(payload)) {
    const spans = Array.isArray(entry.item.spans) ? entry.item.spans : [];
    for (const span of spans) {
      const start = Number(span?.start), end = Number(span?.end);
      if (!Number.isInteger(start) || !Number.isInteger(end) || start < 0 || end <= start || end > source.length) continue;
      candidates.push({ start, end, style: entry.style, category: entry.category, term: String(entry.item.term) });
    }
  }
  candidates.sort((a, b) => (b.end - b.start) - (a.end - a.start) || a.start - b.start);
  const accepted = [];
  for (const candidate of candidates) {
    if (!accepted.some(item => candidate.start < item.end && candidate.end > item.start)) accepted.push(candidate);
  }
  accepted.sort((a, b) => a.start - b.start);
  if (!accepted.length) return reviewEscape(source);
  let cursor = 0;
  const html = [];
  for (const match of accepted) {
    html.push(reviewEscape(source.slice(cursor, match.start)));
    html.push(`<mark class="ai-highlight ai-${match.style}" title="${reviewEscape(match.category)}: ${reviewEscape(match.term)}">${reviewEscape(source.slice(match.start, match.end))}</mark>`);
    cursor = match.end;
  }
  html.push(reviewEscape(source.slice(cursor)));
  return html.join("");
}

function renderNerText(text, nerLabels) {
  const source = String(text || "Không có abstract được lưu trong database.");
  if (!Array.isArray(nerLabels) || !nerLabels.length) return reviewEscape(source);
  // Gom các term NER duy nhất (dùng label làm từ khóa tìm kiếm)
  const terms = [...new Map(nerLabels.map(item => [String(item.label || "").trim().toLowerCase(), item])).values()]
    .filter(item => item.label && item.label.trim());
  // Tìm tất cả vị trí xuất hiện của mỗi term trong text (case-insensitive)
  const candidates = [];
  for (const item of terms) {
    const term = item.label.trim();
    const lower = source.toLowerCase();
    const termLower = term.toLowerCase();
    let idx = 0;
    while ((idx = lower.indexOf(termLower, idx)) !== -1) {
      candidates.push({ start: idx, end: idx + term.length, code: item.code || "", label: term, type: item.type || "" });
      idx += term.length;
    }
  }
  // Loại bỏ chồng lấp — ưu tiên match dài hơn
  candidates.sort((a, b) => (b.end - b.start) - (a.end - a.start) || a.start - b.start);
  const accepted = [];
  for (const c of candidates) {
    if (!accepted.some(a => c.start < a.end && c.end > a.start)) accepted.push(c);
  }
  accepted.sort((a, b) => a.start - b.start);
  if (!accepted.length) return reviewEscape(source);
  let cursor = 0;
  const html = [];
  for (const match of accepted) {
    html.push(reviewEscape(source.slice(cursor, match.start)));
    const title = [match.code, match.type].filter(Boolean).join(" · ");
    html.push(`<mark class="ai-highlight ai-disease" title="${reviewEscape(title || "NER")}: ${reviewEscape(match.label)}">${reviewEscape(source.slice(match.start, match.end))}</mark>`);
    cursor = match.end;
  }
  html.push(reviewEscape(source.slice(cursor)));
  return html.join("");
}

function renderFullAiResult(payload) {
  const entries = aiResultEntries(payload);
  if (!entries.length) return '<div class="review-empty">AI không trả về thực thể nào.</div>';
  const groups = new Map();
  for (const entry of entries) {
    if (!groups.has(entry.category)) groups.set(entry.category, []);
    groups.get(entry.category).push(entry);
  }
  return `<div class="ai-result-groups">${[...groups.entries()].map(([category, group]) => `
    <section class="ai-result-group"><h4>${reviewEscape(category)} <span>${group.length}</span></h4><div class="ai-result-tags">
      ${group.map(({ item, style }) => `<span class="ai-result-tag ai-${style}">${reviewEscape(item.term)}${item.code ? `<small>${reviewEscape(item.code)}</small>` : ""}</span>`).join("")}
    </div></section>`).join("")}</div>`;
}

const aiAnalyzeState = {};

function aiLabelsForDocument(documentData) {
  return Array.isArray(documentData.aiLabels) && documentData.aiLabels.length
    ? documentData.aiLabels
    : (documentData.aiLabel ? [documentData.aiLabel] : []);
}

function aiAnnotationSeed(documentData) {
  const label = aiLabelsForDocument(documentData)[0];
  if (!label) return [];
  return aiResultEntries(label.labels || {}).map(({ item, category }) => ({
    text: item.term || item.text || item.label_vn || item.label || "",
    label: item.term || item.text || item.label_vn || item.label || "",
    code: item.code || item.icd10_code || item.icd10Code || "",
    type: { Disease: "DISEASE", Symptom: "SYMPTOM", Cause: "CAUSE", Diagnostic: "DIAGNOSTIC_PROCEDURE", Treatment: "TREATMENT" }[category] || category || "DISEASE",
    action: "KEEP"
  })).filter(item => item.text);
}

function aiLabelDisplayName(label, index) {
  const model = String(label?.model_name || `Model ${index + 1}`).trim();
  const created = label?.created_at ? ` · ${reviewDate(label.created_at)}` : "";
  return `${model}${created}`;
}

function aiEntryKey(entry) {
  const item = entry.item || {};
  const code = normalizeAiCode(item.code || item.icd10_code || item.icd10Code);
  const term = normalizeAiLabel(item.term || item.text || item.label_vn || item.label);
  return `${normalizeAiLabel(entry.category)}|${code}|${term}`;
}

function aiEntrySpan(entry) {
  const spans = Array.isArray(entry.item?.spans) ? entry.item.spans : [];
  const first = spans.find(span => Number.isInteger(Number(span?.start)) && Number.isInteger(Number(span?.end)));
  return first ? { start: Number(first.start), end: Number(first.end) } : null;
}

function aiDifferenceRows(leftLabel, rightLabel) {
  const leftEntries = aiResultEntries(leftLabel?.labels || {});
  const rightEntries = aiResultEntries(rightLabel?.labels || {});
  const leftKeys = new Map(leftEntries.map(entry => [aiEntryKey(entry), entry]));
  const rightKeys = new Map(rightEntries.map(entry => [aiEntryKey(entry), entry]));
  const rows = [];
  for (const [key, entry] of leftKeys.entries()) if (!rightKeys.has(key)) rows.push({ side: "left", key, entry });
  for (const [key, entry] of rightKeys.entries()) if (!leftKeys.has(key)) rows.push({ side: "right", key, entry });
  return rows;
}

function renderAiModelOptions(labels, selectedId, fallbackIndex) {
  return labels.map((label, index) => {
    const selected = Number(label.id) === Number(selectedId || labels[fallbackIndex]?.id) ? "selected" : "";
    return `<option value="${Number(label.id)}" ${selected}>${reviewEscape(aiLabelDisplayName(label, index))}</option>`;
  }).join("");
}

function renderFullAiResultWithDiffs(payload, diffKeys, side) {
  const entries = aiResultEntries(payload);
  if (!entries.length) return '<div class="review-empty">AI không trả về thực thể nào.</div>';
  const groups = new Map();
  for (const entry of entries) {
    if (!groups.has(entry.category)) groups.set(entry.category, []);
    groups.get(entry.category).push(entry);
  }
  return `<div class="ai-result-groups">${[...groups.entries()].map(([category, group]) => `
    <section class="ai-result-group"><h4>${reviewEscape(category)} <span>${group.length}</span></h4><div class="ai-result-tags">
      ${group.map(entry => {
        const item = entry.item || {};
        const diff = diffKeys.has(aiEntryKey(entry)) ? " ai-result-diff" : "";
        const span = aiEntrySpan(entry);
        const attrs = span ? ` onclick="focusAiDifference(${span.start}, ${span.end}, '${side}')"` : "";
        return `<button type="button" class="ai-result-tag ai-${entry.style}${diff}"${attrs}>${reviewEscape(item.term || item.text)}${item.code ? `<small>${reviewEscape(item.code)}</small>` : ""}</button>`;
      }).join("")}
    </div></section>`).join("")}</div>`;
}

function renderAiExtractionColumn(documentData, side, label, differences, selectedId) {
  const labels = aiLabelsForDocument(documentData);
  const diffKeys = new Set(differences.filter(row => row.side === side).map(row => row.key));
  const selectId = side === "left" ? "aiModelA" : "aiModelB";
  const fallbackIndex = side === "left" ? 0 : 1;
  const body = label ? renderFullAiResultWithDiffs(label.labels || {}, diffKeys, side) : '<div class="review-empty">Chưa có dữ liệu AI.</div>';
  const primary = [label?.primary_icd10_code, label?.primary_icd10_label].filter(Boolean).join(" - ") || "Không có ICD-10 chính";
  const confidence = label?.confidence == null ? "Không có confidence" : `${Math.round(Number(label.confidence) * 100)}% confidence`;
  return `<section class="review-panel ai-extraction-column" data-ai-side="${side}"><div class="ai-column-head"><h3>AI Extraction</h3><select id="${selectId}" class="ai-model-select">${renderAiModelOptions(labels, selectedId, fallbackIndex)}</select></div><div class="ai-model-meta"><strong>${reviewEscape(primary)}</strong><span>${reviewEscape(confidence)}</span></div>${body}</section>`;
}

function renderAiPredictionPanel(documentData) {
  const label = aiLabelsForDocument(documentData)[0];
  if (!label) return '<div class="review-empty">No saved AI prediction.</div>';
  return `<h3>AI Prediction</h3>${renderFullAiResult(label.labels || {})}`;
}

function renderAiAnalysisPanel(differences) {
  if (!differences.length) return '<section class="review-panel ai-analysis-panel"><h3>Analyze Differences</h3><div class="review-empty">Không có khác biệt sau khi Analyze.</div></section>';
  return `<section class="review-panel ai-analysis-panel"><div class="ai-analysis-head"><h3>Analyze Differences</h3><span>${differences.length} khác biệt</span></div><div class="ai-diff-list">${differences.map((row, index) => {
    const item = row.entry.item || {};
    const span = aiEntrySpan(row.entry);
    const jump = span ? `focusAiDifference(${span.start}, ${span.end}, '${row.side}')` : "void(0)";
    return `<article class="ai-diff-card"><button type="button" onclick="${jump}"><strong>${reviewEscape(item.term || item.text || "—")}</strong><span>${row.side === "left" ? "Chỉ model A có" : "Chỉ model B có"} · ${reviewEscape(row.entry.category)}${item.code ? ` · ${reviewEscape(item.code)}` : ""}</span></button><label>NOTE lỗi sai<textarea id="aiDiffNote${index}" placeholder="Ghi lỗi, dấu hiệu nhận biết, và cách sửa prompt..."></textarea></label><div class="ai-prompt-tools"><select id="aiDiffWrong${index}"><option value="left">Model A sai</option><option value="right">Model B sai</option><option value="prompt">Prompt gốc thiếu ràng buộc</option></select><button type="button" class="review-action" onclick="draftPromptFix(${index})">Tạo gợi ý sửa prompt</button></div><div class="ai-prompt-suggestion" id="aiPromptSuggestion${index}" data-term="${reviewEscape(item.term || item.text || "")}"></div></article>`;
  }).join("")}</div></section>`;
}

function renderAiComparableText(text, differences) {
  const source = String(text || "Không có abstract được lưu trong database.");
  const spans = differences.map(row => aiEntrySpan(row.entry)).filter(Boolean);
  spans.sort((a, b) => (b.end - b.start) - (a.end - a.start) || a.start - b.start);
  const accepted = [];
  for (const span of spans) {
    if (span.end <= span.start || span.end > source.length) continue;
    if (!accepted.some(item => span.start < item.end && span.end > item.start)) accepted.push(span);
  }
  accepted.sort((a, b) => a.start - b.start);
  if (!accepted.length) return reviewEscape(source);
  let cursor = 0;
  const html = [];
  for (const span of accepted) {
    html.push(reviewEscape(source.slice(cursor, span.start)));
    html.push(`<mark class="ai-highlight ai-diff-text" data-ai-start="${span.start}" data-ai-end="${span.end}">${reviewEscape(source.slice(span.start, span.end))}</mark>`);
    cursor = span.end;
  }
  html.push(reviewEscape(source.slice(cursor)));
  return html.join("");
}

function focusAiDifference(start, end, side) {
  const source = document.getElementById("documentTextSource");
  source?.querySelectorAll(".ai-focused-range").forEach(node => node.classList.remove("ai-focused-range"));
  const mark = source?.querySelector(`[data-ai-start="${start}"][data-ai-end="${end}"]`);
  if (mark) {
    mark.classList.add("ai-focused-range");
    mark.scrollIntoView({ block: "center", behavior: "smooth" });
  }
  document.querySelector(`[data-ai-side="${side}"]`)?.scrollIntoView({ block: "nearest", behavior: "smooth" });
}

function draftPromptFix(index) {
  const note = document.getElementById(`aiDiffNote${index}`)?.value.trim();
  const wrong = document.getElementById(`aiDiffWrong${index}`)?.value || "prompt";
  const target = document.getElementById(`aiPromptSuggestion${index}`);
  if (!target) return;
  const term = target.dataset.term || "mục đang chọn";
  const reason = note || "Kết quả khác giữa hai model, cần kiểm tra ngữ cảnh và bằng chứng nguồn.";
  target.innerHTML = `<strong>Prompt gợi ý:</strong><p>Khi trích xuất '${reviewEscape(term)}', chỉ trả về nếu văn bản nêu trực tiếp thực thể/số liệu trong đúng ngữ cảnh. Nếu chỉ là tiêu đề, chữ ký, chú thích, hoặc không đủ bằng chứng thì đánh dấu uncertain và giải thích. Lỗi nghi ngờ: ${reviewEscape(wrong)}. Căn cứ sửa: ${reviewEscape(reason)}</p><strong>Dấu hiệu prompt đúng:</strong><p>Kết quả mới phải có span nguồn/trang tương ứng, không thêm dữ liệu ngoài văn bản, và giải thích vì sao giữ hoặc loại bỏ mục này.</p>`;
}

function analyzeAiModels(documentId) {
  aiAnalyzeState[documentId] = {
    leftId: Number(document.getElementById("aiModelA")?.value || 0),
    rightId: Number(document.getElementById("aiModelB")?.value || 0),
    analyzed: true,
  };
  openExpertReview(documentId, false, expertCurrentLabelSource);
}

function renderReviewPage(document, editable) {
  const labels = (document.currentLabels || []).map(item => `<div class="label-card"><strong>${reviewEscape(item.code || "No ICD-10 code")}</strong><span>${reviewEscape(item.label || "—")}</span><div class="muted">${reviewEscape(item.source || "")}</div></div>`).join("") || '<div class="review-empty">Không có nhãn hiện tại.</div>';
  const ai = document.aiLabel;
  // AI Labeled documents must show every AI entity, even when it has no
  // matching ICD-10 dictionary label yet.
  const filteredAiLabels = ai ? (ai.labels || {}) : {};
  const primaryPrediction = ai && !aiPrimaryMatchesCurrentLabel(ai, document.currentLabels)
    ? [ai.primary_icd10_code, ai.primary_icd10_label].filter(Boolean).join(" - ")
    : "";
  const aiContent = ai ? `${primaryPrediction ? `<div class="label-card"><strong>${reviewEscape(primaryPrediction)}</strong></div>` : ""}<h4 class="ai-full-result-title">All AI Entities</h4>${renderFullAiResult(filteredAiLabels)}` : '<div class="review-empty">No saved AI prediction.</div>';
  const history = (document.reviewHistory || []).map(item => `<article class="history-card"><strong>${reviewEscape(item.expert_name || "You")}</strong>${statusBadge(item.review_status)}<div class="muted">${reviewDate(item.created_at)}</div><p><b>Suggested:</b> ${reviewEscape([item.suggested_icd10_code, item.suggested_icd10_label].filter(Boolean).join(" - ") || "—")}</p><p>${reviewEscape(item.comment)}</p></article>`).join("") || '<div class="review-empty">No previous reviews.</div>';
  const form = editable ? `<form class="review-form" onsubmit="return saveExpertReview(event, ${Number(document.id)})"><fieldset><legend>Expert Review</legend><label><input type="radio" name="reviewStatus" value="CORRECT" checked> Correct</label><label><input type="radio" name="reviewStatus" value="INCORRECT"> Incorrect</label><label><input type="radio" name="reviewStatus" value="NEEDS_REVISION"> Needs Revision</label></fieldset><input type="hidden" name="labelSource" value="${reviewEscape(expertCurrentLabelSource)}"><section class="expert-annotation-editor"><div class="expert-editor-heading"><div><strong>Entity annotations</strong><p class="expert-editor-help">Enter the exact entity text. The system will detect its position automatically.</p></div><button type="button" class="review-action" onclick="addExpertAnnotation(${Number(document.id)})">+ Add label</button></div><div id="expertAnnotationRows">${renderExpertAnnotationRows(document)}</div></section><label>Expert Comment<textarea id="expertComment" required minlength="3" maxlength="8000" placeholder="Enter your review/comment here..."></textarea></label><p class="auth-error" id="reviewSaveError"></p><button class="review-save" type="submit">Save Review</button></form>` : '';
  const _backView = ["icd10", "ai-labeled", "reviewed"].includes(reviewActiveView) ? reviewActiveView : "dashboard";
  const backAction = editable
    ? `showExpertView('${_backView}')`
    : currentAuthUser?.role === "reviewer"
      ? "showReviewerView('reviews')"
      : "showAdminReviewView('reviews')";
  const showAiPanel = expertCurrentLabelSource !== "icd10";
  const textHtml = expertCurrentLabelSource === "icd10"
    ? (document.nerResult?.highlightedHtml || renderNerText(document.abstract, document.currentLabels))
    : renderAiText(document.abstract, filteredAiLabels);
  const aiLabels = aiLabelsForDocument(document);
  const state = aiAnalyzeState[document.id] || {};
  const leftAi = aiLabels.find(label => Number(label.id) === Number(state.leftId)) || aiLabels[0] || null;
  const rightAi = aiLabels.find(label => Number(label.id) === Number(state.rightId)) || aiLabels[1] || aiLabels[0] || null;
  const differences = showAiPanel && state.analyzed ? aiDifferenceRows(leftAi, rightAi) : [];
  const comparableText = showAiPanel
    ? (state.analyzed && differences.length
      ? renderAiComparableText(document.abstract, differences)
      : renderAiText(document.abstract, leftAi?.labels || {}))
    : textHtml;
  const aiWorkspace = showAiPanel
    ? `<div class="review-panel ai-prediction-panel">${renderAiPredictionPanel(document)}</div>`
    : "";
  return `<div class="review-workspace"><button class="review-action" onclick="${backAction}">← Quay lại</button><div class="review-layout ${showAiPanel ? "review-layout-ai" : ""}" style="margin-top:14px"><div class="review-panel document-review-main"><h3>${reviewEscape(document.title || "Không có tiêu đề")}</h3><p class="muted">${reviewEscape(document.authors || "Không rõ tác giả")} · ${reviewEscape(document.publication_year || "")}</p><h3 style="margin-top:18px">Original Medical Text</h3><div class="document-text" id="documentTextSource">${comparableText}</div>${form}</div>${showAiPanel ? `<aside class="label-list ai-label-list">${aiWorkspace}</aside>` : `<aside class="label-list"><div class="review-panel"><h3>Current ICD-10 Label</h3>${labels}</div><div class="review-panel"><h3>Review History</h3><div class="history-list">${history}</div></div></aside>`}</div></div>`;
}

function removeReviewHistoryPanel(container) {
  const historyList = container.querySelector(".history-list");
  historyList?.closest(".review-panel")?.remove();
}

function annotationCategories(selected) {
  const values = ["DISEASE", "SYMPTOM", "CAUSE", "DIAGNOSTIC_PROCEDURE", "TREATMENT"];
  return values.map(value => `<option value="${value}" ${value === selected ? "selected" : ""}>${value}</option>`).join("");
}

function renderExpertAnnotationRows(document) {
  const id = Number(document.id);
  if (!expertAnnotationState[id]) {
    const seed = document.annotationSeed?.length ? document.annotationSeed : aiAnnotationSeed(document);
    expertAnnotationState[id] = seed.map(item => ({ ...item, action: item.action || "KEEP" }));
  }
  return expertAnnotationState[id].map((item, index) => `
    <div class="expert-annotation-row" data-index="${index}">
      <button type="button" class="annotation-delete" onclick="removeExpertAnnotation(${id}, ${index})" aria-label="Delete entity">Delete</button>
      <div class="expert-annotation-main">
        <label class="annotation-field annotation-field-text"><span>Entity Text</span><input data-field="text" value="${reviewEscape(item.text)}" placeholder="Example: ptosis"></label>
        <label class="annotation-field annotation-field-action"><span>Action</span><select data-field="action">${["KEEP", "EDIT", "DELETE", "ADD"].map(action => `<option value="${action}" ${item.action === action ? "selected" : ""}>${action}</option>`).join("")}</select></label>
      </div>
      <div class="expert-annotation-fields">
        <label class="annotation-field"><span>Entity Name</span><input data-field="label" value="${reviewEscape(item.label || item.text)}" placeholder="Tên thực thể"></label>
        <label class="annotation-field"><span>ICD-10 Code</span><input data-field="code" value="${reviewEscape(item.code)}" placeholder="Ví dụ: H02.4"></label>
        <label class="annotation-field"><span>Entity Type</span><select data-field="type">${annotationCategories(item.type || item.category)}</select></label>
      </div>
    </div>`).join("") || '<div class="review-empty">No labels yet. Click “Add label” to mark a missing entity.</div>';
}

function collectExpertAnnotations(form, documentId) {
  const rows = [...form.querySelectorAll(".expert-annotation-row")];
  const annotations = rows.map(row => {
    const index = Number(row.dataset.index);
    const previous = expertAnnotationState[documentId]?.[index] || {};
    const value = field => row.querySelector(`[data-field="${field}"]`)?.value ?? "";
    return {
      text: value("text").trim(),
      start: previous.start ?? null,
      end: previous.end ?? null,
      label: value("label").trim(),
      code: value("code").trim(),
      type: value("type"),
      category: value("type"),
      action: value("action"),
    };
  });
  expertAnnotationState[documentId] = annotations;
  return annotations;
}

function addExpertAnnotation(documentId) {
  const form = document.querySelector(".review-form");
  if (form) collectExpertAnnotations(form, documentId);
  (expertAnnotationState[documentId] ||= []).push({
    text: "", start: 0, end: 0, label: "", code: "", type: "DISEASE", action: "ADD",
  });
  const screen = document.getElementById("screen-expert-workspace");
  if (screen && reviewerDetailDocument) return;
  const article = { id: documentId, annotationSeed: expertAnnotationState[documentId] };
  const rows = document.getElementById("expertAnnotationRows");
  if (rows) rows.innerHTML = renderExpertAnnotationRows(article);
}

function removeExpertAnnotation(documentId, index) {
  const form = document.querySelector(".review-form");
  if (form) collectExpertAnnotations(form, documentId);
  expertAnnotationState[documentId].splice(index, 1);
  const rows = document.getElementById("expertAnnotationRows");
  if (rows) rows.innerHTML = renderExpertAnnotationRows({ id: documentId, annotationSeed: expertAnnotationState[documentId] });
}

async function saveExpertReview(event, documentId) {
  event.preventDefault();
  const error = document.getElementById("reviewSaveError");
  error.textContent = "";
  const reviewStatus = document.querySelector('input[name="reviewStatus"]:checked')?.value;
  const labelSource = document.querySelector('input[name="labelSource"]')?.value || "icd10";
  const annotations = collectExpertAnnotations(event.currentTarget, documentId);
  const comment = document.getElementById("expertComment").value.trim();
  try {
    await reviewRequest(`/expert/documents/${documentId}/reviews`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ reviewStatus, labelSource, annotations, comment }) });
    if (typeof showToast === "function") showToast("Review saved successfully.", "success");
    openExpertReview(documentId, false, expertCurrentLabelSource);
  } catch (requestError) { error.textContent = `Unable to save review: ${requestError.message}`; }
  return false;
}

function initializeAdminReviewLinks() {
  const nav = document.querySelector(".sidebar-nav");
  if (document.getElementById("adminReviewNav")) return;
  const extra = document.createElement("div");
  extra.id = "adminReviewNav";
  extra.innerHTML = `<div class="nav-section-label" style="margin-top:12px">Review Administration</div><a class="nav-item" onclick="showAdminReviewView('sessions')"><span class="nav-label">Labeling Sessions</span></a><a class="nav-item" onclick="showAdminReviewView('reviews')"><span class="nav-label">Expert Reviews</span></a><a class="nav-item" onclick="showAdminReviewView('users')"><span class="nav-label">Expert Accounts</span></a><a class="nav-item" onclick="showAdminReviewView('reviewers')"><span class="nav-label">Reviewer Accounts</span></a>`;
  nav.appendChild(extra);
}

async function showAdminReviewView(view) {
  if (view === "sessions") history.replaceState({}, "", "#labeling-sessions");
  document.querySelectorAll(".screen").forEach(item => item.classList.remove("active"));
  document.querySelectorAll(".sidebar-nav .nav-item").forEach(item => item.classList.remove("active"));
  document.querySelector(`[onclick="showAdminReviewView('${view}')"]`)?.classList.add("active");
  const screen = ensureReviewScreen("screen-admin-review-workspace");
  screen.classList.add("active");
  if (view === "sessions") return renderLabelingSessionPage(screen);
  setReviewHeader(
    view === "reviews" ? "Expert Reviews" : view === "reviewers" ? "Tài khoản Reviewer" : "Tài khoản chuyên gia",
    view === "reviews" ? "Quản trị dữ liệu review" : view === "reviewers" ? "Cấp tài khoản xem kết quả review" : "Cấp và quản lý tài khoản dành cho chuyên gia",
  );
  screen.innerHTML = `<div class="review-workspace"><div id="adminReviewArea"><div class="review-loading">Đang tải dữ liệu...</div></div></div>`;
  try {
    const data = await reviewRequest(
      view === "reviews"
        ? "/admin/reviews?page_size=50"
        : `/admin/users?page_size=50&role=${view === "reviewers" ? "REVIEWER" : "EXPERT"}`
    );
    const rows = (data.items || []).map(item => view === "reviews"
      ? `<tr><td class="document-cell">${reviewEscape(item.document_title)}</td><td>${reviewEscape(item.expert_name)}</td><td>${statusBadge(item.review_status)}</td><td>${reviewEscape([item.suggested_icd10_code, item.suggested_icd10_label].filter(Boolean).join(" - ") || "—")}</td><td>${reviewEscape(item.comment)}</td><td>${reviewDate(item.created_at)}</td><td><button class="review-action" onclick="openAdminReviewDocument(${Number(item.document_id)})">View</button></td></tr>`
      : `<tr><td class="account-id">#${item.id}</td><td class="account-name">${reviewEscape(item.name)}</td><td>${reviewEscape(item.email)}</td><td>${userRoleBadge(item.role)}</td><td>${accountStatusBadge(item.is_active)}</td><td>${Number(item.review_count) || 0}</td><td>${reviewDate(item.created_at)}</td></tr>`).join("");
    const headings = view === "reviews" ? "<th>Document</th><th>Expert</th><th>Review</th><th>Suggested Label</th><th>Comment</th><th>Date</th><th>Action</th>" : "<th>ID</th><th>Họ và tên</th><th>Email</th><th>Vai trò</th><th>Trạng thái</th><th>Lượt review</th><th>Ngày tạo</th>";
    const table = rows ? `<div class="review-table-wrap"><table class="review-table"><thead><tr>${headings}</tr></thead><tbody>${rows}</tbody></table></div>` : '<div class="review-empty">No data available.</div>';
    const area = document.getElementById("adminReviewArea");
    area.innerHTML = view === "users" || view === "reviewers"
      ? renderAdminUsersPage(table, Number(data.total) || 0, view)
      : `<div class="review-panel"><h3>Expert Reviews</h3>${table}</div>`;
  } catch (error) { document.getElementById("adminReviewArea").innerHTML = `<div class="review-error">Unable to load: ${reviewEscape(error.message)}</div>`; }
}

async function renderLabelingSessionPage(screen) {
  setReviewHeader("Labeling Sessions", "Assign documents to two experts and one reviewer");
  screen.innerHTML = '<div class="review-workspace review-loading">Loading assignment options...</div>';
  try {
    const data = await reviewRequest("/admin/labeling-options");
    window.labelingSessionData = data;
    const articleRows = renderSessionArticles(data.articles || []);
    const expertRows = renderSessionExperts(data.experts || []);
    const reviewerOptions = (data.reviewers || []).map(user => `<option value="${Number(user.id)}">${reviewEscape(user.name)} · ${reviewEscape(user.email)}</option>`).join("");
    screen.innerHTML = `<div class="review-workspace"><section class="review-panel session-card"><div class="session-card-heading"><h2>Create Labeling Session</h2><p class="muted">Assign documents to two experts and one reviewer.</p></div><form id="labelingSessionForm" class="review-form session-form"><div class="session-form-grid"><div class="session-left"><label class="session-field">Session name <span class="required">*</span><input id="sessionName" maxlength="255" placeholder="Medical NER batch 01" aria-required="true"><small class="field-error" id="sessionNameError"></small></label><label class="session-field">Description<textarea id="sessionDescription" maxlength="4000" placeholder="Optional instructions"></textarea></label><div class="session-field"><div class="field-heading"><span>Documents <span class="required">*</span></span><span id="documentCount" class="selection-count">0 selected</span></div><div class="session-toolbar"><input id="documentSearch" type="search" placeholder="Search by title or ID" aria-label="Search documents"><button type="button" class="review-action" onclick="toggleAllSessionDocuments(true)">Select all</button><button type="button" class="review-action" onclick="toggleAllSessionDocuments(false)">Clear</button></div><div id="sessionDocuments" class="checkbox-list" role="group" aria-label="Documents">${articleRows}</div><small class="field-error" id="documentsError"></small></div></div><div class="session-right"><div class="session-field"><div class="field-heading"><span>Experts <span class="required">*</span></span><span class="muted selection-help">Select exactly two</span><span id="expertCount" class="selection-count">0/2 selected</span></div><div id="sessionExperts" class="checkbox-list experts-list" role="group" aria-label="Experts">${expertRows}</div><small class="field-error" id="expertsError"></small></div><label class="session-field">Reviewer <span class="required">*</span><select id="sessionReviewer" aria-required="true"><option value="">Select reviewer</option>${reviewerOptions}</select><small class="field-error" id="reviewerError"></small></label><div class="session-summary" id="sessionSummary">0 documents · 0 experts · 0 reviewers</div></div></div><div class="session-actions"><label class="email-toggle"><input id="sendAssignmentEmail" type="checkbox" checked> Send email notifications to experts</label><span id="sessionMessage" class="session-message"></span><button class="review-save" type="submit">Create session</button></div></form></section></div>`;
    document.getElementById("labelingSessionForm").onsubmit = submitLabelingSession;
    document.getElementById("documentSearch").oninput = event => filterSessionDocuments(event.target.value);
    document.getElementById("sessionDocuments").onchange = updateSessionControls;
    document.getElementById("sessionExperts").onchange = updateSessionControls;
    document.getElementById("sessionReviewer").onchange = updateSessionControls;
    updateSessionControls();
  } catch (error) {
    screen.innerHTML = `<div class="review-workspace review-error">Unable to load assignment options: ${reviewEscape(error.message)}</div>`;
  }
}

function renderSessionArticles(articles) {
  return articles.map(article => { const assigned = Number(article.assigned_sessions || 0) > 0; return `<label class="checkbox-row document-row" data-search="${reviewEscape(`#${article.id} ${article.title || ""}`.toLowerCase())}"><input type="checkbox" name="sessionArticle" value="${Number(article.id)}"><span class="checkbox-copy" title="${reviewEscape(article.title || "Untitled")}"><strong>#${Number(article.id)}</strong> ${reviewEscape(article.title || "Untitled")}</span><span class="assignment-status ${assigned ? "assigned" : "available"}">${assigned ? "Assigned" : "Available"}</span></label>`; }).join("") || '<div class="session-empty">No documents available.</div>';
}

function renderSessionExperts(experts) {
  return experts.map(user => `<label class="checkbox-row"><input type="checkbox" name="sessionExpert" value="${Number(user.id)}"><span class="checkbox-copy"><strong>${reviewEscape(user.name)}</strong><small>${reviewEscape(user.email)}</small></span></label>`).join("") || '<div class="session-empty">No experts available.</div>';
}

function filterSessionDocuments(query) {
  const value = String(query || "").toLowerCase().trim();
  document.querySelectorAll("#sessionDocuments .document-row").forEach(row => { row.hidden = value && !row.dataset.search.includes(value); });
}

function toggleAllSessionDocuments(checked) {
  document.querySelectorAll("#sessionDocuments input[name=sessionArticle]").forEach(input => { if (!input.closest(".document-row").hidden) input.checked = checked; });
  updateSessionControls();
}

function updateSessionControls() {
  const articles = [...document.querySelectorAll("#sessionDocuments input:checked")];
  const experts = [...document.querySelectorAll("#sessionExperts input:checked")];
  document.getElementById("documentCount").textContent = `${articles.length} selected`;
  document.getElementById("expertCount").textContent = `${experts.length}/2 selected`;
  document.querySelectorAll("#sessionExperts input:not(:checked)").forEach(input => { input.disabled = experts.length >= 2; });
  const reviewer = document.getElementById("sessionReviewer");
  [...reviewer.options].forEach(option => { option.disabled = experts.some(expert => expert.value === option.value); });
  if (reviewer.selectedOptions[0]?.disabled) reviewer.value = "";
  document.getElementById("sessionSummary").textContent = `${articles.length} documents · ${experts.length} experts · ${reviewer.value ? 1 : 0} reviewer`;
}

async function submitLabelingSession(event) {
  event.preventDefault();
  const submitButton = event.currentTarget.querySelector('button[type="submit"]');
  const message = document.getElementById("sessionMessage");
  message.textContent = "";
  const articleIds = [...document.querySelectorAll("#sessionDocuments input:checked")].map(input => Number(input.value));
  const expertIds = [...document.querySelectorAll("#sessionExperts input:checked")].map(input => Number(input.value));
  const reviewerId = Number(document.getElementById("sessionReviewer").value);
  document.querySelectorAll(".field-error").forEach(node => node.textContent = "");
  let valid = true;
  const sessionName = document.getElementById("sessionName");
  const reviewer = document.getElementById("sessionReviewer");
  sessionName.removeAttribute("aria-invalid"); reviewer.removeAttribute("aria-invalid");
  if (!sessionName.value.trim()) { document.getElementById("sessionNameError").textContent = "Session name is required."; sessionName.setAttribute("aria-invalid", "true"); valid = false; }
  if (!articleIds.length) { document.getElementById("documentsError").textContent = "Select at least one document."; valid = false; }
  if (expertIds.length !== 2) { document.getElementById("expertsError").textContent = "Select exactly two experts."; valid = false; }
  if (!reviewerId) { document.getElementById("reviewerError").textContent = "Select a reviewer."; reviewer.setAttribute("aria-invalid", "true"); valid = false; }
  if (!valid) return false;
  submitButton.disabled = true;
  submitButton.textContent = "Creating...";
  try {
    const result = await reviewRequest("/admin/labeling-sessions", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ name: document.getElementById("sessionName").value.trim(), description: document.getElementById("sessionDescription").value.trim(), articleIds, expertIds, reviewerId, sendEmail: document.getElementById("sendAssignmentEmail").checked }) });
    message.className = "reviewer-save-success";
    message.textContent = result.emailStatus === "sent" ? "Session created. Email notifications sent to both experts." : "Session created. SMTP is not configured, so no email was sent.";
  } catch (error) { message.textContent = error.message; }
  finally { submitButton.disabled = false; submitButton.textContent = "Create session"; }
  return false;
}

function userRoleBadge(role) {
  const normalized = String(role || "expert").toLowerCase();
  const label = normalized === "admin" ? "Admin" : normalized === "reviewer" ? "Reviewer" : "Expert";
  return `<span class="account-role account-role-${reviewEscape(normalized)}">${label}</span>`;
}

function accountStatusBadge(isActive) {
  return isActive
    ? '<span class="account-status account-status-active"><i></i>Đang hoạt động</span>'
    : '<span class="account-status account-status-inactive"><i></i>Đã khóa</span>';
}

function renderAdminUsersPage(table, total, view = "users") {
  const reviewer = view === "reviewers";
  return `<div class="admin-accounts-page">
    <section class="admin-account-summary">
      <div class="admin-summary-icon" aria-hidden="true">
        <svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M16 21v-2a4 4 0 0 0-4-4H6a4 4 0 0 0-4 4v2"/><circle cx="9" cy="7" r="4"/><path d="M19 8v6M22 11h-6"/></svg>
      </div>
      <div><h2>${reviewer ? "Quản lý tài khoản Reviewer" : "Quản lý tài khoản chuyên gia"}</h2><p>${reviewer ? "Cấp tài khoản chỉ đọc để theo dõi kết quả review của các chuyên gia." : "Cấp tài khoản truy cập khu vực đánh giá corpus và theo dõi hoạt động review."}</p></div>
      <div class="admin-account-total"><strong>${total}</strong><span>Tổng tài khoản</span></div>
    </section>
    ${reviewer ? renderCreateReviewerForm() : renderCreateExpertForm()}
    <section class="review-panel admin-users-list">
      <div class="admin-card-header admin-list-header">
        <div><h3>Danh sách tài khoản</h3><p>Thông tin tài khoản đang được lưu trong cơ sở dữ liệu MySQL.</p></div>
        <span class="admin-count-badge">${total} tài khoản</span>
      </div>
      ${table}
    </section>
  </div>`;
}

function renderCreateExpertForm() {
  return `<section class="review-panel admin-user-create-card">
    <div class="admin-card-header">
      <div class="admin-card-title">
        <span class="admin-card-icon" aria-hidden="true">
          <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M15 21v-2a4 4 0 0 0-4-4H5a4 4 0 0 0-4 4v2"/><circle cx="8" cy="7" r="4"/><path d="M19 8v6M22 11h-6"/></svg>
        </span>
        <div><h3>Tạo tài khoản chuyên gia</h3><p>Tài khoản mới được cấp cố định vai trò Expert và có thể đăng nhập ngay sau khi tạo.</p></div>
      </div>
      <span class="admin-security-badge">
        <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="3" y="11" width="18" height="10" rx="2"/><path d="M7 11V7a5 5 0 0 1 10 0v4"/></svg>
        Chỉ Admin
      </span>
    </div>
    <form class="admin-user-form" onsubmit="return createExpertAccount(event)" novalidate>
      <label><span>Họ và tên</span><input id="adminExpertName" type="text" autocomplete="name" minlength="2" maxlength="200" placeholder="Ví dụ: Nguyễn Văn An" required></label>
      <label><span>Email đăng nhập</span><input id="adminExpertEmail" type="email" autocomplete="off" maxlength="254" placeholder="chuyengia@example.com" required></label>
      <label><span>Mật khẩu tạm thời</span><input id="adminExpertPassword" type="password" autocomplete="new-password" minlength="10" maxlength="256" placeholder="Tối thiểu 10 ký tự" required></label>
      <label><span>Xác nhận mật khẩu</span><input id="adminExpertConfirmPassword" type="password" autocomplete="new-password" minlength="10" maxlength="256" placeholder="Nhập lại mật khẩu" required></label>
      <p class="auth-error admin-user-form-message" id="adminCreateUserError" role="alert"></p>
      <div class="admin-user-form-footer">
        <span class="admin-password-note">
          <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10"/><path d="m9 12 2 2 4-4"/></svg>
          Mật khẩu được hash trước khi lưu vào database.
        </span>
        <button class="admin-create-button" id="adminCreateUserButton" type="submit">
          <svg width="17" height="17" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M15 21v-2a4 4 0 0 0-4-4H5a4 4 0 0 0-4 4v2"/><circle cx="8" cy="7" r="4"/><path d="M19 8v6M22 11h-6"/></svg>
          Tạo tài khoản Expert
        </button>
      </div>
    </form>
  </section>`;
}

function renderCreateReviewerForm() {
  return `<section class="review-panel admin-user-create-card">
    <div class="admin-card-header">
      <div class="admin-card-title">
        <span class="admin-card-icon" aria-hidden="true">
          <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="8" r="4"/><path d="M4 21a8 8 0 0 1 16 0"/><path d="M19 8h3M20.5 6.5v3"/></svg>
        </span>
        <div><h3>Tạo tài khoản Reviewer</h3><p>Tài khoản chỉ xem dashboard và kết quả review của các chuyên gia.</p></div>
      </div>
      <span class="admin-security-badge">Chỉ Admin</span>
    </div>
    <form class="admin-user-form" onsubmit="return createReviewerAccount(event)" novalidate>
      <label><span>Họ và tên</span><input id="adminReviewerName" type="text" autocomplete="name" minlength="2" maxlength="200" placeholder="Ví dụ: Nguyễn Văn Reviewer" required></label>
      <label><span>Email đăng nhập</span><input id="adminReviewerEmail" type="email" autocomplete="off" maxlength="254" placeholder="reviewer@example.com" required></label>
      <label><span>Mật khẩu tạm thời</span><input id="adminReviewerPassword" type="password" autocomplete="new-password" minlength="10" maxlength="256" placeholder="Tối thiểu 10 ký tự" required></label>
      <label><span>Xác nhận mật khẩu</span><input id="adminReviewerConfirmPassword" type="password" autocomplete="new-password" minlength="10" maxlength="256" placeholder="Nhập lại mật khẩu" required></label>
      <p class="auth-error admin-user-form-message" id="adminCreateReviewerError" role="alert"></p>
      <div class="admin-user-form-footer">
        <span class="admin-password-note">Mật khẩu được hash trước khi lưu vào database.</span>
        <button class="admin-create-button" id="adminCreateReviewerButton" type="submit">Tạo tài khoản Reviewer</button>
      </div>
    </form>
  </section>`;
}

async function createExpertAccount(event) {
  event.preventDefault();
  const error = document.getElementById("adminCreateUserError");
  const button = document.getElementById("adminCreateUserButton");
  const fullName = document.getElementById("adminExpertName").value.trim();
  const email = document.getElementById("adminExpertEmail").value.trim();
  const password = document.getElementById("adminExpertPassword").value;
  const confirmPassword = document.getElementById("adminExpertConfirmPassword").value;
  error.textContent = "";
  if (password !== confirmPassword) {
    error.textContent = "Xác nhận mật khẩu không khớp.";
    return false;
  }
  button.disabled = true;
  button.textContent = "Đang tạo...";
  try {
    const payload = await reviewRequest("/admin/users", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ fullName, email, password, confirmPassword }),
    });
    if (typeof showToast === "function") showToast(payload.message || "Đã tạo tài khoản chuyên gia.", "success");
    await showAdminReviewView("users");
  } catch (requestError) {
    error.textContent = requestError.message;
    button.disabled = false;
    button.innerHTML = '<svg width="17" height="17" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M15 21v-2a4 4 0 0 0-4-4H5a4 4 0 0 0-4 4v2"/><circle cx="8" cy="7" r="4"/><path d="M19 8v6M22 11h-6"/></svg>Tạo tài khoản Expert';
  }
  return false;
}

async function createReviewerAccount(event) {
  event.preventDefault();
  const error = document.getElementById("adminCreateReviewerError");
  const button = document.getElementById("adminCreateReviewerButton");
  const fullName = document.getElementById("adminReviewerName").value.trim();
  const email = document.getElementById("adminReviewerEmail").value.trim();
  const password = document.getElementById("adminReviewerPassword").value;
  const confirmPassword = document.getElementById("adminReviewerConfirmPassword").value;
  error.textContent = "";
  if (password !== confirmPassword) {
    error.textContent = "Xác nhận mật khẩu không khớp.";
    return false;
  }
  button.disabled = true;
  button.textContent = "Đang tạo...";
  try {
    const payload = await reviewRequest("/admin/reviewers", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ fullName, email, password, confirmPassword }),
    });
    if (typeof showToast === "function") showToast(payload.message || "Đã tạo tài khoản Reviewer.", "success");
    await showAdminReviewView("reviewers");
  } catch (requestError) {
    error.textContent = requestError.message;
    button.disabled = false;
    button.textContent = "Tạo tài khoản Reviewer";
  }
  return false;
}

async function openAdminReviewDocument(documentId) {
  const screen = ensureReviewScreen("screen-admin-review-workspace");
  screen.innerHTML = '<div class="review-workspace review-loading">Loading document...</div>';
  try {
    const document = await reviewRequest(`/admin/documents/${documentId}`);
    screen.innerHTML = renderReviewPage(document, false);
    removeReviewHistoryPanel(screen);
  } catch (error) { screen.innerHTML = `<div class="review-workspace review-error">Unable to load document: ${reviewEscape(error.message)}</div>`; }
}

async function openReviewerReviewDocument(documentId, push = true) {
  if (push && location.pathname !== `/reviewer/review/${documentId}`) {
    history.pushState({}, "", `/reviewer/review/${documentId}`);
  }
  const screen = ensureReviewScreen("screen-reviewer-workspace");
  document.querySelectorAll(".screen").forEach(item => item.classList.remove("active"));
  screen.classList.add("active");
  setReviewHeader("Review Details", `Văn bản #${documentId}`);
  screen.innerHTML = '<div class="review-workspace review-loading">Đang tải văn bản...</div>';
  try {
    const document = await reviewRequest(`/reviewer/documents/${documentId}`);
    reviewerDetailDocument = document;
    reviewerDetailComparison = buildReviewerComparison(document);
    screen.innerHTML = renderReviewerDetails(document);
  } catch (error) {
    screen.innerHTML = `<div class="review-workspace review-error">Không thể tải văn bản: ${reviewEscape(error.message)}</div>`;
  }
}

function reviewerEntityText(item) {
  if (typeof item === "string") return item.trim();
  if (!item || typeof item !== "object") return "";
  return String(item.term || item.text || item.entity || item.name || item.label || item.concept_name || "").trim();
}

function reviewerEntityCategory(item) {
  if (!item || typeof item !== "object") return "Khác";
  return String(item.category || item.type || item.entity_type || item.dictionary_type || "Khác").trim() || "Khác";
}

function reviewerCanonicalCategory(value) {
  const key = String(value || "").trim().toLowerCase();
  const aliases = {
    "bệnh lý": "disease", disease: "disease",
    "triệu chứng": "symptom", symptom: "symptom",
    "điều trị": "treatment", treatment: "treatment",
    "xét nghiệm": "lab_test", "lab_test": "lab_test", "lab test": "lab_test",
    "hình ảnh": "imaging", imaging: "imaging",
    "đông y": "trad_med", trad_med: "trad_med", "traditional medicine": "trad_med",
  };
  return aliases[key] || key.replace(/\s+/g, "_");
}

function reviewerEntityCode(item) {
  if (!item || typeof item !== "object") return "";
  return String(item.code || item.icd10_code || item.concept_code || item.label_code || "").trim();
}

function reviewerEntityList(review, sourceText = "") {
  const raw = review?.annotations || review?.entities || review?.labels || review?.original_labels || [];
  const used = new Set();
  return Array.isArray(raw) ? raw.map((item) => {
    const text = reviewerEntityText(item);
    let start = item && typeof item === "object" && Number.isInteger(Number(item.start)) ? Number(item.start) : null;
    let end = item && typeof item === "object" && Number.isInteger(Number(item.end)) ? Number(item.end) : null;
    if ((start == null || end == null) && sourceText && text) {
      let cursor = 0;
      while (true) {
        const found = sourceText.toLocaleLowerCase().indexOf(text.toLocaleLowerCase(), cursor);
        if (found < 0) break;
        const candidate = `${found}:${found + text.length}`;
        if (!used.has(candidate)) {
          start = found;
          end = found + text.length;
          break;
        }
        cursor = found + 1;
      }
    }
    if (start != null && end != null) used.add(`${start}:${end}`);
    const category = reviewerEntityCategory(item);
    return {
      text, category, categoryKey: reviewerCanonicalCategory(category),
      code: reviewerEntityCode(item),
      label: item && typeof item === "object" ? String(item.label || item.text || "").trim() : text,
      type: category, start, end,
      action: item && typeof item === "object" ? String(item.action || "KEEP").toUpperCase() : "KEEP",
      spans: item && typeof item === "object" && Array.isArray(item.spans) ? item.spans : [],
    };
  }).filter((item) => item.text) : [];
}

function reviewerPositionKey(entity) {
  const text = String(entity?.text || "").normalize("NFKC").toLowerCase().replace(/\s+/g, " ").trim();
  const start = Number.isInteger(entity?.start) ? entity.start : "";
  const end = Number.isInteger(entity?.end) ? entity.end : "";
  return `${start}:${end}|${text}`;
}

function reviewerExactKey(entity) {
  return `${reviewerPositionKey(entity)}|${entity?.categoryKey || reviewerCanonicalCategory(entity?.category)}|${String(entity?.code || "").toLowerCase()}`;
}

function mergeSystemDefaultEntities(entities, defaults, sourceText) {
  const merged = [...entities];
  const systemEntities = reviewerEntityList({ annotations: defaults }, sourceText);
  for (const systemEntity of systemEntities) {
    const exists = merged.some((entity) => reviewerPositionKey(entity) === reviewerPositionKey(systemEntity));
    if (!exists) merged.push({ ...systemEntity, action: "KEEP", source: "system-default" });
  }
  return merged;
}

function dedupeReviewerEntities(entities) {
  const seen = new Set();
  return entities.filter(entity => {
    const key = `${String(entity.text || "").normalize("NFKC").toLocaleLowerCase("vi-VN").replace(/\s+/g, " ").trim()}|${entity.categoryKey || ""}|${String(entity.code || "").toLowerCase()}`;
    if (seen.has(key)) return false;
    seen.add(key);
    return true;
  });
}

function buildReviewerComparison(document) {
  const history = [...(document.reviewHistory || [])].sort((a, b) => new Date(b.created_at || 0) - new Date(a.created_at || 0));
  const latestByExpert = new Map();
  for (const review of history) {
    const expertKey = review.expert_id || review.expert_email || review.expert_name || `review-${review.id}`;
    if (!latestByExpert.has(expertKey)) latestByExpert.set(expertKey, review);
  }
  const experts = [...latestByExpert.values()].sort((a, b) => new Date(a.created_at || 0) - new Date(b.created_at || 0));
  const a = experts[0] || {};
  const b = experts[1] || {};
  const sourceText = String(document.abstract || "");
  const systemDefaults = Array.isArray(document.annotationSeed) ? document.annotationSeed : [];
  const expertAEntities = dedupeReviewerEntities(mergeSystemDefaultEntities(reviewerEntityList(a, sourceText), systemDefaults, sourceText));
  const expertBEntities = dedupeReviewerEntities(mergeSystemDefaultEntities(reviewerEntityList(b, sourceText), systemDefaults, sourceText));
  const expertA = { name: a.expert_name || "Expert A", review: a, entities: expertAEntities };
  const expertB = { name: b.expert_name || "Expert B", review: b, entities: expertBEntities };
  const grouped = new Map();
  for (const side of [expertA, expertB]) {
    for (const entity of side.entities) {
      const base = `${String(entity.text || "").normalize("NFKC").toLocaleLowerCase("vi-VN").replace(/\s+/g, " ").trim()}|${entity.categoryKey || ""}|${String(entity.code || "").toLowerCase()}`;
      if (!grouped.has(base)) grouped.set(base, { id: `entity-${grouped.size}`, entity: entity.text, category: entity.category, a: null, b: null });
      grouped.get(base)[side === expertA ? "a" : "b"] = entity;
    }
  }
  const rows = [...grouped.values()].map((row) => {
    const exact = row.a && row.b && reviewerExactKey(row.a) === reviewerExactKey(row.b)
      && (row.a.action === "DELETE") === (row.b.action === "DELETE");
    const status = !row.a || !row.b
      ? "conflict"
      : row.a.action === "DELETE" || row.b.action === "DELETE"
        ? "conflict"
        : exact ? "agree" : "partial";
    if (!(row.id in reviewerDecisionState)) reviewerDecisionState[row.id] = { decision: status === "agree" ? "agree" : "", custom: "" };
    return { ...row, status };
  });
  return { expertA, expertB, rows };
}

function reviewerStatusLabel(status) {
  return status === "agree" ? "Agreement" : status === "partial" ? "Partial Difference" : "Conflict";
}

function reviewerEntityValue(entity) {
  if (!entity) return "No label";
  const deleted = entity.action === "DELETE" ? " · DELETED" : "";
  return `${entity.text}${entity.code ? ` · ${entity.code}` : ""}${deleted}`;
}

function reviewerTextMarkup(document, comparison) {
  const source = String(document.abstract || "Không có nội dung văn bản.");
  const candidates = [];
  for (const row of comparison.rows) {
    const entities = [row.a, row.b].filter(Boolean);
    const spans = entities.flatMap((entity) => entity.spans || []).filter((span) => Number.isInteger(Number(span.start)) && Number.isInteger(Number(span.end)));
    for (const span of spans) candidates.push({ start: Number(span.start), end: Number(span.end), row });

    // One comparison row represents one unique entity, but the entity may
    // occur several times in the source text. Highlight every occurrence.
    const term = String(row.entity || "").trim();
    if (term) {
      const lowerSource = source.toLocaleLowerCase("vi-VN");
      const lowerTerm = term.toLocaleLowerCase("vi-VN");
      let index = 0;
      while ((index = lowerSource.indexOf(lowerTerm, index)) !== -1) {
        candidates.push({ start: index, end: index + term.length, row });
        index += Math.max(lowerTerm.length, 1);
      }
    }
  }
  candidates.sort((a, b) => (a.end - a.start) - (b.end - b.start) || a.start - b.start);
  const accepted = [];
  for (const candidate of candidates) {
    if (candidate.end <= candidate.start || candidate.end > source.length) continue;
    if (!accepted.some((item) => candidate.start < item.end && candidate.end > item.start)) accepted.push(candidate);
  }
  accepted.sort((a, b) => a.start - b.start);
  let cursor = 0;
  const html = [];
  for (const item of accepted) {
    html.push(reviewEscape(source.slice(cursor, item.start)));
    const row = item.row;
    const tooltip = `Expert A: ${reviewerEntityValue(row.a)} | Expert B: ${reviewerEntityValue(row.b)}`;
    html.push(`<mark class="review-highlight review-highlight-${row.status}" title="${reviewEscape(tooltip)}">${reviewEscape(source.slice(item.start, item.end))}</mark>`);
    cursor = item.end;
  }
  html.push(reviewEscape(source.slice(cursor)));
  return html.join("");
}

function reviewerGroupedRows(rows, side) {
  const groups = new Map();
  for (const row of rows) {
    const entity = row[side];
    if (!groups.has(row.category)) groups.set(row.category, []);
    groups.get(row.category).push({ row, entity });
  }
  return [...groups.entries()].map(([category, values]) => `<section class="reviewer-category"><h4>${reviewEscape(category)} <span>${values.length}</span></h4>${values.map(({ row, entity }) => `<div class="reviewer-entity-row reviewer-entity-row-${side}"><span class="reviewer-entity-icon">${row.status === "agree" ? "✓" : row.status === "partial" ? "↔" : "!"}</span><div><strong>${reviewEscape(reviewerEntityValue(entity))}</strong><small>${reviewEscape(reviewerStatusLabel(row.status))}</small></div></div>`).join("")}</section>`).join("") || '<div class="review-empty">No data available.</div>';
}

function reviewerDecisionHtml(row) {
  if (row.status === "agree") return "";
  const state = reviewerDecisionState[row.id] || {};
  return `<article class="reviewer-decision-card"><div class="reviewer-decision-title"><div><strong>${reviewEscape(row.entity)}</strong><span>${reviewEscape(row.category)} · ${row.a?.start ?? row.b?.start}:${row.a?.end ?? row.b?.end}</span></div>${statusBadge(row.status === "partial" ? "Partial Difference" : "Conflict")}</div><div class="reviewer-choice-row"><button class="${state.decision === "expert_a" ? "selected" : ""}" onclick="setReviewerDecision('${row.id}', 'expert_a')">Choose A</button><button class="${state.decision === "expert_b" ? "selected" : ""}" onclick="setReviewerDecision('${row.id}', 'expert_b')">Choose B</button><button class="${state.decision === "custom" ? "selected" : ""}" onclick="setReviewerDecision('${row.id}', 'custom')">Edit Label</button></div><div class="reviewer-side-values"><span>A: ${reviewEscape(reviewerEntityValue(row.a))}</span><span>B: ${reviewEscape(reviewerEntityValue(row.b))}</span></div>${state.decision === "custom" ? `<div class="reviewer-custom-fields"><input class="reviewer-custom-input" value="${reviewEscape(state.customText || row.entity)}" oninput="setReviewerCustomField('${row.id}', 'text', this.value)" placeholder="Entity text"><input class="reviewer-custom-input" value="${reviewEscape(state.customLabel || row.entity)}" oninput="setReviewerCustomField('${row.id}', 'label', this.value)" placeholder="Label"><input class="reviewer-custom-input" value="${reviewEscape(state.customType || row.category)}" oninput="setReviewerCustomField('${row.id}', 'type', this.value)" placeholder="Entity type"><input class="reviewer-custom-input" value="${reviewEscape(state.customCode || '')}" oninput="setReviewerCustomField('${row.id}', 'code', this.value)" placeholder="ICD-10 code"></div>` : ""}</article>`;
}

function renderReviewerTable(comparison) {
  return `<div class="review-table-wrap"><table class="review-table reviewer-adjudication-table"><thead><tr><th>Entity</th><th>Expert A</th><th>Expert B</th><th>Trạng thái</th><th>Quyết định</th></tr></thead><tbody>${comparison.rows.map((row) => `<tr><td><strong>${reviewEscape(row.entity)}</strong><div class="muted">${reviewEscape(row.category)}</div></td><td>${reviewEscape(reviewerEntityValue(row.a))}</td><td>${reviewEscape(reviewerEntityValue(row.b))}</td><td><span class="reviewer-status reviewer-status-${row.status}">${reviewerStatusLabel(row.status)}</span></td><td>${row.status === "agree" ? "Tự động thống nhất" : reviewEscape(reviewerDecisionState[row.id]?.decision || "Chưa chọn")}</td></tr>`).join("")}</tbody></table></div>`;
}

function renderReviewerDetails(document) {
  const comparison = reviewerDetailComparison || buildReviewerComparison(document);
  const conflicts = comparison.rows.filter((row) => row.status !== "agree");
  const adjudication = document.adjudication;
  if (adjudication?.decisions) {
    for (const saved of adjudication.decisions) if (saved.id && reviewerDecisionState[saved.id]) reviewerDecisionState[saved.id] = saved;
  }
  const resolutionStatus = String(document.resolutionStatus || (adjudication?.resolution_status || (conflicts.length ? "CONFLICT" : "PENDING"))).toUpperCase();
  const finalLabels = adjudication?.final_labels || [];
  const finalLabelsHtml = finalLabels.length
    ? `<section class="review-panel reviewer-final-panel"><div class="reviewer-panel-heading"><div><h3>Final Labels</h3><p>Labels confirmed by the reviewer.</p></div><span class="reviewer-status reviewer-status-agree">${reviewEscape(resolutionStatus)}</span></div><div class="reviewer-final-labels">${finalLabels.map(item => `<span>${reviewEscape(item.label || item.text)}${item.code ? ` · ${reviewEscape(item.code)}` : ""}</span>`).join("")}</div></section>`
    : "";
  const comparisonContent = reviewerDetailMode === "table"
    ? `<section class="review-panel reviewer-table-panel">${renderReviewerTable(comparison)}</section>`
    : `<div class="reviewer-main-grid">
        <section class="review-panel reviewer-source-panel">
          <div class="reviewer-panel-heading"><div><h3>Original Text</h3><p>Highlights show the level of agreement between the two experts.</p></div></div>
          <div class="reviewer-source-text">${reviewerTextMarkup(document, comparison)}</div>
        </section>
        <section class="review-panel reviewer-compare-panel">
          <div class="reviewer-panel-heading"><div><h3>Expert Comparison</h3><p>Compare entities by category.</p></div></div>
          <div class="reviewer-expert-columns">
            <div><div class="reviewer-expert-heading"><span class="expert-avatar expert-avatar-a">A</span><div><strong>${reviewEscape(comparison.expertA.name)}</strong><small>${reviewEscape(REVIEW_STATUS_TEXT[comparison.expertA.review.review_status] || "Not reviewed")}</small></div></div>${reviewerGroupedRows(comparison.rows.filter((row) => row.a), "a")}</div>
            <div><div class="reviewer-expert-heading"><span class="expert-avatar expert-avatar-b">B</span><div><strong>${reviewEscape(comparison.expertB.name)}</strong><small>${reviewEscape(REVIEW_STATUS_TEXT[comparison.expertB.review.review_status] || "Not reviewed")}</small></div></div>${reviewerGroupedRows(comparison.rows.filter((row) => row.b), "b")}</div>
          </div>
        </section>
      </div>`;
  const selectedCount = conflicts.filter((row) => reviewerDecisionState[row.id]?.decision).length;
  return `<div class="reviewer-detail-page">
    <div class="reviewer-detail-toolbar"><button class="review-action" onclick="showReviewerView('reviews')">← Back</button><div class="reviewer-view-toggle"><button class="${reviewerDetailMode === "text" ? "active" : ""}" onclick="setReviewerDetailMode('text')">Text view</button><button class="${reviewerDetailMode === "table" ? "active" : ""}" onclick="setReviewerDetailMode('table')">Table view</button></div></div>
    <section class="reviewer-case-header"><div><div class="reviewer-eyebrow">REVIEW DETAILS · CASE #${Number(document.id)}</div><h2>${reviewEscape(document.title || "Untitled")}</h2><p>${reviewEscape(document.authors || "Unknown author")} · ${reviewEscape(document.publication_year || "")}</p><span class="reviewer-case-status reviewer-case-status-${resolutionStatus.toLowerCase()}">${reviewEscape(resolutionStatus)}</span></div></section>
    <div class="reviewer-legend"><span><i class="legend-agree"></i> Both experts agree</span><span><i class="legend-partial"></i> Partial difference</span><span><i class="legend-conflict"></i> Conflict</span></div>
    ${comparisonContent}
    ${finalLabelsHtml}
    <section class="review-panel reviewer-decision-panel"><div class="reviewer-panel-heading"><div><h3>Reviewer Decision</h3><p>Select an option for each unresolved entity, then confirm the case.</p></div><span class="reviewer-progress">${selectedCount}/${conflicts.length} selected</span></div><div class="reviewer-decision-list">${conflicts.map(reviewerDecisionHtml).join("") || '<div class="review-empty">Both experts agree on all entities.</div>'}</div><label class="reviewer-note-label">Overall Note<textarea id="reviewerOverallNote" maxlength="8000" placeholder="Record the rationale or notes for the final decision...">${reviewEscape(adjudication?.note || "")}</textarea></label><div class="reviewer-confirm-row"><span id="reviewerSaveMessage" class="muted"></span><button class="review-save" onclick="saveReviewerAdjudication(${Number(document.id)})">Confirm Decision</button></div></section>
  </div>`;
}

function setReviewerDetailMode(mode) {
  reviewerDetailMode = mode;
  const screen = document.getElementById("screen-reviewer-workspace");
  if (screen && reviewerDetailDocument) screen.innerHTML = renderReviewerDetails(reviewerDetailDocument);
}

function setReviewerDecision(rowId, decision) {
  reviewerDecisionState[rowId] = { ...(reviewerDecisionState[rowId] || {}), decision };
  const screen = document.getElementById("screen-reviewer-workspace");
  if (screen && reviewerDetailDocument) screen.innerHTML = renderReviewerDetails(reviewerDetailDocument);
}

function setReviewerCustom(rowId, value) {
  reviewerDecisionState[rowId] = { ...(reviewerDecisionState[rowId] || {}), decision: "custom", custom: value };
}

function setReviewerCustomField(rowId, field, value) {
  reviewerDecisionState[rowId] = { ...(reviewerDecisionState[rowId] || {}), decision: "custom", [`custom${field[0].toUpperCase()}${field.slice(1)}`]: value };
}

async function saveReviewerAdjudication(documentId) {
  const comparison = reviewerDetailComparison || buildReviewerComparison(reviewerDetailDocument || {});
  const conflicts = comparison.rows.filter((row) => row.status !== "agree");
  const incomplete = conflicts.find((row) => {
    const state = reviewerDecisionState[row.id];
    return !state?.decision || (state.decision === "custom" && !(state.customText || row.entity).trim());
  });
  const message = document.getElementById("reviewerSaveMessage");
  if (incomplete) {
    message.textContent = `Decision not selected for: ${incomplete.entity}`;
    message.className = "auth-error";
    return;
  }
  try {
    const note = document.getElementById("reviewerOverallNote")?.value.trim() || "";
    const decisions = conflicts.map((row) => ({ id: row.id, entity: row.entity, category: row.category, status: row.status, start: row.a?.start ?? row.b?.start, end: row.a?.end ?? row.b?.end, ...reviewerDecisionState[row.id] }));
    const finalLabels = comparison.rows.flatMap((row) => {
      const state = reviewerDecisionState[row.id] || {};
      if (row.status === "agree") return row.a ? [{ ...row.a, action: "KEEP" }] : [];
      if (state.decision === "expert_a") return row.a && row.a.action !== "DELETE" ? [{ ...row.a, action: "KEEP" }] : [];
      if (state.decision === "expert_b") return row.b && row.b.action !== "DELETE" ? [{ ...row.b, action: "KEEP" }] : [];
      if (state.decision === "custom") return [{
        text: state.customText || row.entity,
        label: state.customLabel || row.entity,
        type: state.customType || row.category,
        category: state.customType || row.category,
        code: state.customCode || "",
        start: row.a?.start ?? row.b?.start ?? 0,
        end: row.a?.end ?? row.b?.end ?? 0,
        action: "KEEP",
      }];
      return [];
    });
    await reviewRequest(`/reviewer/documents/${documentId}/adjudication`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ decisions, finalLabels, resolutionStatus: "RESOLVED", note }) });
    message.textContent = "Reviewer decision saved.";
    message.className = "reviewer-save-success";
    if (typeof showToast === "function") showToast("Review result confirmed.", "success");
    if (reviewerDetailDocument) {
      reviewerDetailDocument.adjudication = { ...(reviewerDetailDocument.adjudication || {}), decisions, final_labels: finalLabels, resolution_status: "RESOLVED", note };
      reviewerDetailDocument.resolutionStatus = "RESOLVED";
      message.closest(".reviewer-detail-page")?.replaceWith(document.createRange().createContextualFragment(renderReviewerDetails(reviewerDetailDocument)));
    }
  } catch (error) {
    message.textContent = error.message;
    message.className = "auth-error";
  }
}
