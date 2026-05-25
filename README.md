# se-tracking

ระบบกลางสำหรับติดตามสถานะของแต่ละงาน (อ้างอิงด้วย เลขเคลม และ เลขเซอร์เวย์)

## Workflow

```
1. บันทึกงาน   →  2. จบงาน    →  3. อนุมัติ (DEFERRED)  →  4. ตัดหนี้
   (se-key)        (se-billing)    (pw - รอภายหลัง)        (วางบิลรับเช็ค)
```

## Setup

```bash
pip install -r requirements.txt
cp .env.example .env
# แก้ค่าใน .env ให้ตรงกับเครื่อง
python app.py
```

เปิด <http://localhost:5400/>

## Configuration

ดู `.env.example`

## Architecture

| Component | Path |
|-----------|------|
| Flask app + routes | `app.py` |
| SQLite cache | `data/tracking.db` (auto-created) |
| Source adapters | `adapters/` |
| Normalization | `normalize.py` |
| Auto-sync scheduler | `scheduler.py` |
| Dashboard templates | `templates/` |

## Testing

```bash
python -m pytest tests/ -v
```

## Adding a new source

1. สร้าง `adapters/my_source.py` extends `SyncAdapter`
2. ลงทะเบียนใน `scheduler.py` ด้วย `scheduler.add_job(...)`
3. เพิ่ม schema `stage_*` ใน `db.py` ถ้าจำเป็น
4. Update `jobs.py` rebuild logic ถ้าเพิ่ม stage ใหม่
