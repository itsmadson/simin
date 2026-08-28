'use client';

/*
  Lab: run a configuration over history and read the verdict.

  The result layout puts the *comparisons* first — buy-and-hold and the same
  strategy at doubled costs — before the headline return. A +18% return means
  nothing until you know the asset returned +45% over the same window, and a
  strategy that dies when fees double was never profitable, only lucky about
  liquidity. Showing the headline alone is how backtests sell systems that lose
  money.

  The gates panel is a checklist of thirteen lights, and it can go red on a run
  that made money. That is intentional.
*/

import { useCallback, useEffect, useState } from 'react';
import { useApp } from '@/components/Shell';
import { ApiError, api, type BacktestResult, type Venue } from '@/lib/api';
import { digits, money, num, pct, t } from '@/lib/i18n';

export default function LabPage() {
  const { lang, setHeat } = useApp();
  const [venues, setVenues] = useState<Venue[]>([]);
  const [level, setLevel] = useState(4);
  const [venue, setVenue] = useState('offline');
  const [symbols, setSymbols] = useState('BTCUSDT');
  const [bars, setBars] = useState(4000);
  const [equity, setEquity] = useState(10000);
  const [walkForward, setWalkForward] = useState(true);
  const [res, setRes] = useState<BacktestResult | null>(null);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState('');

  useEffect(() => {
    api.venues().then((v) => setVenues(v.venues)).catch(() => {});
  }, []);
  useEffect(() => { setHeat((level - 1) / 9); }, [level, setHeat]);

  const run = useCallback(async () => {
    setBusy(true);
    setErr('');
    setRes(null);
    try {
      const r = await api.backtest({
        risk_level: level,
        symbols: symbols.split(',').map((s) => s.trim().toUpperCase()).filter(Boolean),
        venue,
        bars,
        starting_equity: equity,
        walk_forward: walkForward,
      });
      setRes(r);
    } catch (e) {
      setErr(e instanceof ApiError ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  }, [level, symbols, venue, bars, equity, walkForward]);

  const m = res?.metrics;

  return (
    <div className="rise">
      <header className="head">
        <div>
          <h1>{t('lab', lang)}</h1>
          <p className="sub">
            {lang === 'fa'
              ? 'یک تنظیمات را روی تاریخچه اجرا کنید و نتیجه را بی‌رحمانه ببینید.'
              : 'Run one configuration over history and read the verdict without flattery.'}
          </p>
        </div>
      </header>

      <section className="panel form">
        <div className="fields">
          <label>
            <span className="label">{t('dial', lang)}</span>
            <input type="number" min={1} max={10} value={level}
                   onChange={(e) => setLevel(Math.max(1, Math.min(10, Number(e.target.value))))} />
          </label>
          <label>
            <span className="label">{t('venue', lang)}</span>
            <select value={venue} onChange={(e) => setVenue(e.target.value)}>
              {venues.map((v) => <option key={v.name} value={v.name}>{v.display_name}</option>)}
            </select>
          </label>
          <label>
            <span className="label">{t('symbols', lang)}</span>
            <input value={symbols} onChange={(e) => setSymbols(e.target.value)} />
          </label>
          <label>
            <span className="label">{t('bars', lang)}</span>
            <input type="number" value={bars} onChange={(e) => setBars(Number(e.target.value))} />
          </label>
          <label>
            <span className="label">{t('startingEquity', lang)}</span>
            <input type="number" value={equity} onChange={(e) => setEquity(Number(e.target.value))} />
          </label>
          <label className="check">
            <input type="checkbox" checked={walkForward} onChange={(e) => setWalkForward(e.target.checked)} />
            <span>{t('walkForward', lang)}</span>
          </label>
        </div>
        <button className="btn primary" onClick={run} disabled={busy} style={{ marginTop: 16 }}>
          {busy ? t('running_', lang) : t('runBacktest', lang)}
        </button>
        {err && <p className="err">{err}</p>}
      </section>

      {busy && <div className="panel loading fade">{t('running_', lang)}</div>}

      {res && m && (
        <>
          {/* ── The comparisons, before the headline ──────────────────────── */}
          <section className="grid g3 compare fade">
            <div className="panel cmp">
              <div className="label">{t('totalReturn', lang)}</div>
              <div className={`value lg tab ${m.total_return >= 0 ? 'gain' : 'loss'}`}>
                {pct(m.total_return, lang, 1)}
              </div>
              <div className="sub">{pct(m.monthly_return, lang, 2)} {t('perMonth', lang)}</div>
            </div>
            <div className="panel cmp">
              <div className="label">{t('buyHold', lang)}</div>
              <div className={`value lg tab ${res.benchmark.total_return >= 0 ? 'gain' : 'loss'}`}>
                {pct(res.benchmark.total_return, lang, 1)}
              </div>
              <div className={`sub ${m.total_return > res.benchmark.total_return ? 'gain' : 'loss'}`}>
                {m.total_return > res.benchmark.total_return
                  ? (lang === 'fa' ? 'ربات بهتر بود' : 'bot won')
                  : (lang === 'fa' ? 'نگهداری ساده بهتر بود' : 'holding won')}
              </div>
            </div>
            <div className="panel cmp">
              <div className="label">{t('atDoubleCosts', lang)}</div>
              <div className={`value lg tab ${res.stressed_2x_costs.total_return >= 0 ? 'gain' : 'loss'}`}>
                {pct(res.stressed_2x_costs.total_return, lang, 1)}
              </div>
              <div className="sub">
                {lang === 'fa' ? 'کارمزد رفت‌وبرگشت ' : 'round trip '}
                {pct(res.cost_model.round_trip, lang, 2).replace('+', '')}
              </div>
            </div>
          </section>

          {/* ── Target vs measured, again ─────────────────────────────────── */}
          <section className="panel gapbox fade">
            <div className="gapgrid">
              <div>
                <div className="label">{t('target', lang)}</div>
                <div className="value ghost tab">{pct(res.target_monthly_return, lang, 0)}</div>
              </div>
              <div className="arrow">→</div>
              <div>
                <div className="label">{t('measured', lang)}</div>
                <div className={`value tab ${m.monthly_return >= 0 ? 'gain' : 'loss'}`}>
                  {pct(m.monthly_return, lang, 2)}
                </div>
              </div>
              <div className="gaptext">
                <div className="label">{t('honestyGap', lang)}</div>
                <p>{t('honestyGapExplain', lang)}</p>
              </div>
            </div>
          </section>

          <section className="grid g4 stats fade">
            <Stat label={t('maxDrawdown', lang)} value={pct(m.max_drawdown, lang, 1).replace('+', '')} tone="loss"
                  sub={`${digits(m.max_drawdown_duration_days.toFixed(0), lang)} ${lang === 'fa' ? 'روز' : 'days'}`} />
            <Stat label={t('trades', lang)} value={digits(m.trades, lang)}
                  sub={`${num(m.trades_per_month, lang, 1)} ${lang === 'fa' ? 'در ماه' : '/mo'}`} />
            <Stat label={t('winRate', lang)} value={pct(m.win_rate, lang, 0).replace('+', '')}
                  sub={`${t('expectancy', lang)} ${num(m.expectancy_r, lang, 3)}R`} />
            <Stat label={t('profitFactor', lang)}
                  value={Number.isFinite(m.profit_factor) ? num(m.profit_factor, lang, 2) : '∞'}
                  tone={m.profit_factor > 1.15 ? 'gain' : 'loss'}
                  sub={`${t('sharpe', lang)} ${num(m.sharpe, lang, 2)}`} />
            <Stat label={t('fees', lang)} value={money(m.total_fees, lang, 0)}
                  sub={`${pct(m.cost_drag, lang, 0).replace('+', '')} ${lang === 'fa' ? 'از سود ناخالص' : 'of gross'}`} />
            <Stat label={lang === 'fa' ? 'سیگنال‌ها' : 'Signals'}
                  value={digits(res.signals_generated, lang)}
                  sub={`${digits(res.signals_taken, lang)} ${lang === 'fa' ? 'گرفته‌شده' : 'taken'}`} />
            <Stat label={lang === 'fa' ? 'بدترین ضرر' : 'Largest loss'} value={money(m.largest_loss, lang, 0)} tone="loss"
                  sub={`${digits(m.max_consecutive_losses, lang)} ${lang === 'fa' ? 'ضرر پیاپی' : 'in a row'}`} />
            <Stat label={lang === 'fa' ? 'زمان در بازار' : 'Exposure'} value={pct(m.exposure, lang, 0).replace('+', '')}
                  sub={`${num(m.avg_bars_held, lang, 0)} ${lang === 'fa' ? 'کندل میانگین' : 'bars avg'}`} />
          </section>

          <div className="cols">
            {/* ── Gates ──────────────────────────────────────────────────── */}
            <section className={`panel gates fade ${res.gates.passed ? 'ok' : 'bad'}`}>
              <div className="ghead">
                <div className="label">{t('gates', lang)}</div>
                <span className={`chip ${res.gates.passed ? 'live' : 'real'}`}>
                  {res.gates.passed ? t('gatesPassed', lang) : t('gatesFailed', lang)}
                </span>
              </div>
              <ul>
                {res.gates.gates.map((g) => (
                  <li key={g.name} className={g.passed ? 'pass' : g.critical ? 'fail' : 'soft'}>
                    <span className="mark">{g.passed ? '✓' : '✕'}</span>
                    <div>
                      <b>{g.name.replace(/_/g, ' ')}</b>
                      <span className="detail">{g.detail}</span>
                    </div>
                  </li>
                ))}
              </ul>
            </section>

            <div className="side">
              {res.monte_carlo && (
                <section className="panel fade">
                  <div className="label">{t('monteCarlo', lang)}</div>
                  <div className="mcrow">
                    <span>{t('worstCase', lang)}</span>
                    <b className="tab loss">{pct(res.monte_carlo.final_return_p05, lang, 1)}</b>
                  </div>
                  <div className="mcrow">
                    <span>{t('median', lang)}</span>
                    <b className={`tab ${res.monte_carlo.final_return_median >= 0 ? 'gain' : 'loss'}`}>
                      {pct(res.monte_carlo.final_return_median, lang, 1)}
                    </b>
                  </div>
                  <div className="mcrow">
                    <span>{t('bestCase', lang)}</span>
                    <b className="tab gain">{pct(res.monte_carlo.final_return_p95, lang, 1)}</b>
                  </div>
                  <div className="mcrow hi">
                    <span>{t('ruinRisk', lang)}</span>
                    <b className="tab" style={{ color: res.monte_carlo.prob_ruin > 0.2 ? 'var(--loss)' : 'var(--warn)' }}>
                      {pct(res.monte_carlo.prob_ruin, lang, 1).replace('+', '')}
                    </b>
                  </div>
                  <div className="mcrow">
                    <span>{lang === 'fa' ? 'احتمال از دست دادن نیمی از حساب' : 'Chance of losing half'}</span>
                    <b className="tab loss">{pct(res.monte_carlo.prob_half_loss, lang, 1).replace('+', '')}</b>
                  </div>
                  <p className="sub tiny">
                    {digits(res.monte_carlo.runs, lang)} {lang === 'fa' ? 'شبیه‌سازی' : 'simulations'}
                  </p>
                </section>
              )}

              {res.walk_forward && (
                <section className="panel fade">
                  <div className="label">{t('walkForward', lang)}</div>
                  <div className="mcrow">
                    <span>{lang === 'fa' ? 'پنجره‌های سودده' : 'Profitable windows'}</span>
                    <b className="tab">{pct(res.walk_forward.oos_consistency, lang, 0).replace('+', '')}</b>
                  </div>
                  <div className="mcrow">
                    <span>{lang === 'fa' ? 'افت نسبت به درون‌نمونه' : 'Degradation'}</span>
                    <b className={`tab ${res.walk_forward.degradation > 0.6 ? 'loss' : ''}`}>
                      {pct(res.walk_forward.degradation, lang, 0).replace('+', '')}
                    </b>
                  </div>
                  <p className="sub tiny">
                    {digits(res.walk_forward.windows, lang)} {lang === 'fa' ? 'پنجره' : 'windows'}
                  </p>
                </section>
              )}

              <section className="panel fade">
                <div className="label">{lang === 'fa' ? 'نحوهٔ خروج' : 'How trades ended'}</div>
                {Object.entries(m.exit_breakdown).map(([k, v]) => (
                  <div key={k} className="mcrow">
                    <span>{k.replace(/_/g, ' ')}</span>
                    <b className="tab">{digits(v, lang)}</b>
                  </div>
                ))}
              </section>
            </div>
          </div>

          {res.trades.length > 0 && (
            <section className="panel fade" style={{ marginTop: 16 }}>
              <div className="label">{t('trades', lang)}</div>
              <div className="tablewrap">
                <table>
                  <thead>
                    <tr>
                      <th>{t('symbol', lang)}</th>
                      <th>{t('direction', lang)}</th>
                      <th className="num">{t('entry', lang)}</th>
                      <th className="num">{lang === 'fa' ? 'خروج' : 'Exit'}</th>
                      <th className="num">{t('pnl', lang)}</th>
                      <th className="num">R</th>
                      <th>{t('reason', lang)}</th>
                      <th>{t('strategy', lang)}</th>
                    </tr>
                  </thead>
                  <tbody>
                    {res.trades.slice(-40).reverse().map((tr, i) => (
                      <tr key={i}>
                        <td><b>{tr.symbol}</b></td>
                        <td>
                          <span className={tr.direction === 'long' ? 'gain' : 'loss'}>
                            {tr.direction === 'long' ? t('long', lang) : t('short', lang)}
                          </span>
                        </td>
                        <td className="num">{money(tr.entry_price, lang, 2)}</td>
                        <td className="num">{money(tr.exit_price, lang, 2)}</td>
                        <td className={`num ${tr.net_pnl >= 0 ? 'gain' : 'loss'}`}>{money(tr.net_pnl, lang, 2)}</td>
                        <td className={`num ${tr.r_multiple >= 0 ? 'gain' : 'loss'}`}>{num(tr.r_multiple, lang, 2)}</td>
                        <td className="dim">{tr.reason.replace(/_/g, ' ')}</td>
                        <td className="dim">{tr.strategy}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </section>
          )}
        </>
      )}

      <style jsx>{`
        .head { margin-bottom: 20px; }
        h1 { font-size: 26px; }
        .form { margin-bottom: 18px; }
        .fields {
          display: grid; grid-template-columns: repeat(auto-fit, minmax(150px, 1fr));
          gap: 14px; align-items: end;
        }
        .fields label { display: flex; flex-direction: column; }
        .fields :global(.label) { margin-bottom: 6px; }
        .fields input, .fields select { width: 100%; }
        .check { flex-direction: row !important; align-items: center; gap: 8px; padding-bottom: 9px; }
        .check input { width: auto; }
        .check span { font-size: 13px; color: var(--silver-2); }
        .err { color: var(--loss); font-size: 13px; margin: 12px 0 0; }
        .loading { text-align: center; padding: 40px; color: var(--silver-3); }

        .compare { margin-bottom: 16px; }
        .cmp { text-align: center; }

        .gapbox { margin-bottom: 16px; }
        .gapgrid {
          display: grid; grid-template-columns: auto auto auto 1fr;
          gap: 22px; align-items: center;
        }
        @media (max-width: 780px) { .gapgrid { grid-template-columns: 1fr; gap: 12px; } .arrow { display: none; } }
        .ghost { color: transparent; -webkit-text-stroke: 1.4px var(--silver-3); }
        .arrow { font-size: 22px; color: var(--silver-4); }
        .gaptext p { margin: 0; font-size: 12px; color: var(--silver-3); line-height: 1.65; }

        .stats { margin-bottom: 18px; }
        .cols { display: grid; grid-template-columns: 1fr 340px; gap: 16px; align-items: start; }
        @media (max-width: 1080px) { .cols { grid-template-columns: 1fr; } }
        .side { display: flex; flex-direction: column; gap: 16px; }

        .gates.ok { border-color: rgba(52,211,153,0.3); }
        .gates.bad { border-color: rgba(244,67,108,0.3); }
        .ghead { display: flex; justify-content: space-between; align-items: center; margin-bottom: 10px; }
        .ghead :global(.label) { margin-bottom: 0; }
        .gates ul { list-style: none; margin: 0; padding: 0; }
        .gates li {
          display: flex; gap: 11px; align-items: flex-start;
          padding: 9px 0; border-bottom: 1px solid var(--rule); font-size: 12.5px;
        }
        .gates li:last-child { border-bottom: none; }
        .gates .mark { font-size: 13px; width: 14px; flex: none; font-weight: 700; }
        .gates .pass .mark { color: var(--gain); }
        .gates .fail .mark { color: var(--loss); }
        .gates .soft .mark { color: var(--warn); }
        .gates b { display: block; font-weight: 600; text-transform: capitalize; }
        .gates .detail { color: var(--silver-3); font-size: 11.5px; line-height: 1.5; }

        .mcrow {
          display: flex; justify-content: space-between; align-items: center; gap: 12px;
          padding: 7px 0; border-bottom: 1px solid var(--rule); font-size: 12.5px;
          color: var(--silver-2);
        }
        .mcrow:last-of-type { border-bottom: none; }
        .mcrow.hi { background: rgba(244,67,108,0.05); margin: 0 -8px; padding: 8px; border-radius: 6px; }
        .mcrow b { font-size: 13px; font-weight: 600; }
        .tiny { font-size: 11px; }

        .tablewrap { overflow-x: auto; }
      `}</style>
    </div>
  );
}

function Stat({ label, value, sub, tone }: { label: string; value: string; sub?: string; tone?: string }) {
  return (
    <div className="panel flat">
      <div className="label">{label}</div>
      <div className={`value sm tab ${tone ?? ''}`}>{value}</div>
      {sub && <div className="sub">{sub}</div>}
    </div>
  );
}
