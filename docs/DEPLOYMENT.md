# Deploying InvoiceFlow for free

Stack: **GitHub** (code) -> **Streamlit Community Cloud** (the app) + **Supabase** (PostgreSQL) + **Gemini free tier** (AI).
Nothing in this guide needs a credit card. **Do not enable billing** on Google AI Studio or Supabase for this assignment.

> This is a **demonstration** deployment with **synthetic data only**. It is not a production payment-approval system.
> Document text is sent to Google Gemini, and on the free tier Google may use submitted content to improve its products,
> so never upload real invoices.

Nothing here has been done for you: no account, repository, project or app has been created, and nothing has been pushed.

## 0. Pre-flight checklist (run on your machine)

```powershell
python -m pytest -q                       # 496 passed
ruff check . ; ruff format --check .      # all checks passed / already formatted
git status --short                        # review what will be committed
git ls-files | Select-String "\.env$|secrets\.toml$|\.db$"   # must print NOTHING
```

What the repository already provides for Streamlit Community Cloud:

| Requirement | Where |
|---|---|
| Entry point | `app.py` (`streamlit run app.py`) |
| Python packages (all pinned) | `requirements.txt` (verified to resolve on Linux, Python 3.12; no known vulnerabilities per `pip-audit`) |
| System package for OCR | `packages.txt` -> `tesseract-ocr`, `tesseract-ocr-eng` |
| Server settings | `.streamlit/config.toml` (10 MB upload cap, XSRF protection, generic error messages for visitors) |
| Secrets template | `.streamlit/secrets.toml.example` (placeholders only) |
| Sample data | `sample_data/` (12 purchase orders, 12 synthetic invoice PDFs) |

## 1. Create the Supabase PostgreSQL database (free)

1. Sign up at <https://supabase.com/> and create a **New project**. Pick a region near you and **save the database password** you set.
2. Wait until the project is ready, then open **Connect** (top of the project page).
3. Choose the **Transaction pooler** connection string (port **6543**). Use the pooler, not the *Direct connection*: the direct host is IPv6-only on the free plan and most hosting platforms cannot reach it.
4. Convert it into the URL InvoiceFlow expects (SQLAlchemy + psycopg):

   ```
   postgresql+psycopg://postgres.<project-ref>:<password>@aws-0-<region>.pooler.supabase.com:6543/postgres
   ```

   - Keep the user exactly as Supabase shows it (`postgres.<project-ref>`).
   - If the password contains characters such as `@ / : # ?`, URL-encode them (`@` -> `%40`, `/` -> `%2F`, `:` -> `%3A`, `#` -> `%23`). Easiest: reset the password to letters and digits.
   - A plain `postgresql://...` string also works; the app converts it to the psycopg driver.
5. You do **not** need to create tables. The app creates and migrates its schema on first start.

> Free Supabase projects can be paused after a period of inactivity. If the app reports the database as unreachable later, open the Supabase dashboard and restore the project.

## 2. Get a Gemini API key (free)

1. Open <https://aistudio.google.com/>, choose **Get API key**, and create a key. Leave billing **off**.
2. Keep the key private. It goes only into the Streamlit secrets in step 5, never into the repository.
3. Check the free-tier limits that apply to your project in AI Studio and in [Google's rate-limit documentation](https://ai.google.dev/gemini-api/docs/rate-limits). Free-tier limits are low and can change; InvoiceFlow handles a quota error by failing the run with a clear message and offering manual entry.

## 3. Push the code to GitHub (needs your approval and your account)

The local repository already has an `origin` remote (`https://github.com/itsavinashverma/invoiceflow.git`). Confirm it is the repository you want, and that the repository exists on GitHub (create an **empty** repository there first if it does not; public or private both work with Streamlit Community Cloud).

```powershell
git remote -v                         # confirm the destination
git status --short                    # review every file that will be committed
git add -A
git status --short | Select-String "\.env$|secrets\.toml$"   # must print NOTHING (these are git-ignored)
git commit -m "Prepare InvoiceFlow for deployment"
git push -u origin master             # your current branch is "master"; use the branch name you want to deploy
```

If you prefer `main`: `git branch -M main` then `git push -u origin main`. In step 5 select whichever branch you pushed.
After pushing, open the repository on GitHub and confirm there is **no** `.env`, no `secrets.toml` and no `*.db` file.

## 4. Open Streamlit Community Cloud and connect GitHub

1. Go to <https://share.streamlit.io/> and **Sign in with GitHub** (authorise Streamlit to read your repositories; you can limit it to this one).
2. Click **Create app** (or **New app**) and choose **Deploy a public app from GitHub** (a private repository works too; Streamlit will ask for access).

## 5. Configure and deploy

1. **Repository:** select `<you>/invoiceflow`. **Branch:** the branch you pushed. **Main file path:** `app.py`.
2. Click **Advanced settings**:
   - **Python version:** select **3.12**. (The version is chosen at deployment time; if you need to change it later, you may have to delete and redeploy the app.)
   - **Secrets:** paste the following (TOML), replacing the placeholders. Keys must be top-level:

     ```toml
     GEMINI_API_KEY = "YOUR_KEY"
     GEMINI_MODEL = "gemini-3.8-flash"
     DATABASE_URL = "postgresql+psycopg://postgres.YOUR_PROJECT_REF:YOUR_PASSWORD@aws-0-YOUR_REGION.pooler.supabase.com:6543/postgres"
     ACCESS_CODE = "choose-a-long-random-phrase"
     ```

     `ACCESS_CODE` is optional but strongly recommended for a public URL: without it, anyone who finds the link can run invoices and spend your free Gemini quota.
     For a fully private demo, set the app's sharing to specific viewers in Streamlit's app settings instead.
3. Click **Deploy**. The first build installs the packages from `requirements.txt` and the system package from `packages.txt` (Tesseract); this takes a few minutes. The logs are visible from the app's *Manage app* menu.

You can edit secrets later (App -> Settings -> Secrets); the app restarts to apply them. Pushing to the deployed branch redeploys automatically.

## 6. Test the deployed application

Work through this checklist on the live URL (use the files in `sample_data/invoices/`):

| # | Action | Expected |
|---|---|---|
| 1 | Open the URL | If you set `ACCESS_CODE`: the access form appears; the right code opens the Dashboard. |
| 2 | **About / System Health** | Database: *Connected and up to date* (PostgreSQL, schema v4). Gemini: *API key is configured*. PyMuPDF loaded. OCR: *Tesseract ... is available*. No red cards, no key or password visible. |
| 3 | Click **Test Gemini connection** | *Gemini is reachable and the model is available.* (If it reports overload/quota, wait or change `GEMINI_MODEL`.) |
| 4 | **Purchase Orders** -> *Load synthetic demo purchase orders* | 12 purchase orders appear; clicking again adds none. |
| 5 | **Process Invoice** -> `invoice_standard_digital.pdf` | Live stage view runs; result `APPROVED`; PO balance shown. |
| 6 | Process the same file again | `DUPLICATE_BLOCKED`, naming the earlier run. |
| 7 | `invoice_scanned.pdf` | OCR stage runs (not skipped); result approved, or review with reasons if OCR is uncertain. |
| 8 | `invoice_missing_po.pdf` | `REVIEW_REQUIRED` with candidate POs as suggestions. |
| 9 | A text file renamed to `.pdf` | Clear rejection message; no run is approved. |
| 10 | **Processing History** | Runs listed; open one to see its execution history; *Download CSV report* works. |
| 11 | Supabase -> Table Editor | `invoices`, `processing_runs`, `processing_events`, `purchase_orders` contain the rows (no PDF content is stored). |
| 12 | Reboot the app (Manage app -> Reboot) | The history is still there (proves the data is in PostgreSQL, not on the temporary disk). |

## Troubleshooting

| Symptom | Cause and fix |
|---|---|
| Banner: *stores data in a local SQLite file ... will be erased* | `DATABASE_URL` is not set in the secrets. Add it (step 1). |
| *Database ... Not reachable* | Wrong password or URL; using the direct (IPv6) host instead of the pooler; the Supabase project is paused. Re-copy the **Transaction pooler** string. |
| `prepared statement ... already exists` | Should not occur (the app disables prepared statements); it means a different pooler mode or driver is in use. Use the transaction pooler URL with `postgresql+psycopg://`. |
| OCR card red / scanned PDFs fail | `packages.txt` was not picked up: it must be at the repository root and contain `tesseract-ocr`. Reboot the app after fixing. |
| *GEMINI_API_KEY is not set* / auth error | Secret missing or mistyped. Secrets must be top-level keys, not inside a `[section]`. |
| Runs fail with *rate limited* / *service unavailable* | Free-tier quota or model overload. Wait, or set another model in `GEMINI_MODEL`. Manual entry is offered meanwhile. |
| Uploads above 10 MB rejected | Intended. To raise it, change both `MAX_UPLOAD_MB` (secret) and `server.maxUploadSize` (`.streamlit/config.toml`). |
| App is slow on the first visit | Free apps sleep when idle and wake on the next visit. |

## Updating, rotating and tearing down

- **Update:** commit and push; Streamlit redeploys. Database migrations run automatically and only ever add to the schema.
- **Rotate the Gemini key:** create a new key in AI Studio, update the secret, delete the old key.
- **Rotate the database password:** reset it in Supabase (Project settings -> Database), update `DATABASE_URL`.
- **Tear down:** delete the app in Streamlit, delete the Supabase project, delete the API key in AI Studio.
