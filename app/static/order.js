// Salesman screens: order entry + offline queue. Orders carry a client_uuid so retries are idempotent on the server.
(function () {
  const Q = 'oa_queue', CAT = 'oa_catalog';
  const $ = s => document.querySelector(s);
  const load = (k, d) => { try { return JSON.parse(localStorage.getItem(k)) ?? d; } catch (e) { return d; } };
  const save = (k, v) => localStorage.setItem(k, JSON.stringify(v));
  const uuid = () => (crypto.randomUUID ? crypto.randomUUID() : 'xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx'.replace(/[xy]/g, c => { const r = Math.random() * 16 | 0; return (c === 'x' ? r : (r & 3 | 8)).toString(16); }));
  const money = p => '₹' + (p / 100).toLocaleString('en-IN', { minimumFractionDigits: 2, maximumFractionDigits: 2 });

  async function post(payload) {
    const r = await fetch('/m/api/orders', { method: 'POST', credentials: 'same-origin', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(payload) });
    if (r.status === 401 || r.redirected) throw new Error('login');
    return { status: r.status, body: await r.json().catch(() => ({})) };
  }

  async function flush() {
    const q = load(Q, []); if (!q.length) return paintQueue();
    const keep = [];
    for (const o of q) {
      try {
        const res = await post(o);
        if (res.status >= 500) keep.push(o);              // server trouble: retry later
        else if (res.status >= 400) { const f = load('oa_failed', []); f.push({ o, err: res.body.error }); save('oa_failed', f); }
      } catch (e) { keep.push(o); }                        // offline: retry later
    }
    save(Q, keep); paintQueue();
  }

  function paintQueue() {
    const q = load(Q, []), el = $('#queue');
    if (el) el.innerHTML = q.length ? `<div class="warnbox">${q.length} order(s) saved offline - will send when you are back online.</div>` : '';
  }

  async function catalog() {
    try { const r = await fetch('/m/api/catalog', { credentials: 'same-origin' }); if (r.ok) save(CAT, await r.json()); } catch (e) { }
    return load(CAT, null);
  }

  async function initForm() {
    const cat = await catalog(), msg = $('#msg');
    if (!cat) { msg.textContent = 'Open this page once with network to download customers and prices.'; return; }
    const sel = $('#customer'); sel.innerHTML = cat.customers.map(c => `<option value="${c.id}">${c.name} (${c.type})</option>`).join('');
    const box = $('#products'), cart = {};
    const price = (cid, pid) => (cat.prices[cid] || {})[pid] || 0;
    function draw() {
      const cid = sel.value;
      box.innerHTML = cat.products.map(p => `<div class="card"><div class="row" style="justify-content:space-between;align-items:center">
        <div><b>${p.name}</b><div class="mute">${money(price(cid, p.id))} per packet</div></div>
        <div class="stepper"><button type="button" class="sec" data-p="${p.id}" data-d="-1">-</button>
        <input type="number" min="0" inputmode="numeric" id="q${p.id}" value="${cart[p.id] ? cart[p.id].qty : 0}" data-p="${p.id}">
        <button type="button" class="sec" data-p="${p.id}" data-d="1">+</button>
        <select id="u${p.id}">${p.units.map(u => `<option ${cart[p.id] && cart[p.id].unit === u ? 'selected' : ''}>${u}</option>`).join('')}</select></div></div></div>`).join('');
      total();
    }
    function read() {
      return cat.products.map(p => ({ item_id: p.id, qty: parseInt($('#q' + p.id).value || '0', 10), unit: $('#u' + p.id).value })).filter(l => l.qty > 0);
    }
    function total() {
      const cid = sel.value; let t = 0;
      read().forEach(l => { const p = cat.products.find(x => x.id === l.item_id); t += l.qty * (l.unit === 'carton' ? p.carton_size : 1) * price(cid, l.item_id); });
      $('#total').textContent = money(t);
      cat.products.forEach(p => cart[p.id] = { qty: $('#q' + p.id).value, unit: $('#u' + p.id).value });
    }
    box.addEventListener('click', e => { const b = e.target.closest('button[data-d]'); if (!b) return; const i = $('#q' + b.dataset.p); i.value = Math.max(0, (parseInt(i.value || '0', 10)) + parseInt(b.dataset.d, 10)); total(); });
    box.addEventListener('input', total); box.addEventListener('change', total); sel.addEventListener('change', draw); draw();

    $('#submit').addEventListener('click', async () => {
      const lines = read(); if (!lines.length) { msg.className = 'flash error'; msg.textContent = 'Add at least one product.'; return; }
      const payload = { client_uuid: uuid(), customer_id: parseInt(sel.value, 10), lines, notes: $('#notes').value };
      try {
        const res = await post(payload);
        if (res.body.ok) { msg.className = 'flash ok'; msg.textContent = `Order ${res.body.order_no} submitted to the factory.`; cat.products.forEach(p => { cart[p.id] = null; }); draw(); $('#notes').value = ''; }
        else { msg.className = 'flash error'; msg.textContent = res.body.error || 'Could not submit'; }
      } catch (e) {
        const q = load(Q, []); q.push(payload); save(Q, q); paintQueue();
        msg.className = 'flash ok'; msg.textContent = 'No network - order saved on this phone and will be sent automatically.';
        cat.products.forEach(p => { cart[p.id] = null; }); draw();
      }
    });
  }

  window.addEventListener('online', flush);
  document.addEventListener('DOMContentLoaded', () => { paintQueue(); flush(); if ($('#products')) initForm(); });
  setInterval(flush, 30000);
  if ('serviceWorker' in navigator) navigator.serviceWorker.register('/sw.js').catch(() => { });
})();
