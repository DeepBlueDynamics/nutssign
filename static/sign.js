// Signer page: highlights the signer's fields, collects a drawn or typed
// signature, and posts it. All of the signer's fields are filled at once.
(function () {
  const payload = JSON.parse(document.getElementById('payload').textContent);
  const consent = document.getElementById('consent');
  const openBtn = document.getElementById('open-sign');
  const modal = document.getElementById('modal');
  const pad = document.getElementById('pad');
  const ctx = pad.getContext('2d');
  const typed = document.getElementById('typed');
  const preview = document.getElementById('typed-preview');
  const errBox = document.getElementById('sign-error');
  let mode = 'draw';
  let strokes = 0;
  let drawing = false;
  const checks = {};  // checkbox field id -> true
  const gate = document.getElementById('gate-status');

  function requiredGroups() {
    const groups = {};
    payload.fields.forEach(function (f) {
      if (f.kind !== 'checkbox' || !f.required) return;
      const g = f.group || f.id;
      groups[g] = groups[g] || (checks[f.id] === true);
    });
    return groups;
  }

  function updateGate() {
    const groups = requiredGroups();
    const open = Object.keys(groups).filter(function (g) { return !groups[g]; }).length;
    if (gate) {
      const pages = {};
      payload.fields.forEach(function (f) { if (f.kind === 'checkbox' && f.required && !groups[f.group || f.id]) pages[f.page] = true; });
      gate.textContent = open ? ('Mark one box in each of ' + open + ' statement' + (open === 1 ? '' : 's') + ' (page ' + Object.keys(pages).join(', ') + ') before signing.') : '';
    }
    openBtn.disabled = !(consent.checked && open === 0);
  }

  function toggleCheck(f) {
    if (checks[f.id]) { delete checks[f.id]; }
    else {
      payload.fields.forEach(function (o) { if (o.kind === 'checkbox' && f.group && o.group === f.group) delete checks[o.id]; });
      checks[f.id] = true;
    }
    renderFields();
    updateGate();
  }

  function renderFields() {
    document.querySelectorAll('.page').forEach(function (pageEl) {
      const n = parseInt(pageEl.dataset.page, 10);
      const img = pageEl.querySelector('img');
      const overlay = pageEl.querySelector('.overlay');
      const size = payload.page_sizes[n - 1];
      if (!img.clientWidth || !size) return;
      const scale = img.clientWidth / size[0];
      overlay.innerHTML = '';
      payload.fields.filter(function (f) { return f.page === n; }).forEach(function (f) {
        const box = document.createElement('div');
        box.className = 'field' + (f.kind === 'checkbox' ? ' checkbox' + (checks[f.id] ? ' on' : '') : '');
        box.style.left = (f.x * scale) + 'px';
        box.style.top = (f.y * scale) + 'px';
        box.style.width = (f.w * scale) + 'px';
        box.style.height = (f.h * scale) + 'px';
        if (f.kind === 'checkbox') {
          box.title = 'Click to mark this box';
          box.addEventListener('click', function () { toggleCheck(f); });
        } else {
          box.textContent = f.kind === 'signature' ? 'Sign here' : f.kind === 'date' ? 'Date (auto)' : f.kind;
        }
        overlay.appendChild(box);
      });
    });
  }
  updateGate();
  document.querySelectorAll('.page img').forEach(function (img) {
    if (img.complete) renderFields(); else img.addEventListener('load', renderFields);
  });
  window.addEventListener('resize', renderFields);

  // ----- canvas pad (HiDPI aware)
  function sizePad() {
    const ratio = window.devicePixelRatio || 1;
    const w = pad.clientWidth, h = pad.clientHeight;
    const snapshot = strokes ? pad.toDataURL() : null;
    pad.width = Math.round(w * ratio); pad.height = Math.round(h * ratio);
    ctx.setTransform(ratio, 0, 0, ratio, 0, 0);
    ctx.lineWidth = 2.4; ctx.lineCap = 'round'; ctx.lineJoin = 'round'; ctx.strokeStyle = '#1a237e';
    if (snapshot) { const im = new Image(); im.onload = function () { ctx.drawImage(im, 0, 0, w, h); }; im.src = snapshot; }
  }
  function pos(ev) { const r = pad.getBoundingClientRect(); return [ev.clientX - r.left, ev.clientY - r.top]; }
  pad.addEventListener('pointerdown', function (ev) { drawing = true; pad.setPointerCapture(ev.pointerId); const p = pos(ev); ctx.beginPath(); ctx.moveTo(p[0], p[1]); });
  pad.addEventListener('pointermove', function (ev) { if (!drawing) return; const p = pos(ev); ctx.lineTo(p[0], p[1]); ctx.stroke(); strokes++; });
  function stop(ev) { if (drawing) { drawing = false; } }
  pad.addEventListener('pointerup', stop); pad.addEventListener('pointercancel', stop); pad.addEventListener('pointerleave', stop);
  document.getElementById('clear-pad').addEventListener('click', function () { strokes = 0; sizePad(); ctx.clearRect(0, 0, pad.width, pad.height); });

  // ----- modal
  consent.addEventListener('change', updateGate);
  openBtn.addEventListener('click', function () { modal.hidden = false; errBox.hidden = true; requestAnimationFrame(sizePad); });
  function close() { modal.hidden = true; }
  document.getElementById('close-modal').addEventListener('click', close);
  document.getElementById('cancel-sign').addEventListener('click', close);
  document.querySelectorAll('.tab').forEach(function (t) {
    t.addEventListener('click', function () {
      mode = t.dataset.tab;
      document.querySelectorAll('.tab').forEach(function (x) { x.classList.toggle('active', x === t); });
      document.getElementById('pane-draw').hidden = mode !== 'draw';
      document.getElementById('pane-type').hidden = mode !== 'type';
      if (mode === 'draw') requestAnimationFrame(sizePad);
    });
  });
  typed.addEventListener('input', function () { preview.textContent = typed.value; });

  // ----- submit
  document.getElementById('adopt').addEventListener('click', function () {
    const body = { consent: consent.checked, checks: checks };
    if (mode === 'draw') {
      if (strokes < 3) { showError('Please draw your signature first.'); return; }
      body.kind = 'drawn';
      body.image = pad.toDataURL('image/png');
    } else {
      if (!typed.value.trim()) { showError('Please type your name.'); return; }
      body.kind = 'typed';
      body.typed_name = typed.value.trim();
    }
    const btn = document.getElementById('adopt');
    btn.disabled = true; btn.textContent = 'Signing…';
    fetch('/sign/' + payload.token, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) })
      .then(function (r) { return r.json().then(function (j) { return { ok: r.ok, j: j }; }); })
      .then(function (res) {
        if (res.ok && res.j.next) { window.location = res.j.next; return; }
        showError((res.j && (res.j.detail || res.j.error)) || 'Signing failed. Please try again.');
        btn.disabled = false; btn.textContent = 'Adopt and Sign';
      })
      .catch(function () { showError('Network error. Please try again.'); btn.disabled = false; btn.textContent = 'Adopt and Sign'; });
  });
  function showError(msg) { errBox.textContent = msg; errBox.hidden = false; }
})();
