const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');
const source = fs.readFileSync(path.join(__dirname, '../patches/claude-custom.js'), 'utf8');
const moduleSource = source.slice(source.indexOf(' * ACCOUNT SWITCHER BUTTON'));
function extract(name) {
  const start = moduleSource.indexOf('  function ' + name + '(');
  assert(start >= 0, name);
  return moduleSource.slice(start, moduleSource.indexOf('\n  }', start) + 4);
}
class Element {
  constructor(tagName = 'div') { this.tagName = tagName; this.children = []; this.listeners = {}; this.style = {}; this.connected = false; }
  set textContent(value) { this.text = value; for (const c of this.children) c.parentNode = null; this.children = []; }
  get textContent() { return (this.text || '') + this.children.map(c => c.textContent).join(''); }
  get isConnected() { return this.connected || !!this.parentNode?.isConnected; }
  appendChild(el) { el.parentNode = this; this.children.push(el); return el; }
  insertBefore(el, before) { this.children = this.children.filter(child => child !== el); this.children.splice(this.children.indexOf(before), 0, el); el.parentNode = this; }
  addEventListener(name, handler) { this.listeners[name] = handler; }
  setAttribute(name, value) { (this.attributes || (this.attributes = {}))[name] = value; }
  click() { this.listeners.click?.({ preventDefault() {}, stopPropagation() {} }); }
}
const pending = [];
const panel = new Element(); panel.connected = true;
const body = panel.appendChild(new Element());
let switched = 0;
const context = vm.createContext({
  panel, switching: false, accountDetailsCache: Object.create(null), accountsSnapshot: [],
  showSub: false, showUsage: false, Date, encodeURIComponent,
  document: { createElement: tag => new Element(tag) },
  positionPanel() {}, usageBlock() { return new Element(); }, logInfo() {},
  accountMeta: () => '', accountSubtitle: () => '', gearIcon: () => new Element(),
  switchTo() { switched++; }, openConfigEditor() {},
  renderAccounts(target) { target.textContent = 'accounts'; },
  fetch: url => new Promise((resolve, reject) => pending.push({ url, resolve, reject })),
});
vm.runInContext(['renderError', 'accountRow', 'accountLocalDate', 'accountBucketLabel', 'accountChartValue', 'accountWeekTooltip', 'renderAccountChart', 'renderWeeklyUsage', 'renderAccountDetails', 'openAccountDetails'].map(extract).join('\n'), context);
async function flush() { for (let i = 0; i < 10; i++) await Promise.resolve(); }
(async () => {
  const acc = { file: 'settings_test.json', name: 'Test', isActive: true };
  const row = context.accountRow(acc, {}, body);
  row.children[0].children[0].click();
  assert.equal(switched, 0, 'Row opens details without switching, even for the active account');
  assert.equal(pending.length, 1);
  const first = pending.shift();
  body.children[0].children[0].children[0].click();
  assert.equal(body.textContent, 'accounts');
  first.resolve({ json: async () => ({ ok: true, billing: 'api', windows: [], days: [], updatedAt: 1 }) });
  await flush();
  assert.equal(body.textContent, 'accounts', 'Late history response cannot reopen details after going back');
  context.openAccountDetails({ ...acc, isActive: false }, {}, body);
  assert(body.textContent.includes('Учтено за период') || body.textContent.includes('Нет записанной стоимости'), 'Cached details render immediately');
  body.children[0].children[0].children[1].click();
  assert(body.children[0].children[0].children[1].className.includes('claude-accs-use-account'));
  assert.equal(switched, 1, 'Account switching remains available via an explicit button');
  pending.shift().reject(new Error('offline'));
  await flush();
  assert(body.textContent.includes('Показаны сохранённые данные'));
  const output = new Element();
  context.renderAccountDetails(output, { billing: 'api', updatedAt: 1, days: [
    { date: '2026-09-18', api: { usd: 0, messages: 1, unknown: 1, requests: 1 } },
    { date: '2026-09-17', windows: [] },
    { date: '2026-09-16', windows: [{ label: '7 d', percent: 0, first: 5, last: 5 }] },
  ] });
  assert(output.textContent.includes('Нет записанной стоимости'));
  assert(!output.textContent.includes('$0.0000'), 'Unknown cost is not shown as free');
  assert(output.textContent.includes('Нет данных'));
  assert(!output.textContent.includes('Подробности по'), 'Expandable details are removed');
  const weekly = new Element();
  context.renderWeeklyUsage(weekly, { billing: 'subscription', windows: [{ key: 'w', label: '7 дн' }],
    weeklyUsage: [
      { number: 1, start: '2026-09-01T00:00:00Z', end: '2026-09-08T00:00:00Z', windows: [] },
      { number: 2, start: '2026-09-08T00:00:00Z', end: '2026-09-15T00:00:00Z', windows: [{ key: 'w', label: '7 дн', percent: 39 }] },
    ] });
  const weeklySection = weekly.children[0];
  assert.equal(weeklySection.children[1].hidden, true, 'Weekly plot starts collapsed');
  weeklySection.children[0].click();
  assert.equal(weeklySection.children[1].hidden, false);
  assert.equal(weeklySection.children[0].attributes['aria-expanded'], 'true');
  const weeklyBars = weeklySection.children[1].children.find(el => el.className === 'claude-accs-weekly-bars');
  assert.equal(weeklyBars.children.length, 2);
  assert(weeklyBars.children[1].title.includes('≥39 п.п.'));
  assert.equal(weeklyBars.children[1].children.find(el => el.className === 'claude-accs-chart-value').textContent, '39');
  assert.equal(weeklyBars.children[0].children.find(el => el.className === 'claude-accs-chart-value'), undefined);
  context.renderWeeklyUsage(weekly, { billing: 'api' });
  assert.equal(weekly.hidden, true, 'API accounts have no subscription-week chart');
  const zai = { billing: 'subscription', period: { mode: 'day', date: '2026-09-18' },
    windows: [{ key: 'five_hour', label: '5 ч', reset_at: null }, { key: 'seven_day', label: '7 дн' }],
    buckets: [{ date: '2026-09-18T10', windows: [{ key: 'seven_day', label: '7 дн', percent: 12 }] }],
    weeklyUsage: [{ number: 1, start: '2026-09-18T00:00:00Z', end: '2026-09-25T00:00:00Z',
      windows: [{ key: 'seven_day', label: '7 дн', percent: 12 }] }] };
  const zaiChart = new Element();
  context.renderAccountChart(zaiChart, zai, () => {});
  const dailySelect = zaiChart.children[0].children.find(el => el.tagName === 'select');
  assert.equal(dailySelect, undefined, 'Both quotas are shown together without a selector');
  const zaiPair = zaiChart.children[0].children.find(el => el.className === 'claude-accs-chart-plot').children[1].children[0];
  assert.equal(zaiPair.className, 'claude-accs-chart-pair');
  assert.equal(zaiPair.children[0].children[0].attributes['data-missing'], 'true', 'Missing short quota stays unknown');
  assert.equal(zaiPair.children[1].children.find(el => el.className === 'claude-accs-chart-value').textContent, '12');
  assert.equal(zaiPair.children[1].attributes['data-outline'], 'false', 'The only known limit stays filled');
  assert(zaiChart.textContent.includes('5 ч · Claude Code'));
  assert(zaiChart.textContent.includes('7 дн · Вне Claude Code'));

  const pairedChart = new Element();
  const pairedChanges = [];
  context.renderAccountChart(pairedChart, { ...zai, period: { mode: 'month', date: '2024-02' }, buckets: [
    { date: '2024-02-01', windows: [
      { key: 'seven_day', label: '7 дн', percent: 2, localPercent: 1, externalPercent: 1 },
      { key: 'five_hour', label: '5 ч', percent: 10, localPercent: 6, externalPercent: 4 },
    ] },
    { date: '2024-02-02', windows: [
      { key: 'seven_day', label: '7 дн', percent: 0 },
      { key: 'five_hour', label: '5 ч', percent: 0 },
    ] },
    { date: '2024-02-03', windows: [
      { key: 'seven_day', label: '7 дн', percent: 3 },
      { key: 'five_hour', label: '5 ч', percent: 1 },
    ] },
  ] }, value => pairedChanges.push(value));
  const pairedBars = pairedChart.children[0].children.find(el => el.className === 'claude-accs-chart-plot').children[1];
  assert.equal(pairedBars.children.length, 3, 'One group per calendar bucket');
  const pair = pairedBars.children[0];
  assert(pair.children[0].title.startsWith('5 ч'), 'Series retain their quota identity');
  assert.equal(pair.children[0].children[0].style.height, '100%');
  assert.equal(pair.children[1].children[0].style.height, '20%', 'Quotas share a scale but are not summed');
  assert.equal(pair.children[0].children[0].style.background, 'transparent', 'Larger short quota is outlined');
  assert(pair.children[0].children[0].style.borderImage.includes('#409cff 40%'));
  assert(pair.children[1].children[0].style.background.includes('#ff9b4588 50%'), 'Smaller weekly quota is filled');
  assert(pair.children[1].children[0].style.borderImage.includes('#ff9b45 50%'));
  assert(pair.children[1].children[0].style.borderImage.includes('#c080ff'));
  assert.equal(pair.children[0].children[1].textContent, '10');
  assert.equal(pair.children[1].children[1].textContent, '2');
  assert.equal(pair.children[2].textContent, '01', 'Both bars share one date label');
  pair.children[1].click();
  assert.equal(pairedChanges.pop().date, '2024-02-01');
  assert.equal(pairedBars.children[1].children[0].children[1].textContent, '0', 'Known zero remains visible');
  const reversePair = pairedBars.children[2];
  assert.equal(reversePair.children[0].attributes['data-outline'], 'false');
  assert.equal(reversePair.children[0].children[0].style.background, '#7fff0066');
  assert.equal(reversePair.children[1].attributes['data-outline'], 'true');
  assert.equal(reversePair.children[1].children[0].style.background, 'transparent', 'Larger weekly quota is outlined');
  context.renderWeeklyUsage(weekly, zai);
  assert.equal(weekly.children[1].children[1].hidden, false, 'Refresh preserves disclosure state');
  assert.equal(weekly.children[1].children[1].children.find(el => el.tagName === 'select'), undefined,
    'Five-hour quota has its own chart instead of a weekly selector entry');
  assert.equal(weekly.children[0].children[0].textContent, '5 часовые лимиты');
  assert.equal(weekly.children[1].children[0].textContent, 'Недельные лимиты');
  assert.equal(weekly.children[0].children[1].hidden, true);
  weekly.children[0].children[0].click();
  assert.equal(weekly.children[0].children[1].hidden, false);
  context.renderWeeklyUsage(weekly, { ...zai, shortUsage: [
    { id: 'short-1', start: 1790000000, end: 1790018000, used: 35 },
    { id: 'short-2', start: 1790018000, end: 1790036000, used: 0 },
  ] });
  const shortBody = weekly.children[0].children[1];
  assert.equal(shortBody.hidden, false);
  const shortBars = shortBody.children[0].children[0];
  assert.equal(shortBars.children.length, 2);
  assert(shortBars.children[0].title.includes('35%'));
  assert.equal(shortBars.children[1].children.find(el => el.className === 'claude-accs-chart-value').textContent, '0');
  weekly.children[0].children[0].click();
  assert.equal(shortBody.hidden, true, 'Second click closes the five-hour graph');
  assert.equal(context.accountChartValue({ windows: [] }, 'w|week'), null);
  assert.equal(context.accountChartValue({ windows: [{ key: 'w', label: 'week', percent: 0 }] }, 'w|week'), 0);
  assert.equal(context.accountChartValue({ api: { usd: 0, messages: 1, unknown: 1 } }, 'api'), null);
  const changes = [];
  const chart = new Element();
  context.renderAccountChart(chart, { period: { mode: 'month', date: '2024-02' }, billing: 'api',
    buckets: [{ date: '2024-02-29', api: { usd: 3, messages: 1, unknown: 0 } }] }, p => changes.push(p));
  const section = chart.children[0];
  const controls = section.children[0];
  controls.children[2].click();
  assert.equal(changes.pop().date, '2024-01', 'Previous month navigates across calendar boundaries');
  const bars = section.children.find(el => el.className === 'claude-accs-chart-plot').children[1];
  assert.equal(bars.children[0].children.find(el => el.className === 'claude-accs-chart-value').textContent, '3');
  assert(bars.children[0].title.includes('$3.0000'), 'API tooltip keeps cost details');
  bars.children[0].click();
  assert.equal(changes.pop().date, '2024-02-29', 'Clicking monthly bar opens that exact day');
  controls.children[0].click();
  assert.equal(changes.pop().date, '2024-02-01', 'Switching old month to hours selects its first day');
  const subscriptionChart = new Element();
  context.renderAccountChart(subscriptionChart, { period: { mode: 'day', date: '2026-09-18' }, billing: 'subscription',
    buckets: [
      { date: '2026-09-18T10', windows: [{ key: 'w', label: '7 d', percent: 2.5 }],
        subscriptionWeeks: [{ number: 4, start: '2026-09-17T00:00:00+03:00', end: '2026-09-24T00:00:00+03:00' }] },
      { date: '2026-09-18T11', windows: [{ key: 'w', label: '7 d', percent: 0 }] },
      { date: '2026-09-18T12', windows: [] },
    ] }, () => {});
  const subBars = subscriptionChart.children[0].children.find(el => el.className === 'claude-accs-chart-plot').children[1];
  assert(subBars.children[0].title.includes('4-я неделя подписки'));
  assert(!subBars.children[0].title.includes('п.п.'));
  assert.equal(subBars.children[0].children.find(el => el.className === 'claude-accs-chart-value').textContent, '2.5');
  assert.equal(subBars.children[1].children.find(el => el.className === 'claude-accs-chart-value').textContent, '0');
  assert.equal(subBars.children[2].children.find(el => el.className === 'claude-accs-chart-value'), undefined);
  assert(subBars.children[1].title.includes('Нет данных о неделе'));
  const mixedChart = new Element();
  context.renderAccountChart(mixedChart, { billing: 'subscription',
    period: { mode: 'day', date: '2026-09-18' }, buckets: [
      { date: '2026-09-18T14', windows: [{ key: 'w', label: '7 d', percent: 10, externalPercent: 6, localPercent: 4,
        observedFrom: 1790000000, observedTo: 1790003600 }] },
      { date: '2026-09-18T15', windows: [{ key: 'w', label: '7 d', percent: 2, externalPercent: 2 }] },
    ] }, () => {});
  const mixedBars = mixedChart.children[0].children.find(el => el.className === 'claude-accs-chart-plot').children[1];
  assert(mixedBars.children[0].children[0].style.background.includes('60%'), 'Mixed bar preserves local and external shares');
  assert.equal(mixedBars.children[1].children[0].style.borderColor, '#409cff');
  assert(mixedBars.children[0].title.includes('Между проверками'));
  assert(mixedBars.children[0].title.includes('Вне Claude Code: 6'));
  assert(mixedBars.children[0].title.includes('Замеры задачи Claude Code: 4'));
  assert(!mixedBars.children[0].title.includes('подтверждено пользователем'));
  context.openAccountDetails(acc, {}, body);
  const todayRequest = pending.shift();
  assert(todayRequest.url.includes('mode=day&date=' + context.accountLocalDate()));
  const detailContent = body.children[0].children[3];
  const currentChart = detailContent.children.find(el => el.className === 'claude-accs-chart');
  currentChart.children[0].children[1].click();
  const monthRequest = pending.shift();
  monthRequest.resolve({ json: async () => ({ ok: true, billing: 'api', windows: [], buckets: [], updatedAt: 2,
    period: { mode: 'month', date: '2024-02' } }) });
  await flush();
  todayRequest.resolve({ json: async () => ({ ok: true, billing: 'api', windows: [], buckets: [], updatedAt: 1,
    period: { mode: 'day', date: context.accountLocalDate() } }) });
  await flush();
  const finalChart = detailContent.children.find(el => el.className === 'claude-accs-chart');
  assert.equal(finalChart.children[0].children[3].value, '2024-02', 'Late day response cannot replace the selected month');
  console.log('Account details: navigation, no accidental switch, cached opening, late responses, missing costs: OK');
})().catch(error => { console.error(error); process.exitCode = 1; });
