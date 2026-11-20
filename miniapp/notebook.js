/* Personal records keep the storage formats used by earlier Mini App releases. */
window.Notebook = (() => {
  const keys = { archives: 'stepasha-group-archives', tasks: 'stepasha-tasks', grades: 'stepasha-grades', events: 'stepasha-events', saved: 'stepasha-favs' };
  const titles = { home: 'Блокнот', tasks: 'Задания', grades: 'Оценки', events: 'События', saved: 'Пары и заметки' };
  let page = 'home';
  let editor = null;
  let saved = [];
  const app = () => window.Step;
  const esc = value => app().esc(value ?? '');
  const icon = name => app().icon(name);

  function records(type) {
    try {
      const items = JSON.parse(localStorage.getItem(keys[type]) || '[]');
      return Array.isArray(items) ? items : [];
    } catch { return []; }
  }

  function persist(type, items) {
    localStorage.setItem(keys[type], JSON.stringify(items));
  }

  function dayLabel(value) {
    if (!/^\d{4}-\d{2}-\d{2}$/.test(value)) return '';
    const date = new Date(value + 'T12:00:00');
    if (!Number.isFinite(date.getTime())) return '';
    const year = date.getFullYear() === new Date().getFullYear() ? undefined : 'numeric';
    return date.toLocaleDateString('ru-RU', { day: 'numeric', month: 'short', year });
  }

  function collectSaved() {
    const favorites = new Set(records('saved').filter(key => typeof key === 'string'));
    const all = new Set(favorites);
    try {
      for (let index = 0; index < localStorage.length; index++) {
        const key = localStorage.key(index);
        if (key?.startsWith('note:') && localStorage.getItem(key)?.trim()) all.add(key.slice(5));
      }
      return [...all].map(key => {
        const [, entity, date, number, ...subject] = key.split('|');
        return { key, entity, date, number, title: subject.join('|') || 'Занятие',
          marked: favorites.has(key), note: localStorage.getItem('note:' + key) || '' };
      }).sort((a, b) => String(b.date).localeCompare(String(a.date)));
    } catch { return []; }
  }

  function action(text, attrs, glyph, css = '') {
    return `<button type="button" class="nb-action ${css}" ${attrs}>${glyph ? icon(glyph) : ''}<span>${text}</span></button>`;
  }
  function iconButton(label, attrs, glyph) {
    return `<button type="button" class="nb-icon" aria-label="${esc(label)}" ${attrs}>${icon(glyph)}</button>`;
  }
  function menuItem(target, name, glyph, count = '') {
    return `<button class="nb-menu-item" type="button" data-nb-page="${target}">${icon(glyph)}<strong>${name}</strong><span>${count}</span>${icon('chevron')}</button>`;
  }
  function sectionTitle(text, target) {
    return `<header class="nb-section-head"><h2>${text}</h2>${action('Все', `data-nb-page="${target}"`, 'arrow-right', 'nb-quiet')}</header>`;
  }
  function sortedTasks() {
    return records('tasks').map((item, index) => ({ item, index }))
      .sort((a, b) => Number(Boolean(a.item.done)) - Number(Boolean(b.item.done))
        || (a.item.due || '9999').localeCompare(b.item.due || '9999') || b.index - a.index);
  }
  function taskRow({ item, index }, editable) {
    const meta = [item.subject, item.due && 'До ' + dayLabel(item.due)].filter(Boolean).map(esc).join(' · ');
    return `<div class="nb-row">
      <label class="nb-task"><input type="checkbox" data-nb-check="${index}" ${item.done ? 'checked' : ''}>
        <span><strong>${esc(item.title)}</strong>${meta ? `<small>${meta}</small>` : ''}</span></label>
      ${editable ? iconButton('Изменить задание', `data-nb-edit="${index}"`, 'edit') : ''}
    </div>`;
  }
  function savedRow(item, index, full = false) {
    const meta = [dayLabel(item.date), item.number && 'Пара ' + item.number, item.entity].filter(Boolean).map(esc).join(' · ');
    const open = `data-nb-note="${index}"`;
    return `<div class="nb-row nb-saved"><button type="button" class="nb-lesson" ${open}>
      ${icon(item.marked ? 'bookmark' : 'edit')}<span><strong>${esc(item.title)}</strong><small>${meta}</small>
      ${item.note ? `<em>${esc(item.note)}</em>` : ''}</span></button>
      ${full && item.marked ? iconButton('Убрать из сохранённых', `data-nb-unmark="${index}"`, 'close') : ''}</div>`;
  }
  function average() {
    const values = records('grades').map(row => Number(row.value)).filter(Number.isFinite);
    return values.length ? (values.reduce((sum, v) => sum + v, 0) / values.length)
      .toLocaleString('ru-RU', { minimumFractionDigits: 1, maximumFractionDigits: 2 }) : '—';
  }
  function home() {
    const upcoming = sortedTasks().filter(({ item }) => !item.done).slice(0, 3);
    const snapshot = app().getExport();
    return `<section class="nb-block">${sectionTitle('На очереди', 'tasks')}
      ${upcoming.map(task => taskRow(task, false)).join('') || '<p class="nb-empty">Открытых заданий пока нет.</p>'}
      ${action('Записать задание', 'data-nb-add="tasks"', 'plus', 'nb-primary')}
      </section>
      <section class="nb-block">${sectionTitle('Сохранённые пары', 'saved')}
      ${saved.slice(0, 2).map((item, i) => savedRow(item, i)).join('') || '<p class="nb-empty">Сохрани пару в расписании или оставь к ней заметку.</p>'}
      </section>
      <section class="nb-menu" aria-label="Учебные инструменты">
      ${menuItem('grades', 'Оценки', 'grid', average())}
      ${menuItem('events', 'События', 'calendar', records('events').length || '')}
      <button type="button" class="nb-menu-item" data-nb-export>
        ${icon('download')}<span><strong>Добавить расписание в календарь</strong><small>${esc(snapshot?.group || 'Основная группа')} · Все доступные даты</small></span></button>
      </section><section class="nb-block"><h2>Расписания групп</h2>
      ${records('archives').map(a => `<details><summary>${esc(a.group)} · ${esc(a.college)} · дней: ${Object.keys(a.days).length}</summary>
        ${Object.entries(a.days).map(([d, rows]) => `<details><summary>${esc(dayLabel(d))} · ${esc(d.slice(0,4))}</summary>
        ${rows.map(r => `<p><strong>${esc(r.lesson_no)} · ${esc(r.subject || r.lesson)}</strong><br>${[r.start && [r.start,r.end].filter(Boolean).join('–'),r.teacher,r.room].filter(Boolean).map(esc).join(' · ')}</p>`).join('')}</details>`).join('')}</details>`).join('') || '<p class="nb-empty">Здесь появятся сохранённые расписания.</p>'}
      </section>`;
  }
  function form(type) {
    const record = editor.index === null ? {} : records(type)[editor.index] || {};
    const editing = editor.index !== null;
    const field = (label, name, attrs = '') => `<label>${label}<input name="${name}" ${attrs} value="${esc(record[name] || '')}"></label>`;
    const fields = {
      tasks: () => field('Задание', 'title', 'required maxlength="400"') + field('Предмет', 'subject', 'maxlength="120"') + field('Срок сдачи', 'due', 'type="date"'),
      grades: () => field('Предмет', 'subject', 'maxlength="120"') + `<label>Оценка<select name="value">${[5, 4, 3, 2].map(v => `<option ${Number(record.value) === v ? 'selected' : ''} value="${v}">${v}</option>`).join('')}</select></label>`,
      events: () => field('Событие', 'title', 'required maxlength="400"') + field('Дата', 'date', 'required type="date"')
    };
    return `<form class="nb-form" data-nb-form="${type}">${fields[type]()}
      <div class="nb-controls"><button type="submit" class="nb-action nb-primary">Сохранить</button>
      ${action('Отмена', 'data-nb-cancel', null)}${editing ? action('Удалить', 'data-nb-remove', null, 'nb-remove') : ''}</div></form>`;
  }
  function collection() {
    if (editor && page !== 'saved') return form(page);
    if (page === 'saved') {
      if (editor) {
        const item = saved.find(entry => entry.key === editor.key);
        if (item) return `<form class="nb-form" data-nb-form="note"><h2>${esc(item.title)}</h2>
          <label>Заметка к паре<textarea rows="4" name="note">${esc(item.note)}</textarea></label>
          <div class="nb-controls"><button class="nb-action nb-primary">Сохранить</button>${action('Отмена', 'data-nb-cancel')}</div></form>`;
      }
      return saved.map((item, i) => savedRow(item, i, true)).join('') || '<p class="nb-empty">Здесь появятся твои сохранённые пары и заметки.</p>';
    }
    const labels = { tasks: 'Записать задание', grades: 'Записать оценку', events: 'Добавить событие' };
    let list;
    if (page === 'tasks') list = sortedTasks().map(task => taskRow(task, true));
    else {
      const rows = records(page).map((item, index) => ({ item, index }));
      if (page === 'events') rows.sort((a, b) => a.item.date.localeCompare(b.item.date));
      else rows.reverse();
      list = rows.map(({ item, index }) => `<div class="nb-row">
        <div class="nb-entry">${page === 'grades' ? `<b class="nb-grade">${esc(item.value)}</b><strong>${esc(item.subject || 'Оценка')}</strong>` :
        `<span><strong>${esc(item.title)}</strong><small>${esc(dayLabel(item.date))}</small></span>`}</div>
        ${iconButton('Изменить запись', `data-nb-edit="${index}"`, 'edit')}</div>`);
    }
    const metric = page === 'grades' ? `<div class="nb-metric"><span>Средний балл</span><strong>${average()}</strong></div>` : '';
    return `${metric}${action(labels[page], `data-nb-add="${page}"`, 'plus', 'nb-primary')}${list.length ? list.join('') : '<p class="nb-empty">Записей пока нет.</p>'}`;
  }
  function render(focus = false) {
    saved = collectSaved();
    const view = app().view;
    view.innerHTML = `<article class="nb">
      ${page !== 'home' ? action('Блокнот', 'data-nb-page="home"', 'arrow-left', 'nb-back') : ''}
      <header class="nb-heading"><h1>${titles[page]}</h1>${page === 'home' ? '<p>Личные записи на этом устройстве</p>' : ''}</header>
      ${page === 'home' ? home() : collection()}</article>`;
    const node = view.firstElementChild;
    node.addEventListener('click', handleClick);
    node.addEventListener('change', handleChange);
    node.addEventListener('submit', handleSubmit);
    if (focus) node.querySelector('.nb-form input, .nb-form select, .nb-form textarea')?.focus();
  }

  function handleClick(event) {
    const btn = event.target.closest('button');
    if (!btn) return;
    const d = btn.dataset;
    try {
      if ('nbPage' in d) { page = d.nbPage; editor = null; }
      else if ('nbAdd' in d) { page = d.nbAdd; editor = { index: null }; }
      else if ('nbEdit' in d) editor = { index: Number(d.nbEdit) };
      else if ('nbNote' in d) { editor = { key: saved[Number(d.nbNote)].key }; page = 'saved'; }
      else if ('nbCancel' in d) editor = null;
      else if ('nbRemove' in d) {
        const list = records(page);
        list.splice(editor.index, 1);
        persist(page, list); editor = null;
      } else if ('nbUnmark' in d) {
        const key = saved[Number(d.nbUnmark)].key;
        persist('saved', records('saved').filter(value => value !== key));
      } else if ('nbExport' in d) { exportDay(btn); return; }
      else if ('nbSchedule' in d) { app().navigate('day'); return; }
      else return;
      render(Boolean(editor));
    } catch { app().notify('Не удалось обновить записи на устройстве.'); }
  }
  function handleChange(event) {
    const input = event.target.closest('[data-nb-check]');
    if (!input) return;
    const list = records('tasks');
    const entry = list[Number(input.dataset.nbCheck)];
    if (!entry) return render();
    entry.done = input.checked;
    try { persist('tasks', list); } catch { app().notify('Не удалось сохранить отметку.'); }
    render();
  }
  function handleSubmit(event) {
    event.preventDefault();
    const target = event.target;
    const fields = new FormData(target);
    const type = target.dataset.nbForm;
    const value = name => String(fields.get(name) || '').trim();
    try {
      if (type === 'note') {
        const key = 'note:' + editor.key;
        if (value('note')) localStorage.setItem(key, value('note')); else localStorage.removeItem(key);
      } else {
        const list = records(type);
        const patches = {
          tasks: () => ({ title: value('title'), subject: value('subject'), due: value('due') }),
          grades: () => ({ subject: value('subject'), value: Number(value('value')) }),
          events: () => ({ title: value('title'), date: value('date') })
        };
        const patch = patches[type]();
        if ('title' in patch && !patch.title) { app().notify('Введи название.'); return; }
        if (editor.index === null) list.push(type === 'tasks' ? { ...patch, done: false } : patch);
        else list[editor.index] = { ...list[editor.index], ...patch };
        persist(type, list);
      }
      editor = null; render(); app().notify('Сохранено');
    } catch { app().notify('Не удалось сохранить запись на устройстве.'); }
  }

  let sending = false;
  async function exportDay(button) {
    const day = app().getExport();
    if (sending) return;
    sending = true;
    button.disabled = true;
    try {
      const result = await app().api({ group: day?.group || '' }, undefined, true);
      if (!button.isConnected) return;
      const url = new URL(result.path, location.origin);
      const link = document.createElement('a');
      link.href = url.href;
      link.className = 'nb-menu-item';
      link.textContent = 'Открыть календарь и добавить занятия';
      link.target = '_blank';
      link.rel = 'noopener';
      link.onclick = event => {
        if (window.Telegram?.WebApp?.openLink) {
          event.preventDefault();
          window.Telegram.WebApp.openLink(url.href);
        }
        link.remove();
      };
      button.nextElementSibling?.matches('a.nb-menu-item') && button.nextElementSibling.remove();
      button.after(link);
      link.focus();
    } catch (error) {
      app().notify(error.message || 'Не удалось сохранить расписание.');
    } finally {
      button.disabled = false;
      sending = false;
    }
  }

  return { open() { page = 'home'; editor = null; render(); } };
})();
