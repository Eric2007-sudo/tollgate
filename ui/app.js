/* tollgate 工作台：只读渲染 /api/state，零依赖 */
"use strict";

var CNY = 6.6892;
var POLL_MS = 3000;

function $(s) { return document.querySelector(s); }

function el(tag, cls, text) {
  var e = document.createElement(tag);
  if (cls) e.className = cls;
  if (text != null) e.textContent = text;
  return e;
}

function fmtK(n) {
  n = n || 0;
  if (n >= 1e6) return (n / 1e6).toFixed(2) + "M";
  if (n >= 1e3) return (n / 1e3).toFixed(1) + "k";
  return String(n);
}

function usd(v) {
  if (v == null) return "无价目";
  return "$" + v.toFixed(6) + "（¥" + (v * CNY).toFixed(4) + "）";
}

function pct(v) { return (v * 100).toFixed(1) + "%"; }

function cash(v) { return v == null ? "—" : usd(v); }

/* 表格里放不下「$0.000123（¥0.000823）」，只留金额；全文放 title */
function usdShort(v) {
  if (v == null) return "-";
  return "$" + (v >= 0.01 ? v.toFixed(4) : v.toFixed(6));
}

function card(k, v, note) {
  var c = el("div", "card");
  c.appendChild(el("div", "k", k));
  c.appendChild(el("div", "v", v));
  if (note) c.appendChild(el("div", "n", note));
  return c;
}

function gauge(label, value, total, cls) {
  var ratio = total ? Math.min(1, value / total) : 0;
  var g = el("div", "gauge");
  var top = el("div", "top");
  top.appendChild(el("span", null, label));
  top.appendChild(el("span", null, fmtK(value) + " / " + fmtK(total) +
    "  " + (total ? pct(ratio) : "-")));
  g.appendChild(top);
  var bar = el("div", "bar");
  var fill = el("div", "fill" + (cls ? " " + cls : ""));
  fill.style.width = (ratio * 100).toFixed(1) + "%";
  bar.appendChild(fill);
  g.appendChild(bar);
  return g;
}

function renderCards(s) {
  var t = s.totals, c = s.ctx, cum = c && c.cumulative, turn = c && c.turn;
  var box = $("#cards");
  box.textContent = "";

  box.appendChild(card("进度", t.done + " / " + t.steps,
    t.budget ? "预算 " + fmtK(t.budget) + " tok" : "未设 token 预算"));

  box.appendChild(card("已记 token", fmtK(t.spent),
    t.budget ? pct(t.spent / t.budget) + " 用掉" : "—"));

  box.appendChild(card("轮数", String(t.reqs), t.ledger_rows + " 条账本记录"));

  var m = s.money || {};
  if (m.ledger_usd != null) {
    box.appendChild(card("账本花费", cash(m.ledger_usd),
      m.rounds + " 轮 · 单轮 " + cash(m.per_round_usd) +
      (m.est_rows ? " · 含 " + m.est_rows + " 笔估算" : "") + " · 截至上次 sync"));
  } else {
    box.appendChild(card("账本花费", "没有", "跑 tg sync 让宿主报账"));
  }

  if (turn && turn.ctx_tokens) {
    box.appendChild(card("本轮上下文", fmtK(turn.ctx_tokens),
      (c.truncated ? "最近 " + c.turns + " 轮中的最后一轮" : "第 " + (c.turns || "?") + " 轮") +
      " · 命中 " + pct(turn.cache_read / turn.ctx_tokens)));
  }

  var g = c && c.growth;
  if (g && g.rounds_left != null) {
    box.appendChild(card("还能撑", g.rounds_left + " 轮",
      "增速 +" + fmtK(g.tok_per_round) + " tok/轮（近 " + g.samples + " 轮中位数）"));
  }

  if (cum) {
    box.appendChild(card("本会话花费",
      usd(cum.usd_db > 0 ? cum.usd_db : cum.usd_recalc),
      cum.usd_db > 0 ? "opencode.db 记录值" : "db 记不到，此为上界重算"));
    box.appendChild(card("缓存命中率", pct(cum.cache_hit),
      "健康线 30%（低于则前缀在漂移）"));
  }
}

function sparkline(history) {
  if (!history || !history.length) return null;
  var max = 1;
  history.forEach(function (h) { if (h.ctx > max) max = h.ctx; });
  var wrap = el("div", "spark");
  history.forEach(function (h) {
    var b = el("div", "sparkbar");
    var tall = Math.max(4, Math.round(h.ctx / max * 100));
    b.style.height = tall + "%";
    b.title = fmtK(h.ctx) + " tok · " + usd(h.usd) + " · 命中 " + pct(h.hit || 0);
    if (h.hit && h.hit < 0.3) b.className += " bad";
    wrap.appendChild(b);
  });
  return wrap;
}

function renderCtx(s) {
  var c = s.ctx || {}, turn = c.turn, price = c.price, sess = c.session;
  var box = $("#ctx");
  box.textContent = "";

  var left = el("div");
  if (!sess) {
    left.appendChild(el("div", "muted", "读不到 opencode.db 的会话行。"));
  } else {
    var window_ = (price && price.context) || 0;
    if (turn && window_) {
      var cls = turn.ctx_tokens >= window_ * 0.8 ? "bad"
              : turn.ctx_tokens >= window_ * 0.5 ? "warn" : "";
      left.appendChild(gauge("本轮上下文 / 模型窗口", turn.ctx_tokens, window_, cls));
    }
    var dl = el("dl", "kv");
    function kv(k, v) {
      dl.appendChild(el("dt", null, k));
      dl.appendChild(el("dd", null, v));
    }
    kv("会话", (sess.session || "").slice(0, 20));
    kv("模型", (sess.provider || "?") + "/" + (sess.model || "?"));

    var sy = s.sync || {};
    if (sy.session) {
      var pend = sy.pending_in_window;
      var ptxt = pend == null ? "未知"
              : (pend === 0 ? "已全部入账" : pend + " 轮未入账（跑 tg sync）");
      kv("对账", (sy.same_session ? "" : "换了会话 · ") + ptxt +
        (sy.updated ? " · " + sy.updated.replace("T", " ").slice(0, 16) : ""));
    } else {
      kv("对账", "还没 sync 过（账本里只有手记的数）");
    }
    var ctxTokens = (turn && turn.ctx_tokens) || 0;
    if (c.turns) kv("逐轮数据", c.turns + " 轮可读");
    if (ctxTokens) {
      kv("input / cache_read", fmtK(turn.input) + " / " + fmtK(turn.cache_read));
      kv("本轮命中率", pct(turn.cache_read / ctxTokens));
    }
    var g = c.growth;
    if (g) {
      kv("增速", "+" + fmtK(g.tok_per_round) + " tok/轮");
      if (g.rounds_left != null) {
        kv("撞天花板", g.rounds_left + " 轮后到 90% 窗口（" + fmtK(g.ceiling) + "）");
      }
    }
    if (turn && turn.usd != null) kv("本轮花费", usd(turn.usd));
    if (c.cumulative) {
      kv("累计", "db " + usd(c.cumulative.usd_db));
      kv("累计重算", usd(c.cumulative.usd_recalc) + "（上界）");
    }
    left.appendChild(dl);

    var sp = sparkline(c.history);
    if (sp) {
      left.appendChild(el("div", "sparklabel", "最近 " + c.history.length + " 轮上下文"));
      left.appendChild(sp);
    }
  }

  var right = el("div");
  var sv = c.savings;
  if (sv) {
    var dl2 = el("dl", "kv");
    dl2.appendChild(el("dt", null, "压缩到 1/3"));
    dl2.appendChild(el("dd", null, usd(sv.compress_usd)));
    dl2.appendChild(el("dt", null, "handoff 换会话"));
    dl2.appendChild(el("dd", null, usd(sv.handoff_usd)));
    if (sv.ratio) {
      dl2.appendChild(el("dt", null, "换手 / 压缩"));
      dl2.appendChild(el("dd", null, sv.ratio.toFixed(1) + "×"));
    }
    right.appendChild(dl2);
    right.appendChild(el("div", "muted",
      "比值是结构性的：换手把上下文打到 ~600 tok，压缩只削 2/3 且每轮要重付一次压缩调用。"));
  }

  var verdicts = c.verdict || [];
  var ul = el("ul", "verdict");
  if (!verdicts.length) {
    var li0 = el("li", "none", "没有触发任何阈值：可以继续，不用压也不用换。");
    ul.appendChild(li0);
  } else {
    verdicts.forEach(function (v) { ul.appendChild(el("li", null, v)); });
  }
  right.appendChild(ul);

  box.appendChild(left);
  box.appendChild(right);
}

function renderSteps(s) {
  $("#goal").textContent = s.goal || "（还没有计划：先跑 tg init \"你的目标\"）";
  if (s.why) {
    var w = el("span", "why", "为什么该做：" + s.why);
    $("#goal").appendChild(w);
  }

  var t = $("#steps");
  t.textContent = "";
  var thead = el("thead");
  var hr = el("tr");
  [["#", "num"], ["状态", ""], ["步骤", ""], ["档", "t"], ["预算 tok", "num"],
   ["已用 tok", "num"], ["轮", "num"], ["花费", "num"]].forEach(function (c) {
    var th = el("th", c[1] || null, c[0]);
    hr.appendChild(th);
  });
  thead.appendChild(hr);
  t.appendChild(thead);

  var tb = el("tbody");
  (s.steps || []).forEach(function (st) {
    var tr = el("tr");
    tr.appendChild(el("td", "num", String(st.n)));
    var tdst = el("td");
    tdst.appendChild(el("span", "badge " + st.status, st.status));
    tr.appendChild(tdst);

    var td = el("td", "title", st.title);
    if (st.out) td.appendChild(el("span", "out", "→ " + st.out));
    tr.appendChild(td);

    tr.appendChild(el("td", "t", st.tier || "-"));
    tr.appendChild(el("td", "num", st.budget ? fmtK(st.budget) : "-"));

    var spent = el("td", "num" + (st.budget && st.spent > st.budget ? " over" : ""),
      st.spent ? fmtK(st.spent) : "-");
    tr.appendChild(spent);

    var reqs = el("td", "num" + (st.budget_rounds && st.reqs > st.budget_rounds ? " over" : ""),
      st.reqs + (st.budget_rounds ? " / " + st.budget_rounds : ""));
    tr.appendChild(reqs);

    var ctd = el("td", "num", usdShort(st.usd));
    if (st.usd != null) ctd.title = usd(st.usd);
    tr.appendChild(ctd);
    tb.appendChild(tr);
  });
  t.appendChild(tb);
}

function render(s) {
  if (s.error) {
    $("#status").textContent = "错误：" + s.error;
    return;
  }
  renderCards(s);
  renderCtx(s);
  renderSteps(s);
  $("#ts").textContent = (s.ts || "").replace("T", " ").slice(0, 19) +
    " · " + (s.root || "");
}

async function pull() {
  try {
    var r = await fetch("/api/state", { cache: "no-store" });
    render(await r.json());
    $("#status").textContent = "";
  } catch (e) {
    $("#status").textContent = "连不上后端：" + e;
  }
}

$("#sample").addEventListener("click", async function () {
  $("#status").textContent = "采样中…";
  try {
    var r = await fetch("/api/sample", { method: "POST" });
    var j = await r.json();
    $("#status").textContent = j.ok ? "已记一笔采样" : ("失败：" + j.error);
  } catch (e) {
    $("#status").textContent = "失败：" + e;
  }
  pull();
});

pull();
setInterval(pull, POLL_MS);
