// Upload ตัดหนี้ Excel → debt-api proxy

document.addEventListener('DOMContentLoaded', () => {
    const btn = document.getElementById('uploadBtn');
    const modal = document.getElementById('uploadModal');
    const closeBtn = document.getElementById('uploadClose');
    const dropzone = document.getElementById('dropzone');
    const fileInput = document.getElementById('fileInput');
    const results = document.getElementById('uploadResults');
    const history = document.getElementById('uploadHistory');

    if (!btn) return;

    btn.addEventListener('click', () => {
        modal.style.display = 'flex';
        loadHistory();
    });
    closeBtn.addEventListener('click', () => {
        modal.style.display = 'none';
        // Refresh dashboard data — uploads may have changed stage_debt
        if (typeof refreshAll === 'function') refreshAll();
        if (typeof fetchStatus === 'function') fetchStatus();
    });
    modal.addEventListener('click', (e) => {
        if (e.target === modal) closeBtn.click();
    });

    // File input
    fileInput.addEventListener('change', (e) => uploadFiles(e.target.files));

    // Drag and drop
    ['dragenter', 'dragover'].forEach(ev =>
        dropzone.addEventListener(ev, (e) => { e.preventDefault(); dropzone.classList.add('dragover'); })
    );
    ['dragleave', 'drop'].forEach(ev =>
        dropzone.addEventListener(ev, (e) => { e.preventDefault(); dropzone.classList.remove('dragover'); })
    );
    dropzone.addEventListener('drop', (e) => {
        uploadFiles(e.dataTransfer.files);
    });

    async function uploadFiles(fileList) {
        const files = Array.from(fileList).filter(f => f.name.toLowerCase().endsWith('.xlsx'));
        if (!files.length) {
            addResult({ filename: '(no xlsx files selected)', ok: false, error: 'เลือกไฟล์ .xlsx' });
            return;
        }
        for (const file of files) {
            const placeholder = addResult({ filename: file.name, ok: null, status: 'กำลังอัพโหลด...' });
            try {
                const fd = new FormData();
                fd.append('file', file);
                const r = await fetch('/api/debt/upload', { method: 'POST', body: fd });
                const data = await r.json();
                placeholder.update(data);
            } catch (e) {
                placeholder.update({ filename: file.name, ok: false, error: String(e) });
            }
        }
        await loadHistory();
    }

    function addResult(initial) {
        const div = document.createElement('div');
        div.className = 'upload-result';
        results.appendChild(div);

        function render(d) {
            const ok = d.ok === true;
            div.classList.toggle('ok', ok);
            div.classList.toggle('err', d.ok === false);
            const stats = ok
                ? `added=${(d.added||0).toLocaleString('th-TH')} · updated=${(d.updated||0).toLocaleString('th-TH')} · skipped=${(d.skipped||0).toLocaleString('th-TH')}`
                : (d.status || '');
            div.innerHTML = `
                <div class="name">${ok ? '✓' : (d.ok === false ? '✗' : '⏳')} ${esc(d.filename || '(file)')}</div>
                <div class="stats">${esc(stats)}</div>
                ${d.error ? `<div class="err-msg">${esc(d.error)}</div>` : ''}
            `;
        }

        render(initial);
        return { update: render };
    }

    async function loadHistory() {
        try {
            const r = await fetch('/api/debt/upload-log');
            const data = await r.json();
            const rows = data.rows || [];
            if (!rows.length) {
                history.innerHTML = '<p class="muted">ยังไม่มีประวัติ</p>';
                return;
            }
            history.innerHTML = `
                <table>
                    <thead><tr>
                        <th>เวลา</th><th>ไฟล์</th>
                        <th class="num">เพิ่ม</th><th class="num">อัพเดท</th>
                        <th class="num">ข้าม</th><th>สถานะ</th>
                    </tr></thead>
                    <tbody>${rows.map(r => `
                        <tr>
                            <td>${esc(r.uploaded_at)}</td>
                            <td>${esc(r.filename)}</td>
                            <td class="num">${(r.rows_added||0).toLocaleString('th-TH')}</td>
                            <td class="num">${(r.rows_updated||0).toLocaleString('th-TH')}</td>
                            <td class="num">${(r.rows_skipped||0).toLocaleString('th-TH')}</td>
                            <td>${r.error ? `<span style="color:#f87171;">${esc(r.error.slice(0,40))}</span>` : '<span style="color:#4ade80;">✓ ok</span>'}</td>
                        </tr>
                    `).join('')}</tbody>
                </table>
            `;
        } catch (e) {
            history.innerHTML = `<p class="err">โหลดประวัติไม่ได้: ${esc(String(e))}</p>`;
        }
    }

    function esc(s) {
        if (s == null) return '';
        return String(s).replace(/[&<>"']/g, c => ({
            '&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'
        }[c]));
    }
});
