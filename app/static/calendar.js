/* 经期日历：纯 JS 渲染月视图
 *
 * 浏览态：已记录的经期＝实色；其他日子按阶段着色（经期/卵泡/排卵/黄体，通俗视角为三段）；
 *         推断的日子（今天之后、以及下一次经期）同色但整体变淡。
 * 编辑态（点「✏️ 编辑经期」）：点一天即生效，规则在服务端（POST /cycle/tap）：
 *         点空白日＝从这天起勾「默认经期天数」天；点段内靠后的一天＝经期到前一天为止；
 *         点该段起始日＝删除这段；相邻段自动合并。点错再点一次即可还原。
 */
(function () {
  var el = document.getElementById('caldata');
  if (!el) return;
  var data = JSON.parse(el.textContent || '{}');
  var days = data.days || {};            // "YYYY-MM-DD" -> {p: 阶段, est: 0|1}
  var marks = data.marks || {};          // 日期 -> [日志类型]
  var todayIso = data.today || '';
  var canEdit = !!data.can_edit;
  var defaultDays = parseInt(data.default_days, 10) || 5;
  var ovu = {};                          // 事件式：估算排卵日（圈出数字）
  (data.ovu || []).forEach(function (d) { ovu[d] = 1; });
  var ranges = (data.periods || []).map(function (r) { return [r.start, r.end]; });
  var editing = false;
  // 日历显示方式（事件式 / 阶段配色）：打勾时原样回传，保证接口与整页用同一套数据
  var calMode = /(?:^|[?&])cal=phase(?:&|$)/.test(location.search) ? 'phase' : 'event';
  var view = (location.search.match(/[?&]view=([a-z]+)/) || [])[1] || 'med';

  function iso(d) {
    return d.getFullYear() + '-' + String(d.getMonth() + 1).padStart(2, '0') + '-' +
           String(d.getDate()).padStart(2, '0');
  }
  function parse(s) { return new Date(s + 'T12:00:00'); }      // 正午，避开时区/夏令时边界
  function inRanges(key) {
    for (var i = 0; i < ranges.length; i++) {
      if (key >= ranges[i][0] && key <= ranges[i][1]) return true;
    }
    return false;
  }
  function phaseLabel(k) {
    var it = document.querySelector('[data-phase-label="' + k + '"]');
    return it ? it.getAttribute('data-phase-text') : '';
  }
  function say(txt) {
    var s = document.getElementById('calEditState');
    if (s) s.textContent = txt || '';
  }

  var month = new Date((todayIso || iso(new Date())) + 'T12:00:00');
  month.setDate(1);

  function render() {
    var y = month.getFullYear(), m = month.getMonth();
    document.getElementById('calTitle').textContent = y + ' 年 ' + (m + 1) + ' 月';
    var grid = document.getElementById('calGrid');
    grid.innerHTML = '';
    grid.className = 'calgrid' + (editing ? ' editing' : '');
    ['一', '二', '三', '四', '五', '六', '日'].forEach(function (w) {
      var c = document.createElement('div'); c.className = 'dow'; c.textContent = w; grid.appendChild(c);
    });
    var first = new Date(y, m, 1, 12);
    var offset = (first.getDay() + 6) % 7;                       // 周一为一周起点
    var nDays = new Date(y, m + 1, 0).getDate();
    for (var i = 0; i < offset; i++) {
      var e = document.createElement('div'); e.className = 'day out';
      e.innerHTML = '<span class="n"></span>'; grid.appendChild(e);
    }
    var prevPhase = null;
    for (var d = 1; d <= nDays; d++) {
      var key = y + '-' + String(m + 1).padStart(2, '0') + '-' + String(d).padStart(2, '0');
      var info = days[key] || {};
      var period = inRanges(key);
      var est = period ? 0 : (info.est ? 1 : 0);
      var phase = period ? 'menstrual' : (info.p || '');
      // 阶段变化处留一道缝：色带像分段的珠子，而不是一整片（和周期环的观感一致）
      var brk = !!(phase && prevPhase && phase !== prevPhase);
      var cell = document.createElement('div');
      cell.className = 'day' + (phase ? ' ph-' + phase : '') + (period ? ' period' : '') +
                     (est ? ' est' : '') + (key === todayIso ? ' today' : '') +
                     (editing ? ' editable' : '') + (brk ? ' pbreak' : '') +
                     (ovu[key] ? ' ovu' : '');
      if (phase) prevPhase = phase; else prevPhase = null;
      cell.innerHTML = '<span class="n">' + d + '</span>' +
                       (key === todayIso ? '<span class="tl">今天</span>' : '');
      // 不再画「情绪/疼痛/用药/备注」这类日志点：日历保持干净，记录靠对话登记
      var label = key;
      if (period) label += '：经期（已记录）';
      else if (info.p) label += '：' + (phaseLabel(info.p) || info.p) + (est ? '（推断）' : '');
      if (ovu[key]) label += '；估算排卵日';
      cell.title = label;
      cell.dataset.k = key;
      cell.addEventListener('click', function () { onDay(this.dataset.k); });
      grid.appendChild(cell);
    }
  }

  function onDay(key) {
    if (editing) { tap(key); return; }
    // 浏览态不响应点击：以前会把日期填进「日志」表单并滚动过去，容易误触
  }

  function csrfValue() {
    var el2 = document.querySelector('input[name=csrf]');
    return el2 ? el2.value : '';
  }

  /* 编辑态：点一天 → 服务端按规则处理 → 用返回值重绘（规则只有一份，在服务端） */
  function tap(key) {
    var fd = new FormData();
    fd.append('csrf', csrfValue());
    fd.append('day', key);
    say('处理中…');
    fetch('/cycle/tap?view=' + encodeURIComponent(view) + '&cal=' + calMode,
          { method: 'POST', body: fd, credentials: 'same-origin' })
      .then(function (r) { return r.json(); })
      .then(function (j) {
        if (!j || !j.ok) throw new Error('bad');
        ranges = (j.ranges || []).map(function (r) { return [r.start, r.end]; });
        days = j.days || days;
        if (j.ovu) {                     // 估算排卵日也会随记录变，一起更新
          ovu = {};
          j.ovu.forEach(function (d) { ovu[d] = 1; });
        }
        if (j.default_days) {
          defaultDays = j.default_days;
          var h = document.getElementById('calNDaysHint');
          if (h) h.textContent = '点新的一段＝自动勾 ' + defaultDays + ' 天（按你以往记录推算）';
        }
        say(j.msg || '已更新');
        applyBlocks(j.blocks);
        render();
      })
      .catch(function () { say('没保存成功，检查网络后再点一次'); });
  }

  /* 把服务端重算好的卡片就地换掉（环／受孕率／激素图／统计与分布／身体数据）。
     片段顶层元素都带 id，按 id 逐个替换：不用刷新整页，也不会丢掉日历当前月份与编辑状态。 */
  function applyBlocks(blocks) {
    if (!blocks) return;
    Object.keys(blocks).forEach(function (name) {
      var html = blocks[name];
      if (!html || !html.trim()) return;
      var doc = new DOMParser().parseFromString(html, 'text/html');
      Array.prototype.forEach.call(doc.body.children, function (node) {
        if (!node.id) return;
        var cur = document.getElementById(node.id);
        if (cur) {
          cur.replaceWith(node);
          // 就地更新的卡片给一点点动效反馈（CSS 里定义了 bgPulse；开了减少动效会自动关掉）
          node.classList.remove('blk-updated');
          void node.offsetWidth;
          node.classList.add('blk-updated');
          setTimeout(function () { node.classList.remove('blk-updated'); }, 600);
        }
      });
    });
    if (window.__bgPager) window.__bgPager(document);   // 表格翻页要在片段换掉之后重跑
  }

  // 从「今天大姨妈来了吗」点「来了」跳过来时（?edit=1）：直接进入编辑模式，方便微调天数
  if (location.search.indexOf('edit=1') >= 0) {
    var _tgl = document.getElementById('calEditToggle');
    if (_tgl && (_tgl.textContent || '').indexOf('完成编辑') < 0) _tgl.click();
  }

  document.getElementById('calPrev').addEventListener('click', function () {
    month.setMonth(month.getMonth() - 1); render();
  });
  document.getElementById('calNext').addEventListener('click', function () {
    month.setMonth(month.getMonth() + 1); render();
  });

  // 回到今天：翻回当前月份并把日历滚进视野（翻月看别的月份后一键回来）
  var todayBtn = document.getElementById('calToday');
  if (todayBtn) todayBtn.addEventListener('click', function () {
    month = new Date((todayIso || iso(new Date())) + 'T12:00:00');
    month.setDate(1);
    render();
    var wrap = document.getElementById('calwrap');
    if (wrap && wrap.scrollIntoView) wrap.scrollIntoView({ behavior: 'smooth', block: 'start' });
  });

  var toggle = document.getElementById('calEditToggle');
  var bar = document.getElementById('calEditBar');
  function setEditing(on) {
    editing = on;
    if (bar) bar.hidden = !on;
    if (toggle) toggle.textContent = on ? '✓ 完成编辑' : '✏️ 编辑经期';
    var tip = document.getElementById('calTipView');
    var tipE = document.getElementById('calTipEdit');
    if (tip) tip.hidden = on;
    if (tipE) tipE.hidden = !on;
    if (!on) say('');
    render();
  }
  if (toggle) toggle.addEventListener('click', function () { setEditing(!editing); });

  // 天数说明：不再让用户设「默认经期天数」，改为按历史推算（后端算好）
  var nHint = document.getElementById('calNDaysHint');
  if (nHint) nHint.textContent = '点新的一段＝从这天起一次勾 ' + defaultDays + ' 天；点错了再点起始日即可删掉';
  render();
})();
