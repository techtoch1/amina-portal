(() => {
  const $ = (id) => document.getElementById(id);
  const csrf = document.querySelector('meta[name=csrf]').content;
  const post = async (url, body) => {
    const r = await fetch(url, {method: 'POST', credentials: 'same-origin',
      headers: {'Content-Type': 'application/json', 'X-CSRF-Token': csrf}, body: JSON.stringify(body || {})});
    let data = {};
    try { data = await r.json(); } catch (e) { data = {error: 'Unexpected response (HTTP ' + r.status + ')'}; }
    if (!r.ok && !data.error) data.error = data.detail || ('HTTP ' + r.status);
    return {ok: r.ok, data};
  };
  const el = (tag, text, cls) => { const e = document.createElement(tag); if (text) e.textContent = text; if (cls) e.className = cls; return e; };

  $('logout').onclick = async () => { await post('/logout'); location.href = '/login'; };

  // Wbiztool status
  fetch('/api/status', {credentials: 'same-origin'}).then(r => r.json()).then(s => {
    const box = $('wb-status');
    if (s.error) { box.textContent = 'Wbiztool: ' + s.error; box.className = 'err'; return; }
    const wa = s.whatsapp || {};
    const detail = wa.data ? JSON.stringify(wa.data) : (wa.message || 'ok');
    box.textContent = 'Credentials OK · WhatsApp: ' + detail;
    box.className = 'ok';
  }).catch(() => { $('wb-status').textContent = 'Could not load status'; });

  $('test-btn').onclick = async (e) => {
    e.target.disabled = true; $('test-result').textContent = 'Sending…';
    const {ok, data} = await post('/api/test-message');
    $('test-result').textContent = ok ? 'Test message sent ✔' : 'Failed: ' + data.error;
    $('test-result').className = ok ? 'ok' : 'err';
    e.target.disabled = false;
  };

  $('copy').onchange = (e) => post('/api/settings', {copy_to_me: e.target.checked});

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
    await post('/api/settings', {copy_to_me: $('copy').checked});
    const {ok, data} = await post('/api/sync/preview');
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
    const {ok, data} = await post('/api/sync/apply', {hash: planHash});
    const out = $('apply-result'); out.replaceChildren();
    if (!ok) { out.append(el('li', data.error, 'err')); return; }
    data.results.forEach((r) => out.append(el('li', (r.ok ? '✔ ' : '✖ ') + r.action + (r.error ? ' — ' + r.error : '') + (r.warning ? ' ⚠ ' + r.warning : ''), r.ok ? 'ok' : 'bad')));
    $('plan').hidden = true; planHash = null;
  };
})();
