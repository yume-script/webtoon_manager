(function () {
  // 이 파일은 코어에 의해 new Function('pluginId', 'container', js)(pluginId, container)
  // 형태로 실행되는 것으로 확인됨(RCLONE_MANAGER 등 기존 플러그인과 동일 컨벤션).
  // pluginId, container 는 바깥 스코프에서 주입됨.

  var dbType = (container.dataset && container.dataset.dbType) ||
    (window.currentDbType) ||
    (window.BookOasisDbType) ||
    'general';

  var DATA_URL = '/api/media/dashboard/widgets/' + pluginId + '/data?db_type=' + encodeURIComponent(dbType) + '&limit=5000';

  // 코어 소스(api/routes/plugin_routes.py)를 직접 grep해서 확인된 유일한 진짜
  // 액션 엔드포인트. run_context_menu_action(db_type, action_id, context)로
  // 라우팅되며, 요청 바디는 최상위 필드로 type/plugin_id/action_id/context 만
  // 읽는다(item_data, book_id, db_type 같은 이름은 서버가 읽지 않음 — 넣어도
  // 무해하지만 무시됨).
  var ACTION_URL = '/api/media/context-menu/book/plugins/action';

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
    var resp = await fetch(DATA_URL, { credentials: 'same-origin' });
    if (!resp.ok) throw new Error('데이터 조회 실패: HTTP ' + resp.status);
    var body = await resp.json();
    var items = body.items || (body.data && body.data.items) || [];
    return items[0] || {
      titles: [], authors_tags: { authors: [], tags: [] }, history: [], job: {}, log_tail: [],
      update_status: {}, repo_url: 'https://github.com/yume-script/webtoon_manager'
    };
  }

  async function callAction(actionId, payload) {
    payload = payload || {};
    // 코어 라우트가 실제로 읽는 필드만 최상위에 둔다: type(=db_type), plugin_id,
    // action_id, context. (예전엔 db_type이라는 이름으로 보내서 서버가 못 읽고
    // 매번 general로 취급되던 버그가 있었음 — type으로 고침.)
    var body = {
      type: dbType,
      plugin_id: pluginId,
      action_id: actionId,
      context: Object.assign({ action: actionId }, payload)
    };

    try {
      var resp = await fetch(ACTION_URL, {
        method: 'POST',
        credentials: 'same-origin',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body)
      });
      var data = await resp.json();
      // 코어는 run_context_menu_action()이 {'success': bool, 'message'|'error': str}
      // dict를 반환할 것으로 기대하고, success=false면 HTTP 400으로 내려준다.
      // resp.ok(2xx) 여부가 아니라 data.success로 성공/실패를 판단해야 한다.
      var success = !!data.success;
      var message = data.message || data.error || '';
      return { success: success, message: message, raw: data };
    } catch (e) {
      var errMsg = e.message || String(e);
      console.error('[webtoon_manager] 액션 호출 실패:', errMsg);
      return { success: false, message: '액션 호출 실패: ' + errMsg };
    }
  }

  function parseMaybeJson(text) {
    if (typeof text !== 'string') return text;
    try { return JSON.parse(text); } catch (e) { return null; }
  }

  // ------------------------------------------------------------------
  // 상태
  // ------------------------------------------------------------------
  var state = { titles: [], authors_tags: { authors: [], tags: [] }, history: [], job: {}, log_tail: [] };
  var currentTab = 'all';
  var searchQuery = '';
  var dayFilter = 'all';
  var sortMode = 'default';
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
    // 'all' 은 필터 없이 전체

    if (dayFilter === 'finished') {
      list = list.filter(function (t) { return t.status === '완결'; });
    } else if (dayFilter !== 'all') {
      list = list.filter(function (t) { return (t.weekdays || []).indexOf(dayFilter) >= 0; });
    }

    if (searchQuery) {
      var q = searchQuery.toLowerCase();
      list = list.filter(function (t) {
        return (t.title || '').toLowerCase().indexOf(q) >= 0 ||
          (t.author || '').toLowerCase().indexOf(q) >= 0;
      });
    }

    if (sortMode === 'rating') {
      list.sort(function (a, b) {
        var diff = (b.rating == null ? -1 : b.rating) - (a.rating == null ? -1 : a.rating);
        if (diff !== 0) return diff;
        // 평점이 같으면(=동점) titleId로 순서를 고정한다. 서버가 주는 원래
        // 배열 순서(last_seen_at)로 동점자를 처리하면, 스캔이 진행 중일 때
        // last_seen_at이 계속 바뀌면서 화면이 매번 흔들리기 때문.
        return String(a.titleId).localeCompare(String(b.titleId));
      });
    } else if (sortMode === 'title') {
      list.sort(function (a, b) {
        var diff = (a.title || '').localeCompare(b.title || '', 'ko');
        if (diff !== 0) return diff;
        return String(a.titleId).localeCompare(String(b.titleId));
      });
    }
    // 'default' 는 이미 last_seen_at 내림차순으로 정렬된 state.titles 순서 그대로 사용

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
      return '<button class="wtm-btn wtm-btn-small wtm-btn-primary" data-card-action="download_title" data-title-id="' + t.titleId + '" title="last_downloaded_no 이후의 새 회차를 지금 바로 찾아서 받습니다">새회차 다운로드</button>' +
        '<button class="wtm-btn wtm-btn-small" data-goto-manual="' + t.titleId + '">선택 회차 다운로드</button>' +
        '<button class="wtm-btn wtm-btn-small" data-card-action="resync_title" data-title-id="' + t.titleId + '" title="파일을 직접 지운 회차가 있으면 눌러주세요 - 다음 다운로드 때 전체 회차를 다시 확인합니다">다시 확인</button>' +
        '<button class="wtm-btn wtm-btn-small" data-card-action="unsubscribe" data-title-id="' + t.titleId + '">구독해제</button>' +
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
        '<div class="wtm-card-author">' + escapeHtml(t.author || '') + '</div>' +
        (t.rating != null ? '<div class="wtm-card-rating">★ ' + t.rating.toFixed(2) + '</div>' : '') +
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

    // 스캔/전체실행과 독립된 개별 작품 다운로드 상태(title_job)도 함께 표시
    var tjob = state.title_job || {};
    var tbar = el('[data-el="title-job-bar"]');
    var tmsg = el('[data-el="title-job-message"]');
    if (tbar) tbar.style.display = tjob.running ? '' : 'none';
    if (tmsg) tmsg.textContent = tjob.message || '';
    if (tjob.running) pollFastUntil = Date.now() + 30000;
  }

  function renderAll() {
    renderStatusBar();
    renderGrid();
    renderAuthorsTags();
    renderHistory();
    renderSettingsSummary();
    var verEl = el('[data-el="plugin-version"]');
    if (verEl) verEl.textContent = state.plugin_version ? ('v' + state.plugin_version) : '';
    renderUpdateBadge();
  }

  function renderUpdateBadge() {
    var badge = el('[data-el="update-badge"]');
    if (!badge) return;
    var upd = state.update_status || {};
    if (upd.update_available && upd.latest_version) {
      badge.style.display = '';
      badge.title = '현재 v' + (state.plugin_version || '?') + ' \u2192 최신 v' + upd.latest_version +
        ' (GitHub 저장소 열기, 새 코드를 직접 받아 교체하거나 환경설정의 업데이트 버튼을 사용하세요)';
      badge.innerHTML = '<i class="fa-solid fa-arrow-up"></i> 업데이트 가능 (v' + escapeHtml(upd.latest_version) + ')';
      badge.href = state.repo_url || 'https://github.com/yume-script/webtoon_manager';
    } else {
      badge.style.display = 'none';
    }
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
    var toolbar = el('[data-panel="all,subscribed,unsubscribed,excluded"]');
    if (toolbar) toolbar.style.display = isListTab ? '' : 'none';

    renderGrid();
  }

  // ------------------------------------------------------------------
  // 이벤트 바인딩
  // ------------------------------------------------------------------
  container.addEventListener('click', async function (ev) {
    var tabBtn = ev.target.closest('.wtm-tab');
    if (tabBtn) { setTab(tabBtn.getAttribute('data-tab')); return; }

    var dayBtn = ev.target.closest('.wtm-daytab');
    if (dayBtn) {
      dayFilter = dayBtn.getAttribute('data-day');
      els('.wtm-daytab').forEach(function (b) { b.classList.toggle('active', b === dayBtn); });
      renderGrid();
      return;
    }

    var headerAction = ev.target.closest('[data-action]');
    if (headerAction) {
      var action = headerAction.getAttribute('data-action');
      if (action === 'refresh') { await refresh(); return; }
      if (action === 'scan_now' || action === 'scan_finished_now' || action === 'run_full_cycle_now' || action === 'cancel_job' ||
          action === 'cancel_title_job' || action === 'test_discord' || action === 'force_reset_job') {
        if (action === 'force_reset_job' && !confirm('정말로 작업 상태를 강제 초기화할까요? 지금 실제로 뭔가 진행 중이라면 중간에 끊길 수 있습니다.')) return;
        headerAction.disabled = true;
        var r = await callAction(action, {});
        headerAction.disabled = false;
        if (!r.success) alert(r.message || '실패');
        await refresh();
        return;
      }
      if (action === 'add_author' || action === 'add_tag') {
        var key = action === 'add_author' ? 'author-input' : 'tag-input';
        var input = el('[data-el="' + key + '"]');
        if (!input || !input.value.trim()) return;
        var r2 = await callAction(action, { value: input.value.trim() });
        if (r2.success) { input.value = ''; await refresh(); } else { alert(r2.message); }
        return;
      }
      if (action === 'manual_lookup') {
        var idInput = el('[data-el="manual-title-id"]');
        var titleId = idInput && idInput.value.trim();
        if (!titleId) return;
        var resultBox = el('[data-el="manual-result"]');
        if (resultBox) resultBox.innerHTML = '<div class="wtm-hint">조회 중...</div>';
        var r3 = await callAction('manual_lookup', { titleId: titleId });
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
        var r4 = await callAction('manual_download', { titleId: lookupResult.titleId, title: lookupResult.title, episodeNos: checked });
        alert(r4.message || (r4.success ? '시작됨' : '실패'));
        await refresh();
        return;
      }
      if (action === 'manual_download_all') {
        if (!lookupResult) return;
        var freeAllEps = (lookupResult.episodes || []).filter(function (e) { return !e.charge; });
        var freeEps = freeAllEps.map(function (e) { return e.no; });
        if (!freeEps.length) { alert('다운로드 가능한(무료) 회차가 없습니다'); return; }
        var alreadyDone = freeAllEps.filter(function (e) { return e.downloaded; }).length;
        var confirmMsg = '무료 회차 ' + freeEps.length + '개를 전부 다운로드할까요?';
        if (alreadyDone > 0) {
          confirmMsg += ' (이미 받은 ' + alreadyDone + '개는 건너뛰고 나머지 ' +
            (freeEps.length - alreadyDone) + '개만 실제로 받습니다)';
        }
        if (!confirm(confirmMsg)) return;
        var r4b = await callAction('manual_download', { titleId: lookupResult.titleId, title: lookupResult.title, episodeNos: freeEps });
        alert(r4b.message || (r4b.success ? '시작됨' : '실패'));
        await refresh();
        return;
      }
    }

    var gotoManual = ev.target.closest('[data-goto-manual]');
    if (gotoManual) {
      var tidForManual = gotoManual.getAttribute('data-goto-manual');
      setTab('manual');
      var idInputForManual = el('[data-el="manual-title-id"]');
      if (idInputForManual) idInputForManual.value = tidForManual;
      var lookupBtn = document.querySelector('[data-action="manual_lookup"]');
      if (lookupBtn) lookupBtn.click();
      return;
    }

    var cardAction = ev.target.closest('[data-card-action]');
    if (cardAction) {
      var actName = cardAction.getAttribute('data-card-action');
      var titleId2 = cardAction.getAttribute('data-title-id');
      cardAction.disabled = true;
      var r5 = await callAction(actName, { titleId: titleId2 });
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
      var r6 = await callAction(act, { value: value });
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

  var sortSelect = el('[data-el="sort-select"]');
  if (sortSelect) {
    sortSelect.addEventListener('change', function () {
      sortMode = sortSelect.value;
      renderGrid();
    });
  }

  function renderManualResult() {
    var box = el('[data-el="manual-result"]');
    if (!box || !lookupResult) return;
    var eps = lookupResult.episodes || [];
    var downloadedCount = eps.filter(function (e) { return e.downloaded; }).length;
    box.innerHTML =
      '<div class="wtm-box" style="margin-top:10px">' +
      '<div class="wtm-box-title">' + escapeHtml(lookupResult.title) + ' (titleId=' + lookupResult.titleId + ')' +
      ' <span class="wtm-hint" style="margin:0">- 받음 ' + downloadedCount + '/' + eps.length + '화</span></div>' +
      '<div style="max-height:260px;overflow:auto">' +
      eps.map(function (e) {
        var isPaid = !!e.charge;
        var isDone = !!e.downloaded;
        return '<label style="display:flex;align-items:center;gap:8px;padding:3px 0;font-size:12px;' + (isPaid ? 'opacity:.5' : '') + '">' +
          '<input type="checkbox" data-ep-checkbox="' + e.no + '"' + (isPaid ? ' disabled title="유료 회차는 선택할 수 없습니다"' : '') + '> ' +
          '<span' + (isDone ? ' style="opacity:.6"' : '') + '>' + e.no + '화 - ' + escapeHtml(e.subtitle || '') + '</span>' +
          (isPaid ? ' <b>(유료 - 선택불가)</b>' : '') +
          (isDone ? ' <span class="wtm-badge" style="background:color-mix(in srgb, #4f9d76 22%, transparent);color:color-mix(in srgb, #4f9d76 90%, var(--app-text-primary))">받음</span>' : '') +
          '</label>';
      }).join('') +
      '</div>' +
      '<div style="display:flex;gap:8px;margin-top:8px;flex-wrap:wrap">' +
      '<button class="wtm-btn wtm-btn-primary" data-action="manual_download_selected">선택 회차 다운로드</button>' +
      '<button class="wtm-btn wtm-btn-secondary" data-action="manual_download_all">전체 다운로드(무료만)</button>' +
      '</div>' +
      '</div>';
  }

  // ------------------------------------------------------------------
  // 초기화
  // ------------------------------------------------------------------
  setTab('all');
  refresh().then(schedulePoll);
})();
