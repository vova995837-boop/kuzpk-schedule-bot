// A public, read-only example week. Signed Telegram sessions use the live API.
window.PlannerPreview = (() => {
  let snapshot;
  async function api(params, exporting) {
    if (exporting) throw new Error('Для экспорта личного расписания открой приложение из Telegram.');
    snapshot ||= fetch('./sample.json').then(r => r.json()).catch(e => { snapshot = null; throw e; });
    const all = await snapshot;
    const group = params.group || '2601', groups = ['2601','2602'];
    const matches = all.filter(r => r.group === group);
    const result = { section:params.section, college:'kemtip', group, groups, date:params.date, previewDate:'2026-11-23', rows:[], items:groups };
    if (params.section === 'week') {
      const d = new Date(params.date+'T12:00:00Z');
      d.setUTCDate(d.getUTCDate()-(d.getUTCDay()+6)%7);
      result.days = {};
      for (let i=0;i<7;i++,d.setUTCDate(d.getUTCDate()+1)) {
        const day = d.toISOString().slice(0,10);
        result.days[day] = matches.filter(r=>r.date===day);
      }
    } else if (['teachers','rooms'].includes(params.section)) {
      const key = params.section==='teachers'?'teacher':'room';
      result.items = [...new Set(all.map(r=>r[key]).filter(Boolean))].sort().filter(n=>n.toLowerCase().includes((params.q||'').toLowerCase()));
      result.rows = all.filter(r=>r.date===params.date && r[key]===params.name);
    } else result.rows = matches.filter(r=>r.date===params.date);
    return result;
  }
  return {api};
})();
document.addEventListener('DOMContentLoaded',()=>{
 if (!window.Telegram?.WebApp?.initData) document.querySelector('.wordmark').insertAdjacentHTML('afterend','<small class="demo-label">Демоверсия</small>');
});
