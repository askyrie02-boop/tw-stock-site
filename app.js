// 表格排序：點欄位名稱排序，再點一次反過來。數字（含 +、%、億、逗號、▲▼）依數值大小，文字依筆畫；空白與「—」永遠排最後
(function () {
  function value(cell) {
    var s = cell.textContent.replace(/[\s,⭐]/g, "");
    var sign = 1;
    if (s.charAt(0) === "▲") { s = s.slice(1); }
    else if (s.charAt(0) === "▼") { s = s.slice(1); sign = -1; }
    var m = s.match(/^([+-]?)(\d+(?:\.\d+)?)/);
    if (m) { return { num: sign * (m[1] === "-" ? -1 : 1) * parseFloat(m[2]) }; }
    if (s === "" || s === "—" || s === "-") { return { empty: true }; }
    return { text: cell.textContent.trim() };
  }
  function compare(a, b, dir) {
    if (a.empty || b.empty) { return (a.empty ? 1 : 0) - (b.empty ? 1 : 0); }
    if (a.num !== undefined && b.num !== undefined) { return dir * (a.num - b.num); }
    if (a.num !== undefined) { return -1; }
    if (b.num !== undefined) { return 1; }
    return dir * a.text.localeCompare(b.text, "zh-Hant");
  }
  document.querySelectorAll("table").forEach(function (table) {
    var body = table.tBodies[0];
    if (!table.tHead || !body || body.rows.length < 2) { return; }
    var heads = Array.prototype.slice.call(table.tHead.rows[0].cells);
    heads.forEach(function (th, col) {
      th.classList.add("sortable");
      th.tabIndex = 0;
      th.title = "點一下排序";
      function sort() {
        var rows = Array.prototype.slice.call(body.rows);
        var numeric = rows.some(function (r) { return r.cells[col] && value(r.cells[col]).num !== undefined; });
        var dir = th.dataset.dir === "desc" ? 1 : th.dataset.dir === "asc" ? -1 : (numeric ? -1 : 1);
        heads.forEach(function (h) { delete h.dataset.dir; });
        th.dataset.dir = dir === -1 ? "desc" : "asc";
        rows.map(function (r, i) { return { row: r, key: r.cells[col] ? value(r.cells[col]) : { empty: true }, i: i }; })
          .sort(function (a, b) { return compare(a.key, b.key, dir) || a.i - b.i; })
          .forEach(function (x) { body.appendChild(x.row); });
      }
      th.addEventListener("click", sort);
      th.addEventListener("keydown", function (e) {
        if (e.key === "Enter" || e.key === " ") { e.preventDefault(); sort(); }
      });
    });
  });
  // 下拉選單切換區塊（範本選股：一次看一個範本）
  document.querySelectorAll("select[data-pick]").forEach(function (sel) {
    var parts = document.querySelectorAll("section[data-pick='" + sel.dataset.pick + "']");
    function show() {
      parts.forEach(function (p) { p.hidden = sel.value !== "all" && p.dataset.key !== sel.value; });
    }
    sel.addEventListener("change", show);
    show();
    // 個股追蹤：上方總表的代號可以點，直接切到那一檔
    if (sel.dataset.pick === "track") {
      var byCode = {};
      Array.prototype.forEach.call(sel.options, function (o) { byCode[o.textContent.split(" ")[0]] = o.value; });
      document.querySelectorAll("table tbody tr td:first-child").forEach(function (td) {
        var key = byCode[td.textContent.trim()];
        if (!key || td.closest("section[data-pick]")) { return; }
        td.innerHTML = '<a class="pick-to" href="#">' + td.innerHTML + "</a>";
        td.firstChild.addEventListener("click", function (e) {
          e.preventDefault();
          sel.value = key;
          show();
          var sec = document.querySelector("section[data-pick='track'][data-key='" + key + "']");
          if (sec) { sec.scrollIntoView({ behavior: "smooth", block: "start" }); }
        });
      });
    }
  });
})();
// 族群個股：點股票代號，讀 assets/stocks/<代號>.json，畫日 K（5／20／60 日均線、成交量）、法人買賣超、融資融券
(function () {
  var panel = document.getElementById("stock-panel");
  if (!panel) { return; }
  var WEEK = "日一二三四五六";
  var SHOW = 120;
  function num(x, d) {
    if (x === null || x === undefined || isNaN(x)) { return "—"; }
    d = d === undefined ? 2 : d;
    return Number(x).toLocaleString("zh-TW", { minimumFractionDigits: 0, maximumFractionDigits: d });
  }
  function signed(x, d, unit) {
    if (x === null || x === undefined || isNaN(x)) { return "—"; }
    var cls = x > 0 ? "up" : x < 0 ? "down" : "";
    return '<span class="' + cls + '">' + (x > 0 ? "+" : "") + num(x, d) + (unit || "") + "</span>";
  }
  function esc(s) {
    return String(s).replace(/[&<>"]/g, function (c) { return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]; });
  }
  function day(s) {
    var d = new Date(s + "T00:00:00");
    return (d.getMonth() + 1) + "/" + d.getDate() + "（" + WEEK.charAt(d.getDay()) + "）";
  }
  function average(bars, n) {
    var out = [], sum = 0;
    for (var i = 0; i < bars.length; i++) {
      sum += bars[i][4];
      if (i >= n) { sum -= bars[i - n][4]; }
      out.push(i >= n - 1 ? sum / n : null);
    }
    return out;
  }
  function niceTicks(lo, hi, count) {
    var span = hi - lo || Math.abs(hi) || 1, raw = span / count;
    var mag = Math.pow(10, Math.floor(Math.log(raw) / Math.LN10)), step = mag;
    [1, 2, 2.5, 5, 10].some(function (m) { step = m * mag; return step >= raw; });
    var out = [];
    for (var v = Math.ceil(lo / step) * step; v <= hi + step * 1e-9; v += step) { out.push(+v.toFixed(6)); }
    return out;
  }
  function line(points, cls) {
    var d = "", pen = false;
    points.forEach(function (p) {
      if (p === null) { pen = false; return; }
      d += (pen ? "L" : "M") + p[0].toFixed(1) + " " + p[1].toFixed(1);
      pen = true;
    });
    return d ? '<path class="' + cls + '" d="' + d + '"/>' : "";
  }
  function width() { return Math.max(300, Math.min(panel.clientWidth || 600, 860)); }

  function kchart(data) {
    var all = data.bars, n = Math.min(SHOW, all.length), start = all.length - n, bars = all.slice(start);
    var mas = [5, 20, 60].map(function (k) { return average(all, k).slice(start); });
    var W = width(), H = 340, R = 56, top = 10, bottom = 222, vtop = 240, vbottom = 304;
    var step = (W - R) / n, body = Math.max(1, step * 0.62);
    var lo = Infinity, hi = -Infinity, vmax = 0;
    bars.forEach(function (b, i) {
      lo = Math.min(lo, b[3]); hi = Math.max(hi, b[2]); vmax = Math.max(vmax, b[5] || 0);
      mas.forEach(function (m) { if (m[i] !== null) { lo = Math.min(lo, m[i]); hi = Math.max(hi, m[i]); } });
    });
    var pad = (hi - lo) * 0.04 || hi * 0.02 || 1;
    lo -= pad; hi += pad;
    function y(v) { return bottom - (v - lo) / (hi - lo) * (bottom - top); }
    function vy(v) { return vbottom - (v || 0) / (vmax || 1) * (vbottom - vtop); }
    function x(i) { return (i + 0.5) * step; }
    var s = "";
    niceTicks(lo, hi, 4).forEach(function (t) {
      s += '<line class="grid" x1="0" x2="' + (W - R) + '" y1="' + y(t) + '" y2="' + y(t) + '"/>';
      s += '<text class="axis" x="' + (W - R + 6) + '" y="' + (y(t) + 4) + '">' + num(t) + "</text>";
    });
    var month = "";
    bars.forEach(function (b, i) {
      var m = b[0].slice(0, 7);
      if (m !== month && i > 2) {
        s += '<line class="grid" x1="' + x(i) + '" x2="' + x(i) + '" y1="' + top + '" y2="' + vbottom + '"/>';
        s += '<text class="axis" x="' + (x(i) + 3) + '" y="' + (vbottom + 16) + '">' + (+b[0].slice(5, 7)) + "月</text>";
      }
      month = m;
    });
    bars.forEach(function (b, i) {
      var prev = i ? bars[i - 1][4] : (start ? all[start - 1][4] : b[1]);
      var cls = b[4] > b[1] ? "k-up" : b[4] < b[1] ? "k-down" : (b[4] >= prev ? "k-up" : "k-down");
      var y1 = y(Math.max(b[1], b[4])), y2 = y(Math.min(b[1], b[4]));
      s += '<line class="' + cls + '" x1="' + x(i) + '" x2="' + x(i) + '" y1="' + y(b[2]) + '" y2="' + y(b[3]) + '"/>';
      s += '<rect class="' + cls + '" x="' + (x(i) - body / 2) + '" y="' + y1 + '" width="' + body + '" height="' + Math.max(1, y2 - y1) + '"/>';
      var vcls = b[4] >= prev ? "k-up" : "k-down";
      s += '<rect class="' + vcls + ' vol" x="' + (x(i) - body / 2) + '" y="' + vy(b[5]) + '" width="' + body + '" height="' + Math.max(0, vbottom - vy(b[5])) + '"/>';
    });
    ["ma5", "ma20", "ma60"].forEach(function (cls, k) {
      s += line(mas[k].map(function (v, i) { return v === null ? null : [x(i), y(v)]; }), "ma " + cls);
    });
    s += '<text class="axis" x="' + (W - R + 6) + '" y="' + (vtop + 8) + '">' + num(vmax / 1000, 1) + "千張</text>";
    s += '<line class="cursor" x1="0" x2="0" y1="' + top + '" y2="' + vbottom + '" visibility="hidden"/>';
    s += '<rect class="hit" x="0" y="0" width="' + (W - R) + '" height="' + H + '"/>';
    return { svg: '<svg class="chart" viewBox="0 0 ' + W + " " + H + '" width="100%" role="img" aria-label="日K圖">' + s + "</svg>",
             bars: bars, all: all, start: start, mas: mas, step: step, x: x, W: W };
  }

  function info(k, i) {
    var b = k.bars[i], prev = i ? k.bars[i - 1][4] : (k.start ? k.all[k.start - 1][4] : null);
    var chg = prev ? b[4] - prev : null;
    var pct = prev ? (b[4] / prev - 1) * 100 : null;
    return "<b>" + day(b[0]) + "</b>　開 " + num(b[1]) + "｜高 " + num(b[2]) + "｜低 " + num(b[3]) + "｜收 " + num(b[4])
      + "　" + signed(chg) + "（" + signed(pct, 2, "%") + "）　量 " + num(b[5], 0) + " 張"
      + '<br><span class="ma5-t">MA5 ' + num(k.mas[0][i]) + '</span>　<span class="ma20-t">MA20 ' + num(k.mas[1][i])
      + '</span>　<span class="ma60-t">MA60 ' + num(k.mas[2][i]) + "</span>";
  }

  function bind(box, k) {
    var svg = box.querySelector("svg"), out = box.querySelector(".kinfo"), cur = svg.querySelector(".cursor");
    function at(ev) {
      var r = svg.getBoundingClientRect(), px = (ev.clientX - r.left) * k.W / r.width;
      var i = Math.max(0, Math.min(k.bars.length - 1, Math.floor(px / k.step)));
      cur.setAttribute("x1", k.x(i)); cur.setAttribute("x2", k.x(i)); cur.setAttribute("visibility", "visible");
      out.innerHTML = info(k, i);
    }
    svg.addEventListener("pointermove", at);
    svg.addEventListener("pointerdown", at);
    svg.addEventListener("pointerleave", function () {
      cur.setAttribute("visibility", "hidden");
      out.innerHTML = info(k, k.bars.length - 1);
    });
  }

  function sum(rows, col, n) {
    var part = rows.slice(-n), total = 0, any = false;
    part.forEach(function (r) { if (r[col] !== null) { total += r[col]; any = true; } });
    return any ? total : null;
  }
  function streak(rows, col) {
    var n = 0, sign = 0;
    for (var i = rows.length - 1; i >= 0; i--) {
      var v = rows[i][col];
      if (v === null || v === 0) { break; }
      var sg = v > 0 ? 1 : -1;
      if (sign && sg !== sign) { break; }
      sign = sg; n++;
    }
    return n >= 2 ? (sign > 0 ? "連買 " : "連賣 ") + n + " 天" : "";
  }

  function insti(rows) {
    var list = rows.filter(function (r) { return r[1] !== null || r[2] !== null || r[3] !== null; });
    if (!list.length) { return '<p class="note">沒有法人買賣超資料。</p>'; }
    var totals = list.map(function (r) { return (r[1] || 0) + (r[2] || 0) + (r[3] || 0); });
    var W = width(), H = 150, mid = 75, step = W / totals.length, bw = Math.max(1, step * 0.62);
    var mx = Math.max.apply(null, totals.map(Math.abs)) || 1, s = "";
    s += '<line class="grid" x1="0" x2="' + W + '" y1="' + mid + '" y2="' + mid + '"/>';
    totals.forEach(function (v, i) {
      var h = Math.abs(v) / mx * (mid - 8), cls = v >= 0 ? "k-up" : "k-down";
      s += '<rect class="' + cls + '" x="' + (i * step + (step - bw) / 2) + '" y="' + (v >= 0 ? mid - h : mid) + '" width="' + bw + '" height="' + Math.max(0.5, h) + '"><title>' + list[i][0] + "：" + num(v, 0) + " 張</title></rect>";
    });
    var head = "<tr><th>期間</th><th>外資</th><th>投信</th><th>自營商</th><th>合計</th></tr>";
    var body = [[1, "今天 " + day(list[list.length - 1][0])], [5, "近 5 日"], [20, "近 20 日"], [list.length, "近 " + list.length + " 日"]]
      .filter(function (p, i, a) { return i === 0 || p[0] <= list.length && p[0] !== a[i - 1][0]; })
      .map(function (p) {
        var f = sum(list, 1, p[0]), t = sum(list, 2, p[0]), d = sum(list, 3, p[0]);
        return "<tr><td>" + p[1] + "</td><td>" + signed(f, 0) + "</td><td>" + signed(t, 0) + "</td><td>" + signed(d, 0)
          + "</td><td>" + signed((f || 0) + (t || 0) + (d || 0), 0) + "</td></tr>";
      }).join("");
    var tags = [["外資", streak(list, 1)], ["投信", streak(list, 2)]].filter(function (p) { return p[1]; })
      .map(function (p) { return p[0] + p[1]; }).join("、");
    return '<svg class="chart" viewBox="0 0 ' + W + " " + H + '" width="100%" role="img" aria-label="法人買賣超">' + s + "</svg>"
      + '<p class="note">每天三大法人合計買賣超（張），紅色買超、綠色賣超' + (tags ? "｜" + tags : "") + "</p>"
      + '<div class="table-wrap"><table><thead>' + head + "</thead><tbody>" + body + "</tbody></table></div>";
  }

  function mini(rows, col, label) {
    var pts = rows.filter(function (r) { return r[col] !== null; });
    if (pts.length < 2) { return '<div><p class="note">' + label + "：沒有資料</p></div>"; }
    var W = Math.max(150, Math.floor((width() - 12) / 2)), H = 130, top = 12, bottom = 112;
    var vals = pts.map(function (r) { return r[col]; });
    var lo = Math.min.apply(null, vals), hi = Math.max.apply(null, vals);
    var step = W / (pts.length - 1 || 1);
    function y(v) { return hi === lo ? (top + bottom) / 2 : bottom - (v - lo) / (hi - lo) * (bottom - top); }
    var last = vals[vals.length - 1];
    function diff(n) { return vals.length > n ? last - vals[vals.length - 1 - n] : null; }
    var s = line(vals.map(function (v, i) { return [i * step, y(v)]; }), "ma spark");
    s += '<text class="axis" x="2" y="' + (top - 1) + '">' + num(hi, 0) + "</text>";
    s += '<text class="axis" x="2" y="' + (bottom + 14) + '">' + num(lo, 0) + "</text>";
    return "<div><p><b>" + label + "</b> " + num(last, 0) + ' 張<br><span class="note">5 日 ' + signed(diff(5), 0)
      + "｜20 日 " + signed(diff(20), 0) + "</span></p>"
      + '<svg class="chart" viewBox="0 0 ' + W + " " + H + '" width="100%" role="img" aria-label="' + label + '">' + s + "</svg></div>";
  }

  function margin(rows) {
    var list = rows.filter(function (r) { return r[4] !== null || r[5] !== null; });
    if (!list.length) { return '<p class="note">沒有融資融券資料（ETF、部分股票不能信用交易）。</p>'; }
    var last = list[list.length - 1], ratio = last[4] ? last[5] / last[4] * 100 : null;
    return '<div class="pair">' + mini(list, 4, "融資餘額") + mini(list, 5, "融券餘額") + "</div>"
      + '<p class="note">' + day(last[0]) + " 券資比 " + (ratio === null ? "—" : num(ratio, 1) + "%") + "（融券 ÷ 融資）</p>";
  }

  function brokerTable(rows, sell) {
    if (!rows || !rows.length) { return '<p class="note">' + (sell ? "沒有賣超分點" : "沒有買超分點") + "</p>"; }
    var head = "<tr><th>" + (sell ? "賣超分點" : "買超分點") + "</th><th>" + (sell ? "賣超" : "買超") + "（張）</th><th>買進</th><th>賣出</th><th>佔成交</th></tr>";
    var body = rows.map(function (r) {
      var name = esc(r[0]);
      if (r[5] && /^https:\/\/(fubon-ebrokerdj\.fbs\.com\.tw|concords\.moneydj\.com|jdata\.yuanta\.com\.tw)\//.test(r[5])) {
        name = '<a href="' + esc(r[5]) + '" target="_blank" rel="noopener noreferrer">' + name + "</a>";
      }
      return "<tr><td>" + name + "</td><td>" + signed(sell ? -r[3] : r[3], 0) + "</td><td>" + num(r[1], 0) + "</td><td>"
        + num(r[2], 0) + "</td><td>" + (r[4] === null ? "—" : num(r[4], 2) + "%") + "</td></tr>";
    }).join("");
    return '<div class="table-wrap"><table><thead>' + head + "</thead><tbody>" + body + "</tbody></table></div>";
  }

  function brokers(b) {
    if (!b || !b.periods || !Object.keys(b.periods).length) {
      return '<p class="note">這檔股票這次沒有抓到分點資料（來源網站讀不到時會在下次更新補上）。</p>';
    }
    var keys = Object.keys(b.periods).sort(function (x, y) { return x - y; });
    var tabs = keys.map(function (k) { return '<button type="button" class="seg" data-p="' + esc(k) + '">近 ' + esc(k) + " 日</button>"; }).join("");
    var parts = keys.map(function (k) {
      var p = b.periods[k];
      var span = (p.start === p.end ? day(p.end) : day(p.start) + "～" + day(p.end));
      var late = p.updated && p.updated !== p.end ? "｜⚠️ 來源只更新到 " + day(p.updated) : "";
      return '<div class="bk" data-p="' + esc(k) + '" hidden><p class="note">' + span + late + "</p>"
        + brokerTable(p.buy, false) + brokerTable(p.sell, true) + "</div>";
    }).join("");
    return '<div class="segs" role="tablist">' + tabs + "</div>" + parts
      + '<p class="note">資料來源：MoneyDJ 主力進出（富邦、康和、元大券商網站，<b>非官方資料</b>，僅供參考）。'
      + "點分點名稱可以看該分點每天的進出；外資券商（例如摩根大通、美林）常是外資買賣，不一定是特定大戶。</p>";
  }

  function bindBrokers(box) {
    var btns = box.querySelectorAll(".seg"), parts = box.querySelectorAll(".bk");
    if (!btns.length) { return; }
    function pick(k) {
      btns.forEach(function (b) { b.classList.toggle("on", b.dataset.p === k); b.setAttribute("aria-selected", b.dataset.p === k); });
      parts.forEach(function (p) { p.hidden = p.dataset.p !== k; });
    }
    btns.forEach(function (b) { b.addEventListener("click", function () { pick(b.dataset.p); }); });
    var keys = Array.prototype.map.call(btns, function (b) { return b.dataset.p; });
    pick(keys.indexOf("5") >= 0 ? "5" : keys[0]);
  }

  function render(data) {
    var b = data.bars, last = b[b.length - 1], prev = b.length > 1 ? b[b.length - 2][4] : null;
    var groups = (panel.dataset.groups ? JSON.parse(panel.dataset.groups) : {})[data.code] || [];
    var k = kchart(data);
    panel.innerHTML = '<h2 id="stock-title">' + esc(data.code + " " + data.name) + "　" + num(last[4])
      + "　" + (prev ? signed(last[4] - prev) + "（" + signed((last[4] / prev - 1) * 100, 2, "%") + "）" : "") + "</h2>"
      + '<p class="note">' + [data.market, data.industry, groups.length ? "族群：" + groups.join("、") : ""].filter(Boolean).map(esc).join("｜")
      + "｜資料日期 " + day(data.day) + '　<a href="#top-picker">↑ 回到族群列表</a></p>'
      + '<h3>日K（近 ' + k.bars.length + ' 個交易日，還原股價）</h3><div class="kbox"><p class="kinfo">' + info(k, k.bars.length - 1) + "</p>" + k.svg
      + '<p class="note">手指按住圖表左右移動可以看每一天的開高低收；紅K上漲、綠K下跌，下方是成交量</p></div>'
      + "<h3>法人買賣超</h3>" + insti(data.chips)
      + "<h3>融資融券</h3>" + margin(data.chips)
      + '<h3>主力分點</h3><div class="bkbox">' + brokers(data.brokers) + "</div>";
    bind(panel.querySelector(".kbox"), k);
    bindBrokers(panel.querySelector(".bkbox"));
  }

  var cache = {};
  function show(code) {
    panel.hidden = false;
    document.querySelectorAll("a[data-stock]").forEach(function (a) {
      a.closest("tr").classList.toggle("picked", a.dataset.stock === code);
    });
    panel.innerHTML = '<p class="note">讀取 ' + esc(code) + " 的資料…</p>";
    var load = cache[code] || fetch("assets/stocks/" + encodeURIComponent(code) + ".json").then(function (r) {
      if (!r.ok) { throw new Error(r.status); }
      return r.json();
    });
    cache[code] = load;
    load.then(function (data) {
      render(data);
      panel.scrollIntoView({ behavior: "smooth", block: "start" });
    }).catch(function () {
      delete cache[code];
      panel.innerHTML = '<p class="note">' + esc(code) + " 的資料讀不到（可能是網路不穩，或這檔股票今天沒有資料），請稍後再試。</p>";
    });
    if (history.replaceState) { history.replaceState(null, "", "#" + code); }
  }
  document.addEventListener("click", function (e) {
    var a = e.target.closest ? e.target.closest("a[data-stock]") : null;
    if (a) { e.preventDefault(); show(a.dataset.stock); }
  });
  var m = location.hash.match(/^#([0-9A-Za-z]{4,6})$/);
  if (m) {
    var link = document.querySelector('a[data-stock="' + m[1] + '"]');
    var sec = link && link.closest("section[data-pick]");
    var sel = document.querySelector("select[data-pick]");
    if (sec && sel) { sel.value = sec.dataset.key; sel.dispatchEvent(new Event("change")); }
    show(m[1]);
  }
})();
