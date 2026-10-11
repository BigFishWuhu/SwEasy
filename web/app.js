const state = {
  config: null, selected: null, account: null, accountResults: null,
  accountFilter: 'ALL', accountSet: 'ALL', accountSlot: 'ALL', accountMain: 'ALL',
  accountEquipped: 'ALL', accountSort: 'maxWithGrind-desc', accountPage: 0,
};
const $ = (id) => document.getElementById(id);
const uid = (prefix = 'group') => `${prefix}-${Date.now()}-${Math.random().toString(16).slice(2)}`;

async function redirectToActiveService() {
  try {
    const response = await fetch(`/runtime.json?t=${Date.now()}`, { cache: 'no-store' });
    if (!response.ok) return false;
    const runtime = await response.json();
    const targetPort = Number(runtime.port);
    const currentPort = Number(location.port || (location.protocol === 'https:' ? 443 : 80));
    if (!targetPort || targetPort === currentPort || runtime.service !== 'SwEasyRuneWeb') return false;
    const target = `${location.protocol}//127.0.0.1:${targetPort}`;
    const health = await fetch(`${target}/api/health?t=${Date.now()}`, { cache: 'no-store' });
    const payload = await health.json();
    if (!health.ok || payload.service !== 'SwEasyRuneWeb') return false;
    location.replace(`${target}${location.pathname}${location.search}${location.hash}`);
    return true;
  } catch (_) {
    return false;
  }
}

const SETS = [
  ['Energy', '祝福'], ['Guard', '守护'], ['Swift', '迅速'], ['Blade', '刀刃'],
  ['Rage', '激怒'], ['Focus', '集中'], ['Endure', '忍耐'], ['Fatal', '猛攻'],
  ['Despair', '绝望'], ['Vampire', '吸血'], ['Violent', '暴走'], ['Nemesis', '应报'],
  ['Will', '意志'], ['Shield', '保护'], ['Revenge', '反击'], ['Destroy', '覆灭'],
  ['Fight', '斗志'], ['Determination', '决心'], ['Enhance', '发扬'], ['Accuracy', '命中'],
  ['Tolerance', '韧性'], ['Seal', '封印'], ['Intangible', '无形'],
];
const STATS = [['HP', '体力'], ['HP%', '体力%'], ['ATK', '攻击力'], ['ATK%', '攻击%'], ['DEF', '防御力'], ['DEF%', '防御%'], ['SPD', '攻击速度'], ['CR', '暴击率'], ['CD', '暴击伤害'], ['RES', '效果抵抗'], ['ACC', '效果命中']];
const STAT_LABELS = Object.fromEntries(STATS);
const QUALITY = [['Common', '普通'], ['Magic', '魔法'], ['Rare', '稀有'], ['Hero', '英雄'], ['Legend', '传说']];
const SET_LABELS = Object.fromEntries(SETS);
const QUALITY_LABELS = Object.fromEntries(QUALITY);
const PERCENT_STATS = new Set(['HP%', 'ATK%', 'DEF%', 'CR', 'CD', 'RES', 'ACC']);
const fields = [
  { value: 'stars', label: '星级', kind: 'enum', options: [1, 2, 3, 4, 5, 6].map(v => [v, `${v} 星`]) },
  { value: 'quality', label: '品质', kind: 'enum', options: QUALITY },
  { value: 'set', label: '套装', kind: 'enum', options: SETS },
  { value: 'slot', label: '位置', kind: 'enum', options: [1, 2, 3, 4, 5, 6].map(v => [v, `${v} 号位`]) },
  { value: 'ancient', label: '远古', kind: 'enum', options: [[true, '是'], [false, '否']] },
  { value: 'level', label: '强化等级', kind: 'number' },
  { value: 'main_stat.type', label: '主属性', kind: 'enum', options: STATS },
  { value: 'innate.type', label: '先天属性类型', kind: 'enum', options: STATS },
  { value: 'innate.maxed', label: '先天属性满值', kind: 'enum', options: [[true, '是'], [false, '否']] },
  { value: 'innate.CR', label: '先天暴击率（普通满值 6）', kind: 'number' },
  { value: 'innate.CD', label: '先天暴击伤害（普通满值 7）', kind: 'number' },
  { value: 'innate.ACC', label: '先天效果命中（普通满值 8）', kind: 'number' },
  { value: 'hasSubstat.SPD', label: '包含速度副属性', kind: 'enum', options: [[true, '是'], [false, '否']] },
  { value: 'efficiency.current', label: '规则当前效率（固定值半权）', kind: 'number' },
  { value: 'efficiency.max', label: '规则 Max 效率（固定值半权）', kind: 'number' },
  { value: 'efficiency.maxHeroGrind', label: '规则英雄磨炼效率（固定值半权）', kind: 'number' },
  { value: 'efficiency.maxWithGrind', label: '规则传说磨炼效率（固定值半权）', kind: 'number' },
  { value: 'efficiency.practicalCurrent', label: '实用当前效率（固定值半权）', kind: 'number' },
  { value: 'efficiency.practicalMax', label: '实用 Max 效率（固定值半权）', kind: 'number' },
  { value: 'efficiency.practicalMaxHeroGrind', label: '实用英雄磨炼效率（固定值半权）', kind: 'number' },
  { value: 'efficiency.practicalMaxWithGrind', label: '实用传说磨炼效率（固定值半权）', kind: 'number' },
  { value: 'rolls.attackOutput', label: '有效强化次数：攻击输出（攻/暴/爆伤/速）', kind: 'number' },
  { value: 'rolls.hpOutput', label: '有效强化次数：体力输出（体/暴/爆伤/速）', kind: 'number' },
  { value: 'rolls.defOutput', label: '有效强化次数：防御输出（防/暴/爆伤/速）', kind: 'number' },
  { value: 'rolls.scalingOutput', label: '有效强化次数：多类型输出', kind: 'number' },
  { value: 'rolls.guaranteedCrit', label: '有效强化次数：必暴输出（攻/爆伤/速）', kind: 'number' },
  { value: 'rolls.hpGuaranteedCrit', label: '有效强化次数：体力必暴输出', kind: 'number' },
  { value: 'rolls.speedCritHp', label: '有效强化次数：高速暴击体力', kind: 'number' },
  { value: 'rolls.bomber', label: '有效强化次数：炸弹（攻/命中/速）', kind: 'number' },
  { value: 'rolls.control', label: '有效强化次数：肉控', kind: 'number' },
  { value: 'rolls.tank', label: '有效强化次数：坦度', kind: 'number' },
  { value: 'rolls.support', label: '有效强化次数：辅助', kind: 'number' },
  { value: 'rolls.damageControl', label: '有效强化次数：输出控制', kind: 'number' },
  { value: 'rolls.versatile', label: '有效强化次数：泛用属性', kind: 'number' },
  ...['SPD', 'CR', 'CD', 'ATK%', 'HP%', 'DEF%', 'RES', 'ACC'].map(stat => ({ value: `substats.${stat}`, label: `副属性 ${STAT_LABELS[stat]}`, kind: 'number' })),
  ...['SPD', 'CR', 'CD', 'ATK%', 'HP%', 'DEF%', 'RES', 'ACC'].map(stat => ({ value: `buildStats.${stat}`, label: `构筑属性 ${STAT_LABELS[stat]}（主/副）`, kind: 'number' })),
  ...['SPD', 'CR', 'CD', 'ATK%', 'HP%', 'DEF%', 'RES', 'ACC'].map(stat => ({ value: `baseSubstats.${stat}`, label: `未磨炼副属性 ${STAT_LABELS[stat]}`, kind: 'number' })),
];
const ops = [['eq', '等于'], ['neq', '不等于'], ['gt', '大于'], ['gte', '至少'], ['lt', '小于'], ['lte', '至多'], ['in', '属于'], ['not_in', '不属于']];
const fieldInfo = (name) => fields.find(field => field.value === name) || { value: name, label: name, kind: 'text' };
function setStatus(text, kind = '') { $('status').textContent = text; $('status').className = `status ${kind}`; }
function groups() { return state.config?.groups || []; }
function currentGroup() { return groups().find(group => group.id === state.selected); }
function normalizeConfig(config) {
  const result = config || {};
  if (!Array.isArray(result.groups)) {
    result.groups = (Array.isArray(result.rules) ? result.rules : []).map(rule => ({ ...rule, rules: rule.rules || rule.conditions || [] }));
    delete result.rules;
  }
  result.groups.forEach(group => { group.rules = group.rules || group.conditions || []; group.requirements = group.requirements || []; delete group.conditions; });
  return result;
}
function renderGroups() {
  const list = $('rules'); list.replaceChildren();
  const items = groups(); $('rule-count').textContent = items.filter(group => group.enabled !== false).length;
  items.forEach((group, index) => {
    const el = document.createElement('button'); el.className = `rule ${group.id === state.selected ? 'active' : ''}`;
    el.innerHTML = `<span class="drag">${String(index + 1).padStart(2, '0')}</span><span class="rule-content"><span class="rule-name">${escapeHtml(group.name || '未命名规则组')}</span><span class="rule-meta">${(group.rules || []).length} 条规则</span></span><span class="rule-action ${group.action === 'sell' ? 'sell' : ''}">${group.action === 'sell' ? '出售' : '保留'}</span>`;
    el.onclick = () => { state.selected = group.id; renderGroups(); renderEditor(); $('editor').scrollTop = 0; }; list.append(el);
  });
}
function renderEditor() {
  const editor = $('editor'), group = currentGroup();
  if (!group) { editor.className = 'editor-empty'; editor.textContent = '从左侧选择规则组，开始编辑筛选条件。'; $('editor-title').textContent = '选择一个规则组'; return; }
  editor.className = ''; $('editor-title').textContent = group.name || '编辑规则组';
  editor.innerHTML = `<div class="rule-form"><div class="field-row"><div class="field"><label>规则组名称</label><input id="rule-name" value="${escapeAttr(group.name || '')}" /></div><div class="field"><label>命中后动作</label><select id="rule-action"><option value="keep">保留</option><option value="sell">出售</option></select></div></div><label class="toggle"><input id="rule-enabled" type="checkbox" ${group.enabled !== false ? 'checked' : ''}/> 启用这个规则组</label><div class="conditions"><div class="condition-head"><strong>前置条件（全部满足）</strong></div><div id="requirements"></div><button class="add-condition" id="add-requirement">＋ 添加前置条件</button><div class="condition-head rule-match-head"><strong>目标属性</strong><span><select id="rule-match"><option value="all">全部满足</option><option value="any">任一满足</option><option value="at_least">至少满足</option></select><input id="min-match" type="number" min="1" value="${group.min_match || 1}" title="至少命中条数"></span></div><div id="conditions"></div><button class="add-condition" id="add-condition">＋ 添加目标属性</button></div></div>`;
  $('rule-action').value = group.action || 'keep'; $('rule-match').value = group.match || 'all';
  $('rule-name').oninput = event => { group.name = event.target.value; renderGroups(); };
  $('rule-action').onchange = event => { group.action = event.target.value; renderGroups(); };
  $('rule-enabled').onchange = event => { group.enabled = event.target.checked; renderGroups(); };
  $('rule-match').onchange = event => { group.match = event.target.value; renderMatchCount(group); };
  $('min-match').onchange = event => { group.min_match = Math.max(1, Number(event.target.value) || 1); };
  $('add-requirement').onclick = () => { group.requirements.push({ field: 'stars', op: 'gte', value: 6 }); renderEditor(); };
  $('add-condition').onclick = () => { group.rules.push({ field: 'stars', op: 'gte', value: 6 }); renderEditor(); };
  renderMatchCount(group);
  renderConditions(group);
}
function renderMatchCount(group) { $('min-match').hidden = group.match !== 'at_least'; }
function renderConditions(group) {
  renderConditionList(group, 'requirements');
  renderConditionList(group, 'rules');
}
function renderConditionList(group, key) {
  const host = $(key === 'rules' ? 'conditions' : 'requirements'); host.replaceChildren();
  (group[key] || []).forEach((condition, index) => {
    const row = document.createElement('div'); row.className = 'condition';
    const info = fieldInfo(condition.field); const options = fields.map(field => `<option value="${escapeAttr(field.value)}">${escapeHtml(field.label)}</option>`).join('');
    row.innerHTML = `<select class="condition-field">${options}</select><select class="condition-op">${ops.map(([value, label]) => `<option value="${value}">${label}</option>`).join('')}</select><span class="condition-value"></span><button title="删除规则">×</button>`;
    const fieldSelect = row.querySelector('.condition-field'); const opSelect = row.querySelector('.condition-op');
    fieldSelect.value = condition.field; opSelect.value = condition.op || 'eq';
    fieldSelect.onchange = event => { condition.field = event.target.value; condition.value = defaultValue(fieldInfo(condition.field)); renderConditionList(group, key); };
    opSelect.onchange = event => { condition.op = event.target.value; condition.value = normalizeValue(condition.value, fieldInfo(condition.field), condition.op); renderConditionList(group, key); };
    row.querySelector('button').onclick = () => { group[key].splice(index, 1); renderConditionList(group, key); };
    renderValue(row.querySelector('.condition-value'), condition, info);
    host.append(row);
  });
}
function defaultValue(info) { return info.kind === 'number' ? 0 : info.kind === 'enum' ? info.options[0][0] : ''; }
function normalizeValue(value, info, op) {
  if (['in', 'not_in'].includes(op)) return Array.isArray(value) ? value : [value];
  if (info.kind === 'number') return Number(value || 0);
  if (info.kind === 'enum' && info.options[0] && typeof info.options[0][0] === 'boolean') return value === true || value === 'true';
  return value;
}
function renderValue(host, condition, info) {
  const multiple = ['in', 'not_in'].includes(condition.op);
  if (info.kind === 'enum') {
    const select = document.createElement('select'); if (multiple) select.multiple = true;
    const selected = new Set(multiple ? (Array.isArray(condition.value) ? condition.value.map(String) : [String(condition.value)]) : [String(condition.value)]);
    select.innerHTML = info.options.map(([value, label]) => `<option value="${escapeAttr(value)}" ${selected.has(String(value)) ? 'selected' : ''}>${escapeHtml(label)}</option>`).join('');
    select.onchange = () => { condition.value = multiple ? [...select.selectedOptions].map(option => parseOption(option.value, info)) : parseOption(select.value, info); };
    host.append(select); return;
  }
  const input = document.createElement('input'); input.type = info.kind === 'number' ? 'number' : 'text'; input.step = 'any';
  input.value = Array.isArray(condition.value) ? condition.value.join(', ') : condition.value ?? ''; input.placeholder = info.kind === 'number' ? '阈值' : '条件值';
  input.onchange = () => { condition.value = multiple ? input.value.split(',').map(value => value.trim()).filter(Boolean) : (info.kind === 'number' ? Number(input.value) : input.value); };
  host.append(input);
}
function parseOption(value, info) { if (info.options[0] && typeof info.options[0][0] === 'boolean') return value === 'true'; if (info.options[0] && typeof info.options[0][0] === 'number') return Number(value); return value; }
function escapeHtml(value) { return String(value).replace(/[&<>"']/g, character => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[character])); }
function escapeAttr(value) { return escapeHtml(value); }
async function load() {
  const response = await fetch('/api/config'); state.config = normalizeConfig(await response.json());
  state.selected = groups()[0]?.id || null; $('fallback').value = state.config.fallback || 'keep';
  $('fallback').onchange = event => { state.config.fallback = event.target.value; };
  renderGroups(); renderEditor(); setStatus('配置已加载', 'ok');
  const accountResponse = await fetch('/api/account');
  const saved = await accountResponse.json();
  if (accountResponse.ok && saved.saved && saved.account) {
    state.account = saved.account;
    $('account-file-name').textContent = `已保存：${saved.filename}`;
    $('preview-account').disabled = false;
    $('preview-account').textContent = '重新分析';
    await analyzeAccount();
  }
}
$('add-rule').onclick = () => { const group = { id: uid(), name: '新规则组', enabled: true, action: 'keep', match: 'all', rules: [{ field: 'stars', op: 'gte', value: 6 }] }; state.config.groups.push(group); state.selected = group.id; renderGroups(); renderEditor(); $('rules').querySelector('.active')?.scrollIntoView({ block: 'nearest' }); $('editor').scrollTop = 0; };
$('delete-rule').onclick = () => { if (!state.selected) return; state.config.groups = groups().filter(group => group.id !== state.selected); state.selected = groups()[0]?.id || null; renderGroups(); renderEditor(); $('editor').scrollTop = 0; };
$('save').onclick = async () => { setStatus('保存中'); const response = await fetch('/api/config', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(state.config) }); const data = await response.json(); if (response.ok) setStatus('已保存', 'ok'); else setStatus(data.error || '保存失败', 'error'); };
$('restart-web').onclick = async () => {
  setStatus('正在重启网页服务'); $('restart-web').disabled = true;
  try {
    const healthBefore = await fetch(`/api/health?t=${Date.now()}`, { cache: 'no-store' });
    const before = healthBefore.ok ? await healthBefore.json() : {};
    const response = await fetch('/api/restart', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '{}' });
    if (!response.ok) throw new Error('旧服务不支持网页内重启，请在 Maa 中执行一次“启动/重启符文筛选网页”');
    const deadline = Date.now() + 12000;
    while (Date.now() < deadline) {
      await new Promise(resolve => setTimeout(resolve, 300));
      try {
        const health = await fetch(`/api/health?t=${Date.now()}`, { cache: 'no-store' });
        if (health.ok) {
          const current = await health.json();
          if (!before.instance || current.instance !== before.instance) { location.reload(); return; }
        }
      } catch (_) { /* 服务切换过程中连接失败是预期状态。 */ }
    }
    throw new Error('重启超时，请在 Maa 中执行“启动/重启符文筛选网页”');
  } catch (error) {
    setStatus(error.message, 'error'); $('restart-web').disabled = false;
  }
};
$('evaluate').onclick = async () => { try { const rune = JSON.parse($('rune-input').value); const response = await fetch('/api/evaluate', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ rune, config: state.config }) }); const data = await response.json(); if (!response.ok) throw new Error(data.error); const decision = data.decision; $('result').className = `result ${decision.uncertain ? 'uncertain' : decision.action.toLowerCase()}`; $('result').textContent = `${decision.action}\n${decision.reason}\n\nSWEX：当前 ${data.efficiency.current}% · Max ${data.efficiency.max}% · 英雄磨炼 ${data.efficiency.maxHeroGrind}% · 传说磨炼 ${data.efficiency.maxLegendGrind}%\n实用（固定值半权）：当前 ${data.efficiency.practicalCurrent}% · Max ${data.efficiency.practicalMax}% · 英雄磨炼 ${data.efficiency.practicalMaxHeroGrind}% · 传说磨炼 ${data.efficiency.practicalMaxLegendGrind}%`; } catch (error) { $('result').className = 'result uncertain'; $('result').textContent = `输入错误：${error.message}`; } };
redirectToActiveService().then(redirected => {
  if (!redirected) load().catch(error => setStatus(`加载失败：${error.message}`, 'error'));
});
document.addEventListener('change', event => {
  if (event.target.id !== 'account-file') return;
  const file = event.target.files?.[0];
  state.account = null; state.accountResults = null;
  $('account-file-name').textContent = file ? file.name : '尚未选择文件';
  $('preview-account').disabled = !file;
  $('preview-account').textContent = file ? '更新并分析' : '分析账号';
  $('account-result').className = 'account-result empty';
  $('account-result').textContent = file ? '文件已选择，点击“更新并分析”覆盖已保存的账号 JSON。' : '选择 SWEX / SWKit 账号 JSON 后显示规则预览。';
});
$('preview-account').onclick = async () => {
  const file = $('account-file').files?.[0];
  $('preview-account').disabled = true; $('preview-account').textContent = '分析中…';
  $('account-result').className = 'account-result empty'; $('account-result').textContent = '正在解析符文并按当前规则计算。';
  try {
    if (file) {
      state.account = JSON.parse(await file.text());
      const saveResponse = await fetch('/api/account', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ account: state.account, filename: file.name }),
      });
      const saved = await saveResponse.json();
      if (!saveResponse.ok) throw new Error(saved.error || '账号 JSON 保存失败');
      $('account-file').value = '';
      $('account-file-name').textContent = `已保存：${saved.filename}`;
    }
    if (!state.account) throw new Error('请先选择账号 JSON');
    await analyzeAccount();
  } catch (error) {
    $('account-result').className = 'account-result empty error-text'; $('account-result').textContent = `解析失败：${error.message}`;
  } finally { $('preview-account').disabled = false; $('preview-account').textContent = '重新分析'; }
};
async function analyzeAccount() {
  if (!state.account) return;
  const response = await fetch('/api/evaluate_account', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ account: state.account, config: state.config, include_equipped: true }),
  });
  const data = await response.json(); if (!response.ok) throw new Error(data.error || '账号分析失败');
  state.accountResults = data;
  state.accountFilter = state.accountSet = state.accountSlot = state.accountMain = state.accountEquipped = 'ALL';
  state.accountSort = 'maxWithGrind-desc'; state.accountPage = 0; renderAccountPreview();
}
function renderAccountPreview() {
  const data = state.accountResults, host = $('account-result'); if (!data) return;
  const s = data.summary;
  const filtered = data.runes.filter(rune => {
    if (state.accountFilter !== 'ALL' && rune.action !== state.accountFilter) return false;
    if (state.accountSet !== 'ALL' && rune.set !== state.accountSet) return false;
    if (state.accountSlot !== 'ALL' && String(rune.slot) !== state.accountSlot) return false;
    if (state.accountMain !== 'ALL' && String(rune.main_stat?.type) !== state.accountMain) return false;
    if (state.accountEquipped === 'EQUIPPED' && !rune.equipped) return false;
    if (state.accountEquipped === 'UNEQUIPPED' && rune.equipped) return false;
    return true;
  }).sort(accountRuneComparator);
  const pageSize = 100, pages = Math.max(1, Math.ceil(filtered.length / pageSize)); state.accountPage = Math.min(state.accountPage, pages - 1);
  const rows = filtered.slice(state.accountPage * pageSize, (state.accountPage + 1) * pageSize);
  const ruleLabels = new Map((state.config.groups || []).map(group => [group.id, group.name || group.id]));
  const counts = data.rules.map(item => `<tr><td>${escapeHtml(ruleLabels.get(item.rule_id) || (item.rule_id === 'fallback' ? '兜底规则' : item.rule_id))}</td><td>${item.count}</td></tr>`).join('');
  const setOptions = SETS.map(([value, label]) => `<option value="${value}" ${state.accountSet === value ? 'selected' : ''}>${label}</option>`).join('');
  const mainOptions = STATS.map(([value, label]) => `<option value="${value}" ${state.accountMain === value ? 'selected' : ''}>${label}</option>`).join('');
  const speedPanel = renderSpeedTiers(data.speed_tiers);
  host.className = 'account-result';
  host.innerHTML = `<div class="account-summary"><div><strong>${s.total}</strong><span>全部符文</span></div><div class="keep-stat"><strong>${s.keep}</strong><span>预计保留</span></div><div class="sell-stat"><strong>${s.sell}</strong><span>预计出售</span></div><div><strong>${s.equipped}</strong><span>已装备总数</span></div><div class="sell-stat"><strong>${s.equipped_sell}</strong><span>已装备中预计出售</span></div><div><strong>${s.uncertain + s.skipped}</strong><span>不确定/跳过</span></div></div>
    ${speedPanel}
    <div class="account-layout"><section class="account-rules"><div class="table-title">规则命中</div><div class="table-scroll"><table><thead><tr><th>规则组</th><th>数量</th></tr></thead><tbody>${counts || '<tr><td colspan="2">没有命中记录</td></tr>'}</tbody></table></div></section>
    <section class="account-rune-list"><div class="account-table-toolbar"><div class="segmented"><button data-filter="ALL" class="${state.accountFilter === 'ALL' ? 'active' : ''}">全部 ${s.total}</button><button data-filter="KEEP" class="${state.accountFilter === 'KEEP' ? 'active' : ''}">预计保留 ${s.keep}</button><button data-filter="SELL" class="${state.accountFilter === 'SELL' ? 'active' : ''}">预计出售 ${s.sell}</button><button data-filter="KEEP_UNCERTAIN" class="${state.accountFilter === 'KEEP_UNCERTAIN' ? 'active' : ''}">不确定 ${s.uncertain}</button></div><span class="muted">筛选后 ${filtered.length} 枚</span></div>
      <div class="account-filterbar"><label>装备状态<select id="account-equipped"><option value="ALL">全部</option><option value="EQUIPPED">已装备</option><option value="UNEQUIPPED">未装备</option></select></label><label>符文类型<select id="account-set"><option value="ALL">全部套装</option>${setOptions}</select></label><label>位置<select id="account-slot"><option value="ALL">全部位置</option>${[1,2,3,4,5,6].map(value => `<option value="${value}" ${state.accountSlot === String(value) ? 'selected' : ''}>${value}号位</option>`).join('')}</select></label><label>主属性<select id="account-main"><option value="ALL">全部主属性</option>${mainOptions}</select></label><label>排序<select id="account-sort"><option value="maxWithGrind-desc">传说磨炼：高到低</option><option value="maxHeroGrind-desc">英雄磨炼：高到低</option><option value="current-desc">当前效率：高到低</option><option value="max-desc">Max：高到低</option><option value="maxWithGrind-asc">传说磨炼：低到高</option><option value="set-slot">套装与位置</option></select></label></div>
      <div class="table-scroll rune-table-wrap"><table><thead><tr><th>符文类型</th><th>位置</th><th>主属性</th><th>副属性</th><th>效率</th><th>分析结果</th><th>命中规则</th></tr></thead><tbody>${rows.map(accountRuneRow).join('') || '<tr><td colspan="7">没有符合条件的符文</td></tr>'}</tbody></table></div><div class="pagination"><button id="prev-account-page" ${state.accountPage === 0 ? 'disabled' : ''} title="上一页">‹</button><span>${filtered.length ? state.accountPage * pageSize + 1 : 0}-${Math.min((state.accountPage + 1) * pageSize, filtered.length)} / ${filtered.length}　 第 ${state.accountPage + 1} / ${pages} 页</span><button id="next-account-page" ${state.accountPage >= pages - 1 ? 'disabled' : ''} title="下一页">›</button></div></section></div>`;
  host.querySelectorAll('[data-filter]').forEach(button => button.onclick = () => { state.accountFilter = button.dataset.filter; state.accountPage = 0; renderAccountPreview(); });
  bindAccountSelect('account-equipped', 'accountEquipped');
  bindAccountSelect('account-set', 'accountSet');
  bindAccountSelect('account-slot', 'accountSlot');
  bindAccountSelect('account-main', 'accountMain');
  bindAccountSelect('account-sort', 'accountSort');
  $('prev-account-page').onclick = () => { state.accountPage--; renderAccountPreview(); };
  $('next-account-page').onclick = () => { state.accountPage++; renderAccountPreview(); };
}
function renderSpeedTiers(stats) {
  if (!stats) return '';
  const thresholds = stats.thresholds || [30, 25, 20];
  const cells = item => thresholds.map(value => `<td><strong>${Number(item?.tiers?.[value] || 0)}</strong></td>`).join('');
  const head = thresholds.map(value => `<th>未精炼 ${value}+</th>`).join('');
  const row = (label, item, cls = '') => `<tr class="${cls}"><td>${escapeHtml(label)}<small>${Number(item?.with_speed || 0)} 枚带速度副属性</small></td>${cells(item)}</tr>`;
  const keyRows = (stats.by_set || []).filter(item => item.key).map(item => row(setLabel(item.set), item)).join('');
  const allRows = (stats.by_set || []).map(item => row(`${setLabel(item.set)}${item.key ? ' · 关键' : ''}`, item)).join('');
  const keyNames = (stats.key_sets || []).map(setLabel).join('、');
  return `<section class="speed-tier-panel"><div class="speed-tier-head"><div><div class="table-title">副属性速度阶梯</div><p>只计算副属性原始速度，不含磨炼；30+/25+/20+ 为累计数量，包含仓库与已装备符文。</p></div></div>
    <div class="speed-tier-grid"><div class="table-scroll"><table><thead><tr><th>范围</th>${head}</tr></thead><tbody>${row('全部符文', stats.all, 'speed-total')}${row('关键套装小计', stats.key_total, 'speed-key')}${row('其他套装（散件候选）', stats.off_set)}</tbody></table></div>
    <div class="table-scroll"><table><thead><tr><th>关键套装</th>${head}</tr></thead><tbody>${keyRows || '<tr><td colspan="4">暂无关键套装速度符文</td></tr>'}</tbody></table></div></div>
    <p class="speed-tier-note">关键套装范围：${escapeHtml(keyNames)}</p><details class="speed-tier-details"><summary>查看全部套装速度明细</summary><div class="table-scroll"><table><thead><tr><th>符文套装</th>${head}</tr></thead><tbody>${allRows}</tbody></table></div></details></section>`;
}
function bindAccountSelect(id, stateKey) {
  const select = $(id); select.value = state[stateKey];
  select.onchange = () => { state[stateKey] = select.value; state.accountPage = 0; renderAccountPreview(); };
}
function accountRuneComparator(left, right) {
  if (state.accountSort === 'set-slot') return String(left.set).localeCompare(String(right.set)) || Number(left.slot) - Number(right.slot) || Number(right.efficiency?.maxWithGrind || 0) - Number(left.efficiency?.maxWithGrind || 0);
  const [field, direction] = state.accountSort.split('-');
  const delta = Number(right.efficiency?.[field] || 0) - Number(left.efficiency?.[field] || 0);
  return direction === 'asc' ? -delta : delta;
}
function accountRuneRow(rune) {
  const main = formatStat(rune.main_stat);
  const innate = rune.innate ? `<small>先天：${escapeHtml(formatStat(rune.innate))}</small>` : '';
  const subs = (rune.substats || []).map(formatStat).join(' · ');
  const prediction = rune.action === 'SELL' ? '预计出售' : rune.action === 'KEEP_UNCERTAIN' ? '不确定·保留' : '预计保留';
  const annotations = [rune.equipped ? '已装备' : '', rune.locked ? '已锁定' : ''].filter(Boolean);
  const status = [...annotations, prediction].join(' · ');
  const cls = rune.action === 'SELL' ? 'sell' : rune.action === 'KEEP' ? 'keep' : 'uncertain';
  const rule = (state.config.groups || []).find(group => group.id === rune.rule_id)?.name || (rune.rule_id ? rune.rule_id : '兜底规则');
  const efficiency = rune.efficiency || {};
  return `<tr class="${rune.equipped ? 'equipped-rune' : ''}"><td><strong>${escapeHtml(setLabel(rune.set))}</strong><small>${rune.stars}星 ${escapeHtml(qualityLabel(rune.quality))} 强化+${rune.level}${rune.ancient ? ' · 远古' : ''}</small><small>编号 ${escapeHtml(rune.rune_id)}</small></td><td>${rune.slot}号位</td><td>${escapeHtml(main)}${innate}</td><td class="substats-cell">${escapeHtml(subs || '-')}</td><td class="efficiency-cell"><strong>传说磨炼 ${efficiency.maxLegendGrind ?? efficiency.maxWithGrind ?? '-'}%</strong><small>当前 ${efficiency.current ?? '-'}% · Max ${efficiency.max ?? '-'}%</small><small>英雄磨炼 ${efficiency.maxHeroGrind ?? '-'}%</small></td><td><span class="decision ${cls}">${escapeHtml(status)}</span></td><td>${escapeHtml(rule)}</td></tr>`;
}
function statLabel(type) { const canonical = ({1:'HP',2:'HP%',3:'ATK',4:'ATK%',5:'DEF',6:'DEF%',8:'SPD',9:'CR',10:'CD',11:'RES',12:'ACC'})[type] || type; return STAT_LABELS[canonical] || canonical; }
function canonicalStat(type) { return ({1:'HP',2:'HP%',3:'ATK',4:'ATK%',5:'DEF',6:'DEF%',8:'SPD',9:'CR',10:'CD',11:'RES',12:'ACC'})[type] || type; }
function formatStat(stat) {
  if (!stat) return '-';
  const type = canonicalStat(stat.type); const suffix = PERCENT_STATS.has(type) ? '%' : '';
  const grind = Number(stat.grind || stat.enchanted || 0);
  return `${statLabel(type)} +${Number(stat.value || 0)}${suffix}${grind ? `+${grind}${suffix}` : ''}${stat.gemmed || stat.swapped ? '（宝石）' : ''}`;
}
function setLabel(value) { return SET_LABELS[value] || value || '未知套装'; }
function qualityLabel(value) { return QUALITY_LABELS[value] || value || '未知品质'; }
