// se-tracking dashboard logic.
// Filters → API → render table + cards + charts. Refresh triggers SSE sync.

const STATUS_INDEX = {
    'จบงาน': 1, 'บันทึกงาน': 2, 'อนุมัติ': 3, 'ตัดหนี้': 4,
};

const SOURCE_LABELS = {
    'se-key':      'บันทึกงาน',
    'se-billing':  'จบงาน (extension)',
    'isurvey-api': 'จบงาน (iSurvey API)',
    'pw':          'อนุมัติ',
    'debt':        'ตัดหนี้',
};
const sourceLabel = (name) => SOURCE_LABELS[name] || name;

const state = {
    rows: [],
    stats: null,
    sort: 'last_updated_at',
    dir: 'desc',
    granularity: 'all',
    dateBasis: 'first_seen',
    sourceStatus: null,
};

let funnelChart = null;
let trendChart = null;

document.addEventListener('DOMContentLoaded', () => {
    bindUI();
    initDates();
    refreshAll();
    fetchStatus();
    setInterval(fetchStatus, 15000);
});

function bindUI() {
    document.querySelectorAll('input[name=gran]').forEach(el => {
        el.addEventListener('change', () => {
            state.granularity = el.value;
            initDates();
            refreshAll();
        });
    });
    document.querySelectorAll('input[name=date_basis]').forEach(el => {
        el.addEventListener('change', () => {
            state.dateBasis = el.value;
            refreshAll();
        });
    });
    document.getElementById('fromDate').addEventListener('change', refreshAll);
    document.getElementById('toDate').addEventListener('change', refreshAll);
    document.getElementById('searchInput').addEventListener('input', debounce(refreshAll, 250));
    document.querySelectorAll('.status-filter').forEach(el => {
        el.addEventListener('change', refreshAll);
    });

    document.getElementById('refreshBtn').addEventListener('click', startSync);
    document.getElementById('exportBtn').addEventListener('click', exportXlsx);

    document.querySelectorAll('thead th[data-sort]').forEach(th => {
        th.addEventListener('click', () => {
            const col = th.dataset.sort;
            if (state.sort === col) {
                state.dir = state.dir === 'desc' ? 'asc' : 'desc';
            } else {
                state.sort = col;
                state.dir = 'desc';
            }
            refreshAll();
        });
    });
}

function initDates() {
    const today = new Date();
    const fromInput = document.getElementById('fromDate');
    const toInput = document.getElementById('toDate');
    // Default ranges by granularity
    let from, to;
    if (state.granularity === 'all') {
        fromInput.value = '';
        toInput.value = '';
        return;
    } else if (state.granularity === 'year') {
        from = new Date(today.getFullYear(), 0, 1);
        to = new Date(today.getFullYear(), 11, 31);
    } else if (state.granularity === 'month') {
        from = new Date(today.getFullYear(), today.getMonth(), 1);
        to = new Date(today.getFullYear(), today.getMonth() + 1, 0);
    } else {
        // day: today only
        from = today;
        to = today;
    }
    fromInput.value = iso(from);
    toInput.value = iso(to);
}

function iso(d) {
    return d.toISOString().slice(0, 10);
}

function debounce(fn, ms) {
    let t;
    return (...args) => { clearTimeout(t); t = setTimeout(() => fn(...args), ms); };
}

function currentFilters() {
    const statuses = Array.from(document.querySelectorAll('.status-filter:checked')).map(el => el.value);
    return {
        q: document.getElementById('searchInput').value.trim(),
        from_date: document.getElementById('fromDate').value,
        to_date: document.getElementById('toDate').value,
        status: statuses.join(','),
        granularity: state.granularity,
        date_basis: state.dateBasis,
        sort: state.sort,
        dir: state.dir,
        limit: 500,
    };
}

async function refreshAll() {
    await Promise.all([fetchJobs(), fetchStats()]);
}

async function fetchJobs() {
    const params = new URLSearchParams(currentFilters());
    const r = await fetch('/api/jobs?' + params);
    if (!r.ok) { console.error('jobs fetch failed'); return; }
    const data = await r.json();
    state.rows = data.rows || [];
    document.getElementById('rowCount').textContent =
        `${data.rows.length.toLocaleString('th-TH')} / ${data.total.toLocaleString('th-TH')} แถว`;
    renderTable();
}

async function fetchStats() {
    const params = new URLSearchParams(currentFilters());
    const r = await fetch('/api/jobs/stats?' + params);
    if (!r.ok) { console.error('stats fetch failed'); return; }
    state.stats = await r.json();
    renderStats();
    renderCharts();
}

async function fetchStatus() {
    const r = await fetch('/api/sources/status');
    if (!r.ok) return;
    state.sourceStatus = await r.json();
    renderSyncStatus();
}

// ── Rendering ────────────────────────────────────────────────────────

function renderStats() {
    if (!state.stats) return;
    const s = state.stats.stages || {};
    document.querySelector('[data-stat=keyed]').textContent    = (s.keyed    || 0).toLocaleString('th-TH');
    document.querySelector('[data-stat=closed]').textContent   = (s.closed   || 0).toLocaleString('th-TH');
    document.querySelector('[data-stat=approved]').textContent = (s.approved || 0).toLocaleString('th-TH');
    document.querySelector('[data-stat=debt]').textContent     = (s.debt     || 0).toLocaleString('th-TH');

    function pct(num, denom) {
        if (!denom) return '';
        const d = Math.round(((denom - num) / denom) * 1000) / 10;
        return d > 0 ? `▼ ${d}% drop-off` : '';
    }
    document.querySelector('[data-dropoff=keyed]').textContent    = pct(s.keyed,    s.closed);
    document.querySelector('[data-dropoff=approved]').textContent = pct(s.approved, s.keyed);
    document.querySelector('[data-dropoff=debt]').textContent     = pct(s.debt,     s.approved);
}

function renderCharts() {
    if (!state.stats) return;
    renderFunnel();
    renderTrend();
}

function renderFunnel() {
    const s = state.stats.stages || {};
    const labels = ['จบงาน', 'บันทึกงาน', 'อนุมัติ', 'ตัดหนี้'];
    const data = [s.closed || 0, s.keyed || 0, s.approved || 0, s.debt || 0];
    const ctx = document.getElementById('funnelChart').getContext('2d');
    if (funnelChart) funnelChart.destroy();
    funnelChart = new Chart(ctx, {
        type: 'bar',
        data: {
            labels,
            datasets: [{
                data,
                backgroundColor: ['#3b82f6', '#22c55e', '#f59e0b', '#ef4444'],
                borderRadius: 6,
            }],
        },
        options: {
            indexAxis: 'y',
            maintainAspectRatio: false,
            plugins: { legend: { display: false } },
            scales: {
                x: { ticks: { color: '#94a3b8' }, grid: { color: '#334155' } },
                y: { ticks: { color: '#cbd5e1' }, grid: { display: false } },
            },
        },
    });
}

function renderTrend() {
    const ts = state.stats.timeseries || [];
    const labels = ts.map(p => p.d);
    const ctx = document.getElementById('trendChart').getContext('2d');
    if (trendChart) trendChart.destroy();
    trendChart = new Chart(ctx, {
        type: 'line',
        data: {
            labels,
            datasets: [
                { label: 'จบงาน',   data: ts.map(p => p.closed),   borderColor: '#3b82f6', backgroundColor: '#3b82f640', tension: 0.3 },
                { label: 'บันทึกงาน', data: ts.map(p => p.keyed),    borderColor: '#22c55e', backgroundColor: '#22c55e40', tension: 0.3 },
                { label: 'อนุมัติ',  data: ts.map(p => p.approved), borderColor: '#f59e0b', backgroundColor: '#f59e0b40', tension: 0.3 },
                { label: 'ตัดหนี้',  data: ts.map(p => p.debt),     borderColor: '#ef4444', backgroundColor: '#ef444440', tension: 0.3 },
            ],
        },
        options: {
            maintainAspectRatio: false,
            plugins: { legend: { labels: { color: '#cbd5e1', font: { size: 11 } } } },
            scales: {
                x: { ticks: { color: '#94a3b8', maxRotation: 0, autoSkipPadding: 20 }, grid: { display: false } },
                y: { ticks: { color: '#94a3b8' }, grid: { color: '#334155' } },
            },
        },
    });
}

function renderTable() {
    const tbody = document.getElementById('jobsTbody');
    if (!state.rows.length) {
        const hint = (document.getElementById('fromDate').value || document.getElementById('toDate').value)
            ? 'ไม่มีข้อมูลในช่วงวันที่นี้ — ลองเลือก "ทั้งหมด" หรือขยาย range'
            : 'ไม่มีข้อมูล — ลองคลิก 🔄 Refresh';
        tbody.innerHTML = `<tr><td colspan="7" class="muted" style="text-align:center; padding:32px;">${hint}</td></tr>`;
        return;
    }
    tbody.innerHTML = state.rows.map(r => {
        const idx = r.status_index || 0;
        const status = r.current_status || '—';
        const link = `/job/${encodeURIComponent(r.claim_canonical)}/${encodeURIComponent(r.survey_canonical || '')}`;
        return `<tr class="clickable" onclick="location.href='${link}'">
            <td>${esc(r.claim_display || r.claim_canonical)}</td>
            <td>${esc(r.invoice_display || r.invoice_canonical || '—')}</td>
            <td><span class="status-badge s${idx}">${esc(status)}</span></td>
            ${stageCell(r.closed, r.closed_at)}
            ${stageCell(r.keyed, r.keyed_at)}
            ${stageCell(r.approved, r.approved_at)}
            ${stageCell(r.debt, r.debt_cut_date)}
        </tr>`;
    }).join('');
}

function stageCell(done, when) {
    if (done) {
        return `<td class="stage-cell"><span class="icon done" title="${esc(when || '')}">✓</span></td>`;
    }
    return `<td class="stage-cell"><span class="icon miss">—</span></td>`;
}

function renderSyncStatus() {
    if (!state.sourceStatus) return;
    const { adapters, last_sync, row_counts, last_rebuild_at } = state.sourceStatus;
    const sidebar = document.getElementById('syncStatus');
    const dotColor = { ok: '#22c55e', err: '#ef4444', running: '#facc15' };
    sidebar.innerHTML = adapters.map(a => {
        const s = last_sync[a.name];
        const status = s?.status || 'never';
        const cls = status === 'ok' ? 'ok' : (status === 'error' ? 'err' : (status === 'running' ? 'running' : ''));
        const when = s?.finished_at || s?.started_at || '';
        const meta = s?.error ? `<div class="muted" style="font-size:11px; margin-left:14px;">${esc(s.error.slice(0, 80))}</div>` : '';
        const bg = dotColor[cls] || '#475569';
        const anim = cls === 'running' ? 'animation:pulse 1.2s infinite;' : '';
        return `<div style="margin:6px 0;">
            <span class="dot ${cls}" style="display:inline-block; width:8px; height:8px; border-radius:50%; background:${bg}; margin-right:8px; ${anim}"></span>
            <strong>${esc(sourceLabel(a.name))}</strong> <span class="muted">${esc(when.slice(11, 19) || '—')}</span>
            ${meta}
        </div>`;
    }).join('');

    const strip = document.getElementById('syncStrip');
    strip.innerHTML = adapters.map(a => {
        const s = last_sync[a.name];
        const status = s?.status || 'never';
        const cls = status === 'ok' ? 'ok' : (status === 'error' ? 'err' : '');
        const seen = s?.rows_seen != null ? `${s.rows_seen.toLocaleString('th-TH')} แถว` : '—';
        return `<div class="pill"><span class="dot ${cls}"></span><strong>${esc(sourceLabel(a.name))}</strong> · ${seen}</div>`;
    }).join('') + ` <div class="pill"><span>jobs_view: ${(row_counts?.jobs || 0).toLocaleString('th-TH')} แถว</span></div>`
       + (last_rebuild_at ? ` <div class="pill muted">rebuild: ${esc(last_rebuild_at)}</div>` : '');
}

// ── Sync (SSE) ────────────────────────────────────────────────────────

function startSync() {
    const overlay = document.getElementById('progress');
    const log = document.getElementById('progressLog');
    overlay.classList.add('active');
    log.innerHTML = '';
    document.getElementById('refreshBtn').disabled = true;

    const es = new EventSource('/fetch-stream?source=all');
    es.addEventListener('start', () => addLog('เริ่ม sync...'));
    es.addEventListener('progress', (e) => {
        const p = JSON.parse(e.data);
        let line;
        if (p.type === 'start') line = `▶ ${sourceLabel(p.source)}`;
        else if (p.type === 'done') {
            const cls = p.error ? 'err' : 'ok';
            line = `<span class="${cls}">${p.error ? '✗' : '✓'}</span> ${sourceLabel(p.source)} — ${p.rows_changed || 0} changed${p.error ? ` (${p.error.slice(0,60)})` : ''}`;
        }
        else if (p.type === 'skipped') line = `⚠ ${sourceLabel(p.source)} skipped: ${p.reason}`;
        else if (p.type === 'rebuild_start') line = `↻ rebuilding jobs_view... (${p.touched} touched)`;
        else if (p.type === 'rebuild_done') line = `<span class="ok">✓</span> rebuild: ${p.rows} rows`;
        else if (p.type === 'error') line = `<span class="err">✗ ${p.error}</span>`;
        else line = JSON.stringify(p);
        addLog(line);
    });
    es.addEventListener('end', () => {
        addLog('เสร็จสิ้น');
        es.close();
        document.getElementById('refreshBtn').disabled = false;
        setTimeout(() => overlay.classList.remove('active'), 2000);
        refreshAll();
        fetchStatus();
    });
    es.addEventListener('error', () => {
        addLog('<span class="err">ขาดการเชื่อมต่อ</span>');
        es.close();
        document.getElementById('refreshBtn').disabled = false;
    });
}

function addLog(html) {
    const log = document.getElementById('progressLog');
    const div = document.createElement('div');
    div.className = 'line';
    div.innerHTML = html;
    log.appendChild(div);
    log.scrollTop = log.scrollHeight;
}

function exportXlsx() {
    const params = new URLSearchParams(currentFilters());
    params.delete('limit');
    window.location.href = '/api/jobs/export.xlsx?' + params;
}

function fmt(n) {
    if (n == null) return '—';
    return Number(n).toLocaleString('th-TH', { maximumFractionDigits: 2 });
}

function esc(s) {
    if (s == null) return '';
    return String(s).replace(/[&<>"']/g, c => ({
        '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
    }[c]));
}
