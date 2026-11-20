/* The schedule and directory share one date cursor and the temporary group choice. */
window.Agenda = (() => {
  const ui = () => window.Step;
  const esc = value => ui().esc(String(value ?? ''));
  const icon = name => ui().icon(name);
  const fmt = (date, options) => new Intl.DateTimeFormat('ru-RU', options).format(new Date(date + 'T12:00:00'));
  const iso = date => [date.getFullYear(), ...[date.getMonth() + 1, date.getDate()].map(n => String(n).padStart(2, '0'))].join('-');
  const offset = (date, distance) => {
    const value = new Date(date + 'T12:00:00');
    value.setDate(value.getDate() + distance);
    return iso(value);
  };
  const monday = date => offset(date, -((new Date(date + 'T12:00:00').getDay() + 6) % 7));
  const titleCase = text => text.charAt(0).toLocaleUpperCase('ru') + text.slice(1);
  const weekNames = ['Пн', 'Вт', 'Ср', 'Чт', 'Пт', 'Сб', 'Вс'];
  const count = (n, words) => {
    const last = n % 10;
    const teens = n % 100 >= 11 && n % 100 <= 14;
    return n + ' ' + words[!teens && last === 1 ? 0 : !teens && last >= 2 && last <= 4 ? 1 : 2];
  };
  const amount = n => count(n, ['пара', 'пары', 'пар']);
  const durations = n => n < 60 ? n + ' мин' : Math.floor(n / 60) + ' ч' + (n % 60 ? ' ' + n % 60 + ' мин' : '');
  const clockValue = value => {
    const m = String(value || '').trim().match(/^(\d{1,2}):(\d{2})/);
    return m && +m[1] < 24 && +m[2] < 60 ? 60 * +m[1] + +m[2] : null;
  };

  let active = '', period = 'schedule', kind = 'teachers', entity = '', query = '';
  let pickedGroup = '', homeGroup = '', groups = [], college = '';
  let date = iso(new Date()), followToday = true, response = null;
  let campusZone;
  let request, ticker, typing, mount;

  function campusClock() {
    const zone = campusZone;
    const parts = new Intl.DateTimeFormat('en-CA', {
      timeZone: zone, year:'numeric', month:'2-digit', day:'2-digit',
      hour:'2-digit', minute:'2-digit', hourCycle:'h23'
    }).formatToParts(new Date());
    const p = Object.fromEntries(parts.map(({type,value}) => [type,value]));
    return { date: p.year + '-' + p.month + '-' + p.day, minutes: Number(p.hour) * 60 + Number(p.minute) };
  }
  function action(id, label, glyph, css = '') {
    return '<button type="button" data-action="' + id + '" class="ag-action ' + css + '" aria-label="' + esc(label) + '" title="' + esc(label) + '">' + icon(glyph) + '</button>';
  }
  function chip(id, text, isSelected) {
    return '<button type="button" data-action="' + id + '" aria-pressed="' + isSelected + '">' + text + '</button>';
  }
  function groupPicker() {
    const defaultName = active === 'search' ? 'Все группы' : (homeGroup || 'Моя группа');
    const selected = pickedGroup || defaultName;
    const names = [...new Set([homeGroup, pickedGroup, ...groups].filter(Boolean))];
    const opts = names.filter(name => active === 'search' || name !== homeGroup || name === pickedGroup).map(name =>
      '<option value="' + esc(name) + '"' + (pickedGroup === name ? ' selected' : '') + '>' + esc(name) + '</option>').join('');
    return '<label class="ag-group">' + icon('grid') + '<span>' + esc(selected || 'Моя группа') + '</span>' + icon('chevron') +
      '<select aria-label="Группа для просмотра" data-group><option value="">' + esc(defaultName) + '</option>' + opts + '</select></label>';
  }
  function titleBlock() {
    const schedule = active === 'day';
    const title = schedule ? 'Расписание' : 'Поиск';
    return '<header class="ag-heading"><h1>' + title + '</h1><div class="ag-heading-tools">' +
      (schedule && groups.length ? groupPicker() : '') +
      action('refresh', 'Обновить', 'refresh') + '</div></header>';
  }
  function strip() {
    const start = monday(date), today = campusClock().date;
    const days = weekNames.map((name,i) => {
      const value = offset(start, i);
      const current = (active === 'search' || period === 'schedule') && value === date;
      return '<button type="button" data-date="' + value + '" class="ag-date' + (current ? ' chosen' : '') + (value === today ? ' today' : '') + '" aria-label="' +
        esc(fmt(value, {weekday:'long',day:'numeric',month:'long'})) + '" aria-pressed="' + current + '"><span>' + name + '</span><b>' + Number(value.slice(-2)) + '</b></button>';
    }).join('');
    return '<section class="ag-calendar"><div class="ag-month"><strong>' + titleCase(fmt(date, {month:'long',year:'numeric'}).replace(/\s*г\.$/, '')) +
      '</strong><div class="ag-month-nav">' + action('prev', 'Предыдущая неделя', 'arrow-left') + action('next', 'Следующая неделя', 'arrow-right') +
      '</div></div><div class="ag-strip">' + days + '</div></section>';
  }
  function controls() {
    if (active === 'day') {
      return '<div class="ag-selector-row"><div class="ag-segments" aria-label="Период">' +
        chip('day', 'День', period === 'schedule') + chip('week', 'Неделя', period === 'week') +
        '</div><button class="ag-jump" data-action="today">Сегодня</button><button class="ag-jump" data-action="tomorrow">Завтра</button></div>' + strip();
    }
    if (entity) {
      return '<button class="ag-back" data-action="back">' + icon('arrow-left') + 'К результатам</button><h2 class="ag-entity">' + esc(entity) + '</h2>' + strip();
    }
    return '<div class="ag-directory"><div class="ag-segments">' + chip('teachers','Преподаватели',kind === 'teachers') +
      chip('rooms','Кабинеты',kind === 'rooms') + '</div><label class="ag-find">' + icon('search') +
      '<input type="search" data-query autocomplete="off" aria-label="Поиск в справочнике" placeholder="' +
      (kind === 'rooms' ? 'Номер или название кабинета' : 'Фамилия преподавателя') + '" value="' + esc(query) + '"></label></div>';
  }
  function paintChrome() {
    mount.querySelector('.ag-top').innerHTML = titleBlock();
    mount.querySelector('.ag-controls').innerHTML = controls();
  }
  function brief(items) {
    const now = campusClock(), isToday = date === now.date;
    const ordered = items.filter(item => clockValue(item.start) !== null)
      .slice().sort((a,b) => clockValue(a.start) - clockValue(b.start));
    const live = isToday && ordered.find(row => clockValue(row.start) <= now.minutes && clockValue(row.end) > now.minutes);
    const next = isToday && ordered.find(row => clockValue(row.start) > now.minutes);
    const selected = live || next || items[0];
    if (!selected) return '';
    const ended = isToday && !live && !next && items.every(row => clockValue(row.end) !== null && clockValue(row.end) <= now.minutes);
    if (ended) return ticket('Учебный день', 'Занятия завершены', 'Сегодня по расписанию — ' + amount(items.length), '', '');
    const title = selected.subject || selected.lesson || 'Занятие';
    const badge = live ? 'Сейчас идёт' : next ? 'Следующая пара' : isToday ? 'В расписании' : 'Первая пара';
    const detail = [selected.room && 'Каб. ' + selected.room, selected.teacher].filter(Boolean).join(' · ');
    const time = live ? 'Ещё ' + durations(clockValue(live.end) - now.minutes) : next ? 'Через ' + durations(clockValue(next.start) - now.minutes) : '';
    return ticket(badge, title, detail, selected.start || '', time);
  }
  function ticket(kicker, title, detail, time, status) {
    return '<article class="ag-ticket"><div class="ag-ticket-content"><div class="ag-ticket-label"><i></i>' + esc(kicker) + '</div>' +
      '<h2>' + esc(title) + '</h2>' + (detail ? '<p>' + esc(detail) + '</p>' : '') + '</div>' +
      (time || status ? '<div class="ag-ticket-stub"><strong>' + esc(time) + '</strong><span>' + esc(status) + '</span></div>' : '') + '</article>';
  }
  function keys() {
    try {
      const saved = JSON.parse(localStorage.getItem('stepasha-favs') || '[]');
      return Array.isArray(saved) ? saved : [];
    } catch { return []; }
  }
  function noteFor(key) {
    try { return localStorage.getItem('note:' + key) || ''; } catch { return ''; }
  }
  function timeline(items, stamp) {
    const now = campusClock(), favs = keys();
    let previousEnd = null;
    return '<ol class="ag-lines">' + items.map((row,i) => {
      const start = clockValue(row.start), end = clockValue(row.end);
      const number = row.lesson_no || i + 1;
      const key = [active === 'search' ? kind : period, entity || pickedGroup, row.date || stamp, row.lesson_no, row.subject || row.lesson].join('|');
      const note = noteFor(key);
      const live = stamp === now.date && start !== null && end !== null && start <= now.minutes && now.minutes < end;
      const windowTime = previousEnd !== null && start !== null && start > previousEnd ? '<li class="ag-gap">' + (start - previousEnd > 30 ? 'Окно' : 'Перерыв') + ' · ' + durations(start - previousEnd) + '</li>' : '';
      if (end !== null) previousEnd = previousEnd === null ? end : Math.max(previousEnd,end);
      const lines = [row.room ? '<span>' + icon('pin') + esc(row.room) + '</span>' : '',
        row.teacher ? '<span>' + icon('user') + esc(row.teacher) + '</span>' : '',
        active === 'search' && row.group ? '<span>' + icon('grid') + esc(row.group) + '</span>' : ''].join('');
      return windowTime + '<li class="ag-line' + (live ? ' live' : '') + '"><div class="ag-time"><b>' + esc(row.start || '—') + '</b><small>' + esc(row.end || '') +
        '</small></div><div class="ag-entry"><div class="ag-entry-body"><div class="ag-ordinal">' + esc(number) + ' пара' + (live ? ' · сейчас' : '') +
        '</div><h4>' + esc(row.subject || row.lesson || 'Занятие') + '</h4><div class="ag-meta">' + lines + '</div></div>' +
        '<button type="button" class="ag-mark" data-favorite="' + esc(key) + '" aria-pressed="' + favs.includes(key) + '" aria-label="Сохранить пару">' +
        icon('bookmark') + '</button><details class="ag-note"><summary>' + icon('edit') + (note ? 'Моя заметка' : 'Заметка') + '</summary>' +
        '<textarea rows="2" data-note="' + esc(key) + '" aria-label="Заметка к паре" placeholder="Записать к этой паре…">' + esc(note) +
        '</textarea></details></div></li>';
    }).join('') + '</ol>';
  }
  function subhead(title, suffix) {
    return '<div class="ag-subhead"><h3>' + esc(title) + '</h3><span>' + esc(suffix) + '</span></div>';
  }
  function dayContent(items, stamp) {
    return subhead(titleCase(fmt(stamp, {weekday:'long',day:'numeric',month:'long'})), amount(items.length)) +
      (items.length ? timeline(items, stamp) : '<p class="ag-empty">На эту дату занятий в расписании нет.</p>');
  }
  function paintResults() {
    const slot = mount.querySelector('.ag-results');
    if (active === 'search' && !entity) {
      const entries = response.items || [];
      slot.innerHTML = subhead('Результаты', entries.length === 100 ? 'Первые 100' : String(entries.length)) +
        (entries.length ? '<ul class="ag-index">' + entries.map(name => '<li><button type="button" data-entity="' + esc(name) + '"><span class="ag-index-icon">' +
          icon(kind === 'rooms' ? 'pin' : 'user') + '</span><b>' + esc(name) + '</b>' + icon('arrow-right') + '</button></li>').join('') +
          '</ul>' : '<p class="ag-empty">Совпадений нет. Попробуй другое написание.</p>');
    } else if (active === 'day' && period === 'week') {
      const days = Object.entries(response.days || {}).sort(([a],[b]) => a.localeCompare(b));
      slot.innerHTML = days.map(([stamp,items]) => '<section class="ag-weekday">' + dayContent(items, stamp) + '</section>').join('') ||
        '<p class="ag-empty">Расписание на эту неделю пока не опубликовано.</p>';
    } else {
      const rows = response.rows || [];
      slot.innerHTML = (active === 'day' ? '<div data-ag-ticket>' + brief(rows) + '</div>' : '') + dayContent(rows, date);
    }
    if (response.nearest) {
      slot.insertAdjacentHTML('afterbegin', '<button type="button" class="ag-retry" data-date="' + esc(response.nearest) + '">Открыть занятия за ' + esc(fmt(response.nearest, {day:'numeric', month:'long'})) + '</button>');
    }
  }
  async function fetchData(nearest = false) {
    request?.abort();
    const pending = request = new AbortController();
    const slot = mount.querySelector('.ag-results');
    response = null;
    slot.setAttribute('aria-busy', 'true');
    slot.innerHTML = '<div class="ag-load" role="status">Загрузка…</div>';
    const params = { section: active === 'day' ? period : kind, date };
    if (pickedGroup && active === 'day') params.group = pickedGroup;
    if (active === 'search' && entity) { params.name = entity; if (nearest) params.nearest = '1'; }
    if (active === 'search' && query.trim()) params.q = query.trim();
    try {
      const payload = await ui().api(params, pending.signal);
      if (pending.signal.aborted) return;
      response = payload;
      if (college && payload.college !== college && pickedGroup) {
        pickedGroup = ''; homeGroup = ''; groups = []; college = payload.college;
        return fetchData();
      }
      campusZone = payload.timezone || campusZone;
      college = payload.college;
      ui().setCollege(college);
      if (!pickedGroup && active === 'day') homeGroup = payload.group || '';
      if (nearest && entity && payload.date && payload.date !== date) { date = payload.date; followToday = false; paintChrome(); }
      groups = payload.groups || (active === 'day' ? payload.items : null) || groups;
      const localDate = campusClock().date;
      if (followToday && date !== localDate) {
        date = localDate;
        paintChrome();
        return fetchData();
      }
      mount.querySelector('.ag-top').innerHTML = titleBlock();
      if (active === 'day') ui().saveExport({date, rows: payload.rows || [], group: payload.group});
      paintResults();
    } catch (error) {
      if (pending.signal.aborted) return;
      if (error.status === 401) {
        slot.innerHTML = ticket('Личное расписание', 'Открой Степашу в Telegram', 'В чате с ботом нажми кнопку приложения рядом с полем сообщения.', '', '');
        return;
      }
      slot.innerHTML = ticket('Нет данных', active === 'day' ? 'Расписание недоступно' : 'Справочник недоступен', error.message || 'Проверь подключение и попробуй ещё раз.', '', '') +
        '<button type="button" class="ag-retry" data-action="refresh">' + icon('refresh') + 'Повторить</button>';
    } finally {
      if (!pending.signal.aborted) slot.removeAttribute('aria-busy');
    }
  }
  function move(value, today = false) {
    date = value; followToday = today;
    paintChrome();
    fetchData();
  }
  function activate(button) {
    const key = button.dataset.favorite;
    if (key !== undefined) {
      const values = keys(), hadKey = values.includes(key);
      try {
        localStorage.setItem('stepasha-favs', JSON.stringify(hadKey ? values.filter(item => item !== key) : [...values, key]));
        button.setAttribute('aria-pressed', String(!hadKey));
        ui().notify(hadKey ? 'Пара удалена из закладок' : 'Пара сохранена');
      } catch { ui().notify('Не удалось сохранить на устройстве'); }
      return;
    }
    if (button.dataset.entity !== undefined) {
      entity = button.dataset.entity; paintChrome(); fetchData(true); mount.scrollIntoView({block:'start'}); return;
    }
    if (button.dataset.date) {
      if (active === 'day') period = 'schedule';
      move(button.dataset.date); return;
    }
    switch (button.dataset.action) {
      case 'prev': return move(offset(date, -7));
      case 'next': return move(offset(date, 7));
      case 'today': period = 'schedule'; return move(campusClock().date, true);
      case 'tomorrow': period = 'schedule'; return move(offset(campusClock().date, 1));
      case 'refresh': return fetchData();
      case 'week': period = 'week'; break;
      case 'day': period = 'schedule'; break;
      case 'back': entity = ''; break;
      case 'teachers':
      case 'rooms': kind = button.dataset.action; entity = ''; query = ''; break;
      default: return;
    }
    paintChrome();
    fetchData();
  }
  function leave() {
    request?.abort();
    clearTimeout(typing);
    clearInterval(ticker);
    response = null;
    active = '';
  }
  function open(section) {
    leave();
    active = section;
    if (active === 'day') entity = '';
    if (followToday) date = campusClock().date;
    ui().view.innerHTML = '<section class="ag"><div class="ag-top"></div><div class="ag-controls"></div><div class="ag-results"></div></section>';
    mount = ui().view.querySelector('.ag');
    mount.addEventListener('click', event => {
      const button = event.target.closest('button');
      if (button) activate(button);
    });
    mount.addEventListener('change', event => {
      if (event.target.matches('[data-group]')) {
        pickedGroup = event.target.value;
        paintChrome(); fetchData();
      }
    });
    mount.addEventListener('input', event => {
      const input = event.target;
      if (input.matches('[data-note]')) {
        try { localStorage.setItem('note:' + input.dataset.note, input.value); }
        catch { ui().notify('Не удалось сохранить заметку'); }
      }
      if (input.matches('[data-query]')) {
        query = input.value;
        clearTimeout(typing); request?.abort();
        typing = setTimeout(fetchData, 180);
      }
    });
    paintChrome(); fetchData();
    ticker = setInterval(() => {
      if (followToday && date !== campusClock().date) return move(campusClock().date, true);
      const ticketNode = mount.querySelector('[data-ag-ticket]');
      if (ticketNode && response) ticketNode.innerHTML = brief(response.rows || []);
    }, 30_000);
  }
  return {open, leave};
})();
