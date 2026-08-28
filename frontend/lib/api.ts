/*
  The API client.

  One place that knows the backend URL and one shape for errors, so no component
  has to think about either. Every response type mirrors what
  `simin.api.app` actually returns — where the backend can send `null` for a
  measurement that has not been taken, the type says `| null` rather than
  defaulting to 0, because a 0 rendered where "not measured" belongs is exactly
  the lie the whole product is built to avoid.
*/

export const API =
  process.env.NEXT_PUBLIC_API_URL?.replace(/\/$/, '') || 'http://localhost:8000';

export class ApiError extends Error {
  constructor(message: string, readonly status: number) {
    super(message);
    this.name = 'ApiError';
  }
}

async function call<T>(path: string, init?: RequestInit): Promise<T> {
  let res: Response;
  try {
    res = await fetch(`${API}${path}`, {
      ...init,
      headers: { 'Content-Type': 'application/json', ...(init?.headers ?? {}) },
      cache: 'no-store',
    });
  } catch {
    throw new ApiError(`Cannot reach the Simin API at ${API}`, 0);
  }
  const text = await res.text();
  let body: unknown = null;
  if (text) {
    try {
      body = JSON.parse(text);
    } catch {
      body = text;
    }
  }
  if (!res.ok) {
    const detail =
      body && typeof body === 'object' && 'detail' in body
        ? String((body as { detail: unknown }).detail)
        : typeof body === 'string' && body
          ? body
          : `Request failed with ${res.status}`;
    throw new ApiError(detail, res.status);
  }
  return body as T;
}

export const get = <T,>(path: string) => call<T>(path);
export const post = <T,>(path: string, payload?: unknown) =>
  call<T>(path, { method: 'POST', body: JSON.stringify(payload ?? {}) });

// ── Types ──────────────────────────────────────────────────────────────────

export interface DialLevel {
  level: number;
  name_en: string;
  name_fa: string;
  description_en: string;
  description_fa: string;
  target_monthly_return: number;
  /** Null until calibration has run. Never render this as zero. */
  measured_monthly_return: number | null;
  measured_monthly_p05?: number | null;
  measured_monthly_p95?: number | null;
  measured_max_drawdown: number | null;
  measured_max_drawdown_p95?: number | null;
  ruin_probability: number | null;
  win_rate?: number;
  profit_factor?: number;
  sharpe?: number;
  trades_per_month?: number;
  calibrated: boolean;
  calibrated_at?: string;
  sample_months?: number;
  symbol_scope?: string;
  leverage: number;
  risk_per_trade: number;
  worst_case_day: number;
  kind: string;
  warnings_en: string[];
  warnings_fa: string[];
  signal_timeframe: string;
  context_timeframe: string;
  max_positions: number;
  max_trades_per_day: number;
  min_confluence: number;
  allow_shorts: boolean;
  daily_loss_halt: number;
  max_drawdown_halt: number;
  take_profit_r: number;
  atr_stop_mult: number;
  strategies: string[];
}

export interface DialResponse {
  levels: DialLevel[];
  any_calibrated: boolean;
  disclaimer_en: string;
  disclaimer_fa: string;
}

export interface Venue {
  name: string;
  display_name: string;
  supports_futures: boolean;
  supports_shorts: boolean;
  max_leverage: number;
  quote_asset: string;
  notes_en: string;
  notes_fa: string;
  credentials_configured: boolean;
  round_trip_cost: number;
}

export interface PositionView {
  symbol: string;
  direction: string;
  qty: number;
  entry_price: number;
  current_price: number;
  stop_price: number;
  take_profit: number | null;
  /** Null when the position is unleveraged and therefore cannot be liquidated. */
  liquidation_price: number | null;
  leverage: number;
  unrealized: number;
  r_multiple: number;
  opened_at: string;
  bars_held: number;
  strategy: string;
  risk_amount: number;
  breakeven_armed: boolean;
}

export interface BotEvent {
  ts: string;
  kind: string;
  symbol: string;
  message: string;
  message_fa: string;
  data: Record<string, unknown>;
}

export interface BotStatus {
  state: string;
  running?: boolean;
  mode?: string;
  venue?: string;
  risk_level?: number;
  profile_name?: string;
  started_at?: string | null;
  last_bar?: string | null;
  equity?: number;
  starting_equity?: number;
  cash?: number;
  return_pct?: number;
  open_positions?: number;
  drawdown?: number;
  day_realised?: number;
  trades_today?: number;
  total_trades?: number;
  halt?: string;
  halt_note?: string;
  error?: string;
  profile_clamped?: boolean;
  positions?: PositionView[];
  events?: BotEvent[];
  decisions?: Record<
    string,
    {
      accepted: boolean;
      regime: string;
      score: number;
      threshold: number;
      reason: string;
      agreeing: string[];
      dissenting: string[];
    }
  >;
  profile?: Partial<DialLevel>;
}

export interface Metrics {
  start_equity: number;
  end_equity: number;
  total_return: number;
  monthly_return: number;
  annualised_return: number;
  max_drawdown: number;
  max_drawdown_duration_days: number;
  sharpe: number;
  sortino: number;
  calmar: number;
  trades: number;
  win_rate: number;
  profit_factor: number;
  expectancy_r: number;
  avg_win_r: number;
  avg_loss_r: number;
  largest_win: number;
  largest_loss: number;
  max_consecutive_losses: number;
  avg_bars_held: number;
  trades_per_month: number;
  total_fees: number;
  total_funding: number;
  cost_drag: number;
  exposure: number;
  exit_breakdown: Record<string, number>;
  days: number;
  is_viable: boolean;
}

export interface Gate {
  name: string;
  passed: boolean;
  detail: string;
  critical: boolean;
}

export interface MonteCarlo {
  runs: number;
  final_return_p05: number;
  final_return_p25: number;
  final_return_median: number;
  final_return_p75: number;
  final_return_p95: number;
  max_drawdown_median: number;
  max_drawdown_p95: number;
  max_drawdown_worst: number;
  prob_loss: number;
  prob_half_loss: number;
  prob_ruin: number;
  monthly_return_median: number;
  monthly_return_p05: number;
  monthly_return_p95: number;
}

export interface BacktestResult {
  metrics: Metrics;
  profile_level: number;
  symbols: string[];
  timeframe: string;
  bars: number;
  rejections: Record<string, number>;
  signals_generated: number;
  signals_taken: number;
  signal_take_rate: number;
  halted_at: string;
  halt_reason: string;
  trades: Array<{
    symbol: string;
    direction: string;
    entry_price: number;
    exit_price: number;
    opened_at: string;
    closed_at: string;
    net_pnl: number;
    r_multiple: number;
    reason: string;
    strategy: string;
    leverage: number;
    fees: number;
  }>;
  curve: Array<{ ts: string; equity: number; drawdown: number; positions: number }>;
  benchmark: Metrics;
  stressed_2x_costs: Metrics;
  walk_forward: {
    windows: number;
    oos_consistency: number;
    degradation: number;
  } | null;
  monte_carlo: MonteCarlo | null;
  gates: { passed: boolean; gates: Gate[] };
  target_monthly_return: number;
  cost_model: { round_trip: number; breakeven_move: number; venue: string };
}

// ── Endpoints ──────────────────────────────────────────────────────────────

export const api = {
  health: () => get<{ status: string; mode: string; bot: string }>('/health'),
  dial: () => get<DialResponse>('/api/dial'),
  venues: () => get<{ venues: Venue[] }>('/api/venues'),
  strategies: () =>
    get<{
      strategies: Array<{
        name: string;
        name_fa: string;
        regime: string;
        description: string;
        description_fa: string;
      }>;
      by_level: Record<string, string[]>;
    }>('/api/strategies'),
  settings: () =>
    get<{
      mode: string;
      venue: string;
      risk_level: number;
      symbols: string[];
      starting_equity: number;
      credentials: Record<string, boolean>;
      start_problems: string[];
      trading_frozen: boolean;
    }>('/api/settings'),

  bot: () => get<BotStatus>('/api/bot'),
  start: (body: {
    risk_level: number;
    mode: string;
    venue: string;
    symbols: string[];
    starting_equity: number;
    confirmation?: string;
  }) => post<{ started: boolean; clamped: boolean; warnings_en: string[]; warnings_fa: string[] }>(
    '/api/bot/start',
    body,
  ),
  stop: (flatten: boolean) => post<unknown>('/api/bot/stop', { flatten }),
  pause: () => post<{ state: string }>('/api/bot/pause'),
  resume: () => post<{ state: string }>('/api/bot/resume'),
  kill: () => post<{ state: string }>('/api/bot/kill'),
  setRisk: (level: number) => post<unknown>('/api/bot/risk', { level }),
  flatten: () => post<{ closed: number }>('/api/bot/flatten'),

  equity: () =>
    get<{
      curve: Array<{ ts: string; equity: number; drawdown: number; positions: number }>;
      trades: BacktestResult['trades'];
    }>('/api/equity'),

  candles: (symbol: string, timeframe: string, limit = 300) =>
    get<{
      symbol: string;
      timeframe: string;
      candles: Array<{ ts: string; o: number; h: number; l: number; c: number; v: number }>;
      indicators: Record<string, Array<number | null>>;
      levels: Array<{ price: number; touches: number; strength: number; kind: string }>;
      structure: string;
    }>(`/api/candles?symbol=${symbol}&timeframe=${timeframe}&limit=${limit}`),

  backtest: (body: {
    risk_level: number;
    symbols: string[];
    venue: string;
    timeframe?: string;
    bars: number;
    starting_equity: number;
    walk_forward?: boolean;
  }) => post<BacktestResult>('/api/lab/backtest', body),

  calibrate: (body: {
    risk_level: number;
    symbols: string[];
    venue: string;
    bars: number;
    starting_equity: number;
  }) => post<unknown>('/api/lab/calibrate', body),
};
