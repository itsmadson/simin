/*
  Bilingual strings, Persian first.

  Persian is not a translation bolted onto an English product here — the app is
  named سیمین and the primary user reads Persian, so `fa` is the default and
  every string exists in both. A missing `fa` key falls back to `en` loudly
  enough to be spotted in review rather than silently shipping half-English UI.
*/

export type Lang = 'fa' | 'en';

export const DICT = {
  // Navigation
  dial: { en: 'Risk Dial', fa: 'درجهٔ ریسک' },
  live: { en: 'Live', fa: 'زنده' },
  lab: { en: 'Lab', fa: 'آزمایشگاه' },
  markets: { en: 'Markets', fa: 'بازارها' },
  settings: { en: 'Settings', fa: 'تنظیمات' },

  // Modes
  modeLab: { en: 'LAB', fa: 'آزمایشی' },
  modeReal: { en: 'REAL', fa: 'واقعی' },
  labExplain: {
    en: 'Simulated money over real prices. Nothing can be spent.',
    fa: 'پول شبیه‌سازی‌شده روی قیمت واقعی. هیچ پولی خرج نمی‌شود.',
  },
  realExplain: {
    en: 'Real orders on a real account. Real money can be lost.',
    fa: 'سفارش واقعی روی حساب واقعی. پول واقعی از دست می‌رود.',
  },

  // The dial
  chooseRisk: { en: 'Choose your risk', fa: 'ریسک خود را انتخاب کنید' },
  dragDial: {
    en: 'Drag the dial, or use the arrow keys.',
    fa: 'درجه را بکشید، یا از کلیدهای جهت‌دار استفاده کنید.',
  },
  target: { en: 'Design target', fa: 'هدف طراحی' },
  measured: { en: 'Measured', fa: 'اندازه‌گیری‌شده' },
  notMeasured: { en: 'not measured yet', fa: 'هنوز اندازه‌گیری نشده' },
  perMonth: { en: '/ month', fa: 'در ماه' },
  honestyGap: { en: 'Honesty gap', fa: 'شکاف صداقت' },
  honestyGapExplain: {
    en: 'The distance between what this level was designed to attempt and what it actually did in walk-forward testing.',
    fa: 'فاصلهٔ میان آنچه این سطح برای رسیدن به آن طراحی شده و آنچه واقعاً در آزمون پیش‌رونده رخ داده است.',
  },
  runCalibration: { en: 'Measure this level', fa: 'این سطح را اندازه بگیر' },
  calibrating: { en: 'Measuring…', fa: 'در حال اندازه‌گیری…' },

  // Dial parameters
  riskPerTrade: { en: 'Risk per trade', fa: 'ریسک هر معامله' },
  leverage: { en: 'Leverage', fa: 'اهرم' },
  timeframe: { en: 'Timeframe', fa: 'تایم‌فریم' },
  positions: { en: 'Positions', fa: 'موقعیت‌ها' },
  maxPositions: { en: 'Max positions', fa: 'حداکثر موقعیت' },
  tradesPerDay: { en: 'Trades / day', fa: 'معامله در روز' },
  selectivity: { en: 'Entry threshold', fa: 'آستانهٔ ورود' },
  shorts: { en: 'Shorts', fa: 'فروش استقراضی' },
  drawdownHalt: { en: 'Drawdown halt', fa: 'توقف در افت سرمایه' },
  dailyLossHalt: { en: 'Daily loss halt', fa: 'توقف زیان روزانه' },
  worstDay: { en: 'Worst plausible day', fa: 'بدترین روز محتمل' },
  ruinRisk: { en: 'Risk of ruin', fa: 'احتمال نابودی سرمایه' },
  ruinExplain: {
    en: 'Monte Carlo estimate of hitting the drawdown halt. Because losses cluster in bad markets, treat this as a floor.',
    fa: 'برآورد مونت‌کارلو از رسیدن به حد توقف افت سرمایه. چون زیان‌ها در بازار بد خوشه‌ای می‌شوند، این عدد را کف بدانید.',
  },
  maxDrawdown: { en: 'Max drawdown', fa: 'بیشترین افت سرمایه' },
  strategies: { en: 'Strategies', fa: 'استراتژی‌ها' },

  // Bot control
  start: { en: 'Start bot', fa: 'شروع ربات' },
  stop: { en: 'Stop', fa: 'توقف' },
  pause: { en: 'Pause', fa: 'مکث' },
  resume: { en: 'Resume', fa: 'ادامه' },
  kill: { en: 'Kill switch', fa: 'کلید اضطراری' },
  flatten: { en: 'Close all', fa: 'بستن همه' },
  running: { en: 'Running', fa: 'در حال اجرا' },
  paused: { en: 'Paused', fa: 'متوقف موقت' },
  stopped: { en: 'Stopped', fa: 'خاموش' },
  halted: { en: 'Halted', fa: 'متوقف‌شده' },

  // Live
  equity: { en: 'Equity', fa: 'ارزش حساب' },
  openPositions: { en: 'Open positions', fa: 'موقعیت‌های باز' },
  todayPnl: { en: 'Today', fa: 'امروز' },
  totalTrades: { en: 'Trades', fa: 'معاملات' },
  entry: { en: 'Entry', fa: 'ورود' },
  stopLoss: { en: 'Stop', fa: 'حد ضرر' },
  takeProfit: { en: 'Target', fa: 'هدف' },
  liquidation: { en: 'Liquidation', fa: 'لیکویید' },
  current: { en: 'Now', fa: 'اکنون' },
  unrealized: { en: 'Unrealised', fa: 'سود/زیان باز' },
  activity: { en: 'Activity', fa: 'رویدادها' },
  noPositions: { en: 'No open positions', fa: 'موقعیت بازی وجود ندارد' },
  noActivity: { en: 'Nothing has happened yet', fa: 'هنوز رویدادی رخ نداده' },
  botNotRunning: { en: 'No bot is running', fa: 'رباتی در حال اجرا نیست' },
  botNotRunningHint: {
    en: 'Set your risk level, then start the bot.',
    fa: 'سطح ریسک را انتخاب کنید و سپس ربات را روشن کنید.',
  },

  // Lab
  runBacktest: { en: 'Run backtest', fa: 'اجرای بک‌تست' },
  running_: { en: 'Running…', fa: 'در حال اجرا…' },
  symbols: { en: 'Symbols', fa: 'نمادها' },
  venue: { en: 'Exchange', fa: 'صرافی' },
  bars: { en: 'Candles', fa: 'تعداد کندل' },
  startingEquity: { en: 'Starting capital', fa: 'سرمایه اولیه' },
  totalReturn: { en: 'Return', fa: 'بازدهی' },
  monthlyReturn: { en: 'Per month', fa: 'ماهانه' },
  winRate: { en: 'Win rate', fa: 'نرخ برد' },
  profitFactor: { en: 'Profit factor', fa: 'ضریب سود' },
  expectancy: { en: 'Expectancy', fa: 'امید ریاضی' },
  sharpe: { en: 'Sharpe', fa: 'شارپ' },
  fees: { en: 'Fees paid', fa: 'کارمزد پرداختی' },
  buyHold: { en: 'Buy and hold', fa: 'خرید و نگهداری' },
  atDoubleCosts: { en: 'At 2× costs', fa: 'با کارمزد دوبرابر' },
  gates: { en: 'Validation gates', fa: 'دروازه‌های اعتبارسنجی' },
  gatesPassed: { en: 'Passed validation', fa: 'اعتبارسنجی موفق' },
  gatesFailed: { en: 'Failed validation', fa: 'اعتبارسنجی ناموفق' },
  trades: { en: 'Trades', fa: 'معاملات' },
  monteCarlo: { en: 'Monte Carlo', fa: 'مونت‌کارلو' },
  walkForward: { en: 'Walk-forward', fa: 'آزمون پیش‌رونده' },
  worstCase: { en: 'Worst 5%', fa: '۵٪ بدترین' },
  bestCase: { en: 'Best 5%', fa: '۵٪ بهترین' },
  median: { en: 'Median', fa: 'میانه' },

  // Warnings / safety
  warnings: { en: 'Warnings', fa: 'هشدارها' },
  realConfirm: {
    en: 'Type the phrase below exactly to enable real trading.',
    fa: 'برای فعال‌سازی معاملهٔ واقعی، عبارت زیر را دقیقاً تایپ کنید.',
  },
  realPhrase: { en: 'I understand this trades real money', fa: 'I understand this trades real money' },
  cancel: { en: 'Cancel', fa: 'انصراف' },
  confirm: { en: 'Confirm', fa: 'تأیید' },
  spotOnlyWarning: {
    en: 'This exchange is spot only. Leverage has been clamped to 1× and shorting is disabled.',
    fa: 'این صرافی فقط اسپات است. اهرم به ۱ محدود و فروش استقراضی غیرفعال شد.',
  },
  synthetic: {
    en: 'Synthetic data — for learning the interface, not for judging a strategy.',
    fa: 'داده ساختگی — برای آشنایی با رابط کاربری، نه برای قضاوت دربارهٔ استراتژی.',
  },

  // Misc
  noData: { en: 'No data', fa: 'داده‌ای نیست' },
  loading: { en: 'Loading…', fa: 'در حال بارگذاری…' },
  error: { en: 'Error', fa: 'خطا' },
  strategy: { en: 'Strategy', fa: 'استراتژی' },
  reason: { en: 'Reason', fa: 'دلیل' },
  pnl: { en: 'P&L', fa: 'سود/زیان' },
  symbol: { en: 'Symbol', fa: 'نماد' },
  direction: { en: 'Side', fa: 'جهت' },
  long: { en: 'Long', fa: 'خرید' },
  short: { en: 'Short', fa: 'فروش' },
  size: { en: 'Size', fa: 'حجم' },
} as const;

export type Key = keyof typeof DICT;

export function t(key: Key, lang: Lang): string {
  const row = DICT[key];
  if (!row) return key;
  return row[lang] ?? row.en;
}

/** Persian digits, so numerals match the script they sit in. */
const FA_DIGITS = ['۰', '۱', '۲', '۳', '۴', '۵', '۶', '۷', '۸', '۹'];

export function digits(value: string | number, lang: Lang): string {
  const s = String(value);
  if (lang !== 'fa') return s;
  return s.replace(/[0-9]/g, (d) => FA_DIGITS[Number(d)]);
}

export function pct(value: number | null | undefined, lang: Lang, places = 1): string {
  if (value === null || value === undefined || !Number.isFinite(value)) return '—';
  const sign = value > 0 ? '+' : '';
  return digits(`${sign}${(value * 100).toFixed(places)}%`, lang);
}

export function money(value: number | null | undefined, lang: Lang, places = 2): string {
  if (value === null || value === undefined || !Number.isFinite(value)) return '—';
  const s = value.toLocaleString('en-US', {
    minimumFractionDigits: places,
    maximumFractionDigits: places,
  });
  return digits(s, lang);
}

export function num(value: number | null | undefined, lang: Lang, places = 2): string {
  if (value === null || value === undefined || !Number.isFinite(value)) return '—';
  return digits(value.toFixed(places), lang);
}
