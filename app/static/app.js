/* 轻交互：折叠、表单确认、右下角悬浮问答。无框架依赖。 */
(function () {
  // 危险操作二次确认
  document.addEventListener('submit', function (e) {
    var f = e.target;
    if (f.dataset && f.dataset.confirm) {
      if (!window.confirm(f.dataset.confirm)) { e.preventDefault(); }
    }
  });

  // 仅展开一个 details 折叠面板（同一容器内）
  document.querySelectorAll('details.fold').forEach(function (d) {
    d.addEventListener('toggle', function () {
      if (!d.open) return;
      var parent = d.parentElement;
      if (!parent) return;
      parent.querySelectorAll('details.fold[open]').forEach(function (o) {
        if (o !== d) o.open = false;
      });
    });
  });

  // 自动关闭提示条
  document.querySelectorAll('.flash').forEach(function (el) {
    setTimeout(function () { el.style.transition = 'opacity .6s'; el.style.opacity = '0.55'; }, 9000);
  });
})();

/* AI 助手（唯一的问答入口）
 * - 一个用户一个固定会话：打开面板就拉历史，不轮转、不新建
 * - 正文是 Markdown：流式时先按纯文本实时显示，结束后用服务端清洗渲染的 HTML 替换
 * - 思考过程：模型边想边来一行滚动文字（点「查看」看全文）；模型没有思考通道时，
 *   这一行显示服务端真实步骤（读档案/检索知识库/取到几段依据）
 * - 手机：底部中间一颗圆「AI」→ 全屏对话，同一颗按钮变成 ✗ 点回来
 * - 桌面：左上角 ⇕ 按住上下拖改变高度（记在本地）
 * - 左下角 📎 传图 / 📷 拍照上传；上方两个功能键：清空记录、总结归档
 */
(function () {
  var fab = document.getElementById('fab');
  var panel = document.getElementById('fabpanel');
  if (!fab || !panel) return;
  var box = document.getElementById('fabchat');
  var form = document.getElementById('fabform');
  var closeBtn = document.getElementById('fabclose');
  var grip = document.getElementById('fabgrip');
  var ctxEl = document.getElementById('fabctx');
  var emptyEl = document.getElementById('fabempty');
  var fileEl = document.getElementById('fabfile');
  var camEl = document.getElementById('fabcam');
  var clearBtn = document.getElementById('fabclear');
  var archBtn = document.getElementById('fabarch');
  var cardBtn = document.getElementById('fabcard');
  var docEl = document.getElementById('fabdoc');
  var plusBtn = document.getElementById('fabplus');
  var sheetEl = document.getElementById('fabsheet');
  var sendBtn = document.getElementById('fabsend');
  var taEl = form.querySelector('textarea');
  var attsEl = document.getElementById('fabatts');
  var hintEl = document.getElementById('fabhint');
  var csrfEl = form.querySelector('input[name=csrf]');
  var CSRF = csrfEl ? csrfEl.value : '';
  var MIN_H = 240;
  var sid = 0, busy = false, loaded = false;

  function toBottom() { box.scrollTop = box.scrollHeight; }

  function bubble(role, streaming) {
    var d = document.createElement('div');
    d.className = 'msg ' + role + (streaming ? ' streaming' : '');
    var md = document.createElement('div');
    md.className = 'md';
    d.appendChild(md);
    box.appendChild(d);
    toBottom();
    return d;
  }

  function addSources(node, sources) {
    if (!sources || !sources.length) return;
    var s = document.createElement('div');
    s.className = 'src';
    s.textContent = '依据：' + sources.join('、');
    node.appendChild(s);
  }

  /* ---------- 思考过程 ---------- */
  function thinkBox(node) {
    var wrap = node.querySelector('.think');
    if (wrap) return wrap;
    wrap = document.createElement('div');
    wrap.className = 'think';
    wrap.innerHTML = '<span class="thinkline"><span class="tk">思考中…</span></span>' +
                     '<button type="button" class="thinkmore">查看</button>';
    wrap.querySelector('.thinkmore').addEventListener('click', function () { openThink(node); });
    node.insertBefore(wrap, node.firstChild);
    node.__think = node.__think || '';
    return wrap;
  }
  function thinkSay(node, text) {
    node.__think = text ? text : (node.__think || '');
    var wrap = thinkBox(node);
    var tk = wrap.querySelector('.tk');
    if (tk) tk.textContent = text || node.__think.slice(-400);
    var line = wrap.querySelector('.thinkline');
    if (line) line.scrollLeft = line.scrollWidth;   // 一直在往后滑
  }
  function openThink(node) {
    var dlg = document.getElementById('thinkDlg');
    var body = document.getElementById('thinkDlgBody');
    if (!dlg || !body || !dlg.showModal) return;
    var t = (node.__think || '').trim();
    body.textContent = t || '这次没有留下思考过程（模型没吐 reasoning，也没有工具步骤）。';
    dlg.showModal();
  }

  function loadHistory() {
    fetch('/api/qa/history', { credentials: 'same-origin' })
      .then(function (r) { return r.json(); })
      .then(function (j) {
        sid = j.sid;
        form.action = '/qa/' + sid + '/ask';
        loaded = true;
        if (j.messages && j.messages.length) {
          if (emptyEl) emptyEl.hidden = true;
          box.querySelectorAll('.msg').forEach(function (n) { n.remove(); });
          j.messages.forEach(function (m) {
            var d = bubble(m.role, false);
            d.querySelector('.md').innerHTML = m.html;      // 服务端渲染并清洗过
            addSources(d, m.sources);
            if (m.doc_name) {                       // 文件类附件在气泡下留个标记
              var dn = document.createElement('div');
              dn.className = 'src';
              dn.textContent = '📄 ' + m.doc_name;
              d.appendChild(dn);
            }
            if (m.think) { d.__think = m.think; thinkSay(d); }
          });
          var last = j.messages[j.messages.length - 1];
          if (last && last.role === 'user') {
            // 上一次问答断在半路（手机切网、锁屏）：历史里只剩一个没人回答的问题。
            // 在它下面补一张卡片说明 + 「重试这个问题」，不用重新打字。
            var nodes = box.querySelectorAll('.msg');
            var qtext = nodes.length ? (nodes[nodes.length - 1].querySelector('.md').textContent || '').trim() : '';
            var d2 = bubble('assistant', false);
            d2.classList.add('errored');
            d2.querySelector('.md').textContent = '这条问题上次没有生成回答（连接中断了）。';
            addRetry(d2, qtext, false);
          }
        }
        if (ctxEl) {
          if (j.summary) {
            ctxEl.hidden = false;
            ctxEl.textContent = '更早的对话已自动整理成摘要（保留 ' +
              (j.compress_over || 24) + ' 条以内对话，共 ' + j.total + ' 条）';
          } else { ctxEl.hidden = true; }
        }
        toBottom();
      })
      .catch(function () {
        if (emptyEl) emptyEl.hidden = true;
        var d = bubble('assistant', false);
        d.querySelector('.md').textContent = '⚠️ 无法加载会话，刷新页面再试';
      });
  }

  function open() {
    panel.hidden = false;
    applySavedHeight();
    fab.classList.add('open');          // 展开时圆按钮整体收起（收起用右上角 ×）
    document.body.classList.add('fabopen');
    if (!loaded) loadHistory();
    var ta = form.querySelector('textarea');
    if (ta) ta.focus();
  }
  function hide() {
    panel.hidden = true;
    fab.classList.remove('open');
    document.body.classList.remove('fabopen');
  }
  fab.addEventListener('click', function () {
    if (panel.hidden) open(); else hide();
  });
  closeBtn.addEventListener('click', hide);
  document.addEventListener('keydown', function (e) {
    if (e.key !== 'Escape' || panel.hidden) return;
    // 弹层（思考过程）自己会吃掉 Esc；别顺手把整个对话面板也关了
    var dlg = document.getElementById('thinkDlg');
    if (dlg && dlg.open) return;
    hide();
  });

  /* ---------- 输入框左上角那个抓手：上下拖 = 输入框长高；拖到头再拖就动面板 ----------
     - 输入框自己最高 TA_MAX；再往上拖的部分转给面板（手机上就是一拖变成贴底半屏）
     - 往下拖到底再拖，就把面板缩小（手机上腾出页面）；输入框最小高度记在 cmpH  */
  var TA_MAX = 240;
  var taManual = 0;
  try { taManual = parseInt(localStorage.getItem('cmpH') || '0', 10) || 0; } catch (e) { }
  function taMin() { return Math.max(28, taManual); }
  function setTaHeight(px) { if (taEl) taEl.style.height = px + 'px'; }
  /* 随内容长高（1 行起，最高约 1/3 屏；用户拖出来的最小高度优先） */
  function growTa() {
    if (!taEl) return;
    var cap = Math.min(TA_MAX, Math.round(window.innerHeight * (isPhone() ? 0.34 : 0.42)));
    taEl.style.height = 'auto';
    setTaHeight(Math.max(taMin(), Math.min(taEl.scrollHeight + 2, cap)));
  }
  function markSend() {
    if (sendBtn) sendBtn.classList.toggle('ready', !!(taEl && taEl.value.trim()));
  }
  if (taEl) {
    taEl.addEventListener('input', function () { growTa(); markSend(); });
    taEl.addEventListener('focus', growTa);
    growTa(); markSend();
  }

  /* ---------- 尺寸（面板） ---------- */
  function isPhone() { return window.matchMedia('(max-width:899px)').matches; }
  function applySavedHeight() {
    try {
      if (isPhone()) {
        var hm = parseInt(localStorage.getItem('fabHm') || '', 10);
        if (hm >= 260) { panel.classList.add('sheet'); panel.style.height = hm + 'px'; }
        else { panel.classList.remove('sheet'); panel.style.height = ''; }
      } else {
        panel.classList.remove('sheet');
        var hd = parseInt(localStorage.getItem('fabH') || '', 10);
        panel.style.height = (hd >= MIN_H) ? hd + 'px' : '';
      }
    } catch (e) { /* 隐私模式下忽略 */ }
  }
  function bindCmpDrag(el) {
    if (!el || !taEl) return;
    var drag = null;
    el.addEventListener('pointerdown', function (e) {
      drag = { y: e.clientY, h: taEl.getBoundingClientRect().height || taMin(),
               ph: panel.getBoundingClientRect().height, phone: isPhone() };
      el.classList.add('dragging');
      if (el.setPointerCapture) el.setPointerCapture(e.pointerId);
      e.preventDefault();
    });
    el.addEventListener('pointermove', function (e) {
      if (!drag) return;
      var want = Math.round(drag.h + (drag.y - e.clientY));       // 往上拖 = 变高
      var h = Math.max(28, Math.min(want, TA_MAX));
      setTaHeight(h);
      var over = want - h;                                        // 超出输入框上限的部分给面板
      if (over !== 0) {
        if (drag.phone && !panel.classList.contains('sheet')) panel.classList.add('sheet');
        var vmax = drag.phone ? window.innerHeight : Math.round(window.innerHeight * 0.92);
        panel.style.height = Math.max(320, Math.min(drag.ph + over, vmax)) + 'px';
      }
    });
    function endDrag() {
      if (!drag) return;
      var phone = drag.phone;
      drag = null;
      el.classList.remove('dragging');
      try {
        localStorage.setItem('cmpH', String(parseInt(taEl.style.height, 10) || 0));
        var ph = parseInt(panel.style.height, 10) || 0;
        if (phone) {
          if (ph >= window.innerHeight - 6) {
            panel.classList.remove('sheet'); panel.style.height = '';
            localStorage.setItem('fabHm', '0');
          } else if (ph) { localStorage.setItem('fabHm', String(ph)); }
        } else if (ph) { localStorage.setItem('fabH', String(ph)); }
      } catch (e) { /* 忽略 */ }
    }
    el.addEventListener('pointerup', endDrag);
    el.addEventListener('pointercancel', endDrag);
  }
  bindCmpDrag(grip);
  window.addEventListener('resize', function () { applySavedHeight(); growTa(); });
  applySavedHeight();

  /* ---------- ＋ 展开附件面板：照片 / 拍摄 / 文件 ---------- */
  function sheetOpen(on) {
    if (!sheetEl) return;
    sheetEl.hidden = !on;
    if (plusBtn) plusBtn.setAttribute('aria-expanded', on ? 'true' : 'false');
    if (on) toBottom();
  }
  function pickFile(el) {
    if (!el) return;
    el.value = '';
    el.click();
  }
  if (plusBtn) plusBtn.addEventListener('click', function () { sheetOpen(sheetEl.hidden); });
  [['tilePhoto', fileEl], ['tileCam', camEl], ['tileFile', docEl]].forEach(function (pair) {
    var tile = document.getElementById(pair[0]);
    if (tile) tile.addEventListener('click', function () { pickFile(pair[1]); });
  });

  /* ---------- 附件预览：照片缩略图 / 文件卡片，右上角 × 删除，点一下看大图 ---------- */
  var PIC_EXT = { jpg: '图片', jpeg: '图片', png: '图片', webp: '图片', heic: '图片', heif: '图片', gif: '图片' };
  var DOC_ICON = { pdf: '📕', txt: '📄', md: '📝', csv: '📊', json: '🧾', log: '🗒' };
  function attExt(name) {
    var m = String(name || '').toLowerCase().match(/\.([a-z0-9]+)$/);
    return m ? m[1] : '';
  }
  function attTypeName(f) {
    var e = attExt(f.name);
    if (PIC_EXT[e]) return PIC_EXT[e];
    var names = { pdf: 'PDF 文档', txt: '文本文件', md: 'Markdown', csv: '表格(CSV)',
                  json: 'JSON', log: '日志' };
    return names[e] || (f.type || '文件');
  }
  function attIcon(f) {
    return DOC_ICON[attExt(f.name)] || '📎';
  }
  function attSize(n) {
    if (n > 1024 * 1024) return (n / 1048576).toFixed(1) + ' MB';
    return Math.max(1, Math.round(n / 1024)) + ' KB';
  }
  function openPic(file) {
    var dlg = document.getElementById('picDlg'), img = document.getElementById('picDlgImg');
    if (!dlg || !img || !dlg.showModal) return;
    img.src = URL.createObjectURL(file);      // 本地预览，不上传
    dlg.showModal();
  }
  function attItems() {
    var out = [];
    if (fileEl && fileEl.files && fileEl.files[0]) out.push({ input: fileEl, f: fileEl.files[0] });
    if (camEl && camEl.files && camEl.files[0]) out.push({ input: camEl, f: camEl.files[0] });
    if (docEl && docEl.files && docEl.files[0]) out.push({ input: docEl, f: docEl.files[0], doc: true });
    return out;
  }
  function renderAtts() {
    if (!attsEl) return;
    var items = attItems();
    attsEl.innerHTML = '';
    if (!items.length) { attsEl.hidden = true; return; }
    attsEl.hidden = false;
    items.forEach(function (it) {
      var box = document.createElement('div');
      box.className = 'cmp-att ' + (it.doc ? 'doc' : 'pic');
      if (it.doc) {
        var ic = document.createElement('span'); ic.className = 'ic'; ic.textContent = attIcon(it.f);
        var txt = document.createElement('span');
        var nm = document.createElement('span'); nm.className = 'nm'; nm.textContent = it.f.name;
        var ty = document.createElement('span'); ty.className = 'ty';
        ty.textContent = attTypeName(it.f) + ' · ' + attSize(it.f.size);
        txt.appendChild(nm); txt.appendChild(ty);
        box.appendChild(ic); box.appendChild(txt);
        box.title = it.f.name;
      } else {
        var img = document.createElement('img');
        img.src = URL.createObjectURL(it.f);
        img.alt = it.f.name;
        box.appendChild(img);
        box.title = '点一下看大图 · ' + attSize(it.f.size);
        box.addEventListener('click', function () { openPic(it.f); });
      }
      var del = document.createElement('button');
      del.type = 'button'; del.className = 'del'; del.textContent = '×';
      del.title = '移除这个附件';
      del.setAttribute('aria-label', '移除附件');
      del.addEventListener('click', function (e) {
        e.stopPropagation();
        it.input.value = '';                 // 清掉这个入口选中的文件
        renderAtts();
        sheetOpen(false);
      });
      box.appendChild(del);
      attsEl.appendChild(box);
    });
  }

  /* ---------- 三个附件入口（照片/拍摄/文件）：一次只留一个 ---------- */
  function fileChanged(src) {
    // 照片与拍摄是同一种附件（二选一，后端只收一张图）；文件与照片可以同时附
    if (src === fileEl && camEl) camEl.value = '';
    if (src === camEl && fileEl) fileEl.value = '';
    renderAtts();
    sheetOpen(false);
  }
  [fileEl, camEl, docEl].forEach(function (el) {
    if (el) el.addEventListener('change', function () { fileChanged(el); });
  });

  /* ---------- 上方两个功能键 ---------- */
  function say(text) {
    if (!hintEl) return;
    hintEl.textContent = text || '';
    if (text) setTimeout(function () { if (hintEl.textContent === text) hintEl.textContent = ''; }, 8000);
  }
  function toolsBusy(on) {
    [clearBtn, archBtn, cardBtn].forEach(function (b) { if (b) b.disabled = !!on; });
  }
  function postForm(url) {
    var fd = new FormData();
    fd.append('csrf', CSRF);
    return fetch(url, { method: 'POST', body: fd, credentials: 'same-origin' })
      .then(function (r) { return r.json(); });
  }
  function wipeChat() {
    box.querySelectorAll('.msg').forEach(function (n) { n.remove(); });
    if (emptyEl) emptyEl.hidden = false;
    if (ctxEl) ctxEl.hidden = true;
    if (window.__fabReloadPending) window.__fabReloadPending();
  }
  if (clearBtn) clearBtn.addEventListener('click', function () {
    if (busy) return;
    if (!window.confirm('清空这段对话？对话会被收起来（不删除），并开一段新的。')) return;
    toolsBusy(true);
    postForm('/qa/clear').then(function (j) {
      say(j && j.msg ? j.msg : '已清空');
      wipeChat();
      sid = 0; loaded = false; loadHistory();
    }).catch(function () { say('没连上，稍后再试'); })
      .finally(function () { toolsBusy(false); });
  });
  if (archBtn) archBtn.addEventListener('click', function () {
    if (busy) return;
    if (!window.confirm('把这轮对话总结成一篇知识库笔记（存在个人层、标注「待核对」），然后开新对话？')) return;
    toolsBusy(true);
    say('正在总结…模型慢的时候要等一会儿');
    postForm('/qa/archive').then(function (j) {
      say(j && j.msg ? j.msg : '完成');
      if (j && j.ok) { wipeChat(); sid = 0; loaded = false; loadHistory(); }
    }).catch(function () { say('没连上，稍后再试'); })
      .finally(function () { toolsBusy(false); });
  });

  if (cardBtn) cardBtn.addEventListener('click', function () {
    if (busy) return;
    if (!window.confirm('把这段对话整理成一张病症卡片？整理完先给你看，点「确认」才会写入疾病档案。')) return;
    toolsBusy(true);
    say('正在整理病症卡片…模型慢的时候要等一会儿');
    postForm('/qa/condition_card').then(function (j) {
      say(j && j.msg ? j.msg : '完成');
      // 生成的是「待确认」提案：刷新下面的卡片，用户点确认才落库
      if (j && j.ok && window.__fabReloadPending) window.__fabReloadPending();
    }).catch(function () { say('没连上，稍后再试'); })
      .finally(function () { toolsBusy(false); });
  });

  /* 断开/失败后的一键重试：把问题放回输入框。带了照片或文件的重发不了（浏览器的文件框
     不允许脚本回填），就只提醒重新附一次，别让人以为点一下就能无声重发。 */
  function addRetry(node, q, hadAtts) {
    if (!node || !q) return;
    var old = node.querySelector('.retrybtn');
    if (old) old.remove();
    var b = document.createElement('button');
    b.type = 'button';
    b.className = 'btn ghost small retrybtn';
    b.textContent = hadAtts ? '重新附上再发一次' : '重试这个问题';
    b.addEventListener('click', function () {
      var ta = taEl || form.querySelector('textarea');
      if (ta) { ta.value = q; growTa(); markSend(); ta.focus(); }
      if (hadAtts) return;                     // 附件回填不了，让用户自己再选一次
      if (form.requestSubmit) form.requestSubmit();
      else form.dispatchEvent(new Event('submit', { cancelable: true }));
    });
    node.appendChild(b);
  }

  /* ---------- 提问 ---------- */
  form.addEventListener('submit', function (e) {
    e.preventDefault();
    if (busy) return;
    var ta = taEl || form.querySelector('textarea');
    var q = (ta.value || '').trim();
    if (!q || !sid) return;
    var body = new FormData(form);          // 先序列化（含图片与 csrf）再清空输入
    var hadAtts = attItems().length > 0;    // 断开重试时要知道这次有没有带图/文件
    ta.value = '';
    sheetOpen(false);
    growTa();
    markSend();
    [fileEl, camEl, docEl].forEach(function (el) { if (el) el.value = ''; });
    renderAtts();
    var mine = bubble('user', false);
    mine.querySelector('.md').textContent = q;
    if (emptyEl) emptyEl.hidden = true;
    var cur = bubble('assistant', true);
    var mdEl = cur.querySelector('.md');
    mdEl.textContent = '思考中…';
    thinkSay(cur, '正在思考…');
    var text = '', mid = 0, started = false;
    busy = true;
    fetch(form.action, { method: 'POST', body: body, credentials: 'same-origin' })
      .then(function (r) {
        if (!r.ok) throw new Error('请求失败 ' + r.status);
        var reader = r.body.getReader(), dec = new TextDecoder(), buf = '';
        function pump() {
          return reader.read().then(function (res) {
            if (res.done) return;
            buf += dec.decode(res.value, { stream: true });
            var parts = buf.split('\n\n'); buf = parts.pop();
            parts.forEach(function (p) {
              var line = p.replace(/^data:\s*/, ''); if (!line) return;
              var o; try { o = JSON.parse(line); } catch (err) { return; }
              if (o.s) { thinkSay(cur, o.s); }                    // 服务端真实步骤
              if (o.th) { thinkSay(cur, (cur.__think || '') + o.th); }   // 模型的思考
              if (o.t) {
                if (!started) {                                  // 正文开始：过程行收起来
                  started = true;
                  var w = cur.querySelector('.think');
                  if (w) w.classList.add('done');
                  var tk = cur.querySelector('.think .tk');
                  if (tk && cur.__think) tk.textContent = cur.__think.slice(-400);
                  mdEl.textContent = '';
                }
                text += o.t; mdEl.textContent = text; toBottom();
              }
              if (o.e) { mdEl.textContent = text + '\n\n⚠️ ' + o.e; cur.classList.add('errored'); }
              if (o.done && o.mid) mid = o.mid;
            });
            return pump();
          });
        }
        return pump();
      })
      .then(function () {
        if (!mid) { cur.classList.remove('streaming'); return; }
        // 换成服务端渲染的 Markdown（清洗过再插入）
        return fetch('/qa/msg/' + mid, { credentials: 'same-origin' })
          .then(function (r) { return r.json(); })
          .then(function (j) {
            cur.classList.remove('streaming');
            mdEl.innerHTML = j.html || '';
            addSources(cur, j.sources);
            toBottom();
            // 助手可能要登记东西：刷新下方的待确认卡片（用户点确认才写入）
            if (window.__fabReloadPending) window.__fabReloadPending();
          });
      })
      .catch(function (err) {
        cur.classList.remove('streaming');
        var why = (err && err.name === 'AbortError') ? '这次请求被取消了'
          : ('网络中断了' + (err && err.message ? '（' + err.message + '）' : ''));
        // 关键：把问题放回输入框 —— 手机切网/锁屏很容易撞上这个，别让人重新打一遍
        if (ta && !(ta.value || '').trim()) { ta.value = q; growTa(); markSend(); }
        mdEl.textContent = '⚠️ ' + why + '。\n\n问题已经放回输入框，点发送就能重试。'
          + (text ? '\n\n已经生成的部分：\n' + text : '');
        cur.classList.add('errored');
        addRetry(cur, q, hadAtts);
      })
      .finally(function () { busy = false; });
  });

  // 面板里的 Enter 发送
  form.addEventListener('keydown', function (e) {
    var ta = form.querySelector('textarea');
    if (e.key === 'Enter' && !e.shiftKey && document.activeElement === ta) {
      e.preventDefault(); form.requestSubmit();
    }
  });
})();

/* ---- 点阶段图例 → 弹出该阶段的说明（内容取自页面里的 <template>，无网络请求） ----
   注意：每次点击都重新取节点 —— 环卡片会被打勾接口整体替换，缓存的引用会变成游离节点。 */
(function () {
  function el(id) { return document.getElementById(id); }
  document.addEventListener('click', function (e) {
    var t = e.target;
    if (!t || !t.closest) return;
    var chip = t.closest('.lgchip');
    if (chip) {
      var dlg = el('phaseDlg'), body = el('phaseDlgBody'), title = el('phaseDlgTitle');
      var tpl = document.querySelector('template[data-phase-tpl="' + chip.getAttribute('data-phase') + '"]');
      if (!dlg || !body || !title || !tpl || !dlg.showModal) return;
      body.innerHTML = '';
      body.appendChild(tpl.content.cloneNode(true));
      title.textContent = (chip.textContent || '').trim();
      dlg.showModal();
      return;
    }
    if (t.closest('[data-dlg-close]')) { var d = el('phaseDlg'); if (d) d.close(); return; }
    if (t.tagName === 'DIALOG' && t.classList.contains('phasedlg')) t.close();   // 点遮罩关闭
  });
})();

/* ---- 待确认的写入提案：助手登记的东西要用户点「确认」才落库 ----
   提案由助手的 submit_record 工具产生（pending），这里给一个不用进管理页的确认入口。 */
(function () {
  var box = document.getElementById('fabpend');
  var form = document.getElementById('fabform');
  if (!box || !form) return;
  var csrfEl = form.querySelector('input[name=csrf]');
  var csrf = csrfEl ? csrfEl.value : '';

  function esc(s) {
    var d = document.createElement('div');
    d.textContent = s === null || s === undefined ? '' : String(s);
    return d.innerHTML;
  }
  function flash(t) {
    var d = document.createElement('div');
    d.className = 'fpend-msg';
    d.textContent = t;
    box.insertBefore(d, box.firstChild);
    setTimeout(function () { if (d.parentNode) d.parentNode.removeChild(d); }, 2600);
  }
  var STATUS_CN = { active: '进行中', monitoring: '观察中', resolved: '已好' };
  function render(items) {
    if (!items || !items.length) { box.hidden = true; box.innerHTML = ''; return; }
    var html = '<div class="fpend-t">助手想记下 ' + items.length + ' 条，点确认才会写入</div>';
    items.forEach(function (it) {
      // 填好的逐条列出；空字段收成一行提示，别让六行空标签占满卡片
      var filled = [], missing = [];
      Object.keys(it.summary || {}).forEach(function (k) {
        var v = it.summary[k];
        if (v === null || v === undefined || String(v).trim() === '') { missing.push(k); return; }
        if (k === '状态') v = STATUS_CN[v] || v;      // 档案状态用中文，别把 active/monitoring 直接给用户看
        filled.push('<div><span class="fp-k">' + esc(k) + '</span> ' + esc(v) + '</div>');
      });
      var kv = filled.join('') +
        (missing.length ? '<div class="fp-miss">还没填：' + esc(missing.join('、'))
                          + '（可以先确认，之后在疾病档案里补）</div>' : '');
      html += '<div class="fpend" data-id="' + it.id + '">' +
              '<div class="fp-kind">' + esc(it.kind) + '</div>' + kv +
              (it.rationale ? '<div class="fp-r">' + esc(it.rationale) + '</div>' : '') +
              '<div class="fp-b"><button type="button" class="btn small" data-decide="approve">确认</button>' +
              '<button type="button" class="btn ghost small" data-decide="reject">取消</button></div></div>';
    });
    box.innerHTML = html;
    box.hidden = false;
  }
  function load() {
    fetch('/qa/pending', { credentials: 'same-origin' })
      .then(function (r) { return r.json(); })
      .then(function (j) { render(j && j.items); })
      .catch(function () {});
  }
  box.addEventListener('click', function (e) {
    var btn = e.target.closest ? e.target.closest('[data-decide]') : null;
    if (!btn) return;
    var card = btn.closest('.fpend');
    var fd = new FormData();
    fd.append('csrf', csrf);
    fd.append('action', btn.getAttribute('data-decide'));
    btn.disabled = true;
    fetch('/qa/proposal/' + card.getAttribute('data-id') + '/decide',
          { method: 'POST', body: fd, credentials: 'same-origin' })
      .then(function (r) { return r.json(); })
      .then(function (j) {
        load();
        if (j && j.msg) flash(j.msg);
        if (!j || !j.ok) btn.disabled = false;
      })
      .catch(function () { btn.disabled = false; flash('没连上，稍后再试'); });
  });
  load();
  window.__fabReloadPending = load;
})();

/* ---- 「记录月经」：滚到日历并打开编辑模式（没有编辑权限时按链接正常跳转） ----
   同样改为在 document 上委托，按钮被换掉后依然可用。 */
(function () {
  document.addEventListener('click', function (e) {
    var t = e.target;
    if (!t || !t.closest) return;
    if (!t.closest('[data-record-menstruation]')) return;
    var toggle = document.getElementById('calEditToggle');
    if (!toggle) return;
    e.preventDefault();
    if ((toggle.textContent || '').indexOf('完成编辑') < 0) toggle.click();
    var wrap = document.getElementById('calwrap');
    if (wrap && wrap.scrollIntoView) wrap.scrollIntoView({ behavior: 'smooth', block: 'start' });
  });
})();


/* ---- 配色与深浅色：记在这台设备上；深浅色可以跟随系统 ---- */
(function () {
  var root = document.documentElement;
  var mq = window.matchMedia ? window.matchMedia('(prefers-color-scheme: dark)') : null;
  var LABEL = { auto: '跟随系统', light: '浅色', dark: '深色' };
  function saved(k, d) { try { return localStorage.getItem(k) || d; } catch (e) { return d; } }
  function store(k, v) { try { localStorage.setItem(k, v); } catch (e) { /* 忽略 */ } }

  function applyTheme(pref) {
    var dark = pref === 'dark' || (pref === 'auto' && !!mq && mq.matches);
    root.setAttribute('data-theme', dark ? 'dark' : 'light');
    root.setAttribute('data-theme-pref', pref);
    var b = document.getElementById('themeBtn');
    if (b) {
      b.textContent = pref === 'auto' ? '🌗' : (pref === 'dark' ? '🌙' : '☀️');
      b.title = '深浅色：' + LABEL[pref] + '（点一下切换）';
    }
  }
  function applyAccent(a) {
    root.setAttribute('data-accent', a);
    Array.prototype.forEach.call(document.querySelectorAll('.sw'), function (s2) {
      s2.setAttribute('aria-pressed', s2.getAttribute('data-accent') === a ? 'true' : 'false');
    });
  }
  applyAccent(saved('bg-accent', 'rose'));
  applyTheme(saved('bg-theme', 'auto'));
  if (mq) {
    var onSys = function () { if (saved('bg-theme', 'auto') === 'auto') applyTheme('auto'); };
    if (mq.addEventListener) mq.addEventListener('change', onSys); else if (mq.addListener) mq.addListener(onSys);
  }
  var tb = document.getElementById('themeBtn');
  if (tb) tb.addEventListener('click', function () {
    var order = ['auto', 'light', 'dark'];
    var next = order[(order.indexOf(saved('bg-theme', 'auto')) + 1) % order.length];
    store('bg-theme', next);
    applyTheme(next);
  });
  Array.prototype.forEach.call(document.querySelectorAll('.sw'), function (s2) {
    s2.addEventListener('click', function () {
      var a = s2.getAttribute('data-accent');
      store('bg-accent', a);
      applyAccent(a);
      var d = s2.closest('details');
      if (d) d.open = false;
    });
  });
})();

/* ---- 表格翻页（症状与日志）：超过 data-page-size 条就分页；片段重渲染后再调一次 ---- */
(function () {
  function applyPager(root) {
    Array.prototype.forEach.call((root || document).querySelectorAll('.symptable[data-page-size]'),
      function (box) {
        var size = Math.max(1, parseInt(box.getAttribute('data-page-size'), 10) || 10);
        var rows = Array.prototype.slice.call(box.querySelectorAll('tbody tr'));
        var pager = box.querySelector('.pager');
        if (!pager) return;
        var pages = Math.max(1, Math.ceil(rows.length / size));
        var cur = Math.min(Math.max(1, parseInt(box.dataset.page || '1', 10)), pages);
        box.dataset.page = String(cur);
        rows.forEach(function (tr, i) {
          if (Math.floor(i / size) === cur - 1) tr.removeAttribute('hidden');
          else tr.setAttribute('hidden', '');
        });
        pager.hidden = pages <= 1;
        var info = box.querySelector('[data-pg-info]');
        if (info) info.textContent = cur + ' / ' + pages + '（共 ' + rows.length + ' 条）';
        Array.prototype.forEach.call(pager.querySelectorAll('[data-pg]'), function (bn) {
          var k = bn.getAttribute('data-pg');
          bn.disabled = (k === 'prev' && cur <= 1) || (k === 'next' && cur >= pages);
        });
      });
  }
  document.addEventListener('click', function (e) {
    var btn = e.target.closest && e.target.closest('[data-pg]');
    if (!btn) return;
    var box = btn.closest('.symptable');
    if (!box) return;
    var d = btn.getAttribute('data-pg') === 'next' ? 1 : -1;
    box.dataset.page = String((parseInt(box.dataset.page || '1', 10) || 1) + d);
    applyPager(document);
  });
  window.__bgPager = applyPager;
  applyPager(document);
})();

/* ---- 临近经期：打开页面问一句「今天大姨妈来了吗」（本机每天最多问一次） ---- */
(function () {
  var dlg = document.getElementById('periodDlg');
  if (!dlg || !dlg.showModal) return;
  var today = dlg.getAttribute('data-today') || new Date().toISOString().slice(0, 10);
  var KEY = 'periodAskOn';
  var done = '';
  try { done = localStorage.getItem(KEY) || ''; } catch (e) { /* 隐私模式 */ }
  if (done === today) return;
  function remember() { try { localStorage.setItem(KEY, today); } catch (e) { /* 忽略 */ } }
  var no = document.getElementById('pdNo');
  if (no) no.addEventListener('click', function () { remember(); dlg.close(); });
  dlg.addEventListener('cancel', remember);      // 按 Esc 也算今天问过了
  setTimeout(function () { if (!dlg.open) dlg.showModal(); }, 500);
})();
