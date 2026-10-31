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
  });
})();
