// Shared SCADA HMI runtime. Polls /api/state every 2s and dispatches a
// "scada:state" CustomEvent with the latest snapshot. Pages listen and update
// their own widgets. Also keeps the alarm banner and sysbar clock in sync.
(function () {
    const POLL_MS = 2000;

    const sysUtc      = document.getElementById('sys-utc');
    const sysSeq      = document.getElementById('sys-seq');
    const linkPlc     = document.getElementById('link-plc');
    const linkBridge  = document.getElementById('link-bridge');
    const banner      = document.getElementById('alarm-banner');
    const bannerText  = document.getElementById('alarm-banner-text');
    const bannerPri   = document.getElementById('alarm-banner-pri');
    const bannerAck   = document.getElementById('alarm-banner-ack');
    const sbBatch     = document.getElementById('sb-batch');
    const sbMode      = document.getElementById('sb-mode');
    const sbAlarm     = document.getElementById('sb-alarm');
    const sbUpdate    = document.getElementById('sb-update');

    const updateClock = () => {
        const d = new Date();
        if (sysUtc) sysUtc.textContent = d.toISOString().substring(11, 19) + 'Z';
    };
    setInterval(updateClock, 1000);
    updateClock();

    const setLink = (el, level) => {
        if (!el) return;
        el.classList.remove('warn', 'bad');
        if (level === 'warn') el.classList.add('warn');
        else if (level === 'bad') el.classList.add('bad');
    };

    const updateBanner = (state) => {
        if (!banner) return;
        banner.classList.remove('alarm', 'warn');
        const level = state.alarm_level || 'normal';
        const priLabel = level === 'trip' ? 'PRI 1' : level === 'warning' ? 'PRI 2' : 'NORMAL';
        if (bannerPri) bannerPri.textContent = priLabel;
        if (bannerText) bannerText.textContent = state.alarm_text || 'No active alarms';
        if (bannerAck) bannerAck.innerHTML = `MODE <span class="v">${state.control_mode || '—'}</span> · STAGE <span class="v">${state.batch_stage || '—'}</span>`;
        if (level === 'trip') banner.classList.add('alarm');
        else if (level === 'warning') banner.classList.add('warn');
    };

    const updateStatusbar = (state) => {
        if (sbBatch)  sbBatch.textContent  = state.batch_stage || '—';
        if (sbMode)   sbMode.textContent   = state.control_mode || '—';
        if (sbAlarm)  {
            sbAlarm.textContent = state.alarm_text || '—';
            sbAlarm.className = 'v' + (state.alarm_level === 'trip' ? ' alarm' : state.alarm_level === 'warning' ? ' warn' : ' ok');
        }
        if (sbUpdate && state.timestamp) sbUpdate.textContent = new Date(state.timestamp).toLocaleTimeString();
        if (sysSeq && state.sequence != null) sysSeq.textContent = String(state.sequence).padStart(6, '0');
    };

    const refresh = async () => {
        try {
            const resp = await fetch('/api/state');
            if (!resp.ok) throw new Error('state ' + resp.status);
            const state = await resp.json();
            setLink(linkPlc, 'ok');
            setLink(linkBridge, 'ok');
            updateBanner(state);
            updateStatusbar(state);
            window.dispatchEvent(new CustomEvent('scada:state', { detail: state }));
        } catch (err) {
            console.warn('scada refresh failed', err);
            setLink(linkBridge, 'bad');
            window.dispatchEvent(new CustomEvent('scada:bridge-error', { detail: err.message }));
        }
    };

    window.ScadaApi = {
        refresh,
        command: async (path, body = {}) => {
            try {
                await fetch(path, {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify(body),
                });
            } catch (e) {
                console.warn('command failed', path, e);
            }
            await refresh();
        },
        fetchJson: async (path) => {
            const r = await fetch(path);
            if (!r.ok) throw new Error(path + ' ' + r.status);
            return r.json();
        },
        fmtNum: (v, d = 1) => (v == null || isNaN(v)) ? '--' : Number(v).toFixed(d),
        fmtInt: (v) => (v == null || isNaN(v)) ? '--' : Math.round(Number(v)).toString(),
    };

    refresh();
    setInterval(refresh, POLL_MS);
})();
