const { chromium } = require('C:/Users/Administrator/AppData/Roaming/npm/node_modules/openclaw/node_modules/playwright-core');

(async () => {
  const browser = await chromium.connectOverCDP('http://127.0.0.1:18800');
  const contexts = browser.contexts();
  const pages = contexts.flatMap(ctx => ctx.pages());
  const target = pages.find(p => String(p.url() || '').includes('wByVB5LaHzJ90N28'));
  if (!target) throw new Error('target page not found');
  await target.bringToFront();
  await target.waitForLoadState('domcontentloaded').catch(() => {});
  await target.waitForTimeout(1200).catch(() => {});
  const frame = target.frames().find(f => String(f.url() || '').includes('alidocs.dingtalk.com/spreadsheetv2')) || target.mainFrame();

  const result = await frame.evaluate(() => {
    function collectControllers() {
      const nodes = Array.from(document.querySelectorAll('[class*="FlexTableWrapper"]'));
      const ctrls = [];
      for (const el of nodes) {
        const fiberKey = Object.keys(el || {}).find(k => k.startsWith('__reactFiber$'));
        let cur = fiberKey ? el[fiberKey] : null;
        for (let i = 0; cur && i < 80; i++, cur = cur.return) {
          const p = cur.memoizedProps;
          if (p && p.controller) { ctrls.push(p.controller); break; }
        }
      }
      return ctrls;
    }
    function listSheets(ctrl) {
      const sheets = ctrl?.selection?.book?.sheets;
      if (!sheets) return [];
      try {
        if (Array.isArray(sheets)) return sheets.filter(Boolean);
        if (typeof sheets.values === 'function') return Array.from(sheets.values()).filter(Boolean);
        if (typeof sheets.forEach === 'function') {
          const arr = [];
          sheets.forEach(v => arr.push(v));
          return arr.filter(Boolean);
        }
        if (typeof sheets === 'object') return Object.values(sheets).filter(Boolean);
      } catch (e) {}
      return [];
    }

    const ctrl = collectControllers()[0];
    const sheet = listSheets(ctrl)[1] || listSheets(ctrl)[0];
    if (!sheet) throw new Error('no sheet found');
    const row = (sheet.content?.cells || []).find(r => (r || [])[6]);
    const cell = row && row[6];
    return {
      sheetKeys: Object.keys(sheet || {}).sort(),
      sheetPreview: {
        name: sheet.name,
        title: sheet.title,
        id: sheet.id || sheet.sheetId || sheet._id || null,
      },
      cellKeys: Object.keys(cell || {}).sort(),
      cellPreview: cell,
    };
  });

  console.log(JSON.stringify(result, null, 2));
  await browser.close();
})().catch(err => {
  console.error(err && err.stack || String(err));
  process.exit(1);
});
