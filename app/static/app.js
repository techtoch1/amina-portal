(() => {
  const $ = (id) => document.getElementById(id);
  const csrf = document.querySelector('meta[name=csrf]').content;
  const el = (tag, text, cls) => { const e = document.createElement(tag); if (text) e.textContent = text; if (cls) e.className = cls; return e; };
  const call = async (method, url, body) => {
    const opts = {method, credentials: 'same-origin', headers: {'X-CSRF-Token': csrf}};
    if (body instanceof FormData) opts.body = body;
    else if (body !== undefined) { opts.headers['Content-Type'] = 'application/json'; opts.body = JSON.stringify(body); }
    const r = await fetch(url, opts);
    let data = {};
    try { data = await r.json(); } catch (e) { data = {error: 'Unexpected response (HTTP ' + r.status + ')'}; }
    if (!r.ok && !data.error) data.error = data.detail || ('HTTP ' + r.status);
    return {ok: r.ok, data};
  };
  const page = document.body.dataset.page;

  $('logout').onclick = async () => { await call('POST', '/logout'); location.href = '/login'; };

  // ---------------- Medicines ----------------
  if (page === 'medicines') {
    const dlg = $('med-dialog'), form = $('med-form');
    let editId = null;
    const syncFields = () => {
      const f = form.frequency.value;
      $('f-weekly').hidden = f !== 'weekly';
      $('f-everyn').hidden = f !== 'every_n';
      $('f-times').hidden = f === 'as_needed' || f === 'chart_only';
    };
    const addTime = (v) => {
      const row = el('div', null, 'timerow');
      const i = el('input'); i.type = 'time'; i.value = v || ''; i.className = 't';
      const x = el('button', '✕', 'btn small'); x.type = 'button'; x.setAttribute('aria-label', 'Remove time');
      x.onclick = () => row.remove();
      row.append(i, x); $('times').append(row);
    };
    const open = (title, v) => {
      v = v || {};
      form.reset(); $('times').replaceChildren(); $('med-error').textContent = '';
      $('med-title').textContent = title;
      ['name', 'dose', 'purpose', 'instruction', 'when_en', 'purpose_ar', 'notes_en', 'notes_ar'].forEach((k) => { form[k].value = v[k] || ''; });
      form.frequency.value = v.frequency || '';
      form.every_n.value = v.every_n || ''; form.date.value = v.date || ''; form.date_kind.value = v.date_kind || 'start';
      form.querySelectorAll('[name=wd]').forEach((c) => { c.checked = (v.weekdays || []).includes(Number(c.value)); });
      (v.times && v.times.length ? v.times : ['']).forEach(addTime);
      syncFields(); dlg.showModal();
    };
    form.frequency.onchange = syncFields;
    $('add-time').onclick = () => addTime('');
    document.querySelectorAll('[data-preset]').forEach((b) => b.onclick = () => {
      const existing = [...document.querySelectorAll('#times .t')];
      const blank = existing.find((i) => !i.value);
      if (blank) blank.value = b.dataset.preset; else addTime(b.dataset.preset);
    });
    $('add-med').onclick = () => { editId = null; open('Add medicine'); };
    $('med-cancel').onclick = () => dlg.close();
    form.onsubmit = async (e) => {
      e.preventDefault();
      const body = {
        name: form.name.value, dose: form.dose.value, purpose: form.purpose.value, instruction: form.instruction.value,
        when_en: form.when_en.value, purpose_ar: form.purpose_ar.value, notes_en: form.notes_en.value, notes_ar: form.notes_ar.value,
        frequency: form.frequency.value, every_n: form.every_n.value, date: form.date.value, date_kind: form.date_kind.value,
        weekdays: [...form.querySelectorAll('[name=wd]:checked')].map((c) => Number(c.value)),
        times: [...document.querySelectorAll('#times .t')].map((i) => i.value).filter(Boolean),
      };
      $('med-save').disabled = true;
      const {ok, data} = editId ? await call('PUT', '/api/medicines/' + editId, body) : await call('POST', '/api/medicines', body);
      $('med-save').disabled = false;
      if (!ok) { $('med-error').textContent = data.error; return; }
      location.reload();
    };
    document.querySelectorAll('[data-edit]').forEach((b) => b.onclick = async () => {
      const {ok, data} = await call('GET', '/api/medicines/' + b.dataset.edit);
      if (!ok) return alert(data.error);
      editId = b.dataset.edit; open('Edit medicine', data);
    });
    document.querySelectorAll('[data-active]').forEach((b) => b.onclick = async () => {
      await call('POST', '/api/medicines/' + b.dataset.active + '/active', {active: b.dataset.to === '1'}); location.reload();
    });
    document.querySelectorAll('[data-delete]').forEach((b) => b.onclick = async () => {
      if (!confirm('Delete this medicine? Its reminders are cancelled the next time you sync.')) return;
      await call('DELETE', '/api/medicines/' + b.dataset.delete); location.reload();
    });
    $('xlsx').onchange = async (e) => {
      const f = e.target.files[0]; if (!f) return;
      const fd = new FormData(); fd.append('file', f);
      $('import-result').textContent = 'Importing…';
      const {ok, data} = await call('POST', '/api/medicines-import', fd);
      if (!ok) { $('import-result').textContent = data.error; $('import-result').className = 'err'; return; }
      $('import-result').textContent = 'Added ' + data.added + ', skipped ' + data.skipped + ' duplicate(s).';
      $('import-result').className = 'ok';
      setTimeout(() => location.reload(), 900);
    };
  }

  // ---------------- Chart ----------------
  if (page === 'chart') { $('print-btn').onclick = () => window.print(); }

  // ---------------- Mom ----------------
  if (page === 'mom') {
    $('status-form').onsubmit = async (e) => {
      e.preventDefault(); const f = e.target;
      const {ok, data} = await call('POST', '/api/status-log', {kind: f.kind.value, value: f.value.value, note: f.note.value});
      if (!ok) { $('status-error').textContent = data.error; return; }
      location.reload();
    };
    document.querySelectorAll('[data-del-status]').forEach((b) => b.onclick = async () => {
      if (!confirm('Delete this entry?')) return;
      await call('DELETE', '/api/status-log/' + b.dataset.delStatus); location.reload();
    });
    $('profile-form').onsubmit = async (e) => {
      e.preventDefault(); const f = e.target, body = {};
      ['name', 'age', 'conditions', 'allergies', 'doctor', 'doctor_phone', 'emergency_contact', 'pharmacy', 'notes']
        .forEach((k) => { body[k] = f[k].value; });
      const {ok, data} = await call('PUT', '/api/profile', body);
      $('profile-result').textContent = ok ? 'Saved ✔' : data.error;
      $('profile-result').className = ok ? 'ok' : 'err';
    };
  }

  // ---------------- Settings ----------------
  if (page === 'settings') {
    const f = $('conf-form'), out = $('conf-result');
    const show = (ok, msg) => { out.textContent = msg; out.className = ok ? 'ok' : 'err'; };
    const body = () => ({client_id: f.client_id.value, api_key: f.api_key.value, whatsapp_client: f.whatsapp_client.value,
      mom_number: f.mom_number.value, my_number: f.my_number.value, docs_verified: f.docs_verified.checked});
    const save = async () => {
      const {ok, data} = await call('PUT', '/api/config', body());
      if (!ok) { show(false, data.error); return false; }
      f.api_key.value = '';
      f.api_key.placeholder = data.api_key_set ? '•••••• saved (' + data.api_key_length + ' characters) — leave blank to keep' : 'Paste the API key';
      show(true, 'Saved ✔'); return true;
    };
    f.onsubmit = async (e) => { e.preventDefault(); await save(); };
    $('conf-test').onclick = async () => {
      show(true, 'Saving and testing…');
      if (!(await save())) return;
      const {ok, data} = await call('POST', '/api/config/test');
      if (!ok) return show(false, 'Failed: ' + data.error);
      show(true, 'Credentials OK ✔' + (data.account ? ' (' + data.account + ')' : '') +
        (data.whatsapp_error ? ' — but the WhatsApp status check failed: ' + data.whatsapp_error.slice(0, 120) :
          ' · WhatsApp: ' + (typeof data.whatsapp === 'string' ? data.whatsapp : JSON.stringify(data.whatsapp))));
    };
  }

  // ---------------- Reminders / Wbiztool ----------------
  if (page === 'reminders') {
    fetch('/api/status', {credentials: 'same-origin'}).then((r) => r.json()).then((s) => {
      const box = $('wb-status');
      if (s.error) {
        box.textContent = 'Wbiztool: ' + s.error; box.className = 'err';
        if (/api key|auth/i.test(s.error)) {
          $('wb-hint').hidden = false;
          $('wb-hint').textContent = 'Wbiztool was reached but rejected the credentials. Re-enter the API key (and check the client ID) in Settings.';
        }
        return;
      }
      const wa = s.whatsapp || {};
      box.textContent = 'Credentials OK' + (s.whatsapp_error ? ' · WhatsApp status unavailable (the status path in app/wbiztool.py needs checking)' :
        ' · WhatsApp: ' + (wa.data ? JSON.stringify(wa.data) : (wa.message || 'ok')));
      box.className = 'ok';
    }).catch(() => { $('wb-status').textContent = 'Could not load status'; });

    $('test-btn').onclick = async (e) => {
      e.target.disabled = true; $('test-result').textContent = 'Sending…';
      const {ok, data} = await call('POST', '/api/test-message');
      $('test-result').textContent = ok ? 'Test message sent ✔' : 'Failed: ' + data.error;
      $('test-result').className = ok ? 'ok' : 'err';
      e.target.disabled = false;
    };
    $('copy').onchange = (e) => call('POST', '/api/settings', {copy_to_me: e.target.checked});

    let planHash = null;
    const render = (list, items, verb) => {
      list.replaceChildren();
      items.forEach((it) => {
        const li = el('li');
        li.append(el('div', verb + ' · ' + (it.recipient === 'me' ? 'my number' : 'mom') + ' · ' +
          (it.kind === 'cron' ? 'cron ' : 'once at ') + it.when, 'meta'));
        li.append(el('pre', it.message));
        list.append(li);
      });
    };
    $('preview-btn').onclick = async (e) => {
      e.target.disabled = true; $('apply-result').replaceChildren();
      await call('POST', '/api/settings', {copy_to_me: $('copy').checked});
      const {ok, data} = await call('POST', '/api/sync/preview');
      e.target.disabled = false;
      if (!ok) { $('apply-result').append(el('li', data.error, 'err')); return; }
      planHash = data.hash;
      render($('cancel-list'), data.cancel, 'Cancel'); render($('create-list'), data.create, 'Create');
      $('n-cancel').textContent = data.cancel.length; $('n-create').textContent = data.create.length;
      $('n-keep').textContent = data.keep;
      $('approve').checked = false; $('apply-btn').disabled = true;
      $('plan').hidden = false;
      if (!data.cancel.length && !data.create.length) $('apply-result').append(el('li', 'Already in sync — nothing to do.', 'ok'));
    };
    $('approve').onchange = (e) => { $('apply-btn').disabled = !(e.target.checked && planHash); };
    $('apply-btn').onclick = async (e) => {
      e.target.disabled = true;
      const {ok, data} = await call('POST', '/api/sync/apply', {hash: planHash});
      const out = $('apply-result'); out.replaceChildren();
      if (!ok) { out.append(el('li', data.error, 'err')); return; }
      data.results.forEach((r) => out.append(el('li', (r.ok ? '✔ ' : '✖ ') + r.action + (r.error ? ' — ' + r.error : '') + (r.warning ? ' ⚠ ' + r.warning : ''), r.ok ? 'ok' : 'bad')));
      $('plan').hidden = true; planHash = null;
    };
  }
})();
