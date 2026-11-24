/* 工作流配置页 —— 全局参数 / 提示词 两个 tab（画布已移除，流程拓扑由服务端管理） */
(function () {
  const $ = id => document.getElementById(id);

  function esc(s) { return (s == null ? '' : String(s)).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;'); }
  function api(route, method) {
    return fetch(route, { method: method || 'GET' }).then(r => {
      if (!r.ok) throw new Error('HTTP ' + r.status);
      return r.json();
    });
  }

  let dirty = false;
  let view = 'globals';        // globals | prompts
  let currentKey = 'ext_huji'; // 提示词 tab 当前项

  // ---------------- 字段定义 ----------------
  // 全局参数 tab：人设 + 业务阈值（一次全部渲染）
  const GLOBAL_FIELDS = [
    { path: ['system', 'agent_system_prompt'], label: '客服人设（agent system prompt）', type: 'textarea', height: 240,
      sub: '面向用户的所有回复都基于这个 system prompt 生成' },
    { path: ['business', 'male_age_threshold'], label: '男性首次参保年龄门槛（岁，达到 = 不符合）', type: 'number' },
    { path: ['business', 'female_age_threshold'], label: '女性首次参保年龄门槛（岁，达到 = 不符合）', type: 'number' },
    { path: ['business', 'gd_other_years_required'], label: '广东省内非广州户籍：在广州累计缴费年限门槛（年）', type: 'number' },
    { path: ['business', 'other_prov_years_required'], label: '广东省外户籍：在广东省内累计参保年限门槛（年）', type: 'number' },
    { path: ['business', 'max_huji_retry'], label: '反问1（户籍）最多重试次数', type: 'number' },
    { path: ['business', 'max_gender_age_retry'], label: '反问2（性别/年龄/年数）最多重试次数', type: 'number' },
    { path: ['business', 'max_retire_retry'], label: '反问3（退休时间：出生年月/人员类别）最多重试次数', type: 'number' },
    { path: ['business', 'min_contribution_years'], label: '办理退休最低缴费年限（年）', type: 'number' },
    { path: ['business', 'max_unemp_retry'], label: '反问4（失业金：缴费时长/离职原因/失业登记）最多重试次数', type: 'number' },
    { path: ['business', 'unemp_min_wage'], label: '失业金发放基数：最低工资标准（元/月，按 90% 发放）', type: 'number' },
  ];

  // 提示词 tab：左侧导航 → 单项编辑
  const FIELD_DEFS = {
    ext_huji: { title: '抽取户籍 system (extract_huji)', sub: '槽位抽取 LLM 用；输出固定 JSON schema（province / city / reason）', fields: [{ path: ['system', 'extract_huji_system'], type: 'textarea', height: 260 }] },
    ext_ga: { title: '抽取性别年龄 system (extract_gender_age)', sub: '槽位抽取 LLM 用；含出生年份×参保年份 → 首次参保年龄的换算规则', fields: [{ path: ['system', 'extract_gender_age_system'], type: 'textarea', height: 320 }] },
    ext_retire: { title: '抽取出生年月/人员类别 system (extract_retire)', sub: '「退休时间测算」槽位抽取用；输出 birth_year / birth_month / category（male | female_cadre | female_worker）', fields: [{ path: ['system', 'extract_retire_system'], type: 'textarea', height: 320 }] },
    ext_unemp: { title: '抽取失业金槽位 system (extract_unemp)', sub: '「失业保险金测算」槽位抽取用；输出 paid_months（统一折算总月数，如 3年6个月 → 42）、reason_code（**离职情形码，模型按语义直接判**：contract_end/laid_off/dismissed/mutual/employer_fault/other_lawful/voluntary）、reason_text（离职原因原话，留作线索与兜底）、registered（**是否已办理失业登记**；求职要求由经办核验，不问）', fields: [{ path: ['system', 'extract_unemp_system'], type: 'textarea', height: 380 }] },
    sys_intent: { title: '意图识别 system (classify_intent)', sub: '每轮由模型判断用户意图（retire 问退休时间 / location 问在哪领 / unemp 问失业金 / query 只在用户没提新业务诉求、单问手续或查询时命中 → 引导粤省事查询或办理 / followup 结论后追问 / off_topic 无关）。⚠️ 带业务诉求的「怎么操作/怎么申请」一律判业务意图、先进流程算，不能甩粤省事；模型不可用时自动回退规则判定', fields: [{ path: ['system', 'classify_intent_system'], type: 'textarea', height: 380 }] },
    h_ask_huji: { title: '反问 1：户籍 (ask_huji)', sub: '第一次进入工具时询问户籍地（是否省内 / 是否广州）', fields: [{ path: ['hints', 'ask_huji'], type: 'textarea', height: 130 }] },
    h_ask_huji_retry: { title: '反问 1：重试 (ask_huji_retry)', sub: '模板含 {attempt} 占位符', fields: [{ path: ['hints', 'ask_huji_retry'], type: 'textarea', height: 130 }] },
    h_ask_gender_age: { title: '反问 2：性别/年龄 (ask_gender_age)', sub: '省外户籍时收集两个变量（性别、首次参保年龄）；含 {missing} {known} 占位符', fields: [{ path: ['hints', 'ask_gender_age'], type: 'textarea', height: 220 }] },
    h_ask_gender_age_retry: { title: '反问 2：补问 (ask_gender_age_retry)', sub: '含 {attempt} {missing} {known} {note}；{missing} 只列还缺的项，{known} 是已收集到的信息；缺年龄时按搭配问——用户报过参保年份→问出生年份，否则问参保年龄', fields: [{ path: ['hints', 'ask_gender_age_retry'], type: 'textarea', height: 260 }] },
    h_ask_unknown_guide: { title: '答不上来/问怎么办：粤省事指引 (ask_unknown_guide)', sub: '各支线通用：用户答不上被问的那一项（不记得/不清楚/不知道），或反问「这怎么查、在哪办、怎么申请」时使用 → 引导粤省事查询或办理，并说明路径稍候短信发给他', fields: [{ path: ['hints', 'ask_unknown_guide'], type: 'textarea', height: 180 }] },
    h_ask_retire: { title: '反问 3：出生年月/人员类别 (ask_retire)', sub: '「退休时间测算」收集输入：出生年月（年+月）+ 人员类别', fields: [{ path: ['hints', 'ask_retire'], type: 'textarea', height: 160 }] },
    h_ask_retire_retry: { title: '反问 3：补问 (ask_retire_retry)', sub: '模板含 {attempt} {missing} 占位符；{missing} 只列还缺的项', fields: [{ path: ['hints', 'ask_retire_retry'], type: 'textarea', height: 150 }] },
    h_conclusion_gz: { title: '结论 A · 广州户籍', sub: '最后参保地在广州即可', fields: [{ path: ['hints', 'conclusion_gz'], type: 'textarea', height: 140 }] },
    h_conclusion_gd_other: { title: '结论 B · 省内非广州', sub: '模板含 {city_cn}；两条件：广州缴满 5 年 + 最后参保地', fields: [{ path: ['hints', 'conclusion_gd_other'], type: 'textarea', height: 160 }] },
    h_conclusion_other_qualify: { title: '结论 C · 省外·符合', sub: '模板含 {gender_cn} {first_ins_age} {gd_years} {years_required}；三条件', fields: [{ path: ['hints', 'conclusion_other_qualify'], type: 'textarea', height: 170 }] },
    h_conclusion_other_unqualify: { title: '结论 D · 省外·超龄', sub: '模板含 {gender_cn} {first_ins_age}', fields: [{ path: ['hints', 'conclusion_other_unqualify'], type: 'textarea', height: 160 }] },
    h_conclusion_other_unqualify_years: { title: '结论 D · 省外·年限不足', sub: '模板含 {gender_cn} {first_ins_age} {gd_years} {years_required}', fields: [{ path: ['hints', 'conclusion_other_unqualify_years'], type: 'textarea', height: 160 }] },
    h_conclusion_retire: { title: '结论 E · 退休时间', sub: '模板含 {birth_cn} {category_cn} {base_age_cn} {original_retire_cn} {retire_age_cn} {retire_cn} {min_years_cn}；数字由退休计算程序给出，模型只转述', fields: [{ path: ['hints', 'conclusion_retire'], type: 'textarea', height: 200 }] },
    h_ask_unemp: { title: '反问 4：失业金槽位 (ask_unemp)', sub: '「失业保险金测算」收集三个槽位：①累计缴费时长 ②离职原因 ③是否办理失业登记。**一次只问缺的第一项**；{ask_direct} 是程序拼好的现成问句，{known} 是已收集到的信息', fields: [{ path: ['hints', 'ask_unemp'], type: 'textarea', height: 260 }] },
    h_ask_unemp_retry: { title: '反问 4：补问 (ask_unemp_retry)', sub: '模板含 {attempt} {missing} {known} {ask_direct} 占位符；只问还缺的第一项', fields: [{ path: ['hints', 'ask_unemp_retry'], type: 'textarea', height: 200 }] },
    h_conclusion_unemp: { title: '结论 F · 失业金·符合', sub: '资格三项（缴费满1年 / 非本人意愿失业 / 已办失业登记且有求职要求）都满足时的结论；模板含 {paid_cn} {months_cn} {min_wage} {total} {formula_cn}；数字由失业金计算程序给出，模型只转述', fields: [{ path: ['hints', 'conclusion_unemp'], type: 'textarea', height: 200 }] },
    h_conclusion_unemp_ineligible: { title: '结论 F · 失业金·不符合', sub: '资格条件不满足时的结论；模板含 {fail_cn}（哪几项不符合）{advice_cn}（下步建议）。⚠️ 若失败项含「还没办失业登记」，必须说明可登录「粤省事」办理、具体路径稍候短信发您，且不许报具体菜单', fields: [{ path: ['hints', 'conclusion_unemp_ineligible'], type: 'textarea', height: 220 }] },
    h_off_topic: { title: '业务无关提示 (off_topic)', sub: '用户问的内容不属于本工具范围时', fields: [{ path: ['hints', 'off_topic'], type: 'textarea', height: 120 }] },
    h_fallback: { title: '兜底话术 (fallback)', sub: '反问超限或信息不全时告知用户转线下', fields: [{ path: ['hints', 'fallback'], type: 'textarea', height: 140 }] },
    h_followup: { title: '细节追问 (followup)', sub: '出结论后用户继续追问时', fields: [{ path: ['hints', 'followup'], type: 'textarea', height: 110 }] },
    h_guide_ss: { title: '粤省事查询/办理指引 (guide_ss)', sub: '用户问「怎么查/在哪看自己的参保缴费记录、缴够年限没有」，或问「这项业务怎么办、怎么申请」时，引导通过粤省事查询或办理，并说明路径稍候短信发给他。⚠️ 只许说这两层意思，不许报具体查询入口/菜单（禁语：参保证明、社保费查询等，命中即整段换兜底稿）', fields: [{ path: ['hints', 'guide_ss'], type: 'textarea', height: 220 }] },
  };

  // ---------------- 小工具 ----------------
  function getAtPath(obj, path) {
    let v = obj;
    for (const k of path) { if (v == null) return undefined; v = v[k]; }
    return v;
  }
  function setAtPath(obj, path, val) {
    let o = obj;
    for (let i = 0; i < path.length - 1; i++) {
      const k = path[i];
      if (!(k in o) || typeof o[k] !== 'object' || o[k] === null) o[k] = {};
      o = o[k];
    }
    o[path[path.length - 1]] = val;
  }
  function setStatus(cls, txt) {
    const s = $('cfgStatus'); if (!s) return;
    s.className = cls; s.textContent = txt;
  }
  function markDirty() { dirty = true; setStatus('busy', '有未保存改动…'); }

  function buildField(f, mount) {
    const wrap = document.createElement('div');
    wrap.className = 'field';
    const lab = document.createElement('label');
    lab.className = 'field-label';
    lab.textContent = f.label || f.path.join('.');
    wrap.appendChild(lab);
    if (f.sub) {
      const sub = document.createElement('div');
      sub.className = 'field-sub';
      sub.textContent = f.sub;
      wrap.appendChild(sub);
    }
    const val = getAtPath(window.cfgCache, f.path);
    let el;
    if (f.type === 'textarea') {
      el = document.createElement('textarea');
      el.value = val == null ? '' : val;
      if (f.height) el.style.minHeight = f.height + 'px';
    } else if (f.type === 'number') {
      el = document.createElement('input');
      el.type = 'number';
      el.value = val == null ? '' : val;
    } else {
      el = document.createElement('input');
      el.type = 'text';
      el.value = val == null ? '' : val;
    }
    el.addEventListener('input', () => {
      let v = el.value;
      if (f.type === 'number') v = v === '' ? null : Number(v);
      setAtPath(window.cfgCache, f.path, v);
      markDirty();
    });
    wrap.appendChild(el);
    mount.appendChild(wrap);
  }

  // ---------------- 全局参数 tab ----------------
  function renderGlobals() {
    const body = $('cfgGlobalsBody'); if (!body) return;
    body.innerHTML = '';
    GLOBAL_FIELDS.forEach(f => buildField(f, body));
  }

  // ---------------- 提示词 tab ----------------
  function renderPromptForm() {
    const def = FIELD_DEFS[currentKey];
    const body = $('cfgBody'); if (!body) return;
    $('cfgTitle').textContent = def.title;
    $('cfgSub').textContent = def.sub || '';
    body.innerHTML = '';
    def.fields.forEach(f => buildField(f, body));
  }
  function bindCfgNav() {
    const btns = document.querySelectorAll('#cfgNav button.navbtn');
    btns.forEach(b => {
      b.addEventListener('click', () => {
        btns.forEach(x => x.classList.remove('active'));
        b.classList.add('active');
        currentKey = b.dataset.key;
        renderPromptForm();
      });
    });
  }

  // ---------------- tab 切换 ----------------
  function setView(v) {
    view = v;
    $('tabGlobals').classList.toggle('active', v === 'globals');
    $('tabPrompts').classList.toggle('active', v === 'prompts');
    $('cfgGlobalsView').style.display = v === 'globals' ? '' : 'none';
    $('cfgPromptsView').style.display = v === 'prompts' ? '' : 'none';
    if (v === 'globals') renderGlobals();
    if (v === 'prompts') renderPromptForm();
  }

  // ---------------- 保存 / 恢复 ----------------
  async function saveCfg() {
    const btn = $('btnCfgSave');
    btn.disabled = true;
    setStatus('busy', '保存中…');
    try {
      const res = await fetch('/api/config', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(window.cfgCache),
      });
      if (!res.ok) throw new Error('HTTP ' + res.status);
      const final = await res.json();
      window.cfgCache = final;
      dirty = false;
      setStatus('ok', '已保存并立即生效');
      setView(view);
    } catch (e) {
      setStatus('err', '保存失败：' + e.message);
    } finally {
      btn.disabled = false;
    }
  }

  async function resetCfg() {
    if (!confirm('确认恢复初始配置？当前所有改动都会丢失。')) return;
    setStatus('busy', '恢复中…');
    try {
      const res = await fetch('/api/config/reset', { method: 'POST' });
      const final = await res.json();
      window.cfgCache = final;
      dirty = false;
      setStatus('ok', '已恢复初始配置');
      setView(view);
    } catch (e) {
      setStatus('err', '恢复失败：' + e.message);
    }
  }

  // ---------------- 版本预设 ----------------
  async function loadPresets() {
    const sel = $('cfgPreset');
    if (!sel) return;
    try {
      const res = await api('/api/presets');
      const list = res.presets || [];
      sel.innerHTML = '';
      list.forEach(p => {
        const o = document.createElement('option');
        o.value = p.name;
        o.textContent = p.label + (p.desc ? ' · ' + p.desc : '');
        sel.appendChild(o);
      });
      const active = res.active || (window.cfgCache && window.cfgCache.preset) || 'default';
      sel.value = active;
    } catch (e) { /* 忽略：下拉不可用不影响主流程 */ }
  }

  async function applyPreset(name) {
    const sel = $('cfgPreset');
    sel.disabled = true;
    setStatus('busy', '切换中…');
    try {
      const res = await fetch('/api/config/preset', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ preset: name }),
      });
      if (!res.ok) throw new Error('HTTP ' + res.status);
      const final = await res.json();
      window.cfgCache = final;
      dirty = false;
      setStatus('ok', '已切换版本并立即生效');
      setView(view);
    } catch (e) {
      setStatus('err', '切换失败：' + e.message);
    } finally {
      sel.disabled = false;
    }
  }

  // ---------------- 初始化 ----------------
  async function init() {
    const cfg = await api('/api/config');
    window.cfgCache = cfg;
    bindCfgNav();
    bindEvents();
    setView('globals');
    await loadPresets();
    setStatus('ok', '已加载');
  }

  function bindEvents() {
    $('btnCfgSave').onclick = saveCfg;
    $('btnCfgReset').onclick = resetCfg;
    $('tabGlobals').onclick = () => setView('globals');
    $('tabPrompts').onclick = () => setView('prompts');
    if ($('cfgPreset')) {
      $('cfgPreset').onchange = (ev) => {
        const sel = ev.target;
        const name = sel.value;
        const label = sel.options[sel.selectedIndex] ? sel.options[sel.selectedIndex].textContent : name;
        const cur = (window.cfgCache && window.cfgCache.preset) || 'default';
        if (cur === name) return;
        if (confirm('切换到「' + label + '」？\n只切换话术与提示词；当前未保存的改动会被覆盖。')) {
          applyPreset(name);
        } else {
          sel.value = cur;
        }
      };
    }
  }

  window.ConfigPage = { init, save: saveCfg, reset: resetCfg, setView };
})();
