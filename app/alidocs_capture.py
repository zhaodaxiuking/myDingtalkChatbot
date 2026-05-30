import json
import subprocess
import tempfile
from pathlib import Path

from .utils import ensure_dir, parse_cell_range

OPENCLAW_PLAYWRIGHT = 'C:/Users/Administrator/AppData/Roaming/npm/node_modules/playwright-core'
CDP_URL = 'http://127.0.0.1:18810'


def _render_script(task):
    sheet_name = json.dumps(task['sheet_name'], ensure_ascii=False)
    alidocs_url = json.dumps(task['alidocs_url'], ensure_ascii=False)
    output_dir = json.dumps(task['output_dir'], ensure_ascii=False)
    filter_cfg = task.get('filter', {}) or {}
    capture_cfg = task.get('capture', {}) or {}
    browser_target = task.get('browser_target', {}) or {}
    mode = json.dumps(task.get('mode', 'filter_capture'), ensure_ascii=False)
    range_info = parse_cell_range(capture_cfg.get('cell_range', 'M:AE'))
    optimize_width = 'true' if capture_cfg.get('optimize_width', True) else 'false'
    font_size = int(capture_cfg.get('font_size', 11))
    viewport_max_width = int(capture_cfg.get('viewport_max_width', 5200))
    title_prefix = json.dumps(capture_cfg.get('title_prefix', '阿里文档截图'), ensure_ascii=False)
    capture_style = json.dumps(capture_cfg.get('style', 'compact'), ensure_ascii=False)
    filter_enabled = 'true' if filter_cfg.get('enabled') else 'false'
    filter_col = json.dumps(filter_cfg.get('column_name', ''), ensure_ascii=False)
    filter_equals = json.dumps(filter_cfg.get('equals', ''), ensure_ascii=False)
    tab_keyword = json.dumps(browser_target.get('tab_keyword', ''), ensure_ascii=False)
    tab_url_keyword = json.dumps(browser_target.get('tab_url_keyword', ''), ensure_ascii=False)

    return f"""
const fs = require('fs');
const path = require('path');
const {{ pathToFileURL }} = require('url');
const {{ chromium }} = require('{OPENCLAW_PLAYWRIGHT}');
(async()=>{{
  const sheetName = {sheet_name};
  const alidocsUrl = {alidocs_url};
  const outDir = {output_dir};
  const mode = {mode};
  const rangeInfo = {json.dumps(range_info, ensure_ascii=False)};
  const optimizeWidth = {optimize_width};
  const fontSize = {font_size};
  const viewportMaxWidth = {viewport_max_width};
  const titlePrefix = {title_prefix};
  const captureStyle = {capture_style};
  const filterEnabled = {filter_enabled};
  const filterColName = {filter_col};
  const filterEquals = {filter_equals};
  const tabKeyword = {tab_keyword};
  const tabUrlKeyword = {tab_url_keyword};

  fs.mkdirSync(outDir, {{recursive:true}});
  const browser = await chromium.connectOverCDP('{CDP_URL}');
  const contexts = browser.contexts();
  if (!contexts.length) throw new Error('No browser context found in current CDP browser.');
  const pages = contexts.flatMap(ctx => ctx.pages());
  if (!pages.length) throw new Error('No browser pages found in current CDP browser.');

  function norm(v) {{ return String(v || '').toLowerCase().trim(); }}
  function compact(v) {{ return String(v || '').replace(/\\s+/g, '').trim(); }}
  const tabKeywordNorm = norm(tabKeyword);
  const tabUrlKeywordNorm = norm(tabUrlKeyword);
  const alidocsUrlNorm = norm(alidocsUrl);

  async function inspectPage(page) {{
    let titleText = '';
    try {{ titleText = await page.title(); }} catch (e) {{}}
    const rawUrl = String(page.url() || '');
    const url = norm(rawUrl);
    const title = norm(titleText);
    const titleMatched = !!(tabKeywordNorm && title.includes(tabKeywordNorm));
    const urlKeywordMatched = !!(tabUrlKeywordNorm && url.includes(tabUrlKeywordNorm));
    const alidocsExactMatched = !!(alidocsUrlNorm && url === alidocsUrlNorm);
    const alidocsFuzzyMatched = !!(alidocsUrlNorm && (url.includes(alidocsUrlNorm) || alidocsUrlNorm.includes(url)));
    const isAlidocsPage = url.includes('alidocs.dingtalk.com/spreadsheetv2') || url.includes('alidocs.dingtalk.com/i/nodes/');
    return {{
      page,
      url: rawUrl,
      title: titleText,
      titleMatched,
      urlKeywordMatched,
      alidocsExactMatched,
      alidocsFuzzyMatched,
      isAlidocsPage,
    }};
  }}

  async function resolveSheetRuntime(page, sheetName) {{
    try {{
      await page.bringToFront();
      try {{ await page.waitForLoadState('domcontentloaded', {{ timeout: 10000 }}); }} catch (e) {{}}
      try {{ await page.waitForTimeout(1200); }} catch (e) {{}}

      const probeFrame = async (frame) => {{
        try {{
          return await frame.evaluate(async (sheetName) => {{
            function normalize(v) {{
              return String(v || '').replace(/\\s+/g, '').trim();
            }}
            function collectControllers() {{
              const nodes = Array.from(document.querySelectorAll('[class*="FlexTableWrapper"]'));
              const ctrls = [];
              for (const el of nodes) {{
                const fiberKey = Object.keys(el || {{}}).find(k => k.startsWith('__reactFiber$'));
                let cur = fiberKey ? el[fiberKey] : null;
                for (let i = 0; cur && i < 80; i++, cur = cur.return) {{
                  const p = cur.memoizedProps;
                  if (p && p.controller) {{
                    ctrls.push(p.controller);
                    break;
                  }}
                }}
              }}
              return ctrls;
            }}
            function listSheetNamesFromController(ctrl) {{
              const names = [];
              const pushName = (v) => {{ if (v != null && String(v).trim()) names.push(String(v)); }};
              const sheets = ctrl?.selection?.book?.sheets;
              if (!sheets) return names;
              try {{
                if (Array.isArray(sheets)) {{
                  sheets.forEach(x => pushName(x?.name));
                }} else if (typeof sheets.values === 'function') {{
                  for (const x of sheets.values()) pushName(x?.name);
                }} else if (typeof sheets.forEach === 'function') {{
                  sheets.forEach(x => pushName(x?.name));
                }} else if (typeof sheets === 'object') {{
                  Object.values(sheets).forEach(x => pushName(x?.name));
                }}
              }} catch (e) {{}}
              return names;
            }}

            const target = normalize(sheetName);
            for (let attempt = 0; attempt < 20; attempt++) {{
              const ctrls = collectControllers();
              for (const ctrl of ctrls) {{
                if (ctrl && typeof ctrl.getSheetIdByName === 'function') {{
                  try {{
                    if (ctrl.getSheetIdByName(sheetName)) return {{ ok: true, controllerReady: true }};
                  }} catch (e) {{}}
                }}
                const sheetNames = listSheetNamesFromController(ctrl);
                if (sheetNames.some(name => normalize(name) === target)) return {{ ok: true, controllerReady: true }};
              }}
              await new Promise(resolve => setTimeout(resolve, 300));
            }}
            return {{ ok: false, controllerReady: false }};
          }}, sheetName);
        }} catch (e) {{
          return {{ ok: false, error: String(e && e.message || e) }};
        }}
      }};

      const mainResult = await probeFrame(page.mainFrame());
      if (mainResult && mainResult.ok) return {{ ok: true, useFrame: false }};

      for (const frame of page.frames()) {{
        if (frame === page.mainFrame()) continue;
        const url = String(frame.url() || '');
        if (!url.includes('alidocs.dingtalk.com/spreadsheetv2')) continue;
        const result = await probeFrame(frame);
        if (result && result.ok) return {{ ok: true, useFrame: true, frameUrl: url }};
      }}
      return {{ ok: false }};
    }} catch (e) {{
      return {{ ok: false, error: String(e && e.message || e) }};
    }}
  }}

  let inspectedPages = await Promise.all(pages.map(inspectPage));

  if (alidocsUrlNorm && !inspectedPages.some(x => x.alidocsExactMatched)) {{
    const targetPage = await contexts[0].newPage();
    await targetPage.goto(alidocsUrl, {{ waitUntil: 'domcontentloaded', timeout: 60000 }});
    try {{ await targetPage.waitForTimeout(2000); }} catch (e) {{}}
    inspectedPages.push(await inspectPage(targetPage));
  }}

  let candidatePages = inspectedPages.filter(x =>
    x.alidocsExactMatched ||
    x.alidocsFuzzyMatched ||
    x.urlKeywordMatched ||
    x.titleMatched ||
    x.isAlidocsPage
  );
  if (!candidatePages.length) {{
    throw new Error('No matching browser tab found in current CDP browser.');
  }}

  candidatePages = candidatePages.slice().sort((a, b) => {{
    const score = (x) =>
      (x.alidocsExactMatched ? 300 : 0) +
      (x.alidocsFuzzyMatched ? 120 : 0) +
      (x.urlKeywordMatched ? 80 : 0) +
      (x.titleMatched ? 60 : 0) +
      (x.isAlidocsPage ? 20 : 0);
    return score(b) - score(a);
  }});

  let winner = null;
  for (const item of candidatePages) {{
    const runtime = await resolveSheetRuntime(item.page, sheetName);
    if (runtime && runtime.ok) {{
      winner = {{ ...item, runtime }};
      break;
    }}
  }}

  if (!winner) {{
    const debugTitles = candidatePages.map(x => (x.title || x.url || '[untitled]') + ' @ ' + (x.url || '')).slice(0, 5).join(' | ');
    throw new Error('sheet not found in matched tab(s): ' + sheetName + (debugTitles ? ' | tabs=' + debugTitles : ''));
  }}

  const page = winner.page;
  await page.bringToFront();
  try {{ await page.waitForLoadState('domcontentloaded', {{ timeout: 10000 }}); }} catch (e) {{}}
  const frame = winner.runtime && winner.runtime.useFrame && winner.runtime.frameUrl
    ? page.frames().find(f => String(f.url() || '') === String(winner.runtime.frameUrl || ''))
      || page.frames().find(f => String(f.url() || '').includes('alidocs.dingtalk.com/spreadsheetv2'))
      || page.mainFrame()
    : page.mainFrame();

  const data = await frame.evaluate(async (sheetName) => {{
    function normalize(v) {{
      return String(v || '').replace(/\\s+/g, '').trim();
    }}
    function collectControllers() {{
      const nodes = Array.from(document.querySelectorAll('[class*="FlexTableWrapper"]'));
      const ctrls = [];
      for (const el of nodes) {{
        const fiberKey = Object.keys(el || {{}}).find(k=>k.startsWith('__reactFiber$'));
        let cur = fiberKey ? el[fiberKey] : null;
        for (let i=0; cur && i<80; i++, cur=cur.return) {{
          const p = cur.memoizedProps;
          if (p && p.controller) {{ ctrls.push(p.controller); break; }}
        }}
      }}
      return ctrls;
    }}
    function listSheets(ctrl) {{
      const sheets = ctrl?.selection?.book?.sheets;
      if (!sheets) return [];
      try {{
        if (Array.isArray(sheets)) return sheets.filter(Boolean);
        if (typeof sheets.values === 'function') return Array.from(sheets.values()).filter(Boolean);
        if (typeof sheets.forEach === 'function') {{
          const arr = [];
          sheets.forEach(v => arr.push(v));
          return arr.filter(Boolean);
        }}
        if (typeof sheets === 'object') return Object.values(sheets).filter(Boolean);
      }} catch (e) {{}}
      return [];
    }}
    function resolveSheetOnce(sheetName) {{
      const target = normalize(sheetName);
      const ctrls = collectControllers();
      let ctrl = null;
      let sheet = null;
      let sheetId = null;
      for (const c of ctrls) {{
        if (!c) continue;
        if (typeof c.getSheetIdByName === 'function') {{
          try {{
            const id = c.getSheetIdByName(sheetName);
            if (id) {{
              ctrl = c;
              sheetId = id;
              break;
            }}
          }} catch (e) {{}}
        }}
        const matchedSheet = listSheets(c).find(x => normalize(x?.name) === target);
        if (matchedSheet) {{
          ctrl = c;
          sheet = matchedSheet;
          sheetId = matchedSheet?.id || matchedSheet?.sheetId || matchedSheet?._id || null;
          break;
        }}
      }}
      if (!ctrl) return {{ ctrl: null, sheet: null, sheetId: null }};
      if (!sheet) {{
        if (!sheetId) return {{ ctrl, sheet: null, sheetId: null }};
        const sheets = ctrl.selection?.book?.sheets;
        if (sheets?.get) {{
          sheet = sheets.get(sheetId);
        }} else {{
          sheet = listSheets(ctrl).find(x => String(x?.id || x?.sheetId || x?._id || '') === String(sheetId));
        }}
      }}
      return {{ ctrl, sheet, sheetId }};
    }}

    let resolved = {{ ctrl: null, sheet: null, sheetId: null }};
    for (let attempt = 0; attempt < 20; attempt++) {{
      resolved = resolveSheetOnce(sheetName);
      if (resolved.ctrl && resolved.sheet) break;
      await new Promise(resolve => setTimeout(resolve, 300));
    }}

    if (!resolved.ctrl) throw new Error('controller not found');
    if (!resolved.sheet) throw new Error('sheet object not found: ' + sheetName);
    const cells = resolved.sheet.content.cells || [];
    function richTextToPlain(value) {{
      if (value == null) return '';
      if (typeof value === 'string' || typeof value === 'number' || typeof value === 'boolean') return String(value);
      if (Array.isArray(value)) {{
        if (value.length >= 3 && typeof value[0] === 'string' && value[1] && typeof value[1] === 'object' && !Array.isArray(value[1])) {{
          const tag = String(value[0] || '').toLowerCase();
          const props = value[1] || {{}};
          const textFromProps = [];
          if (props.text != null) textFromProps.push(richTextToPlain(props.text));
          if (props.value != null && props.value !== value) textFromProps.push(richTextToPlain(props.value));
          const childrenText = value.slice(2).map(richTextToPlain).join('');
          const merged = [...textFromProps, childrenText].join('');
          if (tag === 'br') return '\\n';
          if (tag === 'p' || tag === 'div') return merged ? `${{merged}}\\n` : '';
          return merged;
        }}
        return value.map(richTextToPlain).join('');
      }}
      if (typeof value === 'object') {{
        if (value.type === 'asl' && Array.isArray(value.data)) {{
          return richTextToPlain(value.data);
        }}
        const collected = [];
        if (value.text != null) collected.push(richTextToPlain(value.text));
        if (value.value != null && value.value !== value) collected.push(richTextToPlain(value.value));
        if (Array.isArray(value.children)) collected.push(richTextToPlain(value.children));
        if (Array.isArray(value.data)) collected.push(richTextToPlain(value.data));
        const text = collected.join('');
        if (text && text !== '[object Object]') return text;
      }}
      try {{
        const s = String(value);
        return s === '[object Object]' ? '' : s;
      }} catch (e) {{
        return '';
      }}
    }}
    function cellVal(cell){{
      if(cell == null) return '';
      const candidates = [
        cell.displayValue,
        cell.showValue,
        cell.text,
        cell.payload?.displayValue,
        cell.payload?.showValue,
        cell.payload?.text,
        cell.payload?.editValue,
        cell.value,
        cell.v,
        cell.payload?.value,
        cell.payload?.v
      ];
      for (const c of candidates) {{
        if (c == null) continue;
        const s = richTextToPlain(c);
        if (s) return s;
      }}
      return '';
    }}
    return cells.map(row => (row||[]).map(cellVal));
  }}, sheetName);

  function isFilled(v) {{
    return String(v ?? '').trim() !== '';
  }}

  function trimMatrix(matrix, options = {{}}) {{
    const keepHeaderRows = options.keepHeaderRows || 0;
    const preserveRowCount = !!options.preserveRowCount;
    if (!Array.isArray(matrix) || matrix.length === 0) {{
      return {{
        matrix: [['']],
        trim: {{ top: 0, bottom: 0, left: 0, right: 0 }},
      }};
    }}
    const rowCount = matrix.length;
    const colCount = Math.max(...matrix.map(r => (r || []).length), 0);
    if (colCount === 0) {{
      return {{
        matrix: [['']],
        trim: {{ top: 0, bottom: Math.max(rowCount - 1, 0), left: 0, right: 0 }},
      }};
    }}

    const rowHas = Array.from({{ length: rowCount }}, (_, r) => {{
      const row = matrix[r] || [];
      for (let c = 0; c < colCount; c++) {{
        if (isFilled(row[c])) return true;
      }}
      return false;
    }});

    const colHas = Array.from({{ length: colCount }}, (_, c) => {{
      for (let r = 0; r < rowCount; r++) {{
        const row = matrix[r] || [];
        if (isFilled(row[c])) return true;
      }}
      return false;
    }});

    let top = keepHeaderRows;
    let bottom = rowCount - 1;
    let headerTop = 0;
    if (!preserveRowCount) {{
      while (top < rowCount && !rowHas[top]) top++;
      while (bottom >= keepHeaderRows && !rowHas[bottom]) bottom--;
      if (keepHeaderRows > 0) top = Math.min(top, keepHeaderRows);
      if (bottom < top) {{
        top = 0;
        bottom = Math.max(Math.min(keepHeaderRows, rowCount) - 1, 0);
      }}
    }} else {{
      headerTop = 0;
      top = 0;
      bottom = rowCount - 1;
    }}

    let left = 0;
    while (left < colCount && !colHas[left]) left++;
    let right = colCount - 1;
    while (right >= 0 && !colHas[right]) right--;
    if (right < left) {{
      left = 0;
      right = 0;
    }}

    const trimmed = matrix.slice(headerTop, bottom + 1).map(row => {{
      const src = row || [];
      const part = src.slice(left, right + 1);
      return part.length ? part : [''];
    }});

    return {{
      matrix: trimmed.length ? trimmed : [['']],
      trim: {{
        top: top,
        bottom: rowCount - 1 - bottom,
        left: left,
        right: colCount - 1 - right,
      }},
      bounds: {{ left, right, top: headerTop, bottom }}
    }};
  }}

  function excelColName(indexZeroBased) {{
    let n = indexZeroBased + 1;
    let s = '';
    while (n > 0) {{
      const m = (n - 1) % 26;
      s = String.fromCharCode(65 + m) + s;
      n = Math.floor((n - 1) / 26);
    }}
    return s;
  }}

  function looksLikeDateHeader(text) {{
    const s = String(text || '').replace(/\s+/g, '').toLowerCase();
    if (!s) return false;
    return s.includes('时间') || s.includes('日期') || s.includes('date') || s.includes('time');
  }}

  function formatSerialDateValue(value) {{
    const raw = String(value ?? '').trim();
    if (!raw) return raw;
    if (!/^\d+(\.\d+)?$/.test(raw)) return raw;
    const num = Number(raw);
    if (!Number.isFinite(num)) return raw;
    if (num < 20000 || num > 80000) return raw;

    const wholeDays = Math.floor(num);
    const dayFraction = num - wholeDays;
    const excelEpochUtc = Date.UTC(1899, 11, 30);
    const ms = excelEpochUtc + wholeDays * 86400000 + Math.round(dayFraction * 86400000);
    const d = new Date(ms);
    if (Number.isNaN(d.getTime())) return raw;

    const pad = (n) => String(n).padStart(2, '0');
    const yyyy = d.getUTCFullYear();
    const mm = pad(d.getUTCMonth() + 1);
    const dd = pad(d.getUTCDate());
    const hh = pad(d.getUTCHours());
    const mi = pad(d.getUTCMinutes());
    const ss = pad(d.getUTCSeconds());
    if (hh === '00' && mi === '00' && ss === '00') return `${{yyyy}}-${{mm}}-${{dd}}`;
    if (ss === '00') return `${{yyyy}}-${{mm}}-${{dd}} ${{hh}}:${{mi}}`;
    return `${{yyyy}}-${{mm}}-${{dd}} ${{hh}}:${{mi}}:${{ss}}`;
  }}

  function findHeaderRowIndex(matrix, targetName) {{
    const target = String(targetName || '').trim();
    if (!target) return 0;
    const limit = Math.min(Array.isArray(matrix) ? matrix.length : 0, 8);
    for (let i = 0; i < limit; i++) {{
      const row = matrix[i] || [];
      const idx = row.findIndex(x => String(x || '').trim() === target);
      if (idx !== -1) return i;
    }}
    return 0;
  }}

  const headerRowIndex = mode === 'filter_capture' && filterEnabled
    ? findHeaderRowIndex(data, filterColName)
    : 0;
  const headerRows = data.slice(0, headerRowIndex + 1);
  const header = headerRows[headerRows.length - 1] || [];
  let rows = data.slice(headerRowIndex + 1);
  let filterIndex = -1;
  if (mode === 'filter_capture' && filterEnabled) {{
    filterIndex = header.findIndex(x => String(x || '').trim() === String(filterColName).trim());
    if (filterIndex === -1) throw new Error('filter column not found: ' + filterColName);
    rows = rows.filter(r => String(r[filterIndex] || '').trim() === String(filterEquals).trim());
  }}

  const startCol = rangeInfo.start_col;
  const endCol = rangeInfo.end_col;
  let workingRows = [...headerRows, ...rows];
  let absoluteStartCol = startCol;
  let absoluteEndCol = endCol;
  let dataStartRowNumber = 2;

  if (mode === 'direct_range' && rangeInfo.has_rows) {{
    const startRow = rangeInfo.start_row;
    const endRow = rangeInfo.end_row;
    workingRows = data.slice(startRow, endRow + 1);
    absoluteStartCol = startCol;
    absoluteEndCol = endCol;
    dataStartRowNumber = startRow + 1;
  }}

  let subset = workingRows.map((r, rowIdx) => {{
    const arr = [];
    for (let c = startCol; c <= endCol; c++) {{
      let cellValue = (r || [])[c] ?? '';
      if (rowIdx >= headerRows.length) {{
        const headerText = header[c] ?? '';
        if (looksLikeDateHeader(headerText)) {{
          cellValue = formatSerialDateValue(cellValue);
        }}
      }}
      arr.push(cellValue);
    }}
    return arr;
  }});

  const trimmedResult = trimMatrix(subset, {{ keepHeaderRows: mode === 'filter_capture' ? headerRows.length : 0, preserveRowCount: mode === 'filter_capture' }});
  subset = trimmedResult.matrix;
  const trimmedStartCol = absoluteStartCol + trimmedResult.bounds.left;
  const trimmedEndCol = absoluteStartCol + trimmedResult.bounds.right;

  const colLabels = [];
  for (let c = trimmedStartCol; c <= trimmedEndCol; c++) {{
    colLabels.push(excelColName(c));
  }}

  const trimmedRangeText = mode === 'direct_range'
    ? `${{excelColName(trimmedStartCol)}}${{dataStartRowNumber + trimmedResult.bounds.top}}:${{excelColName(trimmedEndCol)}}${{dataStartRowNumber + trimmedResult.bounds.bottom}}`
    : `${{excelColName(trimmedStartCol)}}:${{excelColName(trimmedEndCol)}}`;

  function esc(s){{ return String(s ?? '').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/\"/g,'&quot;'); }}
  function visualLen(s){{
    s = String(s || '');
    let len = 0;
    for (const ch of s) len += ch.charCodeAt(0) > 255 ? 1.7 : 1;
    return len;
  }}

  const widths = (subset[0] || []).map((_, cIdx) => {{
    const vals = subset.map(r => String((r || [])[cIdx] || ''));
    const longest = Math.max(...vals.map(v => {{
      const firstLine = String(v || '').split(String.fromCharCode(10)).sort((a,b)=>visualLen(b)-visualLen(a))[0] || '';
      return visualLen(firstLine);
    }}), 8);
    let px = 70 + longest * 7.5;
    if (!optimizeWidth) px = 160;
    px = Math.max(90, Math.min(420, Math.round(px)));
    return px;
  }});

  const totalWidth = widths.reduce((a,b)=>a+b,0);
  const viewportWidth = Math.min(Math.max(totalWidth + 48, 900), viewportMaxWidth);
  const resultRows = mode === 'filter_capture' ? Math.max(subset.length - headerRows.length, 0) : subset.length;
  const title = mode === 'filter_capture'
    ? `${{titlePrefix}}_${{sheetName}}_${{filterEnabled ? filterColName + '_' + filterEquals + '_' : ''}}${{trimmedRangeText.replace(':','-')}}`
    : `${{titlePrefix}}_${{sheetName}}_区域_${{trimmedRangeText.replace(':','-')}}`;

  if (mode === 'filter_capture' && filterEnabled && resultRows === 0) {{
    console.log(JSON.stringify({{
      title,
      htmlPath: '',
      imagePath: '',
      resultRows,
      emptyResult: true,
      mode,
      filterEnabled,
      filterColName,
      filterEquals,
      range: trimmedRangeText,
      trim: trimmedResult.trim,
      style: captureStyle,
      browserTarget: {{
        tabKeyword,
        matchedUrl: winner ? winner.url : page.url(),
        matchedTitle: winner ? winner.title : '',
        matchScore: winner ? winner.score : 0,
        usedIframe: !!(winner && winner.runtime && winner.runtime.useFrame),
        frameUrl: winner && winner.runtime && winner.runtime.frameUrl ? winner.runtime.frameUrl : ''
      }}
    }}, null, 2));
    await browser.close();
    return;
  }}

  const tableHtml = mode === 'filter_capture'
    ? `<table><colgroup>${{widths.map(w=>`<col style="width:${{w}}px">`).join('')}}</colgroup><thead>
      <tr>${{colLabels.map(x=>`<th class="colhead">${{x}}</th>`).join('')}}</tr>
      ${{subset.slice(0, headerRows.length).map(r=>`<tr>${{r.map(x=>`<th>${{esc(x)}}</th>`).join('')}}</tr>`).join('')}}</thead><tbody>
      ${{subset.slice(headerRows.length).map(r=>`<tr>${{r.map(v=>`<td>${{esc(v)}}</td>`).join('')}}</tr>`).join('')}}
      </tbody></table>`
    : `<table><colgroup>${{widths.map(w=>`<col style="width:${{w}}px">`).join('')}}</colgroup><tbody>
      ${{subset.map(r=>`<tr>${{r.map(v=>`<td>${{esc(v)}}</td>`).join('')}}</tr>`).join('')}}
      </tbody></table>`;

  const showMeta = captureStyle !== 'compact';
  const wrapPadding = showMeta ? 6 : 2;
  const html = `<!doctype html><html><head><meta charset="utf-8"><title>${{title}}</title>
  <style>
    html,body{{margin:0;padding:0;background:#fff;}}
    body{{font-family:"Microsoft YaHei","PingFang SC",sans-serif;color:#222;display:inline-block;}}
    .wrap{{display:inline-block;padding:${{wrapPadding}}px;box-sizing:border-box;}}
    .meta{{margin:0 0 6px 0;line-height:1.45;font-size:13px;display:${{showMeta ? 'block' : 'none'}};}}
    .meta div{{margin:0;}}
    table{{border-collapse:collapse;table-layout:fixed;width:${{totalWidth}}px;font-size:${{fontSize}}px}}
    th,td{{border:1px solid #d9d9d9;padding:6px 8px;vertical-align:top;white-space:pre-wrap;word-break:break-word;line-height:1.35}}
    thead tr:nth-child(1) th{{background:#f5f7fa;font-weight:700;text-align:center;font-size:10px}}
    thead tr:nth-child(n+2) th{{background:#fafafa;font-weight:700;font-size:${{fontSize}}px}}
    tbody tr:nth-child(even){{background:#fcfcfc}}
    .colhead{{font-size:10px;color:#667}}
  </style></head><body>
  <div class="wrap">
    <div class="meta"><div><b>Sheet：</b>${{sheetName}}</div><div><b>模式：</b>${{mode === 'filter_capture' ? '筛选截图' : '直接区域截图'}}</div><div><b>筛选：</b>${{mode === 'filter_capture' && filterEnabled ? filterColName + ' = ' + filterEquals : '未启用'}}</div><div><b>原始范围：</b>${{rangeInfo.raw}}</div><div><b>实际截图范围：</b>${{trimmedRangeText}}</div><div><b>结果行数：</b>${{mode === 'filter_capture' ? Math.max(subset.length - headerRows.length, 0) : subset.length}}</div><div><b>表头行数：</b>${{mode === 'filter_capture' ? headerRows.length : 0}}</div><div><b>自动裁边：</b>上${{trimmedResult.trim.top}} / 下${{trimmedResult.trim.bottom}} / 左${{trimmedResult.trim.left}} / 右${{trimmedResult.trim.right}}</div><div><b>说明：</b>${{optimizeWidth ? '已自动优化列宽' : '固定列宽'}} / ${{showMeta ? '标准版' : '紧凑版'}}</div></div>
    ${{tableHtml}}
  </div></body></html>`;

  const htmlPath = path.join(outDir, title + '.html');
  fs.writeFileSync(htmlPath, html, 'utf8');
  const renderPage = await browser.newPage();
  await renderPage.setViewportSize({{ width: viewportWidth, height: 900 }});
  await renderPage.goto(pathToFileURL(htmlPath).href);
  await renderPage.evaluate(() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve))));
  const bodyMetrics = await renderPage.evaluate(() => {{
    const el = document.querySelector('body > .wrap') || document.body;
    const doc = document.documentElement;
    const body = document.body;
    return {{
      scrollWidth: Math.max(doc ? doc.scrollWidth : 0, body ? body.scrollWidth : 0, el ? el.scrollWidth : 0, 1),
      scrollHeight: Math.max(doc ? doc.scrollHeight : 0, body ? body.scrollHeight : 0, el ? el.scrollHeight : 0, 1)
    }};
  }});
  await renderPage.setViewportSize({{
    width: Math.max(1, Math.min(viewportWidth, bodyMetrics.scrollWidth)),
    height: Math.max(900, Math.min(bodyMetrics.scrollHeight, 30000))
  }});
  const imagePath = path.join(outDir, title + '.png');
  const wrapLocator = renderPage.locator('body > .wrap');
  if (await wrapLocator.count()) {{
    await wrapLocator.screenshot({{ path: imagePath }});
  }} else {{
    await renderPage.screenshot({{ path: imagePath, fullPage: true }});
  }}
  console.log(JSON.stringify({{
    title,
    htmlPath,
    imagePath,
    resultRows,
    mode,
    filterEnabled,
    filterColName,
    filterEquals,
    range: trimmedRangeText,
    trim: trimmedResult.trim,
    style: captureStyle,
    browserTarget: {{
      tabKeyword,
      matchedUrl: winner ? winner.url : page.url(),
      matchedTitle: winner ? winner.title : '',
      matchScore: winner ? winner.score : 0,
      usedIframe: !!(winner && winner.runtime && winner.runtime.useFrame),
      frameUrl: winner && winner.runtime && winner.runtime.frameUrl ? winner.runtime.frameUrl : ''
    }}
  }}, null, 2));
  await browser.close();
}})().catch(e=>{{ console.error(e); process.exit(1); }});
"""


def capture_from_alidocs(task):
    ensure_dir(task['output_dir'])
    script_content = _render_script(task)
    with tempfile.NamedTemporaryFile('w', suffix='.js', delete=False, encoding='utf-8') as f:
        f.write(script_content)
        temp_script = Path(f.name)
    try:
        proc = subprocess.run(
            ['node', str(temp_script)],
            capture_output=True,
            text=True,
            encoding='utf-8',
            errors='replace',
            timeout=180,
        )
        if proc.returncode != 0:
            stderr_text = (proc.stderr or '').strip()
            stdout_text = (proc.stdout or '').strip()
            detail = stderr_text or stdout_text or f'Node capture script exited with code {proc.returncode} without output.'
            raise RuntimeError(detail)
        lines = [x for x in proc.stdout.splitlines() if x.strip()]
        payload = json.loads('\n'.join(lines))
        return payload
    finally:
        try:
            temp_script.unlink(missing_ok=True)
        except Exception:
            pass
