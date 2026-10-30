/* 简笔画涂鸦 WebUI */
(function () {
  'use strict';

  var BRIDGE = window.AstrBotPluginView || window.AstrBotPluginPage || null;
  var PLUGIN_NAME = 'astrbot_plugin_svg_doodle';
  // bridge 会把 endpoint 拼成 /api/v1/plugins/extensions/<plugin>/<endpoint>
  // 所以这里只保留插件名，不再带前导路径
  var API_PREFIX = PLUGIN_NAME + '/';
  var API_BASE = '/api/v1/plugins/extensions/' + encodeURIComponent(PLUGIN_NAME) + '/';
  var LAST_ERR = '';

  // bridge 可能剥掉外层 {status,data}，也可能原样返回。统一成 {ok, body}
  // - 剥过：r 本身就是 body（无 status 字段）
  // - 没剥：r = {status:'ok', data: body}
  function unwrap(r) {
    if (!r || typeof r !== 'object') return { ok: false, body: {} };
    if (typeof r.status === 'string') {
      if (r.status === 'error') return { ok: false, body: r, msg: r.message || '' };
      return { ok: r.status === 'ok', body: (r.data !== undefined && r.data !== null) ? r.data : r };
    }
    // 没有 status → bridge 已剥壳，r 就是 body
    return { ok: true, body: r };
  }

  // ---------- 取图：走 bridge.apiGet 拿 base64 data URI（bridge 没有 assetUrl）----------
  var _imgCache = Object.create(null);
  async function fetchImageDataUri(file, width) {
    if (!file) return '';
    var key = width ? file + '@' + width : file;
    if (_imgCache[key]) return _imgCache[key];
    try {
      var params = { file: file };
      if (width) params.w = width;
      var u = unwrap(await apiGet('page/preview', params));
      if (u.ok && u.body && u.body.data_uri) {
        _imgCache[key] = u.body.data_uri;
        return u.body.data_uri;
      }
    } catch (e) { LAST_ERR = 'preview: ' + (e && e.message || e); }
    if (LAST_ERR && window.console) console.warn('[doodle]', LAST_ERR);
    return '';
  }

  // ---------- 请求封装：优先 bridge，退回手写 fetch ----------
  async function apiGet(path, params) {
    if (BRIDGE && typeof BRIDGE.apiGet === 'function') {
      try {
        var out = await BRIDGE.apiGet(path, params || {});
        if (window.console) console.log('[doodle] apiGet(' + path + ') =>', out);
        return out;
      } catch (e) {
        LAST_ERR = 'bridge.apiGet(' + path + '): ' + (e && e.message || e);
        if (window.console) console.error('[doodle] apiGet failed:', e);
      }
    }
    var qs = '';
    if (params) {
      var parts = [];
      for (var k in params) {
        if (Object.prototype.hasOwnProperty.call(params, k) && params[k] != null) {
          parts.push(encodeURIComponent(k) + '=' + encodeURIComponent(params[k]));
        }
      }
      if (parts.length) qs = '?' + parts.join('&');
    }
    return rawFetch(path + qs, { method: 'GET' });
  }

  async function apiPost(path, body) {
    if (BRIDGE && typeof BRIDGE.apiPost === 'function') {
      try { return await BRIDGE.apiPost(path, body || {}); }
      catch (e) { LAST_ERR = 'bridge.apiPost: ' + (e && e.message || e); }
    }
    return rawFetch(path, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body || {}),
    });
  }

  // 兜底：只有 bridge 完全不可用时才会走到这里。iframe 里拿不到 JWT，
  // 优先用 localStorage 的 token（部分版本 AstrBot 允许）再试一次。
  async function rawFetch(path, opts) {
    var headers = (opts && opts.headers) || {};
    var url = API_BASE + String(path || '').replace(/^\/+/, '');
    try {
      var boot = window.__ASTRBOT_BOOT__ || {};
      var tok = (boot && (boot.token || boot.jwt)) ||
                window.localStorage.getItem('astrbot_token') ||
                window.localStorage.getItem('token') || '';
      if (tok) headers['Authorization'] = 'Bearer ' + tok;
    } catch (e) { /* ignore */ }
    var res = await fetch(url, Object.assign({}, opts, { headers: headers }));
    if (!res.ok) {
      throw new Error('HTTP ' + res.status + '（bridge 不可用：' + (LAST_ERR || 'unknown') + '）');
    }
    return await res.json();
  }

  // ---------- DOM ----------
  var $ = function (sel) { return document.querySelector(sel); };
  var $$ = function (sel) { return Array.prototype.slice.call(document.querySelectorAll(sel)); };

  // ---------- 标签切换 ----------
  $$('.tab').forEach(function (btn) {
    btn.addEventListener('click', function () {
      var name = btn.getAttribute('data-tab');
      $$('.tab').forEach(function (b) { b.classList.toggle('active', b === btn); });
      $$('.panel').forEach(function (p) {
        p.classList.toggle('active', p.id === 'panel-' + name);
      });
      if (name === 'gallery') loadGallery();
      if (name === 'config' && !state.schema) loadConfig();
    });
  });

  var state = { models: [], schema: null, values: null, currentCat: '', status: null };

  // ---------- 状态 ----------
  function setPill(sel, text, cls) {
    var el = $(sel);
    if (!el) return;
    el.className = 'pill ' + (cls || '');
    el.querySelector('span').textContent = text;
  }

  async function loadStatus() {
    try {
      var r = await apiGet('page/status');
      var d = unwrap(r).body || {};
      state.status = d;
      setPill('#pill-provider',
        d.provider_available ? ('模型就绪') : ('模型缺失'),
        d.provider_available ? 'on' : 'off');
      setPill('#pill-gallery', (d.gallery_count || 0) + ' 张', 'on');
      setPill('#pill-canvas', (d.canvas_size || 768) + 'px', 'on');
    } catch (e) {
      setPill('#pill-provider', '未连接', 'off');
      setPill('#pill-gallery', '—', '');
      setPill('#pill-canvas', '—', '');
    }
  }

  // ---------- 测试画图 ----------
  var chipWrap = $('#chips');
  if (chipWrap) {
    chipWrap.addEventListener('click', function (e) {
      var chip = e.target.closest('.chip');
      if (!chip) return;
      $('#test-subject').value = chip.getAttribute('data-v') || '';
    });
  }

  var btnTest = $('#btn-test');
  if (btnTest) {
    btnTest.addEventListener('click', async function () {
      var subject = ($('#test-subject').value || '').trim();
      var hint = $('#test-hint');
      if (!subject) {
        hint.textContent = '先写要画什么';
        hint.className = 'hint err';
        return;
      }
      btnTest.disabled = true;
      hint.innerHTML = '<span class="spin"></span>画着呢…（通常 10~20 秒）';
      hint.className = 'hint';
      var stage = $('#preview-stage');
      stage.innerHTML = '<div class="empty-state"><div class="spin"></div><p>生成中</p></div>';
      try {
        var u = unwrap(await apiPost('page/test', { subject: subject }));
        if (!u.ok) throw new Error(u.msg || '生成失败');
        var d = u.body || {};
        var imgUrl = await fetchImageDataUri(d.file, 768);
        stage.innerHTML = '<img src="' + imgUrl + '" alt="预览">';
        var code = $('#svg-code');
        if (code) { code.textContent = d.svg || ''; }
        hint.textContent = '完成 · ' + d.elapsed + 's · ' + (d.svg || '').length + ' 字节 SVG';
        hint.className = 'hint ok';
        loadStatus();
      } catch (e) {
        stage.innerHTML = '<div class="empty-state"><div class="empty-icon">×</div><p>生成失败</p></div>';
        hint.textContent = '失败：' + (e.message || e) + (LAST_ERR ? '｜' + LAST_ERR : '');
        hint.className = 'hint err';
      } finally {
        btnTest.disabled = false;
      }
    });
  }

  var btnSvg = $('#btn-svg-toggle');
  if (btnSvg) {
    btnSvg.addEventListener('click', function () {
      var code = $('#svg-code');
      if (!code) return;
      code.hidden = !code.hidden;
      btnSvg.textContent = code.hidden ? '看 SVG' : '看图片';
    });
  }

  // ---------- 配置 ----------
  var CATS = [
    { key: 'character', title: '角色', fields: ['character_name', 'character_desc'] },
    { key: 'style', title: '画风', fields: ['style_desc', 'body_constraint'] },
    { key: 'prompt', title: '提示词拼接', fields: ['scene_prefix', 'output_suffix'] },
    { key: 'model', title: '模型与画布', fields: ['provider_id', 'custom_model_name', 'custom_api_base', 'custom_api_key', 'custom_extra_body', 'canvas_size', 'max_retry'] },
    { key: 'texture', title: '质感', fields: ['texture_strength', 'edge_jitter', 'grain_strength', 'stroke_rough'] },
    { key: 'sign', title: '签名', fields: ['signature', 'show_date', 'font_path'] },
    { key: 'scope', title: '生效范围', fields: ['enabled_groups', 'allow_private'] },
    { key: 'storage', title: '存储', fields: ['gallery_dir', 'gallery_limit'] },
  ];

  async function loadConfig() {
    var fields = $('#config-fields');
    fields.innerHTML = '<div class="empty-state big"><div class="spin"></div><p>读取配置</p></div>';
    try {
      var u = unwrap(await apiGet('page/config'));
      if (!u.ok) throw new Error('服务端错误：' + (u.msg || '未知'));
      state.schema = u.body.schema || {};
      state.values = u.body.values || {};
      await loadModels();
      renderConfigNav();
      renderConfigFields(state.currentCat || 'character');
    } catch (e) {
      var detail = (e && e.message) || String(e);
      if (!detail || detail === '读取失败') {
        detail = '异常无消息';
      }
      detail += '｜LAST_ERR=' + (LAST_ERR || 'none');
      if (window.console) console.error('[doodle] loadConfig failed:', e, 'LAST_ERR=', LAST_ERR);
      fields.innerHTML = '<div class="empty-state big"><div class="empty-icon">×</div><p>读取失败：' + detail + '</p></div>';
    }
  }

  function renderConfigNav() {
    var nav = $('#config-nav');
    nav.innerHTML = '';
    CATS.forEach(function (cat) {
      var hasAny = cat.fields.some(function (f) { return state.schema[f]; });
      if (!hasAny) return;
      var b = document.createElement('button');
      b.textContent = cat.title;
      b.className = (state.currentCat || 'character') === cat.key ? 'active' : '';
      b.addEventListener('click', function () {
        state.currentCat = cat.key;
        $$('#config-nav button').forEach(function (x) { x.classList.remove('active'); });
        b.classList.add('active');
        renderConfigFields(cat.key);
      });
      nav.appendChild(b);
    });
  }


  // 自定义模型字段：只有 provider_id === '__custom__' 时才显示
  function refreshCustomVisibility() {
    var isCustom = state.values && state.values.provider_id === '__custom__';
    var nodes = document.querySelectorAll('[data-custom-field="1"]');
    for (var i = 0; i < nodes.length; i++) {
      nodes[i].style.display = isCustom ? '' : 'none';
    }
  }

  async function loadModels() {
    try {
      var u = unwrap(await apiGet('page/models'));
      state.models = (u.ok && u.body && u.body.items) || [];
    } catch (e) { state.models = []; }
  }

  function renderConfigFields(catKey) {
    var cat = CATS.filter(function (c) { return c.key === catKey; })[0] || CATS[0];
    var wrap = $('#config-fields');
    wrap.innerHTML = '';
    cat.fields.forEach(function (key) {
      var schema = state.schema[key];
      if (!schema) return;
      wrap.appendChild(buildField(key, schema, state.values[key]));
    });
  }

  function buildField(key, schema, value) {
    var box = document.createElement('div');
    box.className = 'field';
    if (key.indexOf('custom_') === 0) box.setAttribute('data-custom-field', '1');

    var labelRow = document.createElement('div');
    labelRow.className = 'field-label';
    var lbl = document.createElement('span');
    lbl.textContent = schema.description || key;
    var keyTag = document.createElement('span');
    keyTag.className = 'field-key';
    keyTag.textContent = key;
    labelRow.appendChild(lbl);
    labelRow.appendChild(keyTag);
    box.appendChild(labelRow);

    var type = schema.type || 'string';
    var el;

    if (type === 'bool') {
      var sw = document.createElement('label');
      sw.className = 'switch';
      el = document.createElement('input');
      el.type = 'checkbox';
      el.checked = !!value;
      var sl = document.createElement('span');
      sl.className = 'switch-slider';
      sw.appendChild(el);
      sw.appendChild(sl);
      box.appendChild(sw);
      el._key = key;
      el.addEventListener('change', function () {
        state.values[key] = el.checked;
      });
    } else if (schema.slider) {
      var wrapS = document.createElement('div');
      wrapS.className = 'field-slider';
      el = document.createElement('input');
      el.type = 'range';
      el.min = schema.slider.min;
      el.max = schema.slider.max;
      el.step = schema.slider.step;
      el.value = value != null ? value : schema.default;
      var val = document.createElement('span');
      val.className = 'val';
      val.textContent = el.value;
      el.addEventListener('input', function () {
        val.textContent = el.value;
        state.values[key] = parseFloat(el.value);
      });
      wrapS.appendChild(el);
      wrapS.appendChild(val);
      box.appendChild(wrapS);
    } else if (type === 'int' || type === 'float') {
      el = document.createElement('input');
      el.type = 'number';
      if (type === 'float') el.step = 'any';
      el.value = value != null ? value : schema.default;
      el.addEventListener('input', function () {
        state.values[key] = type === 'int' ? parseInt(el.value, 10) || 0 : parseFloat(el.value) || 0;
      });
      box.appendChild(el);
    } else if (type === 'list') {
      el = document.createElement('textarea');
      el.rows = 3;
      el.value = Array.isArray(value) ? value.join(', ') : (value == null ? '' : String(value));
      el.addEventListener('input', function () {
        var raw = el.value.trim();
        state.values[key] = raw ? raw.split(/[,\n]/).map(function (s) { return s.trim(); }).filter(Boolean) : [];
      });
      box.appendChild(el);
    } else if (type === 'text') {
      el = document.createElement('textarea');
      el.rows = 5;
      el.value = value != null ? value : (schema.default || '');
      el.addEventListener('input', function () {
        state.values[key] = el.value;
      });
      box.appendChild(el);
    } else if (type === 'string' && schema._special === 'select_provider') {
      el = document.createElement('select');
      el.className = 'select-provider';
      var cur = value != null ? String(value) : '';
      var opts = (state.models && state.models.length) ? state.models : [];
      if (!opts.some(function (o) { return o.id === cur; })) {
        opts = [{ id: cur, label: cur || '（空）' }].concat(opts);
      }
      opts.forEach(function (o) {
        var op = document.createElement('option');
        op.value = o.id;
        op.textContent = o.label || o.id;
        if (o.id === cur) op.selected = true;
        el.appendChild(op);
      });
      el.addEventListener('change', function () {
        state.values[key] = el.value;
        refreshCustomVisibility();
      });
      box.appendChild(el);
      // 自定义字段显隐
      box.setAttribute('data-custom-owned', '1');
      setTimeout(refreshCustomVisibility, 0);
    } else {
      el = document.createElement('input');
      el.type = 'text';
      el.value = value != null ? value : (schema.default || '');
      el.addEventListener('input', function () {
        state.values[key] = el.value;
      });
      box.appendChild(el);
    }

    if (schema.hint) {
      var d = document.createElement('div');
      d.className = 'field-desc' + (schema.obvious_hint ? ' obvious' : '');
      d.textContent = schema.hint;
      box.appendChild(d);
    }
    return box;
  }

  var btnSave = $('#btn-save');
  if (btnSave) {
    btnSave.addEventListener('click', async function () {
      var hint = $('#save-hint');
      btnSave.disabled = true;
      hint.innerHTML = '<span class="spin"></span>保存中';
      hint.className = 'hint';
      try {
        var r = await apiPost('page/config/save', { values: state.values });
        var u = unwrap(r);
        if (!u.ok) throw new Error(u.msg || '保存失败');
        hint.textContent = '已保存 ' + ((u.body && u.body.changed) || 0) + ' 项';
        hint.className = 'hint ok';
        loadStatus();
      } catch (e) {
        hint.textContent = '失败：' + (e.message || e) + (LAST_ERR ? '｜' + LAST_ERR : '');
        hint.className = 'hint err';
      } finally {
        btnSave.disabled = false;
      }
    });
  }

  var btnReset = $('#btn-reset');
  if (btnReset) {
    btnReset.addEventListener('click', function () { loadConfig(); });
  }

  // ---------- 画册 ----------
  async function loadGallery() {
    var grid = $('#gallery-grid');
    var empty = $('#gallery-empty');
    grid.innerHTML = '<div class="empty-state big"><div class="spin"></div><p>读取画册</p></div>';
    try {
      var u = unwrap(await apiGet('page/gallery'));
      if (!u.ok) throw new Error('服务端错误：' + (u.msg || '未知'));
      var items = (u.body && u.body.items) || [];
      grid.innerHTML = '';
      if (!items.length) { empty.style.display = 'flex'; return; }
      empty.style.display = 'none';
      var cards = [];
      items.forEach(function (it) {
        var card = document.createElement('div');
        card.className = 'g-item';
        var img = document.createElement('img');
        img.alt = it.subject || '';
        img.setAttribute('data-file', it.file || '');
        var meta = document.createElement('div');
        meta.className = 'g-meta';
        var s = document.createElement('div');
        s.className = 'g-subject';
        s.textContent = it.subject || '（无题）';
        var t = document.createElement('div');
        t.className = 'g-time';
        var d = new Date(it.time || 0);
        t.textContent = d.toLocaleString('zh-CN', { hour12: false });
        meta.appendChild(s);
        meta.appendChild(t);
        card.appendChild(img);
        card.appendChild(meta);
        grid.appendChild(card);
        cards.push({ img: img, file: it.file || '' });
      });

      // 串行加载缩略图（360px JPEG），避免一次并发拉十几张大图
      (async function () {
        for (var i = 0; i < cards.length; i++) {
          var c = cards[i];
          if (!c.file) continue;
          try {
            var uri = await fetchImageDataUri(c.file, 360);
            if (uri) c.img.src = uri;
          } catch (e) { /* ignore */ }
        }
      })();
    } catch (e) {
      grid.innerHTML = '<div class="empty-state big"><div class="empty-icon">×</div><p>读取失败：' + (e.message || e) + '</p></div>';
    }
  }

  // ---------- 启动 ----------
  async function init() {
    if (BRIDGE && typeof BRIDGE.ready === 'function') {
      try { await BRIDGE.ready(); } catch (e) { /* ignore */ }
    }
    await loadStatus();
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
})();
