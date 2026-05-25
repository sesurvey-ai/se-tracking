# emcs-api

Central HTTP API for pw records (เลขเคลม/เลขเซอร์เวย์/ใบแจ้งหนี้ + ค่าอนุมัติ จาก Hpw.py).

Replaces the local `D:\trackingDB\emcs.db` file with a network-accessible
service. Hpw.py POSTs scrape results here; se-tracking pulls via the same
HTTP API.

## Endpoints

| Method | Path | Purpose |
|--------|------|---------|
| GET | `/healthz` | liveness + row count |
| GET | `/api/records` | list (since_id, claim_no, survey_no, invoice_no, limit, offset) |
| POST | `/api/records` | insert single (auto-upsert on UNIQUE) |
| POST | `/api/records/bulk` | bulk insert `{records: [...]}` |
| DELETE | `/api/records/:id` | admin |
| GET | `/api/records/export.xlsx` | download all as Excel |

Auth: `X-API-Key` header against `EMCS_API_KEY` env (or `?api_key=` query).

## Local dev

```bash
pip install -r requirements.txt
cp .env.example .env
python app.py    # listens on :5500
```

## Docker

```bash
docker build -t emcs-api .
docker run -p 5500:5500 -v emcs-data:/data -e EMCS_API_KEY=xxxx emcs-api
```

## Schema

`pw_records` — same shape as the old `D:\trackingDB\emcs.db`:

- claim_no (NOT NULL)
- survey_no, invoice_no, invoice_seq
- date_approve, offer_amount, approve_amount, deduct_amount, deduct_reason
- claim_type, surveyer, acc_province
- source_file, source_sheet
- UNIQUE(claim_no, invoice_no, invoice_seq) — idempotent bulk upsert
