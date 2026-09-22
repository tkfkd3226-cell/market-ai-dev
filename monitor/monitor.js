"use strict";

/* =========================================================
   1. Constants
   ========================================================= */

const API_URL = "/api/bridge/kis-efriend/quote-universe";

const POLL_INTERVAL_MS = 10_000;
const REQUEST_TIMEOUT_MS = 5_000;
const STALE_AFTER_MS = 90_000;

const THEME_STORAGE_KEY = "market-ai-monitor-theme";
const EMBED_SIZE_MESSAGE = "market-ai-monitor:content-size";
const EMBED_THEME_READY_MESSAGE = "market-ai-monitor:theme-ready";
const EMBED_THEME_STATE_MESSAGE = "market-ai-monitor:theme-state";
const EMBED_THEME_CHANGE_MESSAGE = "market-ai-monitor:theme-change";

const STATUS = Object.freeze({
  LIVE: "live",
  EXTENDED: "extended",
  CLOSED: "closed",
  WARMING: "warming",
  STALE: "stale",
  ERROR: "error",
});

const STATUS_LABEL = Object.freeze({
  [STATUS.LIVE]: "정상",
  [STATUS.EXTENDED]: "시간외",
  [STATUS.CLOSED]: "장마감",
  [STATUS.WARMING]: "대기",
  [STATUS.STALE]: "지연",
  [STATUS.ERROR]: "오류",
});

const MARKET_SYMBOLS = Object.freeze({
  K200: "FUTURES:KOSPI200",
  KOSPI: "INDEX:KOSPI",
});


/* =========================================================
   2. State
   ========================================================= */

const state = {
  connected: false,
  bridgeConnected: false,
  lastReceivedAt: null,

  marketState: null,
  k200MarketOpen: false,

  markets: {
    k200: createEmptyMarket("K200"),
    kospi: createEmptyMarket("KOSPI"),
  },

  holdings: [],

  pollTimer: null,
  requestController: null,
  requestSequence: 0,
  appliedSequence: 0,
};


/* =========================================================
   3. DOM
   ========================================================= */

const dom = {
  root: document.documentElement,
  shell: document.querySelector(".monitor-shell"),

  systemStatus: document.getElementById("system-status"),
  systemStatusText: document.getElementById("system-status-text"),
  systemClock: document.getElementById("system-clock"),
  themeToggle: document.getElementById("theme-toggle"),

  marketK200: document.getElementById("market-k200"),
  marketKospi: document.getElementById("market-kospi"),

  holdingsGrid: document.getElementById("holdings-grid"),
  holdingTemplate: document.getElementById("holding-card-template"),

  summaryTotal: document.getElementById("summary-total"),
  summaryLive: document.getElementById("summary-live"),
  summaryExtended: document.getElementById("summary-extended"),
  summaryClosed: document.getElementById("summary-closed"),
  summaryWarming: document.getElementById("summary-warming"),
  summaryStale: document.getElementById("summary-stale"),
  summaryError: document.getElementById("summary-error"),

  footerStatusDot: document.getElementById("footer-status-dot"),
  footerConnection: document.getElementById("footer-connection"),
  footerLastReceived: document.getElementById("footer-last-received"),
};


/* =========================================================
   4. Basic Helpers
   ========================================================= */

function createEmptyMarket(name) {
  return {
    name,
    status: STATUS.WARMING,
    price: null,
    changePct: null,
    sessionLabel: "-",
    businessTime: null,
    observedAt: null,
  };
}

function normalizeTicker(value) {
  return String(value ?? "")
    .trim()
    .toUpperCase()
    .replace(/^KRX:/, "");
}

function toFiniteNumber(value) {
  if (value === null || value === undefined || value === "") {
    return null;
  }

  const number = Number(value);

  return Number.isFinite(number)
    ? number
    : null;
}

function parseDate(value) {
  if (!value) {
    return null;
  }

  const parsed = new Date(value);

  return Number.isNaN(parsed.getTime())
    ? null
    : parsed;
}

function normalizeClockText(value) {
  if (!value) {
    return null;
  }

  const text = String(value).trim();

  const compactMatch = text.match(
    /^(\d{2})(\d{2})(\d{2})$/,
  );

  if (compactMatch) {
    const hour = Number(compactMatch[1]);
    const minute = Number(compactMatch[2]);
    const second = Number(compactMatch[3]);
    if (hour > 23 || minute > 59 || second > 59) {
      return null;
    }
    return [
      compactMatch[1],
      compactMatch[2],
      compactMatch[3],
    ].join(":");
  }

  const clockMatch = text.match(
    /^(\d{2}):(\d{2})(?::(\d{2}))?/,
  );

  if (clockMatch) {
    const hour = Number(clockMatch[1]);
    const minute = Number(clockMatch[2]);
    const second = Number(clockMatch[3] ?? "00");
    if (hour > 23 || minute > 59 || second > 59) {
      return null;
    }
    return [
      clockMatch[1],
      clockMatch[2],
      clockMatch[3] ?? "00",
    ].join(":");
  }

  const date = parseDate(text);

  if (date) {
    return formatClock(date);
  }

  return text;
}

function formatClock(value) {
  const date = value instanceof Date
    ? value
    : parseDate(value);

  if (!date) {
    return "--:--:--";
  }

  return new Intl.DateTimeFormat("ko-KR", {
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
    hour12: false,
  }).format(date);
}

function formatPrice(value) {
  const number = toFiniteNumber(value);

  if (number === null) {
    return "-";
  }

  const fractionDigits =
    Math.abs(number % 1) > Number.EPSILON ? 2 : 0;

  return new Intl.NumberFormat("ko-KR", {
    minimumFractionDigits: fractionDigits,
    maximumFractionDigits: 2,
  }).format(number);
}

function formatChangePct(value) {
  const number = toFiniteNumber(value);

  if (number === null) {
    return "-";
  }

  const sign = number > 0 ? "+" : "";

  return `${sign}${number.toFixed(2)}%`;
}

function formatChangeAmount(value) {
  const number = toFiniteNumber(value);

  if (number === null) {
    return "";
  }

  const sign = number > 0 ? "+" : "";

  return `${sign}${new Intl.NumberFormat("ko-KR", {
    maximumFractionDigits: 2,
  }).format(number)}`;
}

function resolveTrend(value) {
  const number = toFiniteNumber(value);

  if (number === null || number === 0) {
    return "flat";
  }

  return number > 0
    ? "positive"
    : "negative";
}

function normalizeStatus(value) {
  const normalized = String(value ?? "")
    .trim()
    .toLowerCase();

  if (
    normalized === "live" ||
    normalized === "normal" ||
    normalized === "ok"
  ) {
    return STATUS.LIVE;
  }

  if (
    normalized === "extended" ||
    normalized === "after_hours" ||
    normalized === "after-hours"
  ) {
    return STATUS.EXTENDED;
  }

  if (
    normalized === "closed" ||
    normalized === "close"
  ) {
    return STATUS.CLOSED;
  }

  if (
    normalized === "warming" ||
    normalized === "waiting" ||
    normalized === "pending"
  ) {
    return STATUS.WARMING;
  }

  if (
    normalized === "stale" ||
    normalized === "delayed"
  ) {
    return STATUS.STALE;
  }

  if (
    normalized === "error" ||
    normalized === "failed"
  ) {
    return STATUS.ERROR;
  }

  return null;
}

function normalizeMarketState(value) {
  return String(value ?? "")
    .trim()
    .toLowerCase();
}

function isCashMarketClosed() {
  return [
    "closed",
    "close",
    "after_close",
    "after-close",
  ].includes(state.marketState);
}

function isCashMarketOpen() {
  return [
    "open",
    "regular",
    "trading",
  ].includes(state.marketState);
}

function isRecent(value) {
  const date = parseDate(value);

  if (!date) {
    return false;
  }

  return (
    Date.now() - date.getTime()
    <= STALE_AFTER_MS
  );
}

function firstDefined(...values) {
  return values.find(
    (value) =>
      value !== undefined &&
      value !== null &&
      value !== "",
  );
}


/* =========================================================
   5. Payload Normalization
   ========================================================= */

function getDashboardTickers(payload) {
  const source =
    payload?.dashboard_display_tickers ??
    payload?.bridge_universe?.dashboard_display_tickers ??
    payload?.dashboard_tickers ??
    payload?.bridge_universe?.dashboard_tickers ??
    [];

  if (!Array.isArray(source)) {
    return [];
  }

  return [
    ...new Set(
      source
        .map((item) => {
          if (typeof item === "string") {
            return normalizeTicker(item);
          }

          return normalizeTicker(
            firstDefined(
              item?.ticker,
              item?.symbol,
              item?.code,
            ),
          );
        })
        .filter(Boolean),
    ),
  ];
}

function getTickerNames(payload) {
  const names = new Map();

  const sources = [
    payload?.dashboard_names,
    payload?.dashboard_ticker_names,
    payload?.ticker_names,
    payload?.display_names,

    payload?.bridge_universe
      ?.dashboard_names,

    payload?.bridge_universe
      ?.dashboard_ticker_names,

    payload?.bridge_universe
      ?.ticker_names,

    payload?.bridge_universe
      ?.display_names,
  ];

  for (const source of sources) {
    if (!source) {
      continue;
    }

    if (Array.isArray(source)) {
      for (const item of source) {
        if (!item || typeof item !== "object") {
          continue;
        }

        const ticker = normalizeTicker(
          firstDefined(
            item.ticker,
            item.symbol,
            item.code,
          ),
        );

        const name = firstDefined(
          item.name,
          item.display_name,
          item.label,
        );

        if (ticker && name) {
          names.set(ticker, String(name));
        }
      }

      continue;
    }

    if (typeof source === "object") {
      for (
        const [tickerValue, nameValue]
        of Object.entries(source)
      ) {
        const ticker = normalizeTicker(tickerValue);

        if (ticker && nameValue) {
          names.set(
            ticker,
            String(nameValue),
          );
        }
      }
    }
  }

  return names;
}

function getTickerMarketStates(payload) {
  const source =
    payload?.dashboard_market_states ??
    payload?.bridge_universe?.dashboard_market_states ??
    {};
  const states = new Map();
  if (source && typeof source === "object" && !Array.isArray(source)) {
    for (const [tickerValue, stateValue] of Object.entries(source)) {
      const ticker = normalizeTicker(tickerValue);
      const marketState = normalizeMarketState(stateValue);
      if (ticker && marketState) states.set(ticker, marketState);
    }
  }
  return states;
}

function getMonitorSnapshots(payload) {
  const source =
    payload?.monitor_snapshots ??
    payload?.bridge_universe?.monitor_snapshots ??
    [];

  return Array.isArray(source)
    ? source
    : [];
}

function createSnapshotMap(payload) {
  const map = new Map();

  for (const item of getMonitorSnapshots(payload)) {
    if (!item || typeof item !== "object") {
      continue;
    }

    const symbol = String(
      firstDefined(
        item.symbol,
        item.instrument,
        item.key,
      ) ?? "",
    )
      .trim()
      .toUpperCase();

    if (!symbol) {
      continue;
    }

    map.set(symbol, item);
  }

  return map;
}

function getMarketState(payload) {
  return normalizeMarketState(
    firstDefined(
      payload?.market_state,
      payload?.bridge_universe?.market_state,
    ),
  );
}

function resolveSessionLabel(snapshot, marketName) {
  const explicit = String(snapshot?.session ?? "").trim().toLowerCase();
  if (explicit === "day") return "주간";
  if (explicit === "night") return "야간";
  if (explicit === "closed") return "장마감";
  if (explicit === "regular") return "정규장";

  const source = String(firstDefined(snapshot?.source, snapshot?.service, "")).toUpperCase();
  if (source.includes("CMEC_R")) return "야간";
  if (source.includes("FC_R")) return "주간";
  if (marketName === "KOSPI") return "정규장";
  return "-";
}

function resolveBusinessTime(snapshot) {
  const candidates = [
    snapshot?.business_time,
    snapshot?.businessTime,
    snapshot?.market_time,
    snapshot?.time,
    snapshot?.observed_at,
  ];
  for (const candidate of candidates) {
    const normalized = normalizeClockText(candidate);
    if (normalized) return normalized;
  }
  return null;
}


/* =========================================================
   6. Status Resolution
   ========================================================= */

function resolveHoldingStatus(snapshot, marketState = "") {
  const explicitStatus = normalizeStatus(
    firstDefined(
      snapshot?.state,
      snapshot?.status,
    ),
  );

  if (explicitStatus) {
    if (
      explicitStatus === STATUS.LIVE &&
      normalizeMarketState(marketState || snapshot?.market_state) === "extended"
    ) {
      return STATUS.EXTENDED;
    }
    return explicitStatus;
  }

  if (!snapshot) {
    return STATUS.WARMING;
  }

  if (normalizeMarketState(marketState || snapshot?.market_state) === "extended") {
    return STATUS.EXTENDED;
  }

  if (isCashMarketClosed()) {
    return STATUS.CLOSED;
  }

  if (!state.bridgeConnected) {
    return STATUS.STALE;
  }

  if (
    isCashMarketOpen() &&
    isRecent(snapshot.observed_at)
  ) {
    return STATUS.LIVE;
  }

  if (isCashMarketOpen()) {
    return STATUS.STALE;
  }

  return isRecent(snapshot.observed_at)
    ? STATUS.LIVE
    : STATUS.STALE;
}

function resolveKospiStatus(snapshot) {
  const explicitStatus = normalizeStatus(
    firstDefined(
      snapshot?.state,
      snapshot?.status,
    ),
  );

  if (explicitStatus) {
    return explicitStatus;
  }

  if (!snapshot) {
    return STATUS.WARMING;
  }

  if (isCashMarketClosed()) {
    return STATUS.CLOSED;
  }

  if (!state.bridgeConnected) {
    return STATUS.STALE;
  }

  if (
    isCashMarketOpen() &&
    isRecent(snapshot.observed_at)
  ) {
    return STATUS.LIVE;
  }

  if (isCashMarketOpen()) {
    return STATUS.STALE;
  }

  return isRecent(snapshot.observed_at)
    ? STATUS.LIVE
    : STATUS.STALE;
}

function resolveK200Status(snapshot) {
  if (!snapshot) {
    return STATUS.WARMING;
  }

  /*
   * K200 선물은 현물 KOSPI와 거래시간이 다르므로 별도 세션 상태를 사용한다.
   * monitor_snapshots에 값이 남아 있어도 Bridge heartbeat가 끊기면 최근 값까지 지연으로 본다.
   */
  if (!state.bridgeConnected) {
    return STATUS.STALE;
  }

  if (isRecent(snapshot.observed_at)) {
    return STATUS.LIVE;
  }

  return state.k200MarketOpen
    ? STATUS.STALE
    : STATUS.CLOSED;
}


/* =========================================================
   7. State Normalization
   ========================================================= */

function normalizeMarket(
  snapshot,
  name,
  statusResolver,
) {
  if (!snapshot) {
    return {
      ...createEmptyMarket(name),
      status: STATUS.WARMING,
    };
  }

  return {
    name,
    status: statusResolver(snapshot),

    price: toFiniteNumber(
      firstDefined(
        snapshot.price,
        snapshot.current_price,
      ),
    ),

    changePct: toFiniteNumber(
      firstDefined(
        snapshot.change_pct,
        snapshot.changePercent,
      ),
    ),

    sessionLabel: resolveSessionLabel(snapshot, name),

    businessTime:
      resolveBusinessTime(snapshot),

    observedAt:
      firstDefined(
        snapshot.observed_at,
        snapshot.observedAt,
      ) ?? null,
  };
}

function normalizeHolding(
  ticker,
  name,
  snapshot,
  marketState = "",
) {
  const effectiveSnapshot = snapshot
    ? { ...snapshot, market_state: marketState || snapshot.market_state }
    : null;
  return {
    ticker,
    name: name || ticker,

    status:
      resolveHoldingStatus(effectiveSnapshot, marketState),

    price: toFiniteNumber(
      firstDefined(
        effectiveSnapshot?.price,
        effectiveSnapshot?.current_price,
      ),
    ),

    changePct: toFiniteNumber(
      firstDefined(
        effectiveSnapshot?.change_pct,
        effectiveSnapshot?.changePercent,
      ),
    ),

    // SC_R 원본 전일대비 금액만 사용한다. legacy durable snapshot에 값이 없으면
    // 등락률에서 역산하지 않고 금액 표시를 비운다.
    changeAmount: toFiniteNumber(
      firstDefined(
        effectiveSnapshot?.change_amount,
        effectiveSnapshot?.changeAmount,
      ),
    ),

    businessTime:
      resolveBusinessTime(effectiveSnapshot),

    observedAt:
      firstDefined(
        effectiveSnapshot?.observed_at,
        effectiveSnapshot?.observedAt,
      ) ?? null,
  };
}

function normalizePayload(payload) {
  const snapshots = createSnapshotMap(payload);

  const tickerNames = getTickerNames(payload);
  const tickerMarketStates = getTickerMarketStates(payload);
  const tickers = getDashboardTickers(payload);

  state.marketState = getMarketState(payload);
  state.bridgeConnected = payload?.bridge_connected !== false;
  state.k200MarketOpen = Boolean(payload?.k200_market_open);

  const k200Snapshot =
    snapshots.get(MARKET_SYMBOLS.K200);

  const kospiSnapshot =
    snapshots.get(MARKET_SYMBOLS.KOSPI);

  const holdings = tickers.map((ticker) => {
    const snapshot =
      snapshots.get(`KRX:${ticker}`);

    return normalizeHolding(
      ticker,
      tickerNames.get(ticker),
      snapshot,
      tickerMarketStates.get(ticker),
    );
  });

  return {
    markets: {
      k200: normalizeMarket(
        k200Snapshot,
        "K200",
        resolveK200Status,
      ),

      kospi: normalizeMarket(
        kospiSnapshot,
        "KOSPI",
        resolveKospiStatus,
      ),
    },

    holdings,
  };
}


/* =========================================================
   8. Market Rendering
   ========================================================= */

function updateStatusBadge(
  card,
  status,
) {
  card.dataset.status = status;

  const badge =
    card.querySelector(
      '[data-role="status"]',
    );

  if (!badge) {
    return;
  }

  badge.dataset.status = status;
  badge.textContent =
    STATUS_LABEL[status] ??
    STATUS_LABEL[STATUS.WARMING];
}

function updateTrendElement(
  element,
  value,
  amount = null,
) {
  if (!element) {
    return;
  }

  element.dataset.trend =
    resolveTrend(value);

  const pctText =
    formatChangePct(value);
  const amountText =
    formatChangeAmount(amount);

  element.textContent = amountText
    ? `${pctText}  ${amountText}`
    : pctText;
}

function updateMarketCard(
  card,
  market,
) {
  updateStatusBadge(
    card,
    market.status,
  );

  const price =
    card.querySelector(
      '[data-role="price"]',
    );

  const change =
    card.querySelector(
      '[data-role="change"]',
    );

  const session =
    card.querySelector(
      '[data-role="session"]',
    );

  const time =
    card.querySelector(
      '[data-role="time"]',
    );

  if (price) {
    price.textContent =
      formatPrice(market.price);
  }

  updateTrendElement(
    change,
    market.changePct,
  );

  if (session) {
    session.textContent =
      market.sessionLabel || "-";
  }

  if (time) {
    time.textContent =
      market.businessTime ||
      "--:--:--";
  }
}


/* =========================================================
   9. Holdings Rendering
   ========================================================= */

function getHoldingCard(ticker) {
  return dom.holdingsGrid.querySelector(
    `.holding-card[data-ticker="${CSS.escape(
      ticker,
    )}"]`,
  );
}

function createHoldingCard(item) {
  const fragment =
    dom.holdingTemplate.content
      .cloneNode(true);

  const card =
    fragment.querySelector(
      ".holding-card",
    );

  card.dataset.ticker =
    item.ticker;

  dom.holdingsGrid.append(fragment);

  return getHoldingCard(item.ticker);
}

function removeObsoleteHoldingCards(
  holdings,
) {
  const activeTickers = new Set(
    holdings.map(
      (item) => item.ticker,
    ),
  );

  const cards =
    dom.holdingsGrid.querySelectorAll(
      ".holding-card",
    );

  for (const card of cards) {
    if (
      !activeTickers.has(
        card.dataset.ticker,
      )
    ) {
      card.remove();
    }
  }
}

function updateHoldingCard(
  card,
  item,
) {
  card.dataset.status =
    item.status;

  const name =
    card.querySelector(
      '[data-role="name"]',
    );

  const ticker =
    card.querySelector(
      '[data-role="ticker"]',
    );

  const status =
    card.querySelector(
      '[data-role="status"]',
    );

  const price =
    card.querySelector(
      '[data-role="price"]',
    );

  const change =
    card.querySelector(
      '[data-role="change"]',
    );

  const time =
    card.querySelector(
      '[data-role="time"]',
    );

  if (name) {
    name.textContent =
      item.name;
  }

  if (ticker) {
    ticker.textContent =
      item.ticker;
  }

  if (status) {
    status.dataset.status =
      item.status;

    status.textContent =
      STATUS_LABEL[item.status] ??
      STATUS_LABEL[STATUS.WARMING];
  }

  if (price) {
    price.textContent =
      formatPrice(item.price);
  }

  updateTrendElement(
    change,
    item.changePct,
    item.changeAmount,
  );

  if (time) {
    time.textContent =
      item.businessTime ||
      "--:--:--";
  }
}

function renderHoldings(
  holdings,
) {
  const empty =
    dom.holdingsGrid.querySelector(
      ".holdings-empty",
    );

  if (!holdings.length) {
    removeObsoleteHoldingCards([]);

    if (!empty) {
      const message =
        document.createElement("div");

      message.className =
        "holdings-empty";

      message.textContent =
        "현재 표시할 보유종목이 없습니다.";

      dom.holdingsGrid.append(
        message,
      );
    }

    dom.holdingsGrid.setAttribute(
      "aria-busy",
      "false",
    );

    return;
  }

  empty?.remove();

  removeObsoleteHoldingCards(
    holdings,
  );

  for (const item of holdings) {
    const card =
      getHoldingCard(item.ticker) ??
      createHoldingCard(item);

    if (card) {
      updateHoldingCard(
        card,
        item,
      );
    }
  }

  /*
   * dashboard_display_tickers가 있으면 Dashboard 현황표와 같은 표시 순서를 따른다.
   * 구버전 backend에서는 dashboard_tickers를 fallback으로 쓰며 별도 정렬은 하지 않는다.
   */
  for (const item of holdings) {
    const card =
      getHoldingCard(item.ticker);

    if (card) {
      dom.holdingsGrid.append(
        card,
      );
    }
  }

  dom.holdingsGrid.setAttribute(
    "aria-busy",
    "false",
  );
}


/* =========================================================
   10. Summary Rendering
   ========================================================= */

function summarizeHoldings(
  holdings,
) {
  const summary = {
    total: holdings.length,
    live: 0,
    extended: 0,
    closed: 0,
    warming: 0,
    stale: 0,
    error: 0,
  };

  for (const item of holdings) {
    if (
      Object.hasOwn(
        summary,
        item.status,
      )
    ) {
      summary[item.status] += 1;
    }
  }

  return summary;
}

function renderSummary(
  holdings,
) {
  const summary =
    summarizeHoldings(holdings);

  dom.summaryTotal.textContent =
    String(summary.total);

  dom.summaryLive.textContent =
    String(summary.live);

  dom.summaryExtended.textContent =
    String(summary.extended);

  dom.summaryClosed.textContent =
    String(summary.closed);

  dom.summaryWarming.textContent =
    String(summary.warming);

  dom.summaryStale.textContent =
    String(summary.stale);

  dom.summaryError.textContent =
    String(summary.error);
}


/* =========================================================
   11. Connection Rendering
   ========================================================= */

function setConnectionState(connected, detail = null) {
  state.connected = connected;

  const status = connected
    ? STATUS.LIVE
    : STATUS.ERROR;

  dom.systemStatus.dataset.status = status;
  dom.systemStatusText.textContent = connected
    ? "시스템 정상"
    : (detail || "연결 지연");

  dom.footerStatusDot.dataset.status = status;
  dom.footerConnection.textContent = connected
    ? "정상"
    : (detail || "지연");
}

function renderLastReceived() {
  dom.footerLastReceived.textContent =
    state.lastReceivedAt
      ? formatClock(
          state.lastReceivedAt,
        )
      : "--:--:--";
}


/* =========================================================
   12. Main Rendering
   ========================================================= */

function renderMonitor() {
  updateMarketCard(
    dom.marketK200,
    state.markets.k200,
  );

  updateMarketCard(
    dom.marketKospi,
    state.markets.kospi,
  );

  renderHoldings(
    state.holdings,
  );

  renderSummary(
    state.holdings,
  );

  renderLastReceived();
  publishEmbeddedContentSize();
}


/* =========================================================
   13. API
   ========================================================= */

async function fetchMonitorData() {
  const sequence = ++state.requestSequence;
  const controller = new AbortController();
  state.requestController = controller;
  const timeoutId = window.setTimeout(
    () => {
      controller.monitorAbortReason = "timeout";
      controller.abort();
    },
    REQUEST_TIMEOUT_MS,
  );

  try {
    const response = await fetch(API_URL, {
      method: "GET",
      cache: "no-store",
      headers: { Accept: "application/json" },
      signal: controller.signal,
    });

    if (!response.ok) {
      throw new Error(`HTTP ${response.status}`);
    }

    const payload = await response.json();
    if (sequence < state.appliedSequence) {
      return;
    }

    const normalized = normalizePayload(payload);
    state.appliedSequence = sequence;
    state.markets = normalized.markets;
    state.holdings = normalized.holdings;
    state.lastReceivedAt = new Date();

    setConnectionState(
      state.bridgeConnected,
      state.bridgeConnected ? null : "Bridge 지연",
    );
    renderMonitor();
  } catch (error) {
    if (error?.name === "AbortError" && controller.monitorAbortReason === "timeout") {
      const timeoutError = new Error("Monitor API request timed out");
      timeoutError.name = "TimeoutError";
      throw timeoutError;
    }
    throw error;
  } finally {
    window.clearTimeout(timeoutId);
    if (state.requestController === controller) {
      state.requestController = null;
    }
  }
}

async function pollMonitor() {
  if (state.requestController || document.visibilityState !== "visible") {
    return;
  }

  try {
    await fetchMonitorData();
  } catch (error) {
    if (error?.name === "AbortError") {
      return;
    }
    console.warn("[Market AI Monitor]", error);
    setConnectionState(false, "API 지연");
  }
}

function scheduleNextPoll() {
  if (state.pollTimer || document.visibilityState !== "visible") {
    return;
  }

  state.pollTimer = window.setTimeout(async () => {
    state.pollTimer = null;
    await pollMonitor();
    scheduleNextPoll();
  }, POLL_INTERVAL_MS);
}

function startPolling() {
  stopPolling();
  if (document.visibilityState !== "visible") {
    return;
  }

  void pollMonitor().finally(scheduleNextPoll);
}

function stopPolling() {
  if (state.pollTimer) {
    window.clearTimeout(state.pollTimer);
    state.pollTimer = null;
  }

  if (state.requestController) {
    const controller = state.requestController;
    state.requestController = null;
    controller.monitorAbortReason = "lifecycle";
    controller.abort();
  }
}


/* =========================================================
   14. Theme
   ========================================================= */

function getPreferredTheme() {
  const stored =
    localStorage.getItem(
      THEME_STORAGE_KEY,
    );

  if (
    stored === "light" ||
    stored === "dark"
  ) {
    return stored;
  }

  return window.matchMedia(
    "(prefers-color-scheme: dark)",
  ).matches
    ? "dark"
    : "light";
}

function applyTheme(theme, { notifyParent = false } = {}) {
  const normalizedTheme =
    theme === "dark" ? "dark" : "light";

  dom.root.dataset.theme =
    normalizedTheme;

  localStorage.setItem(
    THEME_STORAGE_KEY,
    normalizedTheme,
  );

  dom.themeToggle.setAttribute(
    "aria-label",
    normalizedTheme === "dark"
      ? "라이트 테마로 변경"
      : "다크 테마로 변경",
  );

  dom.themeToggle.setAttribute(
    "title",
    normalizedTheme === "dark"
      ? "라이트 테마로 변경"
      : "다크 테마로 변경",
  );

  if (notifyParent) {
    publishEmbeddedThemeMessage(
      EMBED_THEME_CHANGE_MESSAGE,
      normalizedTheme,
    );
  }
}

function toggleTheme() {
  applyTheme(
    dom.root.dataset.theme === "dark"
      ? "light"
      : "dark",
    { notifyParent: true },
  );
}


/* =========================================================
   15. Clock
   ========================================================= */

function updateClock() {
  dom.systemClock.textContent =
    formatClock(new Date());
}

function startClock() {
  updateClock();

  window.setInterval(
    updateClock,
    1_000,
  );
}


/* =========================================================
   16. Embedded Host Bridge
   ========================================================= */

function publishEmbeddedThemeMessage(type, theme) {
  if (window.parent === window) {
    return;
  }

  window.parent.postMessage(
    { type, theme },
    "*",
  );
}

function publishEmbeddedThemeReady() {
  if (window.parent === window) {
    return;
  }

  window.parent.postMessage(
    { type: EMBED_THEME_READY_MESSAGE },
    "*",
  );
}

function handleEmbeddedThemeMessage(event) {
  if (
    window.parent === window ||
    event.source !== window.parent
  ) {
    return;
  }

  const payload = event.data;
  if (
    !payload ||
    payload.type !== EMBED_THEME_STATE_MESSAGE ||
    (payload.theme !== "light" && payload.theme !== "dark")
  ) {
    return;
  }

  applyTheme(payload.theme);
}

let embedSizeFrame = 0;

function publishEmbeddedContentSize() {
  if (window.parent === window || !dom.shell) {
    return;
  }

  if (embedSizeFrame) {
    window.cancelAnimationFrame(embedSizeFrame);
  }

  embedSizeFrame = window.requestAnimationFrame(() => {
    embedSizeFrame = 0;
    const height = Math.ceil(Math.max(
      dom.shell.scrollHeight,
      dom.shell.offsetHeight,
      dom.shell.getBoundingClientRect().height,
    ));
    if (height <= 0) {
      return;
    }
    window.parent.postMessage(
      { type: EMBED_SIZE_MESSAGE, height },
      "*",
    );
  });
}

function observeEmbeddedContentSize() {
  if (window.parent === window || !dom.shell) {
    return;
  }
  publishEmbeddedContentSize();
  if (typeof ResizeObserver === "function") {
    const observer = new ResizeObserver(publishEmbeddedContentSize);
    observer.observe(dom.shell);
  } else {
    window.addEventListener("resize", publishEmbeddedContentSize, { passive: true });
  }
  window.addEventListener("beforeprint", publishEmbeddedContentSize);
  const printMedia = window.matchMedia?.("print");
  printMedia?.addEventListener?.("change", event => {
    if (event.matches) {
      publishEmbeddedContentSize();
    }
  });
}


/* =========================================================
   17. Lifecycle
   ========================================================= */

function bindEvents() {
  dom.themeToggle.addEventListener(
    "click",
    toggleTheme,
  );

  window.addEventListener(
    "message",
    handleEmbeddedThemeMessage,
  );

  document.addEventListener("visibilitychange", () => {
    if (document.visibilityState === "visible") {
      startPolling();
    } else {
      stopPolling();
    }
  });

  window.addEventListener(
    "beforeunload",
    stopPolling,
  );
}

function bootstrap() {
  if (window.parent !== window) {
    dom.root.classList.add("monitor-embedded");
  }

  applyTheme(
    getPreferredTheme(),
  );

  bindEvents();
  publishEmbeddedThemeReady();
  observeEmbeddedContentSize();
  startClock();
  startPolling();
}

bootstrap();