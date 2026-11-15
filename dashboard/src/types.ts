export type BookKey = "SPY" | "QQQ";
export type WindowKey = "2003-2026" | "2007-2026";
export type ViewKey = "overview" | "trades" | "research" | "system";

export interface Metrics {
  trades: number;
  win_rate: number;
  profit_factor: number;
  net: number;
  return_pct: number;
  max_drawdown: number;
  max_drawdown_pct: number;
  sharpe: number;
  net_per_month: number;
  positive_months: number;
  months: number;
  positive_years: number;
  years: number;
  costs: number;
}

export interface Book {
  symbol: BookKey;
  name: string;
  window: WindowKey;
  quantity: number;
  starting_equity: number;
  preset: string;
  source_report: string;
  start: string;
  end: string;
  metrics: Metrics;
}

export interface EquityPoint {
  date: string;
  equity: number;
  drawdown: number;
  daily_pnl: number;
}

export interface MonthlyPoint {
  month: string;
  net: number;
}

export interface Overview {
  symbol: BookKey;
  name: string;
  window: WindowKey;
  start: string;
  end: string;
  quantity: number;
  starting_equity: number;
  preset: string;
  source_report: string;
  metrics: Metrics;
  equity: EquityPoint[];
  monthly: MonthlyPoint[];
  trade_count: number;
}

export interface Trade {
  id: number;
  side: "LONG" | "SHORT";
  entry_time: string | null;
  exit_time: string | null;
  entry_price: number;
  exit_price: number;
  stop_price: number;
  target_price: number;
  exit_reason: string;
  quantity: number;
  net_pnl: number;
  costs: number;
  bars_held: number;
  channel_width: number;
}

export interface TradePage {
  symbol: BookKey;
  window: WindowKey;
  total: number;
  limit: number;
  offset: number;
  rows: Trade[];
}

export interface FuturesRow {
  symbol: string;
  asset_class: string;
  description: string;
  bars: number;
  trades: number;
  win_rate: number;
  profit_factor: number;
  net: number;
  net_per_month: number;
  max_drawdown: number;
  positive_years: number;
  years: number;
  verdict: string;
}

export interface ControlStatus {
  enabled: boolean;
  running: boolean;
  pid: number | null;
  symbol: string | null;
  mode: string | null;
  started_at: string | null;
  log_file: string | null;
  last_action: string | null;
  last_action_at: string | null;
  note: string;
}

export interface AuditEvent {
  at: string | null;
  action: string;
  actor: string;
}

export interface SystemInfo {
  app: string;
  version: string;
  database: {
    dialect: string;
    url: string;
    instruments: number;
    runs: number;
    trades: number;
    equity_points: number;
  };
  brokers_configured: Record<string, boolean>;
  packages: Record<string, boolean>;
  control: ControlStatus;
  latest_audit: AuditEvent[];
}
