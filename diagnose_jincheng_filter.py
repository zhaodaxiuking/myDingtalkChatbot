import json
import subprocess
import tempfile
from pathlib import Path

from app.config_loader import load_config, build_task_config

ROOT = Path(r"C:\Users\Administrator\Documents\code\opencode\DingtalkChatbot\DingtalkChatbot-1.5.7")
CFG = ROOT / 'config' / 'config.json'
OPENCLAW_PLAYWRIGHT = 'C:/Users/Administrator/AppData/Roaming/npm/node_modules/openclaw/node_modules/playwright-core'
CDP_URL = 'http://127.0.0.1:18800'


def main():
    cfg = load_config(CFG)
    task = build_task_config(cfg, '晋城富士康_现场问题是否关闭 = 否_截图发送')
    sheet_name = json.dumps(task['sheet_name'], ensure_ascii=False)
    alidocs_url = json.dumps(task['alidocs_url'], ensure_ascii=False)
    tab_keyword = json.dumps((task.get('browser_target') or {}).get('tab_keyword', ''), ensure_ascii=False)
    tab_url_keyword = json.dumps((task.get('browser_target') or {}).get('tab_url_keyword', ''), ensure_ascii=False)
    filter_col = json.dumps((task.get('filter') or {}).get('column_name', ''), ensure_ascii=False)
    filter_equals = json.dumps((task.get('filter') or {}).get('equals', ''), ensure_ascii=False)

    script = f"""
const {{ chromium }} = require('{OPENCLAW_PLAYWRIGHT}');
(async()=>{{
  const sheetName = {sheet_name};
  const alidocsUrl = {alidocs_url};
  const tabKeyword = {tab_keyword};
  const tabUrlKeyword = {tab_url_keyword};
  const filterColName = {filter_col};
  const filterEquals = {filter_equals};

  function norm(v) {{ return String(v || '').toLowerCase(); }}
  const browser = await chromium.connectOverCDP('{CDP_URL}');
  const pages = browser.contexts().flatMap(ctx => ctx.pages());
  const tabKeywordNorm = norm(tabKeyword);
  const tabUrlKeywordNorm = norm(tabUrlKeyword);
  const alidocsUrlNorm = norm(alidocsUrl);

  async function inspectPage(page) {{
    let titleText = '';
    try {{ titleText = await page.title(); }} catch (e) {{}}
    const rawUrl = String(page.url() || '');
    const url = norm(rawUrl);
    const title = norm(titleText);
    return {{
      page,
      title: titleText,
      url: rawUrl,
      titleMatched: !!(tabKeywordNorm && title.includes(tabKeywordNorm)),
      urlKeywordMatched: !!(tabUrlKeywordNorm && url.includes(tabUrlKeywordNorm)),
      alidocsMatched: !!(alidocsUrlNorm && (url === alidocsUrlNorm || url.includes(alidocsUrlNorm))) || url.includes('alidocs.dingtalk.com/spreadsheetv2')
    }};
  }}

  async function hasSheet(page, sheetName) {{
    try {{
      await page.bringToFront();
      return await page.evaluate((sheetName) => {{
        const el = document.querySelector('.FlexTableWrapper-def767d7') || document.querySelector('[class*="FlexTableWrapper"]');
        if (!el) return false;
        const fiberKey = Object.keys(el || {{}}).find(k => k.startsWith('__reactFiber$'));
        let cur = fiberKey ? el[fiberKey] : null;
        let ctrl = null;
        for (let i = 0; cur && i < 40; i++, cur = cur.return) {{
          const p = cur.memoizedProps;
          if (p && p.controller) {{ ctrl = p.controller; break; }}
        }}
        if (!ctrl || typeof ctrl.getSheetIdByName !== 'function') return false;
        return !!ctrl.getSheetIdByName(sheetName);
      }}, sheetName);
    }} catch (e) {{
      return false;
    }}
  }}

  const inspected = await Promise.all(pages.map(inspectPage));
  let candidates = inspected;
  if (tabKeywordNorm) candidates = inspected.filter(x => x.titleMatched);
  else if (tabUrlKeywordNorm) candidates = inspected.filter(x => x.urlKeywordMatched);
  else candidates = inspected.filter(x => x.alidocsMatched);

  let winner = null;
  for (const item of candidates) {{
    if (await hasSheet(item.page, sheetName)) {{
      winner = item;
      break;
    }}
  }}
  if (!winner) throw new Error('no matching page with target sheet');

  const data = await winner.page.evaluate((sheetName) => {{
    const el = document.querySelector('.FlexTableWrapper-def767d7') || document.querySelector('[class*="FlexTableWrapper"]');
    if (!el) throw new Error('Table wrapper not found');
    const fiberKey = Object.keys(el || {{}}).find(k=>k.startsWith('__reactFiber$'));
    let cur = fiberKey ? el[fiberKey] : null;
    let ctrl = null;
    for (let i=0; cur && i<40; i++, cur=cur.return) {{
      const p = cur.memoizedProps;
      if (p && p.controller) {{ ctrl = p.controller; break; }}
    }}
    if (!ctrl) throw new Error('controller not found');
    const sheetId = ctrl.getSheetIdByName(sheetName);
    if (!sheetId) throw new Error('sheet not found: ' + sheetName);
    const sheet = ctrl.selection.book.sheets.get(sheetId);
    const cells = sheet.content.cells || [];
    function cellVal(cell){{
      if(cell == null) return '';
      const candidates = [cell.value, cell.showValue, cell.displayValue, cell.text, cell.v, cell.payload?.value, cell.payload?.showValue, cell.payload?.displayValue, cell.payload?.text, cell.payload?.v, cell.payload?.editValue];
      for (const c of candidates) {{
        if (c == null) continue;
        try {{
          const s = String(c);
          if (s !== '[object Object]') return s;
        }} catch(e) {{}}
      }}
      return '';
    }}
    return cells.map(row => (row||[]).map(cellVal));
  }}, sheetName);

  const header = data[0] || [];
  const rows = data.slice(1);
  const filterIndex = header.findIndex(x => String(x || '').trim() === String(filterColName).trim());
  if (filterIndex === -1) throw new Error('filter column not found: ' + filterColName);

  const strictRows = [];
  const inheritRows = [];
  let carry = '';
  for (let i = 0; i < rows.length; i++) {{
    const r = rows[i] || [];
    const raw = String(r[filterIndex] || '').trim();
    if (raw) carry = raw;
    const effective = raw || carry;
    const rowNo = i + 2;
    const rowText = (r.slice(0, 16).map(v => String(v || '').trim()).filter(Boolean).join(' | ')).slice(0, 320);
    if (raw === String(filterEquals).trim()) strictRows.push({{ rowNo, raw, effective, preview: rowText }});
    if (effective === String(filterEquals).trim()) inheritRows.push({{ rowNo, raw, effective, preview: rowText }});
  }}

  const strictSet = new Set(strictRows.map(x => x.rowNo));
  const extraRows = inheritRows.filter(x => !strictSet.has(x.rowNo));

  console.log(JSON.stringify({{
    matchedTitle: winner.title,
    matchedUrl: winner.url,
    filterColName,
    filterEquals,
    strictCount: strictRows.length,
    inheritCount: inheritRows.length,
    extraCount: extraRows.length,
    extraRows: extraRows.slice(0, 20),
    lastStrict: strictRows.slice(-12),
    lastInherit: inheritRows.slice(-12)
  }}, null, 2));

  await browser.close();
}})().catch(e=>{{ console.error(e); process.exit(1); }});
"""

    with tempfile.NamedTemporaryFile('w', suffix='.js', delete=False, encoding='utf-8') as f:
        f.write(script)
        temp = Path(f.name)
    try:
        proc = subprocess.run(
            ['node', str(temp)],
            capture_output=True,
            text=True,
            encoding='utf-8',
            errors='replace',
            timeout=180,
        )
        (ROOT / 'output' / 'jincheng_filter_diagnose.stdout.txt').write_text(proc.stdout, encoding='utf-8')
        (ROOT / 'output' / 'jincheng_filter_diagnose.stderr.txt').write_text(proc.stderr, encoding='utf-8')
        if proc.returncode != 0:
            raise SystemExit(proc.returncode)
    finally:
        temp.unlink(missing_ok=True)


if __name__ == '__main__':
    main()
