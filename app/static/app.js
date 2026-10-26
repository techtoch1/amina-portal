(() => {
  const $ = (id) => document.getElementById(id);
  const csrf = document.querySelector('meta[name=csrf]').content;
  const page = document.body.dataset.page;
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
  const say = (node, ok, msg) => { if (!node) return; node.textContent = msg; node.className = ok ? 'ok' : 'err'; };
  const on = (id, fn) => { const n = $(id); if (n) n.onclick = fn; };

  // toast (survives a reload via sessionStorage)
  let toastTimer;
  const toast = (msg, ms) => {
    const t = $('toast'); t.textContent = msg; t.hidden = false;
    clearTimeout(toastTimer); toastTimer = setTimeout(() => { t.hidden = true; }, ms || 6000);
  };
  const toastNext = (msg) => { try { sessionStorage.setItem('toast', msg); } catch (e) { /* private mode */ } };
  try { const m = sessionStorage.getItem('toast'); if (m) { sessionStorage.removeItem('toast'); toast(m, 8000); } } catch (e) { /* ignore */ }

  on('logout', async () => { await call('POST', '/logout'); location.href = '/login'; });
  on('print-btn', () => window.print());

  // ================= Medicines =================
  if (page === 'medicines') {
    // ---- add / edit ----
    const dlg = $('med-dialog'), form = $('med-form');
    let editId = null;
    const TEXT = ['name', 'dose', 'form', 'form_ar', 'when_en', 'purpose', 'notes_en', 'instruction', 'purpose_ar', 'notes_ar'];
    const openMed = (title, v) => {
      form.reset(); $('med-error').textContent = '';
      $('med-title').textContent = title;
      TEXT.forEach((k) => { form[k].value = (v && v[k]) || ''; });
      form.section.value = v && v.frequency === 'as_needed' ? 'as_needed' : 'regular';
      $('med-delete').hidden = !editId;
      dlg.showModal();
    };
    on('add-med', () => { editId = null; openMed('Add medicine'); });
    on('med-cancel', () => dlg.close());
    const openEdit = async (id) => {
      const {ok, data} = await call('GET', '/api/medicines/' + id);
      if (!ok) return toast(data.error);
      editId = String(id); openMed('Edit medicine', data);
    };
    document.querySelectorAll('[data-edit]').forEach((b) => b.onclick = () => openEdit(b.dataset.edit));

    // ---- fill missing Arabic from the glossary ----
    const adlg = $('arabic-dialog');
    const group = (items, key) => items.reduce((acc, it) => { (acc[it.name] = acc[it.name] || []).push(it); return acc; }, {});
    on('arabic-fill', async (e) => {
      e.target.disabled = true;
      const {ok, data} = await call('POST', '/api/arabic/fill');
      e.target.disabled = false;
      if (!ok) return toast(data.error);
      const f = $('arabic-filled'), m = $('arabic-missing');
      f.replaceChildren(); m.replaceChildren();
      f.append(el('h3', 'Filled (' + data.filled.length + ')'));
      const fl = el('ul');
      data.filled.forEach((x) => { const li = el('li'); li.append(el('span', x.name + ' — ' + x.field)); li.append(el('span', x.value, 'ar-val')); li.lastChild.dir = 'rtl'; fl.append(li); });
      f.append(data.filled.length ? fl : el('p', 'Nothing new to fill.', 'muted'));
      m.append(el('h3', 'Still needs you (' + data.missing.length + ')'));
      const ml = el('ul');
      data.missing.forEach((x) => {
        const li = el('li'); li.append(el('span', x.name + ' — ' + x.field + ': "' + x.english + '"'));
        const b = el('button', 'Add Arabic', 'btn small'); b.onclick = () => { adlg.close(); openEdit(x.id); };
        li.append(b); ml.append(li);
      });
      m.append(data.missing.length ? ml : el('p', 'Everything has an Arabic version. ✔', 'ok'));
      adlg.showModal();
    });
    on('arabic-close', () => { adlg.close(); location.reload(); });
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
    on('med-delete', async () => {
      if (!confirm('Delete this medicine? Its reminders are cancelled the next time you apply.')) return;
      await call('DELETE', '/api/medicines/' + editId); location.reload();
    });

    // ---- reminder popup ----
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
    on('r-add-time', () => addTime(''));
    document.querySelectorAll('[data-preset]').forEach((b) => b.onclick = () => {
      const blank = [...document.querySelectorAll('#r-times .t')].find((i) => !i.value);
      if (blank) blank.value = b.dataset.preset; else addTime(b.dataset.preset);
    });
    const openReminder = (id, data) => {
      remId = id;
      rform.reset(); $('r-times').replaceChildren(); $('rem-error').textContent = '';
      const sug = data.suggest || {};
      $('rem-title').textContent = 'Reminder — ' + data.name;
      const said = [data.when_en, data.instruction].filter(Boolean).join('  ·  ');
      $('rem-hint').textContent = said ? 'When to take: ' + said + (!data.remind && (sug.times || []).length ? '  →  suggested times filled in, please check.' : '') : '';
      const freq = data.remind ? data.frequency : (sug.frequency || data.frequency);
      rform.frequency.value = ['daily', 'weekly', 'every_n'].includes(freq) ? freq : '';
      rform.every_n.value = data.every_n || ''; rform.date.value = data.date || ''; rform.date_kind.value = data.date_kind || 'start';
      rform.querySelectorAll('[name=wd]').forEach((c) => { c.checked = (data.weekdays || []).includes(Number(c.value)); });
      const times = data.remind && data.times.length ? data.times : (sug.times || []);
      (times.length ? times : ['']).forEach(addTime);
      $('rem-off').hidden = !data.remind;
      $('rem-save').textContent = data.remind ? 'Save reminder' : 'Turn reminder on';
      syncRem(); rdlg.showModal();
    };
    document.querySelectorAll('[data-remind]').forEach((b) => b.onclick = async () => {
      const {ok, data} = await call('GET', '/api/medicines/' + b.dataset.remind);
      if (!ok) return toast(data.error);
      openReminder(b.dataset.remind, data);
    });
    on('rem-cancel', () => rdlg.close());
    rform.onsubmit = async (e) => {
      e.preventDefault();
      const body = {on: true, frequency: rform.frequency.value, every_n: rform.every_n.value, date: rform.date.value,
        date_kind: rform.date_kind.value, weekdays: [...rform.querySelectorAll('[name=wd]:checked')].map((c) => Number(c.value)),
        times: [...document.querySelectorAll('#r-times .t')].map((i) => i.value).filter(Boolean)};
      const {ok, data} = await call('POST', '/api/medicines/' + remId + '/reminder', body);
      if (!ok) { $('rem-error').textContent = data.error; return; }
      toastNext('Reminder saved. It is not sent to Wbiztool until you Review & apply.'); location.reload();
    };
    on('rem-off', async () => {
      await call('POST', '/api/medicines/' + remId + '/reminder', {on: false});
      toastNext('Reminder turned off.'); location.reload();
    });

    // ---- the switch: one click uses "When to take" when it is clear enough ----
    document.querySelectorAll('[data-toggle]').forEach((sw) => sw.onclick = async () => {
      const id = sw.dataset.toggle;
      if (sw.classList.contains('on')) {
        await call('POST', '/api/medicines/' + id + '/reminder', {on: false});
        toastNext('Reminder turned off.'); return location.reload();
      }
      const {ok, data} = await call('GET', '/api/medicines/' + id);
      if (!ok) return toast(data.error);
      const sug = data.suggest || {};
      if (sug.complete) {
        const r = await call('POST', '/api/medicines/' + id + '/reminder', {on: true, frequency: sug.frequency, times: sug.times});
        if (!r.ok) return openReminder(id, data);
        toastNext('Reminder on at ' + sug.times.join(' · ') + ' (from "When to take"). Check the times, then Review & apply.');
        return location.reload();
      }
      openReminder(id, data);  // needs a day, a date, or the text had no timing
    });

    // ---- Excel import ----
    $('xlsx').onchange = async (e) => {
      const f = e.target.files[0]; if (!f) return;
      const hasRows = document.querySelectorAll('.chart-table tbody tr').length > 0;
      const replace = hasRows ? confirm('Replace ALL current medicines with this file?\n\nOK = replace everything (reminders of removed medicines are cancelled when you apply).\nCancel = keep the current ones and only add new ones.') : false;
      const fd = new FormData(); fd.append('file', f);
      say($('import-result'), true, 'Importing…');
      const {ok, data} = await call('POST', '/api/medicines-import?replace=' + replace, fd);
      if (!ok) return say($('import-result'), false, data.error);
      toastNext('Added ' + data.added + ' medicine' + (data.added === 1 ? '' : 's') + (data.skipped ? ', skipped ' + data.skipped + ' duplicate(s)' : '') + (data.stock_counts ? '; stock counts set for ' + data.stock_counts : '') + '.');
      location.reload();
    };
  }

  // ================= Reminders report =================
  if (page === 'reminders') {
    const rv = $('review-dialog');
    let planHash = null;
    const sendTest = async (btn) => {
      const toMom = btn.dataset.to === 'mom';
      if (toMom && !confirm('Send this test message to MOM now?\n\nIt goes to her WhatsApp number ending …' + (document.body.dataset.momTail || '') +
          ' and starts with a short Arabic note that it is only a test.')) return;
      const label = btn.textContent;
      btn.disabled = true; btn.textContent = 'Sending…';
      const {ok, data} = await call('POST', '/api/test-reminder', {fingerprint: btn.dataset.test, to: toMom ? 'mom' : 'me', confirm: toMom});
      btn.disabled = false;
      btn.textContent = ok ? (toMom ? 'Sent to Mom ✔' : 'Sent ✔ — check your phone') : label;
      if (!ok) { toast('Test failed: ' + data.error, 9000); if ($('plan-error') && rv.open) $('plan-error').textContent = data.error; }
      else toast(toMom ? 'Test message sent to Mom.' : 'Test message sent to your number.');
    };
    document.querySelectorAll('.report [data-test]').forEach((b) => b.onclick = () => sendTest(b));
    const render = (list, items, verb) => {
      list.replaceChildren();
      items.forEach((it) => {
        const li = el('li');
        li.append(el('div', verb + ' · ' + (it.recipient === 'me' ? 'copy to me' : 'Mom') + ' · ' +
          (it.kind === 'cron' ? 'repeats ' : 'once at ') + it.when, 'meta'));
        li.append(el('pre', it.message));
        if (it.fingerprint && verb === 'Create') {
          const row = el('div', null, 'testrow');
          [['me', 'Test to me'], ['mom', 'Test to Mom']].forEach(([to, label]) => {
            const b = el('button', label, 'btn small test-btn' + (to === 'mom' ? ' mom' : '')); b.type = 'button';
            b.dataset.test = it.fingerprint; b.dataset.to = to; b.onclick = () => sendTest(b); row.append(b);
          });
          li.append(row);
        }
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
    const openReview = () => { rv.showModal(); loadPlan(); };
    on('clear-reminders', async () => {
      if (!confirm('Turn off ALL reminders?\n\nEvery reminder switch is switched off. Nothing is cancelled on Wbiztool until you review and approve the list on the next screen. Your times are kept.')) return;
      const {ok, data} = await call('POST', '/api/reminders/clear', {});
      if (!ok) return toast(data.error);
      toastNext(data.turned_off ? data.turned_off + ' reminder' + (data.turned_off === 1 ? '' : 's') + ' turned off. Approve the cancellations below.' : 'No reminders were switched on.');
      location.href = data.turned_off ? '/reminders?review=1' : '/reminders';
    });
    // ---- cleanup of what Wbiztool really holds ----
    const cbody = $('cleanup-table') ? $('cleanup-table').querySelector('tbody') : null;
    let cleanupClient = '';
    const updateCancelBtn = () => { $('cleanup-cancel').disabled = !cbody.querySelector('input:checked'); };
    on('cleanup-load', async (e) => {
      e.target.disabled = true; say($('cleanup-result'), true, 'Loading from Wbiztool…');
      cleanupClient = $('cleanup-client').value.trim();
      const {ok, data} = await call('POST', '/api/wbiz/reminders', {whatsapp_client: cleanupClient});
      e.target.disabled = false;
      $('cleanup-box').hidden = true; $('cleanup-raw-box').hidden = true;
      if (!ok) return say($('cleanup-result'), false, data.error);
      if (!data.items.length) {
        say($('cleanup-result'), true, 'Wbiztool returned no reminders for client ' + data.client + '.');
        if (data.raw) { $('cleanup-raw').textContent = data.raw; $('cleanup-raw-box').hidden = false; }
        return;
      }
      cbody.replaceChildren();
      data.items.forEach((it) => {
        const tr = el('tr'), cb = el('input'); cb.type = 'checkbox'; cb.value = it.id; cb.dataset.tracked = it.tracked ? '1' : '0'; cb.onchange = updateCancelBtn;
        const c0 = el('td'); c0.append(cb); tr.append(c0);
        [it.name || it.id, it.schedule, it.phone, it.status].forEach((v) => tr.append(el('td', v)));
        const c5 = el('td'); c5.append(el('span', it.tracked ? 'in the app' : 'not in the app', 'chip' + (it.tracked ? '' : ' soft'))); tr.append(c5);
        tr.querySelectorAll('td').forEach((td, i) => { td.dataset.label = ['', 'Name', 'Schedule', 'Phone', 'Status', 'In the app?'][i]; });
        cbody.append(tr);
      });
      const orphans = data.items.filter((i) => !i.tracked).length;
      say($('cleanup-result'), true, data.items.length + ' reminder' + (data.items.length === 1 ? '' : 's') + ' on Wbiztool for client ' + data.client + (orphans ? ', ' + orphans + ' not created by this app.' : '.'));
      $('cleanup-box').hidden = false; updateCancelBtn();
    });
    on('cleanup-select-orphans', () => { cbody.querySelectorAll('input').forEach((c) => { c.checked = c.dataset.tracked === '0'; }); updateCancelBtn(); });
    on('cleanup-select-none', () => { cbody.querySelectorAll('input').forEach((c) => { c.checked = false; }); updateCancelBtn(); });
    on('cleanup-cancel', async (e) => {
      const ids = [...cbody.querySelectorAll('input:checked')].map((c) => c.value);
      if (!ids.length) return;
      const tracked = [...cbody.querySelectorAll('input:checked')].filter((c) => c.dataset.tracked === '1').length;
      if (!confirm('Cancel ' + ids.length + ' reminder' + (ids.length === 1 ? '' : 's') + ' on Wbiztool?' + (tracked ? '\n\n' + tracked + ' of them are reminders this app manages; the app will mark them cancelled.' : '') + '\n\nThis cannot be undone here (you would have to create them again).')) return;
      e.target.disabled = true; say($('cleanup-result'), true, 'Cancelling…');
      const {ok, data} = await call('POST', '/api/wbiz/cancel', {ids, whatsapp_client: cleanupClient, confirm: true});
      if (!ok) { e.target.disabled = false; return say($('cleanup-result'), false, data.error); }
      const done = data.results.filter((r) => r.ok).length, failed = data.results.filter((r) => !r.ok);
      say($('cleanup-result'), !failed.length && !data.not_attempted,
        'Cancelled ' + done + ' of ' + ids.length + '.' + (failed.length ? ' Failed: ' + failed.map((f) => f.id + ' (' + f.error.slice(0, 80) + ')').join('; ') : '') +
        (data.not_attempted ? ' ' + data.not_attempted + ' not attempted (Wbiztool stopped answering).' : '') + ' Press "Load list" again to see what is left.');
    });
    on('review-btn', openReview);
    if (rv.dataset.auto === '1' && $('review-btn')) openReview();
    on('review-close', () => { rv.close(); location.href = '/reminders'; });
    $('copy').onchange = loadPlan;
    $('approve').onchange = (e) => { $('apply-btn').disabled = !(e.target.checked && planHash); };
    on('apply-btn', async () => {
      $('apply-btn').disabled = true;
      const {ok, data} = await call('POST', '/api/sync/apply', {hash: planHash});
      const out = $('apply-result'); out.replaceChildren();
      if (!ok) { out.append(el('li', data.error, 'err')); return; }
      data.results.forEach((r) => out.append(el('li', (r.ok ? '✔ ' : '✖ ') + r.action + (r.error ? ' — ' + r.error : '') + (r.warning ? ' ⚠ ' + r.warning : ''), r.ok ? 'ok' : 'bad')));
      $('plan').hidden = true; planHash = null;
    });
    fetch('/api/status', {credentials: 'same-origin'}).then((r) => r.json()).then((s) => {
      if (s.error) { $('wb-dot').textContent = '✖'; $('wb-dot').className = 'num bad'; $('wb-status').textContent = s.error; return; }
      $('wb-dot').textContent = '✔'; $('wb-dot').className = 'num ok';
      $('wb-status').textContent = 'Connected' + (s.whatsapp_error ? ' (status page unavailable)' : '');
    }).catch(() => { $('wb-status').textContent = 'Could not check'; });
  }

  // ================= Stock =================
  if (page === 'stock' && $('stock-root')) {  // the usage report shares this tab but has none of these controls
    const today = $('stock-root').dataset.today;
    const mdlg = $('move-dialog'), mform = $('move-form');
    let moveId = null, counted = false, unitName = 'tablets';
    const KIND_TEXT = {
      buy: ['Quantity added', 'Add what you bought or received.'],
      used: ['Quantity used', 'Use this for doses taken outside the schedule (for example as-needed medicines).'],
      count: ['Number on hand', 'Type the number you really have. The app records the difference as a correction.'],
    };
    const syncKind = () => {
      const k = mform.kind.value;
      $('move-qty-label').firstChild.textContent = KIND_TEXT[k][0] + ' ';
      $('move-hint').textContent = 'Counted in ' + unitName + '. ' + KIND_TEXT[k][1] + (k === 'count' && !counted
        ? ' This is the first count: scheduled use is counted from the day after the date you choose.' : '');
    };
    mform.kind.onchange = syncKind;
    document.querySelectorAll('[data-stock]').forEach((b) => b.onclick = async () => {
      const {ok, data} = await call('GET', '/api/stock/' + b.dataset.id);
      if (!ok) return toast(data.error);
      moveId = b.dataset.id; counted = data.counted; unitName = data.unit;
      mform.reset(); $('move-error').textContent = '';
      mform.kind.value = counted ? b.dataset.stock : 'count';  // the first thing to do with a new medicine is count it
      mform.day.value = today; mform.day.max = today;
      $('move-title').textContent = data.name + ' — ' + (counted ? 'stock' : 'first count');
      syncKind(); mdlg.showModal();
    });
    $('move-cancel').onclick = () => mdlg.close();
    mform.onsubmit = async (e) => {
      e.preventDefault();
      $('move-save').disabled = true;
      const {ok, data} = await call('POST', '/api/stock/' + moveId + '/move',
        {kind: mform.kind.value, qty: mform.qty.value, day: mform.day.value, note: mform.note.value});
      $('move-save').disabled = false;
      if (!ok) { $('move-error').textContent = data.error; return; }
      toastNext('Stock updated.'); location.reload();
    };

    const sdlg = $('st-dialog'), sform = $('st-form');
    let stId = null;
    document.querySelectorAll('[data-stock-settings]').forEach((b) => b.onclick = async () => {
      const {ok, data} = await call('GET', '/api/stock/' + b.dataset.stockSettings);
      if (!ok) return toast(data.error);
      stId = b.dataset.stockSettings;
      sform.unit.value = data.unit === data.unit_default ? '' : data.unit; sform.unit.placeholder = data.unit_default;
      $('st-dose-label').textContent = data.unit.charAt(0).toUpperCase() + data.unit.slice(1) + ' per dose';
      $('st-title').textContent = data.name + ' — stock settings'; $('st-error').textContent = '';
      sform.tracking.checked = data.tracking; sform.units_per_dose.value = data.units_per_dose; sform.daily_use.value = data.daily_use == null ? '' : data.daily_use;
      $('st-hint').textContent = 'How many ' + data.unit + ' are taken each time a dose is due (use 0.5 for half a tablet).' +
        (data.suggested_units > 1 ? ' Suggested from "When to take": ' + data.suggested_units + '.' : '') +
        (data.has_schedule ? '' : ' This medicine has no reminder schedule yet.');
      const list = $('st-moves'); list.replaceChildren();
      if (!data.moves.length) list.append(el('li', 'No stock recorded yet.', 'muted'));
      data.moves.forEach((m) => list.append(el('li', m.day + ' · ' + m.kind + ' · ' + (m.qty > 0 ? '+' : '') + (Math.round(m.qty * 100) / 100) + (m.note ? ' · ' + m.note : ''))));
      sdlg.showModal();
    });
    $('st-cancel').onclick = () => sdlg.close();
    sform.onsubmit = async (e) => {
      e.preventDefault();
      const {ok, data} = await call('PUT', '/api/stock/' + stId + '/settings',
        {tracking: sform.tracking.checked, units_per_dose: sform.units_per_dose.value, daily_use: sform.daily_use.value, unit: sform.unit.value});
      if (!ok) { $('st-error').textContent = data.error; return; }
      toastNext('Saved.'); location.reload();
    };

    $('stock-xlsx').onchange = async (e) => {
      const f = e.target.files[0]; if (!f) return;
      const fd = new FormData(); fd.append('file', f);
      say($('stock-import-result'), true, 'Importing…');
      const {ok, data} = await call('POST', '/api/stock/import', fd);
      if (!ok) return say($('stock-import-result'), false, data.error);
      toastNext('Counts set for ' + data.applied.length + ' medicine(s)' + (data.skipped.length ? '; ' + data.skipped.length + ' already had a count' : '') +
        (data.unmatched.length ? '; not found: ' + data.unmatched.join(', ') : '') + '.');
      location.reload();
    };

    const af = $('alert-form');
    const saveAlerts = async () => {
      const {ok, data} = await call('PUT', '/api/stock/settings', {stock_number: af.stock_number.value, low_days: af.low_days.value, order_days: af.order_days.value});
      say($('alert-result'), ok, ok ? 'Saved ✔' : data.error); return ok;
    };
    af.onsubmit = async (e) => { e.preventDefault(); if (await saveAlerts()) setTimeout(() => location.reload(), 700); };
    $('alert-test').onclick = async () => {
      if (!(await saveAlerts())) return;
      if (!confirm('Send a test alert message to ' + (af.stock_number.value || 'the alert number') + ' now?')) return;
      const {ok, data} = await call('POST', '/api/stock/test-alert', {confirm: true});
      say($('alert-result'), ok, ok ? 'Test alert sent ✔' : 'Failed: ' + data.error);
    };
    $('alert-check').onclick = async () => {
      if (!(await saveAlerts())) return;
      const {ok, data} = await call('POST', '/api/stock/check-alerts', {});
      if (!ok) return say($('alert-result'), false, data.error);
      say($('alert-result'), true, data.sent ? 'Alert sent ✔' : (data.pending.length ? 'Not sent yet (' + data.pending.join(', ') + ' are low) — check Wbiztool settings, or an alert went out within the last hour.' : 'Nothing is low right now.'));
    };
  }

  // ================= Mom =================
  if (page === 'mom') {
    $('status-form').onsubmit = async (e) => {
      e.preventDefault(); const f = e.target;
      const {ok, data} = await call('POST', '/api/status-log', {kind: f.kind.value, value: f.value.value, note: f.note.value, date: f.date.value, time: f.time.value});
      if (!ok) { $('status-error').textContent = data.error; return; }
      toastNext('Added to the log.'); location.reload();
    };
    document.querySelectorAll('[data-del-status]').forEach((b) => b.onclick = async () => {
      if (!confirm('Delete this entry?')) return;
      await call('DELETE', '/api/status-log/' + b.dataset.delStatus); location.reload();
    });
    on('clear-log', async () => {
      if (!confirm('Delete the whole status log?')) return;
      await call('DELETE', '/api/status-log'); location.reload();
    });
    $('profile-form').onsubmit = async (e) => {
      e.preventDefault(); const f = e.target, body = {};
      ['name', 'age', 'conditions', 'allergies', 'doctor', 'doctor_phone', 'emergency_contact', 'pharmacy', 'notes']
        .forEach((k) => { body[k] = f[k].value; });
      const {ok, data} = await call('PUT', '/api/profile', body);
      if (ok) { toastNext('Profile saved.'); location.reload(); } else say($('profile-result'), false, data.error);
    };
  }

  if (page === 'mom' && $('mom-file')) {
    $('mom-file').onchange = async (e) => {
      const f = e.target.files[0]; if (!f) return;
      say($('mom-import-result'), true, 'Importing…');
      const r = await fetch('/api/mom-import?overwrite=' + $('mom-overwrite').checked, {method: 'POST', credentials: 'same-origin',
        headers: {'X-CSRF-Token': csrf, 'Content-Type': 'application/json'}, body: await f.text()});
      let data = {}; try { data = await r.json(); } catch (err) { data = {detail: 'Unexpected response'}; }
      if (!r.ok) return say($('mom-import-result'), false, data.detail || data.error);
      toastNext('Imported: ' + data.profile_fields + ' profile field(s), ' + data.log_added + ' log entr' + (data.log_added === 1 ? 'y' : 'ies') +
        (data.log_skipped ? ', ' + data.log_skipped + ' already there' : '') + '.');
      location.reload();
    };
  }

  // ================= Settings =================
  if (page === 'settings') {
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
    on('conf-test', async () => {
      say($('conf-result'), true, 'Saving and testing…');
      if (!(await saveConf())) return;
      const {ok, data} = await call('POST', '/api/config/test');
      if (!ok) return say($('conf-result'), false, 'Failed: ' + data.error);
      say($('conf-result'), true, 'Credentials OK ✔' + (data.account ? ' (' + data.account + ')' : '') +
        (data.whatsapp_error ? ' — the WhatsApp status check failed: ' + data.whatsapp_error.slice(0, 120) : ''));
    });
    on('test-btn', async (e) => {
      e.target.disabled = true; say($('test-result'), true, 'Sending…');
      const {ok, data} = await call('POST', '/api/test-message');
      say($('test-result'), ok, ok ? 'Test message sent ✔ — check your WhatsApp' : 'Failed: ' + data.error);
      e.target.disabled = false;
    });
    fetch('/api/status', {credentials: 'same-origin'}).then((r) => r.json()).then((s) => {
      const box = $('wb-status');
      if (s.error) { box.textContent = '· ' + s.error; box.className = 'err'; return; }
      box.textContent = '· connected ✔' + (s.whatsapp_error ? ' (status page unavailable)' : ''); box.className = 'ok';
    }).catch(() => {});

    if ($('with-purpose')) $('with-purpose').onchange = async (e) => {
      const {ok, data} = await call('PUT', '/api/message-options', {include_purpose: e.target.checked});
      if (ok) { toastNext('Saved. Reminders will be re-created when you Review & apply.'); location.reload(); } else say($('msg-result'), false, data.error);
    };
    $('slots-form').onsubmit = async (e) => {
      e.preventDefault(); const f = e.target;
      const {ok, data} = await call('PUT', '/api/slots', {morning: f.morning.value, noon: f.noon.value, evening: f.evening.value, night: f.night.value});
      say($('slots-result'), ok, ok ? 'Saved ✔' : data.error);
    };
    $('family-form').onsubmit = async (e) => {
      e.preventDefault();
      const {ok, data} = await call('PUT', '/api/family-access', {password: e.target.password.value});
      if (ok) { toastNext('Family access is on. Share the /family page and the password.'); location.reload(); }
      else say($('family-result'), false, data.error);
    };
    on('family-off', async () => {
      if (!confirm('Turn off family access? They will no longer be able to sign in.')) return;
      await call('DELETE', '/api/family-access'); toastNext('Family access turned off.'); location.reload();
    });
  }
})();
