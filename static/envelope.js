// Field placement on the admin envelope page. Coordinates are stored in PDF
// points with a top-left origin; the overlay scales them to the rendered image.
(function () {
  const payload = JSON.parse(document.getElementById('payload').textContent);
  const pagesEl = document.getElementById('pages');
  const editable = pagesEl.dataset.editable === 'true';
  const signerName = {};
  payload.signers.forEach(function (s) { signerName[s.id] = s.name + (s.role ? ' (' + s.role + ')' : ''); });

  function label(f) {
    const who = signerName[f.signer_id] || '?';
    if (f.kind === 'checkbox') return '☐ ' + who + (f.required ? ' (required)' : '');
    return (f.kind === 'signature' ? 'Sign' : f.kind === 'date' ? 'Date' : f.kind) + ' · ' + who;
  }

  function render() {
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
        box.className = 'field';
        box.style.left = (f.x * scale) + 'px';
        box.style.top = (f.y * scale) + 'px';
        box.style.width = (f.w * scale) + 'px';
        box.style.height = (f.h * scale) + 'px';
        box.textContent = f.kind === 'checkbox' ? '' : label(f);
        box.title = label(f);
        if (f.kind === 'checkbox') box.classList.add('checkbox');
        if (editable) {
          const del = document.createElement('span');
          del.className = 'del';
          del.textContent = '×';
          del.title = 'Remove field';
          del.addEventListener('click', function (ev) {
            ev.stopPropagation();
            fetch('/envelopes/' + payload.id + '/fields/' + f.id, { method: 'DELETE' }).then(function () {
              payload.fields = payload.fields.filter(function (x) { return x.id !== f.id; });
              render();
            });
          });
          box.appendChild(del);
        }
        overlay.appendChild(box);
      });
    });
  }

  document.querySelectorAll('.page img').forEach(function (img) {
    if (img.complete) render(); else img.addEventListener('load', render);
  });
  window.addEventListener('resize', render);

  if (editable) {
    document.querySelectorAll('.page').forEach(function (pageEl) {
      const overlay = pageEl.querySelector('.overlay');
      overlay.addEventListener('click', function (ev) {
        if (ev.target !== overlay) return;
        const n = parseInt(pageEl.dataset.page, 10);
        const img = pageEl.querySelector('img');
        const size = payload.page_sizes[n - 1];
        const scale = img.clientWidth / size[0];
        const kind = document.getElementById('tool-kind').value;
        const dims = payload.sizes[kind];
        const rect = overlay.getBoundingClientRect();
        const x = (ev.clientX - rect.left) / scale - dims[0] / 2;
        const y = (ev.clientY - rect.top) / scale - dims[1] / 2;
        const body = { signer_id: document.getElementById('tool-signer').value, kind: kind, page: n, x: x, y: y };
        fetch(payload.base + '/fields', {
          method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body)
        }).then(function (r) { return r.json(); }).then(function (f) {
          if (f && f.id) { payload.fields.push(f); render(); }
        });
      });
    });
  }

  document.querySelectorAll('button.copy').forEach(function (b) {
    b.addEventListener('click', function () {
      navigator.clipboard.writeText(b.dataset.copy).then(function () { b.textContent = 'copied'; setTimeout(function () { b.textContent = 'copy'; }, 1500); });
    });
  });
})();
