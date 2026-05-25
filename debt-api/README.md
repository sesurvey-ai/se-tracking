# debt-api

Central HTTP API for ตัดหนี้ records (cheque/invoice reconciliation).

Replaces the local `D:\trackingDB\debt.db` + the `extract_ตัดหนี้.py` script
on disk — uploads go through this service instead.

## Endpoints

| Method | Path | Purpose |
|--------|------|---------|
| GET | `/healthz` | liveness + row count |
| GET | `/api/records` | list (since_id, claim, invoice, limit, offset) |
| POST | `/api/records/bulk` | bulk insert `{records: [{claim, invoice, amount, cut_date, source_file, sheet}, ...]}` |
| **POST** | **`/api/upload`** | **multipart upload `.xlsx` → parse + insert** |
| GET | `/api/upload-log` | history of past file uploads |
| DELETE | `/api/records/:id` | admin |
| GET | `/api/records/export.xlsx` | download all |

Auth: `X-API-Key` header against `DEBT_API_KEY` env.

## Upload format

Excel layout (same as legacy):
- Column C (index 2) = CLAIM NO. (e.g. `2025/013047387`)
- Column D (index 3) = เลขที่ใบแจ้งหนี้ (may have leading `'`)
- Column F (index 5) = AMT.
- Header row 1-5: looks for `"เช็ค DD/M/YYYY"` to extract `cut_date`

```bash
curl -X POST http://localhost:5600/api/upload \
  -H "X-API-Key: $DEBT_API_KEY" \
  -F "file=@'excel ตัดหนี้รอบรับเช็ค 22-5-69.xlsx'"
```

Response:
```json
{"ok":true, "upload_id":42, "filename":"...", "added":120, "updated":3, "skipped":15}
```

## Schema

`debt_records`: `claim, invoice, amount, cut_date, source_file, sheet` with
`UNIQUE(claim, invoice)` — idempotent re-upload.

`upload_log`: audit trail of every file ever uploaded.

## Docker

```bash
docker build -t debt-api .
docker run -p 5600:5600 -v debt-data:/data -e DEBT_API_KEY=xxxx debt-api
```
