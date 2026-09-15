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
  const response = await fetch(`/api${path}`, options);
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
  }
}

function initializeReviewerWorkspace() {
  const nav = document.querySelector(".sidebar-nav");
  nav.innerHTML = `
    <div class="nav-section-label">Reviewer</div>
    <a class="nav-item" data-review-view="reviewer-dashboard" onclick="showReviewerView('dashboard')"><span class="nav-label">Dashboard</span></a>
    <a class="nav-item" data-review-view="reviewer-reviews" onclick="showReviewerView('reviews')"><span class="nav-label">Review Dashboard</span></a>
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
  setReviewHeader("Reviewer Dashboard", "Tổng quan kết quả review của chuyên gia");
  screen.innerHTML = '<div class="review-workspace review-loading">Đang tải dashboard...</div>';
  try {
    const stats = await reviewRequest("/reviewer/dashboard");
    screen.innerHTML = `<div class="review-workspace">
      <div class="review-kpis">
        ${reviewKpi("Tổng lượt review", stats.totalReviews)}
        ${reviewKpi("Chuyên gia đã review", stats.activeExperts)}
        ${reviewKpi("Văn bản đã review", stats.reviewedDocuments)}
        ${reviewKpi("Correct", stats.correctReviews)}
        ${reviewKpi("Incorrect", stats.incorrectReviews)}
        ${reviewKpi("Cần chỉnh sửa", stats.needsRevision)}
      </div>
      <div class="review-panel"><h3>Review Dashboard</h3><p>Reviewer có thể theo dõi tổng quan và xem toàn bộ kết quả đánh giá do các chuyên gia trong hệ thống thực hiện.</p></div>
    </div>`;
  } catch (error) {
    screen.innerHTML = `<div class="review-workspace review-error">Không thể tải dashboard: ${reviewEscape(error.message)}</div>`;
  }
}

async function renderReviewerReviews(screen) {
  setReviewHeader("Review Dashboard", "Kết quả review từ các chuyên gia");
  screen.innerHTML = `<div class="review-workspace"><div class="review-panel">
    <div class="review-filter"><input id="reviewerQuery" placeholder="Tìm bài báo hoặc chuyên gia"><select id="reviewerStatus"><option value="">Tất cả trạng thái</option><option value="CORRECT">Correct</option><option value="INCORRECT">Incorrect</option><option value="NEEDS_REVISION">Needs Revision</option></select><button class="review-action" onclick="loadReviewerReviews()">Tìm</button></div>
    <div id="reviewerReviewsArea" class="review-loading">Đang tải kết quả...</div>
  </div></div>`;
  await loadReviewerReviews();
}

async function loadReviewerReviews() {
  const area = document.getElementById("reviewerReviewsArea");
  if (!area) return;
  area.innerHTML = '<div class="review-loading">Đang tải kết quả...</div>';
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
      ? `<div class="review-table-wrap"><table class="review-table"><thead><tr><th>ID</th><th>Văn bản</th><th>Chuyên gia</th><th>Kết quả</th><th>Mã đề xuất</th><th>Nhận xét</th><th>Ngày</th><th></th></tr></thead><tbody>${rows}</tbody></table></div>`
      : '<div class="review-empty">Chưa có kết quả review.</div>';
  } catch (error) {
    area.innerHTML = `<div class="review-error">Không thể tải review: ${reviewEscape(error.message)}</div>`;
  }
}

function initializeExpertWorkspace() {
  const nav = document.querySelector(".sidebar-nav");
  nav.innerHTML = `
    <div class="nav-section-label">Chuyên gia</div>
    <a class="nav-item" data-review-view="dashboard" onclick="showExpertView('dashboard')"><span class="nav-label">Dashboard</span></a>
    <a class="nav-item" data-review-view="icd10" onclick="showExpertView('icd10')"><span class="nav-label">ICD-10 Labeled</span></a>
    <a class="nav-item" data-review-view="ai-labeled" onclick="showExpertView('ai-labeled')"><span class="nav-label">AI Labeled</span></a>
    <a class="nav-item" data-review-view="reviewed" onclick="showExpertView('reviewed')"><span class="nav-label">Reviewed</span></a>
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
    return `<tr><td>${item.id}</td><td class="document-cell">${reviewEscape(item.title || "Không có tiêu đề")}<div class="muted">${reviewEscape(item.authors || "")}</div></td>${middle}<td>${statusBadge(item.reviewStatus)}</td><td><button class="review-action" onclick="openExpertReview(${Number(item.id)})">Review</button></td></tr>`;
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

async function openExpertReview(documentId, push = true) {
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
  } catch (error) { screen.innerHTML = `<div class="review-workspace review-error">Unable to load document: ${reviewEscape(error.message)}</div>`; }
}

const AI_LABEL_CATEGORY_STYLE = {
  "Bệnh lý": "disease",
  "Triệu chứng": "symptom",
  "Điều trị": "treatment",
  "Xét nghiệm": "labtest",
  "Hình ảnh": "imaging",
  "Sinh lý": "physiology",
};

function aiResultEntries(payload) {
  const entries = [];
  if (!payload || typeof payload !== "object") return entries;
  for (const [category, values] of Object.entries(payload)) {
    if (!Array.isArray(values)) continue;
    for (const raw of values) {
      const item = typeof raw === "string" ? { term: raw, spans: [] } : raw;
      if (!item || typeof item !== "object" || !String(item.term || "").trim()) continue;
      entries.push({ category, item, style: AI_LABEL_CATEGORY_STYLE[category] || "other" });
    }
  }
  return entries;
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

function renderReviewPage(document, editable) {
  const labels = (document.currentLabels || []).map(item => `<div class="label-card"><strong>${reviewEscape(item.code || "No ICD-10 code")}</strong><span>${reviewEscape(item.label || "—")}</span><div class="muted">${reviewEscape(item.source || "")}</div></div>`).join("") || '<div class="review-empty">Không có nhãn hiện tại.</div>';
  const ai = document.aiLabel;
  const aiContent = ai ? `<div class="label-card"><strong>${reviewEscape([ai.primary_icd10_code, ai.primary_icd10_label].filter(Boolean).join(" - ") || "Kết quả AI đã lưu")}</strong><span>Model: ${reviewEscape(ai.model_name)}</span><div class="muted">Confidence: ${ai.confidence == null ? "Không có dữ liệu" : `${Math.round(Number(ai.confidence) * 100)}%`}</div></div><h4 class="ai-full-result-title">Toàn bộ thực thể AI</h4>${renderFullAiResult(ai.labels)}` : '<div class="review-empty">Không có AI prediction đã lưu.</div>';
  const history = (document.reviewHistory || []).map(item => `<article class="history-card"><strong>${reviewEscape(item.expert_name || "Bạn")}</strong>${statusBadge(item.review_status)}<div class="muted">${reviewDate(item.created_at)}</div><p><b>Đề xuất:</b> ${reviewEscape([item.suggested_icd10_code, item.suggested_icd10_label].filter(Boolean).join(" - ") || "—")}</p><p>${reviewEscape(item.comment)}</p></article>`).join("") || '<div class="review-empty">Chưa có review trước đó.</div>';
  const form = editable ? `<form class="review-form" onsubmit="return saveExpertReview(event, ${Number(document.id)})"><fieldset><legend>Expert Review</legend><label><input type="radio" name="reviewStatus" value="CORRECT" checked> Correct</label><label><input type="radio" name="reviewStatus" value="INCORRECT"> Incorrect</label><label><input type="radio" name="reviewStatus" value="NEEDS_REVISION"> Needs Revision</label></fieldset><label>ICD-10/YHCT đề xuất (bắt buộc nếu Incorrect hoặc Needs Revision)<input id="suggestedCode" maxlength="100" placeholder="Ví dụ: J15.9"></label><label>Expert Comment<textarea id="expertComment" required minlength="3" maxlength="8000" placeholder="Enter your review/comment here..."></textarea></label><p class="auth-error" id="reviewSaveError"></p><button class="review-save" type="submit">Save Review</button></form>` : '';
  const backAction = editable
    ? `showExpertView('${reviewActiveView === 'reviewed' ? 'reviewed' : 'dashboard'}')`
    : currentAuthUser?.role === "reviewer"
      ? "showReviewerView('reviews')"
      : "showAdminReviewView('reviews')";
  return `<div class="review-workspace"><button class="review-action" onclick="${backAction}">← Quay lại</button><div class="review-layout" style="margin-top:14px"><div class="review-panel"><h3>${reviewEscape(document.title || "Không có tiêu đề")}</h3><p class="muted">${reviewEscape(document.authors || "Không rõ tác giả")} · ${reviewEscape(document.publication_year || "")}</p><h3 style="margin-top:18px">Original Medical Text</h3><div class="document-text">${renderAiText(document.abstract, ai?.labels)}</div>${form}</div><aside class="label-list"><div class="review-panel"><h3>Current ICD-10 Label</h3>${labels}</div><div class="review-panel"><h3>AI Prediction</h3>${aiContent}</div><div class="review-panel"><h3>Review History</h3><div class="history-list">${history}</div></div></aside></div></div>`;
}

async function saveExpertReview(event, documentId) {
  event.preventDefault();
  const error = document.getElementById("reviewSaveError");
  error.textContent = "";
  const reviewStatus = document.querySelector('input[name="reviewStatus"]:checked')?.value;
  const suggestedIcd10Code = document.getElementById("suggestedCode").value.trim();
  const comment = document.getElementById("expertComment").value.trim();
  try {
    await reviewRequest(`/expert/documents/${documentId}/reviews`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ reviewStatus, suggestedIcd10Code, comment }) });
    if (typeof showToast === "function") showToast("Review saved successfully.", "success");
    openExpertReview(documentId);
  } catch (requestError) { error.textContent = `Unable to save review: ${requestError.message}`; }
  return false;
}

function initializeAdminReviewLinks() {
  const nav = document.querySelector(".sidebar-nav");
  if (document.getElementById("adminReviewNav")) return;
  const extra = document.createElement("div");
  extra.id = "adminReviewNav";
  extra.innerHTML = `<div class="nav-section-label" style="margin-top:12px">Quản trị review</div><a class="nav-item" onclick="showAdminReviewView('reviews')"><span class="nav-label">Expert Reviews</span></a><a class="nav-item" onclick="showAdminReviewView('users')"><span class="nav-label">Tài khoản chuyên gia</span></a><a class="nav-item" onclick="showAdminReviewView('reviewers')"><span class="nav-label">Tài khoản Reviewer</span></a>`;
  nav.appendChild(extra);
}

async function showAdminReviewView(view) {
  document.querySelectorAll(".screen").forEach(item => item.classList.remove("active"));
  const screen = ensureReviewScreen("screen-admin-review-workspace");
  screen.classList.add("active");
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

function reviewerEntityCode(item) {
  if (!item || typeof item !== "object") return "";
  return String(item.code || item.icd10_code || item.concept_code || item.label_code || "").trim();
}

function reviewerEntityList(review) {
  const raw = review?.annotations || review?.entities || review?.labels || review?.original_labels || [];
  return Array.isArray(raw) ? raw.map((item) => ({
    text: reviewerEntityText(item),
    category: reviewerEntityCategory(item),
    code: reviewerEntityCode(item),
    spans: item && typeof item === "object" && Array.isArray(item.spans) ? item.spans : [],
  })).filter((item) => item.text) : [];
}

function reviewerKey(text, category, code = "") {
  return `${String(text).normalize("NFKC").toLowerCase().replace(/\s+/g, " ").trim()}|${String(category).toLowerCase()}|${String(code).toLowerCase()}`;
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
  const expertA = { name: a.expert_name || "Expert A", review: a, entities: reviewerEntityList(a) };
  const expertB = { name: b.expert_name || "Expert B", review: b, entities: reviewerEntityList(b) };
  const grouped = new Map();
  for (const side of [expertA, expertB]) {
    for (const entity of side.entities) {
      const base = reviewerKey(entity.text, entity.category);
      if (!grouped.has(base)) grouped.set(base, { id: `entity-${grouped.size}`, entity: entity.text, category: entity.category, a: null, b: null });
      grouped.get(base)[side === expertA ? "a" : "b"] = entity;
    }
  }
  const rows = [...grouped.values()].map((row) => {
    const exact = row.a && row.b && reviewerKey(row.a.text, row.a.category, row.a.code) === reviewerKey(row.b.text, row.b.category, row.b.code);
    const status = exact ? "agree" : row.a && row.b ? "partial" : "conflict";
    if (!(row.id in reviewerDecisionState)) reviewerDecisionState[row.id] = { decision: status === "agree" ? "agree" : "", custom: "" };
    return { ...row, status };
  });
  return { expertA, expertB, rows };
}

function reviewerStatusLabel(status) {
  return status === "agree" ? "Đồng thuận" : status === "partial" ? "Khác biệt một phần" : "Mâu thuẫn";
}

function reviewerEntityValue(entity) {
  if (!entity) return "Không có nhãn";
  return `${entity.text}${entity.code ? ` · ${entity.code}` : ""}`;
}

function reviewerTextMarkup(document, comparison) {
  const source = String(document.abstract || "Không có nội dung văn bản.");
  const candidates = [];
  for (const row of comparison.rows) {
    const entities = [row.a, row.b].filter(Boolean);
    const spans = entities.flatMap((entity) => entity.spans || []).filter((span) => Number.isInteger(Number(span.start)) && Number.isInteger(Number(span.end)));
    if (spans.length) {
      for (const span of spans) candidates.push({ start: Number(span.start), end: Number(span.end), row });
    } else {
      const index = source.toLocaleLowerCase().indexOf(row.entity.toLocaleLowerCase());
      if (index >= 0) candidates.push({ start: index, end: index + row.entity.length, row });
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
  return [...groups.entries()].map(([category, values]) => `<section class="reviewer-category"><h4>${reviewEscape(category)} <span>${values.length}</span></h4>${values.map(({ row, entity }) => `<div class="reviewer-entity-row"><span class="reviewer-entity-icon">${row.status === "agree" ? "✓" : row.status === "partial" ? "↔" : "!"}</span><div><strong>${reviewEscape(reviewerEntityValue(entity))}</strong><small>${reviewEscape(reviewerStatusLabel(row.status))}</small></div></div>`).join("")}</section>`).join("") || '<div class="review-empty">Chưa có dữ liệu.</div>';
}

function reviewerDecisionHtml(row) {
  if (row.status === "agree") return "";
  const state = reviewerDecisionState[row.id] || {};
  return `<article class="reviewer-decision-card"><div class="reviewer-decision-title"><div><strong>${reviewEscape(row.entity)}</strong><span>${reviewEscape(row.category)}</span></div>${statusBadge(row.status === "partial" ? "Khác biệt" : "Mâu thuẫn")}</div><div class="reviewer-choice-row"><button class="${state.decision === "expert_a" ? "selected" : ""}" onclick="setReviewerDecision('${row.id}', 'expert_a')">Theo A</button><button class="${state.decision === "expert_b" ? "selected" : ""}" onclick="setReviewerDecision('${row.id}', 'expert_b')">Theo B</button><button class="${state.decision === "custom" ? "selected" : ""}" onclick="setReviewerDecision('${row.id}', 'custom')">Tự sửa nhãn</button></div><div class="reviewer-side-values"><span>A: ${reviewEscape(reviewerEntityValue(row.a))}</span><span>B: ${reviewEscape(reviewerEntityValue(row.b))}</span></div>${state.decision === "custom" ? `<input class="reviewer-custom-input" value="${reviewEscape(state.custom)}" oninput="setReviewerCustom('${row.id}', this.value)" placeholder="Nhập nhãn cuối cùng">` : ""}</article>`;
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
  const comparisonContent = reviewerDetailMode === "table"
    ? `<section class="review-panel reviewer-table-panel">${renderReviewerTable(comparison)}</section>`
    : `<div class="reviewer-main-grid">
        <section class="review-panel reviewer-source-panel">
          <div class="reviewer-panel-heading"><div><h3>Văn bản gốc</h3><p>Highlight thể hiện mức độ đồng thuận giữa hai chuyên gia.</p></div></div>
          <div class="reviewer-source-text">${reviewerTextMarkup(document, comparison)}</div>
        </section>
        <section class="review-panel reviewer-compare-panel">
          <div class="reviewer-panel-heading"><div><h3>So sánh chuyên gia</h3><p>Đối chiếu theo từng nhóm thực thể.</p></div></div>
          <div class="reviewer-expert-columns">
            <div><div class="reviewer-expert-heading"><span class="expert-avatar expert-avatar-a">A</span><div><strong>${reviewEscape(comparison.expertA.name)}</strong><small>${reviewEscape(REVIEW_STATUS_TEXT[comparison.expertA.review.review_status] || "Chưa đánh giá")}</small></div></div>${reviewerGroupedRows(comparison.rows.filter((row) => row.a), "a")}</div>
            <div><div class="reviewer-expert-heading"><span class="expert-avatar expert-avatar-b">B</span><div><strong>${reviewEscape(comparison.expertB.name)}</strong><small>${reviewEscape(REVIEW_STATUS_TEXT[comparison.expertB.review.review_status] || "Chưa đánh giá")}</small></div></div>${reviewerGroupedRows(comparison.rows.filter((row) => row.b), "b")}</div>
          </div>
        </section>
      </div>`;
  const selectedCount = conflicts.filter((row) => reviewerDecisionState[row.id]?.decision).length;
  return `<div class="reviewer-detail-page">
    <div class="reviewer-detail-toolbar"><button class="review-action" onclick="showReviewerView('reviews')">← Quay lại</button><div class="reviewer-view-toggle"><button class="${reviewerDetailMode === "text" ? "active" : ""}" onclick="setReviewerDetailMode('text')">Text view</button><button class="${reviewerDetailMode === "table" ? "active" : ""}" onclick="setReviewerDetailMode('table')">Table view</button></div></div>
    <section class="reviewer-case-header"><div><div class="reviewer-eyebrow">REVIEW DETAILS · CASE #${Number(document.id)}</div><h2>${reviewEscape(document.title || "Không có tiêu đề")}</h2><p>${reviewEscape(document.authors || "Không rõ tác giả")} · ${reviewEscape(document.publication_year || "")}</p></div><div class="reviewer-case-meta"><strong>${conflicts.length}</strong><span>mục cần quyết định</span></div></section>
    <div class="reviewer-legend"><span><i class="legend-agree"></i> Hai chuyên gia đồng ý</span><span><i class="legend-partial"></i> Khác biệt một phần</span><span><i class="legend-conflict"></i> Mâu thuẫn</span><span class="reviewer-tooltip-hint">Di chuột lên vùng màu để xem nhãn A/B</span></div>
    ${comparisonContent}
    <section class="review-panel reviewer-decision-panel"><div class="reviewer-panel-heading"><div><h3>Quyết định reviewer</h3><p>Chọn phương án cho từng thực thể chưa thống nhất, sau đó xác nhận toàn bộ case.</p></div><span class="reviewer-progress">${selectedCount}/${conflicts.length} đã chọn</span></div><div class="reviewer-decision-list">${conflicts.map(reviewerDecisionHtml).join("") || '<div class="review-empty">Hai chuyên gia đã đồng thuận toàn bộ.</div>'}</div><label class="reviewer-note-label">Ghi chú tổng thể<textarea id="reviewerOverallNote" maxlength="8000" placeholder="Ghi lại lý do hoặc lưu ý cho quyết định cuối...">${reviewEscape(adjudication?.note || "")}</textarea></label><div class="reviewer-confirm-row"><span id="reviewerSaveMessage" class="muted"></span><button class="review-save" onclick="saveReviewerAdjudication(${Number(document.id)})">Xác nhận tổng thể</button></div></section>
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

async function saveReviewerAdjudication(documentId) {
  const comparison = reviewerDetailComparison || buildReviewerComparison(reviewerDetailDocument || {});
  const conflicts = comparison.rows.filter((row) => row.status !== "agree");
  const incomplete = conflicts.find((row) => !reviewerDecisionState[row.id]?.decision || (reviewerDecisionState[row.id].decision === "custom" && !reviewerDecisionState[row.id].custom?.trim()));
  const message = document.getElementById("reviewerSaveMessage");
  if (incomplete) {
    message.textContent = `Chưa chọn quyết định cho: ${incomplete.entity}`;
    message.className = "auth-error";
    return;
  }
  try {
    const note = document.getElementById("reviewerOverallNote")?.value.trim() || "";
    const decisions = conflicts.map((row) => ({ id: row.id, entity: row.entity, category: row.category, status: row.status, ...reviewerDecisionState[row.id] }));
    await reviewRequest(`/reviewer/documents/${documentId}/adjudication`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ decisions, note }) });
    message.textContent = "Đã lưu quyết định reviewer.";
    message.className = "reviewer-save-success";
    if (typeof showToast === "function") showToast("Đã xác nhận kết quả review.", "success");
  } catch (error) {
    message.textContent = error.message;
    message.className = "auth-error";
  }
}
