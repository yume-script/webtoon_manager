(function () {
  // 이 파일은 코어에 의해 new Function('pluginId', 'container', js)(pluginId, container)
  // 형태로 실행되는 것으로 확인됨(RCLONE_MANAGER 등 기존 플러그인과 동일 컨벤션).
  // pluginId, container 는 바깥 스코프에서 주입됨.
  //
  // 엔드포인트는 plugin_board(yume-script/plugin_board)의 실제 동작 중인
  // script.js에서 그대로 확인한 규격을 사용한다:
  //   - 데이터 조회: GET /api/media/dashboard/widgets/{pluginId}/data?type={dbType}
  //   - 액션 호출:   POST /api/media/books/0/apply-metadata
  //                  body: { type: dbType, source: pluginId, item_data: {...} }
  //                  (book_id=0이 URL 경로에 고정, item_data가 apply()의 item_data로 그대로 전달됨)

  function getDbType() {
    var params = new URLSearchParams(window.location.search);
    return params.get('db_type') || 'general';
  }

  function dataUrl() {
    return '/api/media/dashboard/widgets/' + pluginId + '/data?type=' + encodeURIComponent(getDbType());
  }

  async function callAction(actionData, timeoutMs) {
    timeoutMs = timeoutMs || 60000;
    var dbType = getDbType();
    var controller = new AbortController();
    var timer = setTimeout(function () { controller.abort(); }, timeoutMs);
    var resp;
    try {
      resp = await fetch('/api/media/books/0/apply-metadata', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ type: dbType, source: pluginId, item_data: actionData }),
        signal: controller.signal
      });
    } catch (e) {
      clearTimeout(timer);
      if (e && e.name === 'AbortError') {
        return { success: false, message: '요청이 시간 내에 응답하지 않았습니다.' };
      }
      return { success: false, message: '서버에 연결하지 못했습니다: ' + (e && e.message ? e.message : e) };
    }
    clearTimeout(timer);

    var text = '';
    try { text = await resp.text(); } catch (e) { /* ignore */ }
    var data = null;
    if (text) {
      try { data = JSON.parse(text); } catch (e) { /* not json */ }
    }
    if (!data) {
      return { success: false, message: '서버가 올바른 응답을 반환하지 않았습니다 (HTTP ' + resp.status + ').' };
    }
    var success = data.success !== undefined ? !!data.success : false;
    var message = data.message !== undefined ? data.message : (data.error || '');
    return { success: success, message: message, raw: data };
  }

  function parseMaybeJson(text) {
    if (typeof text !== 'string') return text;
    try { return JSON.parse(text); } catch (e) { return null; }
  }

  function el(sel) { return container.querySelector(sel); }
  function els(sel) { return Array.prototype.slice.call(container.querySelectorAll(sel)); }

  function fmtDate(ts) {
    if (!ts) return '';
    var d = new Date(ts * 1000);
    return d.getFullYear() + '-' + String(d.getMonth() + 1).padStart(2, '0') + '-' +
      String(d.getDate()).padStart(2, '0') + ' ' + String(d.getHours()).padStart(2, '0') + ':' +
      String(d.getMinutes()).padStart(2, '0');
  }

  async function fetchData() {
    var resp = await fetch(dataUrl(), { credentials: 'same-origin' });
    if (!resp.ok) throw new Error('데이터 조회 실패: HTTP ' + resp.status);
    var body = await resp.json();
    if (body && body.success === false) throw new Error(body.error || '데이터 조회 실패');
    var items = body.items || [];
    return items[0] || { titles: [], authors_tags: { authors: [], tags: [] }, history: [], job: {}, log_tail: [] };
  }

  // ------------------------------------------------------------------
  // 상태
  // ------------------------------------------------------------------
  var state = { titles: [], authors_tags: { authors: [], tags: [] }, history: [], job: {}, log_tail: [] };
  var currentTab = 'all';
  var currentWeekday = 'all';
  var searchQuery = '';
  var lookupResult = null;
  var pollTimer = null;
  var pollFastUntil = 0;

  function statusOf(t) {
    if (t.excluded) return 'excluded';
    if (t.unsubscribed) return 'unsubscribed';
    if (t.subscribed) return 'subscribed';
    return 'all';
  }

  function filteredTitles() {
    var list = state.titles.slice();
    if (currentTab === 'subscribed') list = list.filter(function (t) { return t.subscribed && !t.excluded && !t.unsubscribed; });
    else if (currentTab === 'unsubscribed') list = list.filter(function (t) { return t.unsubscribed; });
    else if (currentTab === 'excluded') list = list.filter(function (t) { return t.excluded; });

    if (currentWeekday === 'finish') {
      list = list.filter(function (t) { return t.status === '완결'; });
    } else if (currentWeekday !== 'all') {
      list = list.filter(function (t) { return (t.weekdays || []).indexOf(currentWeekday) >= 0; });
    }

    if (searchQuery) {
      var q = searchQuery.toLowerCase();
      list = list.filter(function (t) {
        return (t.title || '').toLowerCase().indexOf(q) >= 0 ||
          (t.author || '').toLowerCase().indexOf(q) >= 0;
      });
    }
    return list;
  }

  function badgeHtml(t) {
    var out = '';
    if (t.new) out += '<span class="wtm-badge new">신작</span>';
    if (t.status === '완결') out += '<span class="wtm-badge finished">완결</span>';
    if (t.rest) out += '<span class="wtm-badge rest">휴재</span>';
    if (t.up_flag) out += '<span class="wtm-badge up">UP</span>';
    return out;
  }

  function cardActionsHtml(t) {
    var st = statusOf(t);
    if (st === 'subscribed') {
      return '<button class="wtm-btn wtm-btn-small" data-card-action="unsubscribe" data-title-id="' + t.titleId + '">구독해제</button>' +
        '<button class="wtm-btn wtm-btn-small wtm-btn-danger" data-card-action="exclude" data-title-id="' + t.titleId + '">제외</button>';
    }
    if (st === 'unsubscribed' || st === 'excluded') {
      return '<button class="wtm-btn wtm-btn-small wtm-btn-primary" data-card-action="restore" data-title-id="' + t.titleId + '">다시 구독</button>';
    }
    return '<button class="wtm-btn wtm-btn-small wtm-btn-primary" data-card-action="subscribe" data-title-id="' + t.titleId + '">구독</button>' +
      '<button class="wtm-btn wtm-btn-small wtm-btn-danger" data-card-action="exclude" data-title-id="' + t.titleId + '">제외</button>';
  }

  function renderGrid() {
    var grid = el('[data-el="title-grid"]');
    if (!grid) return;
    var list = filteredTitles();
    if (!list.length) {
      grid.innerHTML = '<div class="wtm-hint">표시할 작품이 없습니다. "지금 스캔"을 먼저 실행해보세요.</div>';
      return;
    }
    grid.innerHTML = list.map(function (t) {
      return '<div class="wtm-card">' +
        (t.thumbnail ? '<img class="wtm-card-thumb" src="' + t.thumbnail + '" loading="lazy">' :
          '<div class="wtm-card-thumb"></div>') +
        '<div class="wtm-card-body">' +
        '<div class="wtm-card-title">' + escapeHtml(t.title || t.titleId) + '</div>' +
        '<div class="wtm-card-author">' + escapeHtml(t.author || '') +
        ' <span class="wtm-card-id">[' + escapeHtml(t.titleId) + ']</span></div>' +
        '<div class="wtm-badges">' + badgeHtml(t) + '</div>' +
        '<div class="wtm-card-actions">' + cardActionsHtml(t) + '</div>' +
        '</div></div>';
    }).join('');
  }

  function escapeHtml(s) {
    return String(s == null ? '' : s).replace(/[&<>"']/g, function (c) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c];
    });
  }

  function renderAuthorsTags() {
    var at = state.authors_tags || { authors: [], tags: [] };
    var authorList = el('[data-el="author-list"]');
    var tagList = el('[data-el="tag-list"]');
    if (authorList) {
      authorList.innerHTML = (at.authors || []).map(function (a) {
        return '<span class="wtm-chip">' + escapeHtml(a) +
          '<button data-chip-remove="author" data-value="' + escapeHtml(a) + '">&times;</button></span>';
      }).join('') || '<span class="wtm-hint">등록된 작가 없음</span>';
    }
    if (tagList) {
      tagList.innerHTML = (at.tags || []).map(function (a) {
        return '<span class="wtm-chip">' + escapeHtml(a) +
          '<button data-chip-remove="tag" data-value="' + escapeHtml(a) + '">&times;</button></span>';
      }).join('') || '<span class="wtm-hint">등록된 태그 없음</span>';
    }
  }

  function renderHistory() {
    var box = el('[data-el="history-list"]');
    if (!box) return;
    var list = state.history || [];
    if (!list.length) { box.innerHTML = '<div class="wtm-hint">다운로드 이력이 없습니다.</div>'; return; }
    box.innerHTML = list.map(function (h) {
      var isFail = (h.type || '').indexOf('fail') >= 0;
      var text = '';
      if (h.type === 'download' || h.type === 'manual_download') {
        text = escapeHtml(h.title) + ' - ' + h.episode_no + '화 (' + (h.image_count || 0) + '장)';
      } else if (isFail) {
        text = escapeHtml(h.title) + ' - ' + h.episode_no + '화 실패: ' + escapeHtml(h.error || '');
      } else {
        text = JSON.stringify(h);
      }
      return '<div class="wtm-history-item' + (isFail ? ' fail' : '') + '">' +
        '<span>' + fmtDate(h.ts) + '</span> &middot; ' + text + '</div>';
    }).join('');
  }

  function renderSettingsSummary() {
    var box = el('[data-el="settings-summary"]');
    if (!box) return;
    var cfg = state.config_public || {};
    var rows = [
      ['네이버 아이디', cfg.NAVER_ID || '(미설정)'],
      ['쿠키 등록', cfg.has_cookie ? '등록됨' : '(미설정)'],
      ['다운로드 경로', cfg.DOWNLOAD_ROOT || '(기본값)'],
      ['자동 실행', cfg.ENABLE_SCHEDULER ? ('사용 / ' + cfg.INTERVAL_MINUTES + '분 주기') : '사용 안 함'],
      ['작품당 최대 신규 다운로드', cfg.MAX_NEW_EPISODES_PER_TITLE],
      ['디스코드 알림', cfg.has_discord ? '설정됨' : '(미설정)']
    ];
    box.innerHTML = rows.map(function (r) {
      return '<div><b>' + r[0] + '</b><br>' + escapeHtml(String(r[1])) + '</div>';
    }).join('');

    var logBox = el('[data-el="log-tail"]');
    if (logBox) logBox.textContent = (state.log_tail || []).join('\n');
  }

  function renderStatusBar() {
    var job = state.job || {};
    var pill = el('[data-el="status-pill"]');
    var msg = el('[data-el="status-message"]');
    var progWrap = el('[data-el="progress-wrap"]');
    var progBar = el('[data-el="progress-bar"]');
    var cancelBtn = el('[data-el="cancel-btn"]');

    if (pill) {
      pill.className = 'wtm-status-pill' + (job.running ? ' running' : (job.stage === 'error' ? ' error' : (job.stage === 'done' ? ' done' : '')));
      pill.textContent = job.running ? '실행 중' : (job.stage === 'error' ? '오류' : (job.stage === 'done' ? '완료' : '대기 중'));
    }
    if (msg) msg.textContent = job.message || '';
    if (progWrap && progBar) {
      if (job.running && job.total) {
        progWrap.style.display = '';
        progBar.style.width = Math.min(100, Math.round((job.progress / job.total) * 100)) + '%';
      } else {
        progWrap.style.display = 'none';
      }
    }
    if (cancelBtn) cancelBtn.style.display = job.running ? '' : 'none';

    pollFastUntil = job.running ? (Date.now() + 30000) : pollFastUntil;
  }

  function renderAll() {
    renderStatusBar();
    renderGrid();
    renderAuthorsTags();
    renderHistory();
    renderSettingsSummary();
  }

  async function refresh() {
    try {
      state = await fetchData();
      renderAll();
    } catch (e) {
      console.error('[webtoon_manager] 새로고침 실패:', e);
      var msg = el('[data-el="status-message"]');
      if (msg) msg.textContent = '데이터 로드 실패: ' + e.message;
    }
  }

  function schedulePoll() {
    if (pollTimer) clearTimeout(pollTimer);
    var interval = (Date.now() < pollFastUntil) ? 2500 : 10000;
    pollTimer = setTimeout(function () {
      if (!document.hidden) refresh().then(schedulePoll);
      else schedulePoll();
    }, interval);
  }

  // ------------------------------------------------------------------
  // 탭 전환
  // ------------------------------------------------------------------
  function setTab(tab) {
    currentTab = tab;
    els('.wtm-tab').forEach(function (b) { b.classList.toggle('active', b.getAttribute('data-tab') === tab); });

    var isListTab = ['all', 'subscribed', 'unsubscribed', 'excluded'].indexOf(tab) >= 0;
    els('[data-panel-view]').forEach(function (p) {
      var views = p.getAttribute('data-panel-view').split(',');
      p.style.display = views.indexOf(tab) >= 0 ? '' : 'none';
    });
    var toolbars = els('[data-panel="all,subscribed,unsubscribed,excluded"]');
    toolbars.forEach(function (t) { t.style.display = isListTab ? '' : 'none'; });

    renderGrid();
  }

  // ------------------------------------------------------------------
  // 이벤트 바인딩
  // ------------------------------------------------------------------
  container.addEventListener('change', function (ev) {
    var selectAll = ev.target.closest('[data-ep-select-all]');
    if (selectAll) {
      var checked = selectAll.checked;
      els('[data-ep-checkbox]').forEach(function (c) { c.checked = checked; });
    }
  });

  container.addEventListener('click', async function (ev) {
    var tabBtn = ev.target.closest('.wtm-tab');
    if (tabBtn) { setTab(tabBtn.getAttribute('data-tab')); return; }

    var weekdayBtn = ev.target.closest('.wtm-weekday-btn');
    if (weekdayBtn) {
      currentWeekday = weekdayBtn.getAttribute('data-weekday');
      els('.wtm-weekday-btn').forEach(function (b) { b.classList.toggle('active', b === weekdayBtn); });
      renderGrid();
      return;
    }

    var headerAction = ev.target.closest('[data-action]');
    if (headerAction) {
      var action = headerAction.getAttribute('data-action');
      if (action === 'refresh') { await refresh(); return; }
      if (action === 'scan_now' || action === 'run_full_cycle_now' || action === 'cancel_job' || action === 'test_discord') {
        headerAction.disabled = true;
        var r = await callAction({ action: action });
        headerAction.disabled = false;
        if (!r.success) alert(r.message || '실패');
        await refresh();
        return;
      }
      if (action === 'add_author' || action === 'add_tag') {
        var key = action === 'add_author' ? 'author-input' : 'tag-input';
        var input = el('[data-el="' + key + '"]');
        if (!input || !input.value.trim()) return;
        var r2 = await callAction({ action: action, value: input.value.trim() });
        if (r2.success) { input.value = ''; await refresh(); } else { alert(r2.message); }
        return;
      }
      if (action === 'manual_lookup') {
        var idInput = el('[data-el="manual-title-id"]');
        var titleId = idInput && idInput.value.trim();
        if (!titleId) return;
        var resultBox = el('[data-el="manual-result"]');
        if (resultBox) resultBox.innerHTML = '<div class="wtm-hint">조회 중...</div>';
        var r3 = await callAction({ action: 'manual_lookup', titleId: titleId });
        var parsed = r3.success ? parseMaybeJson(r3.message) : null;
        if (!r3.success || !parsed) {
          if (resultBox) resultBox.innerHTML = '<div class="wtm-hint">조회 실패: ' + escapeHtml(r3.message || '') + '</div>';
          return;
        }
        lookupResult = parsed;
        renderManualResult();
        return;
      }
      if (action === 'manual_download_selected') {
        if (!lookupResult) return;
        var checked = els('[data-ep-checkbox]:checked').map(function (c) { return parseInt(c.getAttribute('data-ep-checkbox'), 10); });
        if (!checked.length) { alert('회차를 선택하세요'); return; }
        var r4 = await callAction({ action: 'manual_download', titleId: lookupResult.titleId, title: lookupResult.title, episodeNos: checked });
        alert(r4.message || (r4.success ? '시작됨' : '실패'));
        await refresh();
        return;
      }
    }

    var cardAction = ev.target.closest('[data-card-action]');
    if (cardAction) {
      var actName = cardAction.getAttribute('data-card-action');
      var titleId2 = cardAction.getAttribute('data-title-id');
      cardAction.disabled = true;
      var r5 = await callAction({ action: actName, titleId: titleId2 });
      cardAction.disabled = false;
      if (!r5.success) alert(r5.message || '실패');
      await refresh();
      return;
    }

    var chipRemove = ev.target.closest('[data-chip-remove]');
    if (chipRemove) {
      var kind = chipRemove.getAttribute('data-chip-remove');
      var value = chipRemove.getAttribute('data-value');
      var act = kind === 'author' ? 'remove_author' : 'remove_tag';
      var r6 = await callAction({ action: act, value: value });
      if (r6.success) await refresh(); else alert(r6.message);
      return;
    }
  });

  var searchInput = el('[data-el="search-input"]');
  if (searchInput) {
    searchInput.addEventListener('input', function () {
      searchQuery = searchInput.value.trim();
      renderGrid();
    });
  }

  function renderManualResult() {
    var box = el('[data-el="manual-result"]');
    if (!box || !lookupResult) return;
    var eps = lookupResult.episodes || [];
    box.innerHTML =
      '<div class="wtm-box" style="margin-top:10px">' +
      '<div class="wtm-box-title">' + escapeHtml(lookupResult.title) + ' (titleId=' + lookupResult.titleId + ')</div>' +
      '<label style="display:flex;gap:8px;padding:3px 0 8px;font-size:12px;font-weight:600">' +
      '<input type="checkbox" data-ep-select-all> 전체 선택</label>' +
      '<div style="max-height:260px;overflow:auto">' +
      eps.map(function (e) {
        return '<label style="display:flex;gap:8px;padding:3px 0;font-size:12px">' +
          '<input type="checkbox" data-ep-checkbox="' + e.no + '"> ' +
          e.no + '화 - ' + escapeHtml(e.subtitle || '') + (e.charge ? ' (유료)' : '') + '</label>';
      }).join('') +
      '</div>' +
      '<button class="wtm-btn wtm-btn-primary" style="margin-top:8px" data-action="manual_download_selected">선택 회차 다운로드</button>' +
      '</div>';
  }

  // ------------------------------------------------------------------
  // 초기화
  // ------------------------------------------------------------------
  setTab('all');
  refresh().then(schedulePoll);
})();
