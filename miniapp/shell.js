(() => {
  'use strict';

  const view = document.querySelector('#screen');
  const routes = { day: 'Расписание', search: 'Поиск', notebook: 'Блокнот' };
  const colleges = { kemtip: 'КемТИПиСУ', kuzpk: 'КузПК', kuztagis: 'КузТАГиС', pgt: 'ПГТ', yamk: 'ЯМК' };
  const paths = {
    calendar: 'M4 6h16v15H4z M8 3v5m8-5v5M4 11h16m-12 4h3',
    search: 'M16.7 10.5a6.2 6.2 0 1 1-12.4 0 6.2 6.2 0 0 1 12.4 0z M15.1 15.1l5.1 5.1',
    book: 'M5 3.5h14v17H6a2 2 0 0 1-2-2V5a1.5 1.5 0 0 1 1-1.5z M5 17h14M8 4v13M11 8h5',
    bookmark: 'M6 3.5h12v17L12 17l-6 3.5z',
    clock: 'M21 12a9 9 0 1 1-18 0 9 9 0 0 1 18 0z M12 6.5v6l4 2.3',
    'arrow-left': 'M20 12H4m7-7-7 7 7 7',
    'arrow-right': 'M4 12h16m-7-7 7 7-7 7',
    chevron: 'm9 5 7 7-7 7',
    refresh: 'M20 4v6h-6 M20 10a8 8 0 0 0-14.5-3M4 20v-6h6m-6 0a8 8 0 0 0 14.5 3',
    user: 'M16 8a4 4 0 1 1-8 0 4 4 0 1 1 8 0z M4.5 20a7.5 7.5 0 0 1 15 0',
    pin: 'M19 10c0 5-7 11-7 11S5 15 5 10a7 7 0 1 1 14 0z M14.3 10a2.3 2.3 0 1 1-4.6 0 2.3 2.3 0 1 1 4.6 0z',
    check: 'm4 12 5 5L20 6',
    plus: 'M5 12h14m-7-7v14',
    close: 'm6 6 12 12M18 6 6 18',
    download: 'M12 3v12m-5-5 5 5 5-5M4 16v5h16v-5',
    edit: 'm4 16-1 5 5-1 12-12-4-4z M13.5 6.5l4 4',
    menu: 'M3 6h18M3 12h18M3 18h18',
    grid: 'M3 3h7v7H3z M14 3h7v7h-7z M3 14h7v7H3z M14 14h7v7h-7z'
  };

  function icon(name) {
    const path = paths[name];
    if (!path) return '';
    return '<svg class="icon" aria-hidden="true" focusable="false" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"><path d="' + path + '"/></svg>';
  }

  function esc(value) {
    const chars = { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' };
    return String(value ?? '').replace(/[&<>"']/g, char => chars[char]);
  }

  function errorWithStatus(message, status) {
    return Object.assign(new Error(message), { status });
  }

  async function api(params, signal, exportDay = false) {
    const initData = window.Telegram?.WebApp?.initData;
    if (!initData && window.PlannerPreview) return window.PlannerPreview.api(params, exportDay);
    if (!initData) throw errorWithStatus('Открой мини-приложение из меню бота в Telegram.', 401);
    const url = new URL(exportDay ? '/api/miniapp/export' : '/api/miniapp', window.location.origin);
    url.search = new URLSearchParams(params);
    const response = await fetch(url, {
      method: exportDay ? 'POST' : 'GET',
      headers: { 'X-Telegram-Init-Data': initData },
      signal,
      cache: 'no-store'
    });
    const json = await response.json().catch(() => null);
    if (!response.ok || !json) {
      throw errorWithStatus(json?.error || 'Не удалось получить расписание. Попробуй обновить страницу.', response.status);
    }
    return json;
  }

  const school = document.querySelector('.school');
  function setCollege(id) {
    if (!id) return;
    document.querySelector('#school-name').textContent = colleges[id] || id;
    school.hidden = false;
  }

  let toastTimer;
  function notify(message) {
    const notice = document.querySelector('#notice');
    clearTimeout(toastTimer);
    notice.textContent = message;
    toastTimer = setTimeout(() => { notice.textContent = ''; }, 3200);
  }

  let current = null;
  let exportedDay = null;
  function mount(route) {
    if (!Object.hasOwn(routes, route)) route = 'day';
    if (route === current) return;
    const wasMounted = current !== null;
    if (current === 'day' || current === 'search') window.Agenda.leave();
    if (current === 'notebook') window.Notebook.leave?.();
    current = route;
    document.title = 'Степаша · ' + routes[route];
    document.querySelectorAll('[data-route]').forEach(button => {
      if (button.dataset.route === route) button.setAttribute('aria-current', 'page');
      else button.removeAttribute('aria-current');
    });
    view.replaceChildren();
    window.scrollTo(0, 0);
    if (route === 'notebook') window.Notebook.open();
    else window.Agenda.open(route);
    if (wasMounted) view.focus({ preventScroll: true });
  }

  function navigate(route) {
    if (route === current || !Object.hasOwn(routes, route)) return;
    // Keep Telegram launch parameters in the address bar.
    try { history.pushState({ stepashaRoute: route }, ''); } catch {}
    mount(route);
  }

  window.Step = {
    view, api, icon, esc, setCollege, notify, navigate,
    saveExport(snapshot) { exportedDay = snapshot; },
    getExport() { return exportedDay; }
  };

  document.addEventListener('DOMContentLoaded', () => {
    // All three routes use the same navigation and the same Telegram session.
    document.querySelectorAll('[data-icon]').forEach(el => { el.innerHTML = icon(el.dataset.icon); });
    document.querySelector('.destinations').addEventListener('click', event => {
      const button = event.target.closest('[data-route]');
      if (button) navigate(button.dataset.route);
    });
    window.addEventListener('popstate', event => mount(event.state?.stepashaRoute));
    window.Telegram?.WebApp?.ready();
    window.Telegram?.WebApp?.expand();
    mount(history.state?.stepashaRoute);
  });
})();
