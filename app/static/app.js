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
  const say = (node, ok, msg) => { node.textContent = msg; node.className = ok ? 'ok' : 'err'; };

  $('logout').onclick = async () => { await call('POST', '/logout'); location.href = '/login'; };
  $('print-btn').onclick = () => window.print();
  document.querySelectorAll('[data-open]').forEach((a) => a.onclick = () => { $(a.dataset.open).open = true; });

  // ---------- edit / add medicine ----------
  const dlg = $('med-dialog'), form = $('med-form');
  let editId = null;
  const TEXT = ['name', 'dose', 'form', 'when_en', 'purpose', 'notes_en', 'instruction', 'purpose_ar', 'notes_ar'];
  const openMed = (title, v) => {
    form.reset(); $('med-error').textContent = '';
    $('med-title').textContent = title;
    TEXT.forEach((k) => { form[k].value = (v && v[k]) || ''; });
    form.section.value = v && v.frequency === 'as_needed' ? 'as_needed' : 'regular';
    $('med-delete').hidden = !editId;
    dlg.showModal();
  };
  $('add-med').onclick = () => { editId = null; openMed('Add medicine'); };
  $('med-cancel').onclick = () => dlg.close();
  document.querySelectorAll('[data-edit]').forEach((b) => b.onclick = async () => {
    const {ok, data} = await call('GET', '/api/medicines/' + b.dataset.edit);
    if (!ok) return alert(data.error);
    editId = b.dataset.edit; openMed('Edit medicine', data);
  });
  form.onsubmit = async (e) => {
    e.preventDefault();
    const body = {section: form.section.value};
    TEXT.forEach((k) => { body[k] = form[k].value; });
    $('med-save').disabled = true;
    const {ok, data} = editId ? await call('PUT', '/api/medicines/' + editId, body) : await call('POST', '/api/medicines', body);
    $('med-save').disabled = false;
    if (!ok) { $('med-error').textContent = data.error; return; }
    location.reload();
  };
  $('med-delete').onclick = async () => {
    if (!confirm('Delete this medicine? Its reminders are cancelled the next time you apply.')) return;
    await call('DELETE', '/api/medicines/' + editId); location.reload();
  };

  // ---------- reminder (the bell) ----------
  const rdlg = $('rem-dialog'), rform = $('rem-form');
  let remId = null;
  const syncRem = () => {
    const f = rform.frequency.value;
    $('r-weekly').hidden = f !== 'weekly'; $('r-everyn').hidden = f !== 'every_n';
  };
  const addTime = (v) => {
    const row = el('div', null, 'timerow');
    const i = el('input'); i.type = 'time'; i.value = v || ''; i.className = 't';
    const x = el('button', '✕', 'btn small'); x.type = 'button'; x.setAttribute('aria-label', 'Remove time');
    x.onclick = () => row.remove();
    row.append(i, x); $('r-times').append(row);
  };
  rform.frequency.onchange = syncRem;
  $('r-add-time').onclick = () => addTime('');
  document.querySelectorAll('[data-preset]').forEach((b) => b.onclick = () => {
    const blank = [...document.querySelectorAll('#r-times .t')].find((i) => !i.value);
    if (blank) blank.value = b.dataset.preset; else addTime(b.dataset.preset);
  });
  document.querySelectorAll('[data-remind]').forEach((b) => b.onclick = async () => {
    const {ok, data} = await call('GET', '/api/medicines/' + b.dataset.remind);
    if (!ok) return alert(data.error);
    remId = b.dataset.remind;
    rform.reset(); $('r-times').replaceChildren(); $('rem-error').textContent = '';
    $('rem-title').textContent = 'Reminder — ' + data.name;
    $('rem-hint').textContent = [data.when_en, data.instruction].filter(Boolean).join('  ·  ');
    rform.frequency.value = ['daily', 'weekly', 'every_n'].includes(data.frequency) ? data.frequency : '';
    rform.every_n.value = data.every_n || ''; rform.date.value = data.date || ''; rform.date_kind.value = data.date_kind || 'start';
    rform.querySelectorAll('[name=wd]').forEach((c) => { c.checked = (data.weekdays || []).includes(Number(c.value)); });
    (data.times && data.times.length ? data.times : ['']).forEach(addTime);
    $('rem-off').hidden = !data.remind;
    $('rem-save').textContent = data.remind ? 'Save reminder' : 'Turn reminder on';
    syncRem(); rdlg.showModal();
  });
  $('rem-cancel').onclick = () => rdlg.close();
  rform.onsubmit = async (e) => {
    e.preventDefault();
    const body = {on: true, frequency: rform.frequency.value, every_n: rform.every_n.value, date: rform.date.value,
      date_kind: rform.date_kind.value, weekdays: [...rform.querySelectorAll('[name=wd]:checked')].map((c) => Number(c.value)),
      times: [...document.querySelectorAll('#r-times .t')].map((i) => i.value).filter(Boolean)};
    const {ok, data} = await call('POST', '/api/medicines/' + remId + '/reminder', body);
    if (!ok) { $('rem-error').textContent = data.error; return; }
    location.reload();
  };
  $('rem-off').onclick = async () => { await call('POST', '/api/medicines/' + remId + '/reminder', {on: false}); location.reload(); };

  // ---------- Excel import ----------
  $('xlsx').onchange = async (e) => {
    const f = e.target.files[0]; if (!f) return;
    const hasRows = document.querySelectorAll('.chart-table tbody tr').length > 0;
    const replace = hasRows ? confirm('Replace ALL current medicines with this file?\n\nOK = replace everything (reminders of removed medicines are cancelled when you apply).\nCancel = keep the current ones and only add new ones.') : false;
    const fd = new FormData(); fd.append('file', f);
    say($('import-result'), true, 'Importing…');
    const {ok, data} = await call('POST', '/api/medicines-import?replace=' + replace, fd);
    if (!ok) return say($('import-result'), false, data.error);
    say($('import-result'), true, 'Added ' + data.added + (data.skipped ? ', skipped ' + data.skipped + ' duplicate(s)' : '') + '.');
    setTimeout(() => location.reload(), 800);
  };

  // ---------- review & apply ----------
  const rv = $('review-dialog');
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
  const loadPlan = async () => {
    $('apply-result').replaceChildren(); $('plan-error').textContent = ''; $('plan').hidden = true; $('apply-btn').disabled = true;
    await call('POST', '/api/settings', {copy_to_me: $('copy').checked});
    const {ok, data} = await call('POST', '/api/sync/preview');
    if (!ok) { $('plan-error').textContent = data.error; return; }
    planHash = data.hash;
    render($('cancel-list'), data.cancel, 'Cancel'); render($('create-list'), data.create, 'Create');
    $('n-cancel').textContent = data.cancel.length; $('n-create').textContent = data.create.length;
    $('approve').checked = false; $('plan').hidden = false;
    if (!data.cancel.length && !data.create.length) $('apply-result').append(el('li', 'Already in sync — nothing to do.', 'ok'));
  };
  if ($('review-btn')) $('review-btn').onclick = () => { rv.showModal(); loadPlan(); };
  $('review-close').onclick = () => { rv.close(); location.reload(); };
  $('copy').onchange = loadPlan;
  $('approve').onchange = (e) => { $('apply-btn').disabled = !(e.target.checked && planHash); };
  $('apply-btn').onclick = async (e) => {
    e.target.disabled = true;
    const {ok, data} = await call('POST', '/api/sync/apply', {hash: planHash});
    const out = $('apply-result'); out.replaceChildren();
    if (!ok) { out.append(el('li', data.error, 'err')); return; }
    data.results.forEach((r) => out.append(el('li', (r.ok ? '✔ ' : '✖ ') + r.action + (r.error ? ' — ' + r.error : '') + (r.warning ? ' ⚠ ' + r.warning : ''), r.ok ? 'ok' : 'bad')));
    $('plan').hidden = true; planHash = null;
  };

  // ---------- Mom ----------
  $('status-form').onsubmit = async (e) => {
    e.preventDefault(); const f = e.target;
    const {ok, data} = await call('POST', '/api/status-log', {kind: f.kind.value, value: f.value.value, note: f.note.value});
    if (!ok) { $('status-error').textContent = data.error; return; }
    $('mom-panel').dataset.keep = '1'; location.reload();
  };
  document.querySelectorAll('[data-del-status]').forEach((b) => b.onclick = async () => {
    if (!confirm('Delete this entry?')) return;
    await call('DELETE', '/api/status-log/' + b.dataset.delStatus); location.reload();
  });
  if ($('clear-log')) $('clear-log').onclick = async () => {
    if (!confirm('Delete the whole status log?')) return;
    await call('DELETE', '/api/status-log'); location.reload();
  };
  $('profile-form').onsubmit = async (e) => {
    e.preventDefault(); const f = e.target, body = {};
    ['name', 'age', 'conditions', 'allergies', 'doctor', 'doctor_phone', 'emergency_contact', 'pharmacy', 'notes']
      .forEach((k) => { body[k] = f[k].value; });
    const {ok, data} = await call('PUT', '/api/profile', body);
    say($('profile-result'), ok, ok ? 'Saved ✔ (reload to see it in the table header)' : data.error);
  };

  // ---------- Wbiztool ----------
  const cf = $('conf-form');
  const confBody = () => ({client_id: cf.client_id.value, api_key: cf.api_key.value, whatsapp_client: cf.whatsapp_client.value,
    mom_number: cf.mom_number.value, my_number: cf.my_number.value, docs_verified: cf.docs_verified.checked});
  const saveConf = async () => {
    const {ok, data} = await call('PUT', '/api/config', confBody());
    if (!ok) { say($('conf-result'), false, data.error); return false; }
    cf.api_key.value = '';
    cf.api_key.placeholder = data.api_key_set ? '•••••• saved (' + data.api_key_length + ' characters) — leave blank to keep' : 'Paste the API key';
    say($('conf-result'), true, 'Saved ✔'); return true;
  };
  cf.onsubmit = async (e) => { e.preventDefault(); await saveConf(); };
  $('conf-test').onclick = async () => {
    say($('conf-result'), true, 'Saving and testing…');
    if (!(await saveConf())) return;
    const {ok, data} = await call('POST', '/api/config/test');
    if (!ok) return say($('conf-result'), false, 'Failed: ' + data.error);
    say($('conf-result'), true, 'Credentials OK ✔' + (data.account ? ' (' + data.account + ')' : '') +
      (data.whatsapp_error ? ' — the WhatsApp status check failed: ' + data.whatsapp_error.slice(0, 120) : ''));
  };
  $('test-btn').onclick = async (e) => {
    e.target.disabled = true; say($('test-result'), true, 'Sending…');
    const {ok, data} = await call('POST', '/api/test-message');
    say($('test-result'), ok, ok ? 'Test message sent ✔' : 'Failed: ' + data.error);
    e.target.disabled = false;
  };
  fetch('/api/status', {credentials: 'same-origin'}).then((r) => r.json()).then((s) => {
    const box = $('wb-status');
    if (s.error) { box.textContent = '· ' + s.error; box.className = 'err'; return; }
    box.textContent = '· connected ✔' + (s.whatsapp_error ? ' (status page unavailable)' : '');
    box.className = 'ok';
  }).catch(() => {});
})();
