"""Flask app for se-tracking.

Routes:
    GET  /                                  → dashboard HTML
    GET  /job/<claim>/<survey>             → drill-down detail HTML
    GET  /api/jobs                          → list jobs (with filters)
    GET  /api/jobs/<claim>/<survey>        → detail JSON (all 4 stages)
    GET  /api/jobs/export.xlsx              → Excel export
    GET  /api/jobs/stats                    → counts + drop-off for charts
    GET  /api/sources/status                → per-source last-sync info
    POST /api/sync/<source>                 → manual sync (one source or "all")
    GET  /fetch-stream                      → SSE: stream sync progress
    GET  /healthz                           → liveness probe
"""
from __future__ import annotations

import json
import logging
import os
import queue
import threading
from datetime import datetime
from functools import wraps

import orjson
from dotenv import load_dotenv
from flask import (
    Flask, Response, abort, jsonify, render_template, request,
    send_file, stream_with_context, url_for,
)
import requests as _requests
from flask_compress import Compress

import db
import export
from adapters.base import utc_now_iso
import scheduler

load_dotenv()

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("se-tracking")

APP_VERSION = "0.1.0"

app = Flask(__name__)
Compress(app)

# ── Auth helpers ────────────────────────────────────────────────────────────

API_KEY = os.getenv("TRACKING_API_KEY", "").strip()


def require_api_key(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        if not API_KEY:
            return fn(*args, **kwargs)
        provided = request.headers.get("X-API-Key") or request.args.get("api_key")
        if provided != API_KEY:
            return jsonify({"error": "unauthorized"}), 401
        return fn(*args, **kwargs)
    return wrapper


# ── Helpers ─────────────────────────────────────────────────────────────────

def _jdumps(obj) -> str:
    return orjson.dumps(obj).decode()


def _row_dict(row):
    return {k: row[k] for k in row.keys()} if row else None


# ── Filter parsing ──────────────────────────────────────────────────────────

def _build_where(args) -> tuple[str, list]:
    where = []
    params: list = []

    claim_no = (args.get("claim_no") or "").strip()
    survey_no = (args.get("survey_no") or "").strip()
    status = (args.get("status") or "").strip()
    q = (args.get("q") or "").strip()
    from_date = (args.get("from_date") or "").strip()
    to_date = (args.get("to_date") or "").strip()
    granularity = (args.get("granularity") or "day").strip()
    date_basis = (args.get("date_basis") or "first_seen").strip()

    if claim_no:
        where.append("claim_canonical LIKE ?")
        params.append(f"%{claim_no}%")
    if survey_no:
        # search both esurvey (S6xxxxx) and invoice (SEABI-xxx) — single field in UI
        where.append("(survey_canonical LIKE ? OR invoice_canonical LIKE ?)")
        like_su = f"%{survey_no.upper()}%"
        params.extend([like_su, like_su])
    if status:
        statuses = [s.strip() for s in status.split(",") if s.strip()]
        if statuses:
            placeholders = ",".join("?" * len(statuses))
            where.append(f"current_status IN ({placeholders})")
            params.extend(statuses)
    if q:
        where.append(
            "(claim_canonical LIKE ? OR survey_canonical LIKE ? OR invoice_canonical LIKE ? "
            "OR claim_display LIKE ? OR survey_display LIKE ? OR invoice_display LIKE ? "
            "OR debt_invoice LIKE ?)"
        )
        like = f"%{q}%"
        params.extend([like, like, like, like, like, like, like])

    # Date filter — basis defaults to first_seen, but UI may switch to last_updated
    date_col = "last_updated_at" if date_basis == "last_updated" else "first_seen_at"
    if from_date:
        where.append(f"{date_col} >= ?")
        params.append(from_date)
    if to_date:
        where.append(f"{date_col} <= ?")
        params.append(to_date)

    return (" WHERE " + " AND ".join(where) if where else ""), params


# ── HTML routes ─────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return render_template("dashboard.html", version=APP_VERSION)


@app.route("/job/<claim>/<survey>")
def job_detail(claim, survey):
    return render_template("job_detail.html", claim=claim, survey=survey or "", version=APP_VERSION)


@app.route("/job/<claim>/")
@app.route("/job/<claim>")
def job_detail_no_survey(claim):
    return render_template("job_detail.html", claim=claim, survey="", version=APP_VERSION)


# ── JSON API ─────────────────────────────────────────────────────────────────

@app.route("/healthz")
def healthz():
    return jsonify({"ok": True, "version": APP_VERSION})


@app.route("/api/jobs")
@require_api_key
def api_jobs():
    args = request.args
    where, params = _build_where(args)

    limit = max(1, min(int(args.get("limit") or 200), 5000))
    offset = max(0, int(args.get("offset") or 0))
    sort = (args.get("sort") or "last_updated_at").strip()
    direction = "DESC" if (args.get("dir") or "desc").lower() == "desc" else "ASC"

    allowed_sort = {
        "last_updated_at", "first_seen_at", "current_status", "status_index",
        "claim_canonical", "survey_canonical", "invoice_canonical",
        "debt_amount", "approved_amount", "closed_amount",
        "keyed_at", "closed_at", "approved_at", "debt_cut_date",
    }
    if sort not in allowed_sort:
        sort = "last_updated_at"

    conn = db.open_conn()
    try:
        total = conn.execute(
            f"SELECT COUNT(*) AS c FROM jobs_view{where}", params
        ).fetchone()["c"]

        rows = conn.execute(
            f"SELECT * FROM jobs_view{where} "
            f"ORDER BY {sort} {direction} NULLS LAST "
            f"LIMIT ? OFFSET ?",
            params + [limit, offset],
        ).fetchall()
    finally:
        conn.close()

    return Response(
        _jdumps({
            "rows": [_row_dict(r) for r in rows],
            "total": total, "limit": limit, "offset": offset,
        }),
        mimetype="application/json",
    )


@app.route("/api/jobs/<claim>/")
@app.route("/api/jobs/<claim>/<survey>")
@require_api_key
def api_job_detail(claim, survey=""):
    conn = db.open_conn()
    try:
        view = conn.execute(
            "SELECT * FROM jobs_view WHERE claim_canonical=? AND survey_canonical=?",
            (claim, survey),
        ).fetchone()

        keyed = conn.execute(
            "SELECT * FROM stage_keyed WHERE claim_canonical=? AND survey_canonical=? "
            "ORDER BY source_id DESC",
            (claim, survey),
        ).fetchall()

        closed = conn.execute(
            "SELECT * FROM stage_closed WHERE claim_canonical=? AND survey_canonical=? "
            "ORDER BY source_id DESC",
            (claim, survey),
        ).fetchall()

        approved = conn.execute(
            "SELECT * FROM stage_approved WHERE claim_canonical=? AND survey_canonical=? "
            "ORDER BY date_approve DESC",
            (claim, survey),
        ).fetchall()

        # debt is keyed by claim only (one claim → many invoices)
        debt = conn.execute(
            "SELECT * FROM stage_debt WHERE claim_canonical=? "
            "ORDER BY cut_date DESC, invoice_canonical",
            (claim,),
        ).fetchall()
    finally:
        conn.close()

    return Response(
        _jdumps({
            "view": _row_dict(view),
            "keyed": [_row_dict(r) for r in keyed],
            "closed": [_row_dict(r) for r in closed],
            "approved": [_row_dict(r) for r in approved],
            "debt": [_row_dict(r) for r in debt],
        }),
        mimetype="application/json",
    )


@app.route("/api/jobs/stats")
@require_api_key
def api_jobs_stats():
    args = request.args
    where, params = _build_where(args)

    conn = db.open_conn()
    try:
        # Per-stage counts (each stage reached, regardless of current status)
        stages_sql = f"""
            SELECT
              SUM(keyed)    AS keyed,
              SUM(closed)   AS closed,
              SUM(approved) AS approved,
              SUM(debt)     AS debt,
              COUNT(*)      AS total
              FROM jobs_view{where}
        """
        s = conn.execute(stages_sql, params).fetchone()

        # Current status counts (each job's highest stage)
        by_status = conn.execute(
            f"""SELECT current_status, COUNT(*) AS c
                  FROM jobs_view{where}
                 GROUP BY current_status""",
            params,
        ).fetchall()

        # Time series — daily rebucketing on first_seen_at, last 60 days
        ts_where = where + (
            (" AND first_seen_at >= date('now', '-60 day')")
            if where else " WHERE first_seen_at >= date('now', '-60 day')"
        )
        ts = conn.execute(
            f"""SELECT first_seen_at AS d,
                      SUM(keyed)    AS keyed,
                      SUM(closed)   AS closed,
                      SUM(approved) AS approved,
                      SUM(debt)     AS debt
                  FROM jobs_view{ts_where}
                 GROUP BY first_seen_at
                 ORDER BY first_seen_at""",
            params,
        ).fetchall()
    finally:
        conn.close()

    return Response(
        _jdumps({
            "stages": _row_dict(s),
            "by_status": {(r["current_status"] or "—"): r["c"] for r in by_status},
            "timeseries": [_row_dict(r) for r in ts],
        }),
        mimetype="application/json",
    )


@app.route("/api/jobs/export.xlsx")
@require_api_key
def api_jobs_export():
    args = request.args
    where, params = _build_where(args)

    conn = db.open_conn()
    try:
        rows = conn.execute(
            f"SELECT * FROM jobs_view{where} ORDER BY last_updated_at DESC NULLS LAST",
            params,
        ).fetchall()
    finally:
        conn.close()

    data = export.write_xlsx(rows)
    fn = export.filename_now("tracking")
    return send_file(
        __import__("io").BytesIO(data),
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        as_attachment=True, download_name=fn,
    )


@app.route("/api/sources/status")
@require_api_key
def api_sources_status():
    status = db.latest_sync_status()
    adapters_meta = [
        {"name": a.name, "interval_minutes": a.interval_minutes}
        for a in scheduler.get_adapters()
    ]
    # Add row-count snapshots
    conn = db.open_conn()
    try:
        counts = {
            "keyed":    conn.execute("SELECT COUNT(*) c FROM stage_keyed").fetchone()["c"],
            "closed":   conn.execute("SELECT COUNT(*) c FROM stage_closed").fetchone()["c"],
            "approved": conn.execute("SELECT COUNT(*) c FROM stage_approved").fetchone()["c"],
            "debt":     conn.execute("SELECT COUNT(*) c FROM stage_debt").fetchone()["c"],
            "jobs":     conn.execute("SELECT COUNT(*) c FROM jobs_view").fetchone()["c"],
        }
    finally:
        conn.close()

    return Response(
        _jdumps({
            "adapters": adapters_meta,
            "last_sync": status,
            "row_counts": counts,
            "last_rebuild_at": scheduler.last_rebuild_at(),
        }),
        mimetype="application/json",
    )


# ── Proxy endpoints — single public entry point for emcs + debt APIs ──────
# These let Hpw.py (and other external clients) talk to the internal-only
# emcs-api / debt-api containers via the public tracking.sesurvey.cloud host.

def _proxy_to(svc_url: str, svc_key: str, path: str):
    """Generic proxy. Forwards method + body + selected headers to upstream."""
    if not svc_url:
        return jsonify({"error": "upstream not configured"}), 502
    url = svc_url.rstrip("/") + path
    upstream_headers = {}
    if svc_key:
        upstream_headers["X-API-Key"] = svc_key
    # Pass through Content-Type for JSON / multipart bodies
    if request.headers.get("Content-Type"):
        upstream_headers["Content-Type"] = request.headers["Content-Type"]
    try:
        if request.method == "GET":
            r = _requests.get(url, params=request.args.to_dict(flat=True),
                              headers=upstream_headers, timeout=120)
        else:
            r = _requests.request(
                request.method, url,
                params=request.args.to_dict(flat=True),
                headers=upstream_headers,
                data=request.get_data(),
                timeout=300,
            )
    except Exception as e:
        log.exception("proxy %s %s failed", request.method, url)
        return jsonify({"error": str(e)}), 502
    return Response(r.content, status=r.status_code,
                    mimetype=r.headers.get("Content-Type", "application/json"))


@app.route("/api/emcs/<path:subpath>", methods=["GET", "POST", "DELETE"])
@require_api_key
def emcs_proxy(subpath):
    """Proxy /api/emcs/* → emcs-api internal."""
    return _proxy_to(
        os.getenv("EMCS_API_URL", "http://localhost:5500"),
        os.getenv("EMCS_API_KEY", "").strip(),
        f"/api/{subpath}",
    )


@app.route("/api/emcs/healthz")
@require_api_key
def emcs_health_proxy():
    return _proxy_to(
        os.getenv("EMCS_API_URL", "http://localhost:5500"),
        os.getenv("EMCS_API_KEY", "").strip(),
        "/healthz",
    )


@app.route("/api/debt/healthz")
@require_api_key
def debt_health_proxy():
    return _proxy_to(
        os.getenv("DEBT_API_URL", "http://localhost:5600"),
        os.getenv("DEBT_API_KEY", "").strip(),
        "/healthz",
    )


@app.route("/api/debt/upload", methods=["POST"])
@require_api_key
def debt_upload():
    """Proxy multipart upload to debt-api. Triggers debt sync after success."""
    file = request.files.get("file")
    if not file or not file.filename:
        return jsonify({"error": "no file"}), 400

    debt_url = os.getenv("DEBT_API_URL", "http://localhost:5600").rstrip("/")
    debt_key = os.getenv("DEBT_API_KEY", "").strip()
    headers = {"X-API-Key": debt_key} if debt_key else {}

    try:
        upstream = _requests.post(
            f"{debt_url}/api/upload",
            headers=headers,
            files={"file": (file.filename, file.stream, file.mimetype)},
            timeout=300,
        )
        body = upstream.json()
    except Exception as e:
        log.exception("debt upload proxy failed")
        return jsonify({"error": str(e)}), 502

    # Trigger debt sync (background-style: just run inline since it's fast)
    if upstream.status_code == 200 and body.get("ok"):
        try:
            scheduler.run_sync("debt")
        except Exception as e:
            log.exception("post-upload sync failed")
            body["sync_error"] = str(e)

    return jsonify(body), upstream.status_code


@app.route("/api/debt/upload-log")
@require_api_key
def debt_upload_log():
    debt_url = os.getenv("DEBT_API_URL", "http://localhost:5600").rstrip("/")
    debt_key = os.getenv("DEBT_API_KEY", "").strip()
    headers = {"X-API-Key": debt_key} if debt_key else {}
    try:
        r = _requests.get(f"{debt_url}/api/upload-log", headers=headers, timeout=15)
        return Response(r.text, mimetype="application/json", status=r.status_code)
    except Exception as e:
        return jsonify({"error": str(e)}), 502


@app.route("/api/sync/<source>", methods=["POST"])
@require_api_key
def api_sync(source):
    if source == "all":
        result = scheduler.run_sync_all()
    else:
        try:
            r = scheduler.run_sync(source)
        except ValueError:
            abort(404)
        result = {source: {
            "rows_seen": r.rows_seen,
            "rows_changed": r.rows_changed,
            "error": r.error,
        }}
    return Response(_jdumps(result), mimetype="application/json")


# ── SSE: streaming sync progress ────────────────────────────────────────────

@app.route("/fetch-stream", methods=["POST", "GET"])
@require_api_key
def fetch_stream():
    source = (request.values.get("source") or "all").strip()

    @stream_with_context
    def generate():
        q: queue.Queue = queue.Queue()
        done_event = threading.Event()

        def on_event(payload):
            q.put(payload)

        def worker():
            try:
                if source == "all":
                    scheduler.run_sync_all(on_event=on_event)
                else:
                    scheduler.run_sync(source, on_event=on_event)
            except Exception as e:
                q.put({"type": "error", "error": str(e)})
            finally:
                done_event.set()

        t = threading.Thread(target=worker, daemon=True)
        t.start()

        yield f"event: start\ndata: {_jdumps({'at': utc_now_iso(), 'source': source})}\n\n"

        while True:
            try:
                payload = q.get(timeout=1.0)
                yield f"event: progress\ndata: {_jdumps(payload)}\n\n"
            except queue.Empty:
                if done_event.is_set() and q.empty():
                    break
                yield ": ping\n\n"

        yield f"event: end\ndata: {_jdumps({'at': utc_now_iso()})}\n\n"

    return Response(generate(), mimetype="text/event-stream")


# ── Boot ────────────────────────────────────────────────────────────────────

def _boot():
    db.init_schema()
    scheduler.start()


_boot()


if __name__ == "__main__":
    port = int(os.getenv("PORT", "5400"))
    app.run(host="0.0.0.0", port=port, debug=False, threaded=True)
