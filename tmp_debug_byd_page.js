const { chromium } = require('C:/Users/Administrator/AppData/Roaming/npm/node_modules/openclaw/node_modules/playwright-core');

(async () => {
  const browser = await chromium.connectOverCDP('http://127.0.0.1:18800');
  const pages = browser.contexts().flatMap(c => c.pages());
  const page = pages.find(p => (p.url() || '').includes('/i/nodes/jb9Y4gmKWrbwEXLgujB6mxNNWGXn6lpz'));
  if (!page) throw new Error('target page not found');
  await page.bringToFront();
  try { await page.waitForLoadState('domcontentloaded', { timeout: 10000 }); } catch (e) {}
  await page.waitForTimeout(2500);
  const data = await page.evaluate(() => ({
    title: document.title,
    href: location.href,
    bodyText: (document.body?.innerText || '').slice(0, 5000),
    iframeCount: document.querySelectorAll('iframe').length,
    iframes: Array.from(document.querySelectorAll('iframe')).slice(0, 20).map(x => ({
      src: x.src,
      id: x.id,
      name: x.name,
    })),
    flexCount: document.querySelectorAll('[class*="FlexTableWrapper"]').length,
    hasTargetText: (document.body?.innerText || '').includes('问题关闭及闭环率'),
    buttons: Array.from(document.querySelectorAll('button')).slice(0, 50).map(x => (x.innerText || x.textContent || '').trim()).filter(Boolean),
  }));
  console.log(JSON.stringify(data, null, 2));
  await browser.close();
})().catch(err => {
  console.error(err);
  process.exit(1);
});
