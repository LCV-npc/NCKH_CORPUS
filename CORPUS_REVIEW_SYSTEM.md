# Corpus Review System

## Architecture used in this repository

- Frontend: Vite static application in `frontend/public/` (HTML, CSS and JavaScript).
- Backend: FastAPI in `backend/`, using direct `mysql-connector` queries rather than an ORM.
- Database: MySQL schema `yhoc_corpus`.
- Corpus document: `articles` is the source document table. Manual/dictionary concepts are stored in `extracted_concepts` and original crawler/PDF workflows continue to use their existing storage.
- ICD-10/YHCT source of truth: validated versioned artifacts under `backend/core/Tu Dien Y Hoc/`, as declared by `manifest_v1.json`.
- AI labels: the Admin first runs AI analysis, then explicitly selects **Lưu kết quả AI**. That action persists the exact returned payload in `ai_document_labels`. A confidence is shown only if the model actually provided one.

## Added database tables

The backend creates these idempotently at startup; the same DDL is also in `mysql.txt`.

| Table | Purpose |
| --- | --- |
| `users` | Account name, unique email, PBKDF2-SHA256 password hash, `ADMIN` or `EXPERT` role. |
| `user_sessions` | Hashed opaque session tokens, expiry, last-used time and revocation state. |
| `ai_document_labels` | Persisted AI labeling payload tied to an `articles.id`; no fabricated confidence. |
| `expert_reviews` | Append-only audit trail: document, Expert, status, original labels snapshot, suggested dictionary code, comment and timestamps. |

No existing corpus table is changed or replaced.

## Authentication and authorization

- Expert accounts are created only by an authenticated Admin through `POST /api/admin/users`; there is no public registration route.
- Passwords are hashed using PBKDF2-HMAC-SHA256 with a per-user salt and 600,000 iterations.
- Successful login returns an opaque session token. Only its SHA-256 hash is kept in MySQL.
- All corpus management endpoints require an Admin session.
- Expert endpoints require an Expert session. The backend checks that a requested document has a persisted ICD-10 concept or persisted AI label before it sends any document content. Other documents receive `403`.
- Expert review history is append-only. An Expert receives only their own history; Admin receives all Experts' reviews.

## Admin setup

Public registration deliberately cannot create an Admin. Seed/update one from a trusted terminal; the password is prompted and never placed in shell history:

```powershell
cd backend
.\.venv\Scripts\python.exe seed_admin.py --name "Corpus Admin" --email "admin@example.com"
```

## Main API routes

| Method | Route | Access |
| --- | --- | --- |
| POST | `/api/admin/users` | Admin only, creates Expert |
| POST | `/api/auth/login` | Public |
| GET / POST | `/api/auth/me`, `/api/auth/logout` | Authenticated |
| POST | `/api/ai-label`, `/api/ai-label/save` | Admin |
| GET | `/api/expert/dashboard` | Expert |
| GET | `/api/expert/documents/icd10` | Expert |
| GET | `/api/expert/documents/ai-labeled` | Expert |
| GET | `/api/expert/documents/reviewed` | Expert |
| GET / POST | `/api/expert/documents/{id}`, `/api/expert/documents/{id}/reviews` | Expert, eligible document only |
| GET | `/api/admin/reviews`, `/api/admin/users`, `/api/admin/documents/{id}` | Admin |

## Run

```powershell
# Terminal 1
cd backend
.\.venv\Scripts\python.exe -m uvicorn main:app --reload --no-access-log

# Terminal 2
cd frontend
npm run dev
```

Open the Vite URL and sign in as a seeded Admin. Create Expert accounts from the Admin **Users** page. If needed, copy `backend/.env.example` to `backend/.env` and set `DB_PASSWORD`, `GEMINI_API_KEY`, and optional `AUTH_SESSION_HOURS`.

## Verification commands

```powershell
cd backend
.\.venv\Scripts\python.exe -m unittest tests.test_dictionary_integrity tests.test_auth_security -v
$env:RUN_AUTH_INTEGRATION='1'; .\.venv\Scripts\python.exe -m unittest tests.test_auth_review_integration -v

cd ..\frontend
npm run build
```

The integration test creates temporary users, a temporary eligible concept and one review, verifies persistence and RBAC, then deletes those test records in teardown.
