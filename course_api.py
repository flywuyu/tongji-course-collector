"""Authenticated course discovery in the current account owner's browser."""
DISCOVER_JS = r"""
async () => {
  const sleep = ms => new Promise(r => setTimeout(r, ms));
  const perf = performance.getEntriesByType('resource').map(e => e.name).join('\n');
  let token = (perf.match(/[?&]token=(eyJ[\w-]+\.[\w-]+\.[\w-]+)/) || [])[1] || '';
  if (!token) {
    try {
      for (let i = 0; i < localStorage.length; i++) {
        const v = localStorage.getItem(localStorage.key(i)) || '';
        const m = v.match(/eyJ[\w-]{10,}\.[\w-]{10,}\.[\w-]{6,}/);
        if (m) { token = m[0]; break; }
      }
    } catch (e) {}
  }
  if (!token) return JSON.stringify({ error: 'no-token' });

  let uid = (perf.match(/[?&]user_id=(\d+)/) || [])[1] || '';
  if (!uid) {
    try {
      const p = JSON.parse(atob(token.split('.')[1].replace(/-/g, '+').replace(/_/g, '/')));
      uid = p.user_id || p.uid || p.sub || p.id || '';
    } catch (e) {}
  }

  const api = async u => {
    try {
      const r = await fetch(u, { credentials: 'include', headers: { Authorization: 'Bearer ' + token } });
      return await r.text();
    } catch (e) { return ''; }
  };

  const tj = JSON.parse(await api('/courseapi/index.php/v2/course/get-term-years?tenant=222') || '{}');
  const groups = (tj.result && tj.result.data) || {};
  const terms = [];
  Object.keys(groups).forEach(k => Array.isArray(groups[k]) && groups[k].forEach(t => terms.push(t)));
  if (!terms.length) return JSON.stringify({ error: 'no-term' });

  const today = new Date().toISOString().slice(0, 10);
  const term = terms.find(t => t.begin_date <= today && today <= t.end_date) || terms[0];

  const fmt = d => d.toISOString().slice(0, 10);
  const weeks = [];
  let cur = new Date(term.begin_date + 'T00:00:00');
  const endd = new Date(term.end_date + 'T00:00:00');
  while (cur <= endd) {
    weeks.push([fmt(cur), fmt(new Date(cur.getTime() + 6 * 86400000))]);
    cur = new Date(cur.getTime() + 7 * 86400000);
  }

  const lectures = [], seen = new Set();
  for (const [s, e] of weeks) {
    const body = await api('/courseapi/v2/schedule/get-week-schedules?user_id=' + uid +
      '&tenant_id=222&start_at=' + s + '&end_at=' + e + '&token=' + encodeURIComponent(token));
    let j = {};
    try { j = JSON.parse(body); } catch (err) {}
    for (const day of ((j.result && j.result.list) || [])) {
      for (const c of (day.course || [])) {
        const sub = String(c.id || '');
        if (!sub || seen.has(sub)) continue;
        seen.add(sub);
        lectures.push({
          sub_id: sub, course_id: String(c.course_id || ''), title: c.course_title || '',
          date: day.day || '', start_at: Number(c.start_at || 0),
          minutes: Math.round((Number(c.end_at || 0) - Number(c.start_at || 0)) / 60),
          status: c.status_label || '', teacher: c.teacher_name || ''
        });
      }
    }
    await sleep(100);
  }

  const replay = lectures.filter(l => (l.status || '').indexOf('回放') >= 0);
  const out = [];
  for (let i = 0; i < replay.length; i += 3) {
    const batch = replay.slice(i, i + 3);
    await Promise.all(batch.map(async it => {
      const body = await api('/courseapi/v3/portal-home-setting/get-sub-info?course_id=' +
        it.course_id + '&sub_id=' + it.sub_id);
      let d = {};
      try { d = (JSON.parse(body).data) || {}; } catch (e) {}
      const pu = d.playurl || {};
      out.push({
        sub_id: it.sub_id, course_id: it.course_id, title: it.title, date: it.date,
        start_at: it.start_at, minutes: it.minutes,
        duration: d.duration || '', sub_title: d.sub_title || '',
        playurl: pu['0'] || pu['1'] || ''
      });
    }));
    await sleep(120);
  }

  out.sort((a, b) => a.start_at - b.start_at);
  return JSON.stringify({
    when: new Date().toISOString(),
    term: { name: term.term_name, begin: term.begin_date, end: term.end_date },
    lecture_count: lectures.length, replay_count: replay.length,
    with_playurl: out.filter(x => x.playurl).length, items: out
  });
}
"""
