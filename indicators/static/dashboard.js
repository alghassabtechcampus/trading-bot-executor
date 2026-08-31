/* Read-only dashboard renderer. Fetches /api/snapshot and /api/candles/<symbol>
   and draws them -- no trade is ever placed from this file. */

const POLL_MS = 60_000;
const STATE_LABEL = { bullish: "صاعد", bearish: "هابط", neutral: "محايد" };
const IND_LABEL = {
  macd: "MACD",
  adx: "ADX (قوة الاتجاه)",
  ichimoku: "سحابة إيشيموكو",
  vwap: "VWAP",
};

const TIMEFRAME_STORAGE_KEY = "dashboard_custom_timeframes";

let currentSymbol = null;
let latestSnapshot = null;
let chart = null;
let candleSeries = null;
let priceLines = [];
let customTimeframes = null; // {trend, entry, levels} chosen via the dropdowns, or null = use config.json defaults

function fmtNum(x, forcedDecimals) {
  if (x === null || x === undefined || Number.isNaN(x)) return "—";
  const abs = Math.abs(x);
  let decimals = forcedDecimals;
  if (decimals === undefined) {
    if (abs >= 1000) decimals = 2;
    else if (abs >= 1) decimals = 4;
    else decimals = 6;
  }
  return x.toLocaleString("en-US", { minimumFractionDigits: decimals, maximumFractionDigits: decimals });
}

function fmtPct(x) {
  if (x === null || x === undefined || Number.isNaN(x)) return "—";
  return (x > 0 ? "+" : "") + x.toFixed(2) + "%";
}

function timeAgo(isoString) {
  if (!isoString) return "غير معروف";
  const diffMs = Date.now() - new Date(isoString).getTime();
  const mins = Math.round(diffMs / 60000);
  if (mins < 1) return "الآن";
  if (mins < 60) return `منذ ${mins} دقيقة`;
  const hours = Math.round(mins / 60);
  return `منذ ${hours} ساعة`;
}

async function fetchJSON(url) {
  const resp = await fetch(url, { cache: "no-store" });
  if (!resp.ok) throw new Error(`${url} -> HTTP ${resp.status}`);
  return resp.json();
}

function renderTabs(pairs) {
  const tabsEl = document.getElementById("tabs");
  tabsEl.innerHTML = "";
  Object.keys(pairs).sort().forEach((symbol) => {
    const btn = document.createElement("button");
    btn.className = "tab-btn" + (symbol === currentSymbol ? " active" : "");
    btn.textContent = symbol.replace("USDT", "");
    btn.onclick = () => selectSymbol(symbol);
    tabsEl.appendChild(btn);
  });
}

function renderTopRefresh(snapshot) {
  const dot = document.getElementById("refresh-dot");
  const text = document.getElementById("last-refresh-text");
  const anyStale = Object.values(snapshot.pairs || {}).some(
    (p) => p.data_freshness && Object.values(p.data_freshness).some((f) => f.stale)
  );
  dot.className = "dot" + (anyStale ? " stale" : "");
  text.textContent = `آخر تحديث: ${timeAgo(snapshot.generated_at)}` + (anyStale ? " — تحذير: بعض البيانات قديمة" : "");
}

function indicatorCard(key, result) {
  const state = result.state || "neutral";
  const label = IND_LABEL[key] || key;
  const tf = result.timeframe ? `<span class="ind-tf">(${result.timeframe})</span>` : "";
  let detailsHtml = "";
  const d = result.details || {};

  if (key === "vwap") {
    detailsHtml = `
      <div class="row"><span class="k">السعر الحالي</span><span class="v num">${fmtNum(d.price)}</span></div>
      <div class="row"><span class="k">قيمة VWAP</span><span class="v num">${fmtNum(result.value)}</span></div>
      <div class="row"><span class="k">المسافة</span><span class="v num">${fmtPct(d.distance_pct)}</span></div>
      <div class="row"><span class="k">تأكيد الحجم</span><span class="v">${d.volume_confirmed ? "نعم" : "لا"}</span></div>`;
  } else if (key === "macd") {
    detailsHtml = `
      <div class="row"><span class="k">خط MACD</span><span class="v num">${fmtNum(d.macd_line)}</span></div>
      <div class="row"><span class="k">خط الإشارة</span><span class="v num">${fmtNum(d.signal_line)}</span></div>
      <div class="row"><span class="k">الهيستوجرام</span><span class="v num">${fmtNum(d.histogram)}</span></div>
      ${d.fresh_cross ? '<div class="row"><span class="k">تقاطع جديد</span><span class="v">نعم ✨</span></div>' : ""}`;
  } else if (key === "adx") {
    detailsHtml = `
      <div class="row"><span class="k">ADX</span><span class="v num">${fmtNum(d.adx, 2)}</span></div>
      <div class="row"><span class="k">+DI</span><span class="v num">${fmtNum(d.plus_di, 2)}</span></div>
      <div class="row"><span class="k">-DI</span><span class="v num">${fmtNum(d.minus_di, 2)}</span></div>`;
  } else if (key === "ichimoku") {
    detailsHtml = `
      <div class="row"><span class="k">أعلى السحابة</span><span class="v num">${fmtNum(d.cloud_top)}</span></div>
      <div class="row"><span class="k">أسفل السحابة</span><span class="v num">${fmtNum(d.cloud_bottom)}</span></div>
      <div class="row"><span class="k">المسافة</span><span class="v num">${fmtPct(d.distance_pct)}</span></div>`;
  }

  return `
    <div class="indicator-card ${state}">
      <div class="ind-name">${label} ${tf}</div>
      <span class="state-badge ${state}">${STATE_LABEL[state] || state}</span>
      <div class="ind-details">${detailsHtml || "<div class=\"row\"><span class=\"k\">لا توجد بيانات كافية</span></div>"}</div>
    </div>`;
}

function renderConfluence(conf) {
  const total = conf.total_indicators || 0;
  const bull = conf.bullish_count || 0;
  const bear = conf.bearish_count || 0;
  const neutral = total - bull - bear;
  const pct = (n) => (total ? (n / total) * 100 : 0);
  const direction = conf.direction || "neutral";

  const summaryText = direction === "neutral"
    ? `${bull}-${bear} تعادل من أصل ${total} مؤشرات (محايد)`
    : `${Math.max(bull, bear)} من ${total} مؤشرات ${direction === "bullish" ? "متفقة على اتجاه صاعد" : "متفقة على اتجاه هابط"}`;

  return `
    <div class="card">
      <p class="card-title">التوافق بين المؤشرات (Confluence)</p>
      <div class="confluence-bar">
        <div class="confluence-summary ${direction}">${summaryText}</div>
        <div class="confluence-track">
          <div class="seg-bull" style="width:${pct(bull)}%"></div>
          <div class="seg-neutral" style="width:${pct(neutral)}%"></div>
          <div class="seg-bear" style="width:${pct(bear)}%"></div>
        </div>
      </div>
      <div class="confluence-detail">
        صاعد: ${(conf.bullish_indicators || []).map((k) => IND_LABEL[k] || k).join("، ") || "لا يوجد"} —
        هابط: ${(conf.bearish_indicators || []).map((k) => IND_LABEL[k] || k).join("، ") || "لا يوجد"}
      </div>
    </div>`;
}

function renderTradeZone(tz) {
  if (!tz || tz.setup === "none") {
    return `
      <div class="card">
        <p class="card-title">منطقة الصفقة المقترحة</p>
        <div class="trade-zone-box none">
          <div class="no-setup-msg">⚪ لا يوجد توافق كافٍ حاليًا لاقتراح منطقة دخول</div>
        </div>
      </div>`;
  }

  if (tz.setup === "bearish_no_short") {
    return `
      <div class="card">
        <p class="card-title">منطقة الصفقة المقترحة</p>
        <div class="trade-zone-box bearish_no_short">
          <div class="tz-title bearish_no_short">⚪ اتجاه هابط — لا يوجد اقتراح دخول</div>
          <div class="no-setup-msg" style="text-align: start; padding-inline-start: 2px;">${tz.message}</div>
        </div>
      </div>`;
  }

  // Only "long" reaches here -- this dashboard never proposes a short (sell) setup.
  return `
    <div class="card">
      <p class="card-title">منطقة الصفقة المقترحة</p>
      <div class="trade-zone-box long">
        <div class="tz-title long">🟢 منطقة شراء مقترحة</div>
        <div class="tz-grid">
          <div class="tz-item"><div class="k">نطاق الدخول</div>
            <div class="v num">${fmtNum(tz.entry_zone[0])} – ${fmtNum(tz.entry_zone[1])}</div></div>
          <div class="tz-item"><div class="k">وقف الخسارة</div><div class="v num">${fmtNum(tz.stop_loss)}</div></div>
          <div class="tz-item"><div class="k">الهدف</div><div class="v num">${fmtNum(tz.target)}</div></div>
          <div class="tz-item"><div class="k">نسبة R:R</div><div class="v num">${tz.rr_ratio ?? "—"}</div></div>
          <div class="tz-item"><div class="k">مخاطرة</div><div class="v num">${fmtPct(tz.risk_pct_of_price)}</div></div>
          <div class="tz-item"><div class="k">عائد محتمل</div><div class="v num">${fmtPct(tz.reward_pct_of_price)}</div></div>
        </div>
        <div class="disclaimer">
          ⚠️ هذه منطقة مقترحة للمراجعة فقط، محسوبة آليًا من المؤشرات والدعم/المقاومة —
          <b>مش توصية تنفيذ تلقائي</b>. القرار النهائي بالدخول أو عدمه، وحجم الصفقة، يرجع بالكامل للمستخدم.
        </div>
      </div>
    </div>`;
}

function renderFreshness(freshness) {
  const labels = { trend: "فريم الاتجاه", entry: "فريم الدخول", levels: "فريم المستويات" };
  const chips = Object.entries(freshness || {}).map(([tf, info]) => {
    const cls = info.stale ? "stale" : "live";
    const icon = info.stale ? "⚠️" : "🟢";
    return `<span class="freshness-chip ${cls}">${icon} ${labels[tf] || tf}: ${info.source === "live" ? "مباشر" : "مخزّن مؤقتًا"} (${timeAgo(info.fetched_at)})</span>`;
  });
  return `<div class="card"><p class="card-title">حداثة البيانات</p><div class="freshness-row">${chips.join("")}</div></div>`;
}

function renderSymbol(symbol, data) {
  const main = document.getElementById("main-content");

  if (data.error) {
    main.innerHTML = `<div class="error-box">تعذّر تحميل بيانات ${symbol}: ${data.error}</div>`;
    return;
  }

  const indicatorsHtml = Object.entries(data.indicators || {})
    .map(([key, result]) => indicatorCard(key, result))
    .join("");

  main.innerHTML = `
    <div class="symbol-header">
      <span class="symbol-name">${data.symbol}</span>
      <span class="price num">${fmtNum(data.current_price)}</span>
    </div>

    <div class="card">
      <p class="card-title">الشموع (${data.timeframes.entry}) + VWAP + الدعم/المقاومة</p>
      <div id="chart-container"></div>
      <div class="chart-legend">
        <span><span class="legend-swatch" style="background:#c9a961"></span>VWAP</span>
        <span><span class="legend-swatch" style="background:#35c98f"></span>دعم</span>
        <span><span class="legend-swatch" style="background:#e8556a"></span>مقاومة</span>
      </div>
    </div>

    <div class="card">
      <p class="card-title">المؤشرات</p>
      <div class="indicators-grid">${indicatorsHtml}</div>
    </div>

    ${renderConfluence(data.confluence)}
    ${renderTradeZone(data.trade_zone)}
    ${renderFreshness(data.data_freshness)}
  `;

  initChart(data);
  loadCandles(symbol, data);
}

function initChart(data) {
  const container = document.getElementById("chart-container");
  container.innerHTML = "";
  priceLines = [];

  chart = LightweightCharts.createChart(container, {
    layout: { background: { color: "#161b24" }, textColor: "#97a1b3" },
    grid: { vertLines: { color: "#2a3140" }, horzLines: { color: "#2a3140" } },
    rightPriceScale: { borderColor: "#2a3140" },
    timeScale: { borderColor: "#2a3140", timeVisible: true },
    width: container.clientWidth,
    height: 340,
  });

  candleSeries = chart.addCandlestickSeries({
    upColor: "#35c98f", downColor: "#e8556a",
    borderUpColor: "#35c98f", borderDownColor: "#e8556a",
    wickUpColor: "#35c98f", wickDownColor: "#e8556a",
  });

  addLevelLines(data);

  window.addEventListener("resize", () => {
    if (chart) chart.applyOptions({ width: container.clientWidth });
  });
}

function addLevelLines(data) {
  if (!candleSeries) return;
  priceLines.forEach((line) => candleSeries.removePriceLine(line));
  priceLines = [];

  const vwap = data.indicators?.vwap?.value;
  if (vwap) {
    priceLines.push(candleSeries.createPriceLine({
      price: vwap, color: "#c9a961", lineWidth: 1, lineStyle: LightweightCharts.LineStyle.Dashed,
      axisLabelVisible: true, title: "VWAP",
    }));
  }

  const sr = data.support_resistance || {};
  if (sr.nearest_support) {
    priceLines.push(candleSeries.createPriceLine({
      price: sr.nearest_support, color: "#35c98f", lineWidth: 1, lineStyle: LightweightCharts.LineStyle.Dotted,
      axisLabelVisible: true, title: "دعم",
    }));
  }
  if (sr.nearest_resistance) {
    priceLines.push(candleSeries.createPriceLine({
      price: sr.nearest_resistance, color: "#e8556a", lineWidth: 1, lineStyle: LightweightCharts.LineStyle.Dotted,
      axisLabelVisible: true, title: "مقاومة",
    }));
  }

  const tz = data.trade_zone;
  if (tz && tz.setup === "long") {
    priceLines.push(candleSeries.createPriceLine({
      price: tz.stop_loss, color: "#e8556a", lineWidth: 2, lineStyle: LightweightCharts.LineStyle.Solid,
      axisLabelVisible: true, title: "وقف",
    }));
    priceLines.push(candleSeries.createPriceLine({
      price: tz.target, color: "#35c98f", lineWidth: 2, lineStyle: LightweightCharts.LineStyle.Solid,
      axisLabelVisible: true, title: "هدف",
    }));
  }
}

async function loadCandles(symbol, data) {
  try {
    const entryTf = data.timeframes && data.timeframes.entry;
    const qs = entryTf ? `?entry=${encodeURIComponent(entryTf)}` : "";
    const candles = await fetchJSON(`/api/candles/${symbol}${qs}`);
    if (symbol !== currentSymbol || !candleSeries) return; // user switched tabs while fetching
    candleSeries.setData(candles);
    addLevelLines(data);
    // The chart is created right after a fresh innerHTML swap, before the
    // browser's layout pass has necessarily settled -- without an explicit
    // resize to the container's now-final size, lightweight-charts can end
    // up with an internal canvas buffer that never actually paints the
    // series (empty-looking chart despite correct data). Forcing a resize
    // to the current size is the fix; fitContent() alone isn't enough.
    const container = document.getElementById("chart-container");
    if (container) chart.applyOptions({ width: container.clientWidth, height: 340 });
    chart.timeScale().fitContent();
  } catch (err) {
    console.error("candle load failed", err);
  }
}

// --- Timeframe dropdowns: persisted in localStorage, used instead of the
// periodic snapshot whenever the user has picked non-default timeframes ---

function loadCustomTimeframes() {
  try {
    const raw = localStorage.getItem(TIMEFRAME_STORAGE_KEY);
    if (!raw) return null;
    const parsed = JSON.parse(raw);
    return (parsed && parsed.trend && parsed.entry && parsed.levels) ? parsed : null;
  } catch (err) {
    return null; // corrupt or unavailable storage -- fall back to defaults
  }
}

function saveCustomTimeframes(tf) {
  try {
    if (tf) localStorage.setItem(TIMEFRAME_STORAGE_KEY, JSON.stringify(tf));
    else localStorage.removeItem(TIMEFRAME_STORAGE_KEY);
  } catch (err) {
    // localStorage unavailable (private mode, quota, etc.) -- selection just
    // won't persist across reloads; the dropdowns still work this session.
  }
}

function syncTimeframeControls() {
  const tf = customTimeframes || {};
  document.getElementById("tf-trend").value = tf.trend || "4h";
  document.getElementById("tf-entry").value = tf.entry || "1h";
  document.getElementById("tf-levels").value = tf.levels || "1d";
  document.getElementById("tf-reset").classList.toggle("active-custom", !!customTimeframes);
}

function showLoadingOverlay(show) {
  document.getElementById("loading-overlay").classList.toggle("hidden", !show);
}

async function fetchSymbolData(symbol) {
  if (customTimeframes) {
    const qs = new URLSearchParams(customTimeframes).toString();
    return fetchJSON(`/api/compute/${symbol}?${qs}`);
  }
  if (latestSnapshot && latestSnapshot.pairs[symbol]) {
    return latestSnapshot.pairs[symbol];
  }
  return fetchJSON(`/api/compute/${symbol}`); // no snapshot yet -- compute on demand
}

async function loadAndRenderSymbol(symbol) {
  const usingCustom = !!customTimeframes;
  if (usingCustom) showLoadingOverlay(true);
  try {
    const data = await fetchSymbolData(symbol);
    if (symbol !== currentSymbol) return; // user switched tabs while this was in flight
    renderSymbol(symbol, data);
  } catch (err) {
    if (symbol === currentSymbol) {
      document.getElementById("main-content").innerHTML =
        `<div class="error-box">تعذّر تحميل بيانات ${symbol}: ${err.message}</div>`;
    }
  } finally {
    if (usingCustom) showLoadingOverlay(false);
  }
}

function setupTimeframeControls() {
  ["tf-trend", "tf-entry", "tf-levels"].forEach((id) => {
    document.getElementById(id).addEventListener("change", () => {
      customTimeframes = {
        trend: document.getElementById("tf-trend").value,
        entry: document.getElementById("tf-entry").value,
        levels: document.getElementById("tf-levels").value,
      };
      saveCustomTimeframes(customTimeframes);
      syncTimeframeControls();
      if (currentSymbol) loadAndRenderSymbol(currentSymbol);
    });
  });

  document.getElementById("tf-reset").addEventListener("click", () => {
    customTimeframes = null;
    saveCustomTimeframes(null);
    syncTimeframeControls();
    if (currentSymbol) loadAndRenderSymbol(currentSymbol);
  });
}

function selectSymbol(symbol) {
  currentSymbol = symbol;
  renderTabs(latestSnapshot ? latestSnapshot.pairs : { [symbol]: true });
  loadAndRenderSymbol(symbol);
}

async function refresh() {
  try {
    const snapshot = await fetchJSON("/api/snapshot");
    latestSnapshot = snapshot;
    renderTopRefresh(snapshot);

    const symbols = Object.keys(snapshot.pairs || {}).sort();
    if (!currentSymbol && symbols.length) currentSymbol = symbols[0];

    renderTabs(snapshot.pairs);
    if (currentSymbol) {
      await loadAndRenderSymbol(currentSymbol);
    }
  } catch (err) {
    document.getElementById("main-content").innerHTML =
      `<div class="error-box">تعذّر تحميل لقطة البيانات: ${err.message}</div>`;
  }
}

customTimeframes = loadCustomTimeframes();
syncTimeframeControls();
setupTimeframeControls();
refresh();
setInterval(refresh, POLL_MS);
