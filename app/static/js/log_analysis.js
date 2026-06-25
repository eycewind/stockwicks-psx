const URL_PREFIX =
  window.STOCKWICKS_URL_PREFIX ||
  ((window.location.pathname.match(/^\/clients\/[^/]+/) || [""])[0]);

async function fetchJson(url) {
  const response = await fetch(url);

  if (!response.ok) {
    const text = await response.text();
    console.error("Request failed:", url, response.status, text);
    throw new Error(`Request failed: ${url} (${response.status})`);
  }

  return await response.json();
}

function selectedSymbol() {
  const el = document.getElementById("symbolSelect");
  return el ? el.value : "";
}

function selectedDate(id) {
  const el = document.getElementById(id);
  return el ? el.value : "";
}

function qs() {
  const params = new URLSearchParams();
  const symbol = selectedSymbol();
  const startDate = selectedDate("startDate");
  const endDate = selectedDate("endDate");

  if (symbol) params.set("symbol", symbol);
  if (startDate) params.set("start_date", startDate);
  if (endDate) params.set("end_date", endDate);

  const query = params.toString();
  return query ? `?${query}` : "";
}

function fmt(value, digits = 4) {
  if (value === null || value === undefined || value === "") return "";
  const n = Number(value);
  if (Number.isNaN(n)) return value;
  return n.toFixed(digits);
}

function safeText(value) {
  if (value === null || value === undefined) return "";
  return String(value)
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

async function loadSymbols() {
  const data = await fetchJson(`${URL_PREFIX}/analysis/api/logs/symbols`);
  const symbolSelect = document.getElementById("symbolSelect");
  if (!symbolSelect) return;

  const currentValue = symbolSelect.value;
  const symbols = data.symbols || [];
  symbolSelect.innerHTML = [
    `<option value="">All Symbols</option>`,
    ...symbols.map(symbol => `<option value="${safeText(symbol)}">${safeText(symbol)}</option>`)
  ].join("");

  if (currentValue && symbols.includes(currentValue)) {
    symbolSelect.value = currentValue;
  }
}

async function loadSummary() {
  const data = await fetchJson(`${URL_PREFIX}/analysis/api/logs/summary${qs()}`);
  const cards = document.getElementById("summaryCards");

  if (!cards) return;

  const actionHtml = Object.entries(data.by_action || {})
    .sort((a, b) => b[1] - a[1])
    .map(([k, v]) => `<div>${safeText(k)}: <strong>${v}</strong></div>`)
    .join("");

  const reasonHtml = Object.entries(data.by_reason || {})
    .sort((a, b) => b[1] - a[1])
    .slice(0, 10)
    .map(([k, v]) => `<div>${safeText(k)}: <strong>${v}</strong></div>`)
    .join("");

  const symbolHtml = Object.entries(data.by_symbol || {})
    .sort((a, b) => b[1] - a[1])
    .map(([k, v]) => `<div>${safeText(k)}: <strong>${v}</strong></div>`)
    .join("");

  cards.innerHTML = `
    <div class="card">
      <div class="card-body">
        <h5>Total Rows</h5>
        <h2>${data.rows || 0}</h2>
      </div>
    </div>

    <div class="card">
      <div class="card-body">
        <h5>Symbols</h5>
        ${symbolHtml || "<div>No symbols found</div>"}
      </div>
    </div>

    <div class="card">
      <div class="card-body">
        <h5>Actions</h5>
        ${actionHtml || "<div>No actions found</div>"}
      </div>
    </div>

    <div class="card">
      <div class="card-body">
        <h5>Top Reasons</h5>
        ${reasonHtml || "<div>No reasons found</div>"}
      </div>
    </div>
  `;
}

async function loadChart() {
  const data = await fetchJson(`${URL_PREFIX}/analysis/api/logs/chart-data${qs()}`);
  const candles = data.candles || [];
  const pricePoints = data.price_points || [];
  const events = data.events || [];

  const chartEl = document.getElementById("priceChart");
  if (!chartEl) return;

  if (!candles.length && !pricePoints.length) {
    chartEl.innerHTML = `<div class="alert alert-warning">No price data found.</div>`;
    renderEntryExitTable(events);
    return;
  }

  const priceTrace = candles.length
    ? {
        x: candles.map(c => c.time),
        open: candles.map(c => c.open),
        high: candles.map(c => c.high),
        low: candles.map(c => c.low),
        close: candles.map(c => c.close),
        type: "candlestick",
        name: "Candles"
      }
    : {
        x: pricePoints.map(p => p.time),
        y: pricePoints.map(p => p.price),
        mode: "lines",
        type: "scatter",
        name: "Close Price",
        line: { color: "#57e6c2", width: 2 },
        text: pricePoints.map(p =>
          `Close ${fmt(p.close || p.price, 2)}<br>${safeText(p.action || "")}<br>${safeText(p.reason || "")}`
        ),
        hoverinfo: "text+x+y"
      };

  const openEvents = events.filter(e =>
    e.action === "OPEN_LONG" ||
    e.action === "OPEN_SHORT"
  );

  const exitEvents = events.filter(e =>
    e.action &&
    (
      e.action.startsWith("EXIT_") ||
      e.action.startsWith("CLOSE_") ||
      e.action === "STOP_LOSS" ||
      e.action === "TAKE_PROFIT" ||
      e.action === "EOD_CLOSE"
    )
  );

  const openTrace = {
    x: openEvents.map(e => e.time),
    y: openEvents.map(e => e.price),
    mode: "markers",
    type: "scatter",
    name: "Entries",
    marker: {
      size: 13,
      symbol: "triangle-up"
    },
    text: openEvents.map(e =>
      `${safeText(e.action)}<br>${safeText(e.reason)}<br>UP ${fmt(e.prob_up)} DOWN ${fmt(e.prob_down)}`
    ),
    hoverinfo: "text+x+y"
  };

  const exitTrace = {
    x: exitEvents.map(e => e.time),
    y: exitEvents.map(e => e.price),
    mode: "markers",
    type: "scatter",
    name: "Exits",
    marker: {
      size: 13,
      symbol: "x"
    },
    text: exitEvents.map(e =>
      `${safeText(e.action)}<br>${safeText(e.reason)}<br>UP ${fmt(e.prob_up)} DOWN ${fmt(e.prob_down)}`
    ),
    hoverinfo: "text+x+y"
  };

  Plotly.newPlot(
    "priceChart",
    [priceTrace, openTrace, exitTrace],
    {
      title: candles.length ? "Entry / Exit Candles" : "Entry / Exit Close Price",
      paper_bgcolor: "#0b0a2a",
      plot_bgcolor: "#0b0a2a",
      font: { color: "#ffffff" },
      xaxis: {
        rangeslider: { visible: false },
        gridcolor: "#26324a"
      },
      yaxis: {
        title: "Price",
        gridcolor: "#26324a"
      },
      hovermode: "x unified",
      margin: { t: 50, r: 30, b: 40, l: 60 }
    },
    { responsive: true }
  );

  renderEntryExitTable(events);
}

function renderEntryExitTable(events) {
  const tbody = document.querySelector("#entryExitTable tbody");
  if (!tbody) return;

  if (!events.length) {
    tbody.innerHTML = `
      <tr>
        <td colspan="7" class="text-muted" style="text-align:center;">
          No entry or exit rows found.
        </td>
      </tr>
    `;
    return;
  }

  tbody.innerHTML = events.map(e => `
    <tr>
      <td>${safeText(e.time)}</td>
      <td>${safeText(e.symbol)}</td>
      <td><strong>${safeText(e.action)}</strong></td>
      <td>${fmt(e.price, 2)}</td>
      <td>${fmt(e.prob_up)}</td>
      <td>${fmt(e.prob_down)}</td>
      <td style="max-width: 620px; white-space: normal;">${safeText(e.reason)}</td>
    </tr>
  `).join("");
}

async function loadBlockedEntries() {
  const data = await fetchJson(`${URL_PREFIX}/analysis/api/logs/blocked-entries${qs()}`);
  const tbody = document.querySelector("#blockedTable tbody");

  if (!tbody) return;

  const rows = data.blocked_entries || [];

  if (!rows.length) {
    tbody.innerHTML = `
      <tr>
        <td colspan="8" class="text-muted" style="text-align:center;">
          No blocked high-probability entries found.
        </td>
      </tr>
    `;
    return;
  }

  tbody.innerHTML = rows.map(r => `
    <tr>
      <td>${safeText(r.time)}</td>
      <td>${safeText(r.symbol)}</td>
      <td><strong>${safeText(r.wanted)}</strong></td>
      <td>${fmt(r.prob_up)}</td>
      <td>${fmt(r.prob_down)}</td>
      <td><strong>${safeText(r.blocked_by)}</strong></td>
      <td>${fmt(r.close, 2)}</td>
      <td style="max-width: 520px; white-space: normal;">${safeText(r.raw_reason)}</td>
    </tr>
  `).join("");
}

async function loadFeatures() {
  const data = await fetchJson(`${URL_PREFIX}/analysis/api/logs/features${qs()}`);
  const featureSelect = document.getElementById("featureSelect");
  const chartEl = document.getElementById("featureChart");

  if (!featureSelect || !chartEl) return;

  const featureNames = Object.keys(data.features || {}).sort();

  if (!featureNames.length) {
    featureSelect.innerHTML = "";
    chartEl.innerHTML = `<div class="alert alert-warning">No feature data found.</div>`;
    return;
  }

  const previousValue = featureSelect.value;

  featureSelect.innerHTML = featureNames
    .map(name => `<option value="${safeText(name)}">${safeText(name)}</option>`)
    .join("");

  if (previousValue && featureNames.includes(previousValue)) {
    featureSelect.value = previousValue;
  } else {
    featureSelect.value = featureNames[0];
  }

  function drawFeature() {
    const name = featureSelect.value;

    const trace = {
      x: data.times || [],
      y: data.features[name] || [],
      mode: "lines+markers",
      type: "scatter",
      name
    };

    Plotly.newPlot(
      "featureChart",
      [trace],
      {
        title: `Feature: ${name}`,
        paper_bgcolor: "#0b0a2a",
        plot_bgcolor: "#0b0a2a",
        font: { color: "#ffffff" },
        xaxis: {
          title: "Time",
          gridcolor: "#26324a"
        },
        yaxis: {
          title: name,
          gridcolor: "#26324a"
        },
        margin: { t: 50, r: 30, b: 40, l: 60 }
      },
      { responsive: true }
    );
  }

  featureSelect.onchange = drawFeature;
  drawFeature();
}

async function loadDiagnostics() {
  const summary = await fetchJson(`${URL_PREFIX}/analysis/api/logs/summary${qs()}`);
  const diagnosticsBox = document.getElementById("diagnosticsBox");

  if (!diagnosticsBox) return;

  const reasons = summary.by_reason || {};
  const actions = summary.by_action || {};

  const cooldown = reasons.COOLDOWN || 0;
  const blockedProb = reasons.BOTH_PROBS_BELOW_THRESHOLDS || 0;
  const obvLong = reasons.OBV_BLOCKS_LONG || 0;
  const obvShort = reasons.OBV_BLOCKS_SHORT || 0;
  const lowVolume = reasons.LOW_VOLUME || 0;

  const opens = (actions.OPEN_LONG || 0) + (actions.OPEN_SHORT || 0);
  const exits = Object.entries(actions)
    .filter(([k]) =>
      k.startsWith("EXIT_") ||
      k.startsWith("CLOSE_") ||
      k === "STOP_LOSS" ||
      k === "TAKE_PROFIT" ||
      k === "EOD_CLOSE"
    )
    .reduce((sum, [, v]) => sum + v, 0);

  let warnings = "";

  if (cooldown > 0) {
    warnings += `
      <li>
        Cooldown rows: <strong>${cooldown}</strong>.
        Check for negative cooldown deltas and timezone bugs.
      </li>
    `;
  }

  if (blockedProb > 0) {
    warnings += `
      <li>
        Probability threshold blocks: <strong>${blockedProb}</strong>.
        Review whether thresholds are too strict by symbol and interval.
      </li>
    `;
  }

  if (obvLong + obvShort > 0) {
    warnings += `
      <li>
        OBV blocks: <strong>${obvLong + obvShort}</strong>.
        Add forward-return analysis to prove whether OBV is helping or blocking winners.
      </li>
    `;
  }

  if (lowVolume > 0) {
    warnings += `
      <li>
        Low-volume blocks: <strong>${lowVolume}</strong>.
        Compare blocked low-volume signals against 3-bar and 5-bar forward returns.
      </li>
    `;
  }

  diagnosticsBox.innerHTML = `
    <p><strong>Model / rule-layer diagnostics:</strong></p>
    <ul>
      ${warnings || "<li>No major blocking rule found in summary.</li>"}
      <li>Total entries: <strong>${opens}</strong></li>
      <li>Total exits: <strong>${exits}</strong></li>
    </ul>

    <p>
      Important missing analysis: for every blocked signal, calculate future return after
      1, 3, 5, and 10 bars. That tells you whether the model is weak or the rule layer
      is blocking good trades.
    </p>
  `;
}

async function refreshAll() {
  const btn = document.getElementById("refreshBtn");

  try {
    if (btn) {
      btn.disabled = true;
      btn.innerText = "Loading...";
    }

    await loadSummary();
    await loadChart();
    await loadBlockedEntries();
    await loadFeatures();
    await loadDiagnostics();
  } finally {
    if (btn) {
      btn.disabled = false;
      btn.innerText = "Refresh";
    }
  }
}

document.addEventListener("DOMContentLoaded", () => {
  const refreshBtn = document.getElementById("refreshBtn");
  const symbolSelect = document.getElementById("symbolSelect");
  const startDate = document.getElementById("startDate");
  const endDate = document.getElementById("endDate");

  if (refreshBtn) {
    refreshBtn.addEventListener("click", refreshAll);
  }

  if (symbolSelect) {
    symbolSelect.addEventListener("change", refreshAll);
  }

  if (startDate) {
    startDate.addEventListener("change", refreshAll);
  }

  if (endDate) {
    endDate.addEventListener("change", refreshAll);
  }

  loadSymbols()
    .then(refreshAll)
    .catch(err => {
    console.error(err);
    alert(err.message);
  });
});
