'use client';

/*
  The Universe page: what can this account actually trade, and is any of it edge?

  Three questions in order, because the order is the argument:

    1. Which markets can absorb my position size? (capacity)
    2. How many independent bets is that really? (correlation)
    3. Does any of it survive having been searched for? (significance)

  Most tools answer only the first and present a ranked list, which reads as a
  recommendation. The second question usually reveals that twenty markets are
  two bets. The third usually reveals that the best-looking one is the luckiest
  one. Both are more valuable than the list.

  So the screen table leads with the deflated Sharpe and the verdict, and the
  raw return is deliberately not the sort key. A user who scans this page and
  takes the top row by return has been actively misled by the layout, and that
  is a design failure, not a user error.
*/

import { useCallback, useState } from 'react';
import { useApp } from '@/components/Shell';
import {
  ApiError,
  api,
  type PortfolioReport,
  type ScreenReport,
  type UniverseReport,
} from '@/lib/api';
import { digits, money, num, pct, t } from '@/lib/i18n';

type Tab = 'capacity' | 'correlation' | 'significance';

export default function UniversePage() {
  const { lang } = useApp();
  const [tab, setTab] = useState<Tab>('capacity');
  const [venue, setVenue] = useState('coinex');
  const [equity, setEquity] = useState(10000);
  const [position, setPosition] = useState(3000);
  const [level, setLevel] = useState(4);

  const [uni, setUni] = useState<UniverseReport | null>(null);
  const [pf, setPf] = useState<PortfolioReport | null>(null);
  const [scr, setScr] = useState<ScreenReport | null>(null);
  const [busy, setBusy] = useState('');
  const [err, setErr] = useState('');

  const run = useCallback(
    async (which: Tab) => {
      setBusy(which);
      setErr('');
      try {
        if (which === 'capacity') {
          setUni(await api.universe({
            venue, equity, position_notional: position, timeframe: '2h', max_markets: 40,
          }));
        } else if (which === 'correlation') {
          setPf(await api.portfolio({ venue, timeframe: '2h', bars: 2000, threshold: 0.75 }));
        } else {
          setScr(await api.screen({
            risk_level: level, venue, equity, position_notional: position,
            top: 15, bars: 5000, null_runs: 10,
          }));
        }
      } catch (e) {
        setErr(e instanceof ApiError ? e.message : String(e));
      } finally {
        setBusy('');
      }
    },
    [venue, equity, position, level],
  );

  const fa = lang === 'fa';

  return (
    <div className="rise">
      <header className="head">
        <div>
          <h1>{fa ? 'جهان معاملاتی' : 'Universe'}</h1>
          <p className="sub">
            {fa
              ? 'کدام بازارها را واقعاً می‌توان با این حساب معامله کرد، و آیا اصلاً برتری واقعی وجود دارد.'
              : 'Which markets this account can actually trade, and whether any of it is real edge.'}
          </p>
        </div>
      </header>

      <section className="panel form">
        <div className="fields">
          <label>
            <span className="label">{t('venue', lang)}</span>
            <select value={venue} onChange={(e) => setVenue(e.target.value)}>
              <option value="coinex">CoinEx</option>
              <option value="offline">Offline demo</option>
            </select>
          </label>
          <label>
            <span className="label">{t('startingEquity', lang)}</span>
            <input type="number" value={equity} onChange={(e) => setEquity(+e.target.value)} />
          </label>
          <label>
            <span className="label">{fa ? 'اندازهٔ یک موقعیت' : 'One position size'}</span>
            <input type="number" value={position} onChange={(e) => setPosition(+e.target.value)} />
          </label>
          <label>
            <span className="label">{t('dial', lang)}</span>
            <input type="number" min={1} max={10} value={level}
                   onChange={(e) => setLevel(Math.max(1, Math.min(10, +e.target.value)))} />
          </label>
        </div>
        <p className="hint">
          {fa
            ? 'اندازهٔ موقعیت مهم‌ترین ورودی است: بازاری که با ۵۰٬۰۰۰ دلار غیرقابل‌معامله است، با ۵۰۰ دلار کاملاً سالم است. گفتن «این کوین نقدشوندگی ندارد» بدون ذکر اندازه، هیچ معنایی ندارد.'
            : 'Position size is the input that matters: a market that is untradeable at $50,000 is perfectly fine at $500. Saying "this coin is illiquid" without naming a size says nothing.'}
        </p>
      </section>

      <nav className="tabs">
        {(['capacity', 'correlation', 'significance'] as Tab[]).map((k) => (
          <button key={k} className={`tab ${tab === k ? 'on' : ''}`} onClick={() => setTab(k)}>
            {k === 'capacity' && (fa ? '۱ · ظرفیت' : '1 · Capacity')}
            {k === 'correlation' && (fa ? '۲ · همبستگی' : '2 · Correlation')}
            {k === 'significance' && (fa ? '۳ · معناداری' : '3 · Significance')}
          </button>
        ))}
      </nav>

      {err && <div className="panel errbox fade">{err}</div>}

      {/* ── 1. Capacity ─────────────────────────────────────────────────── */}
      {tab === 'capacity' && (
        <section className="panel fade">
          <div className="between">
            <div>
              <div className="label">{fa ? 'ظرفیت واقعی بازار' : 'Real market capacity'}</div>
              <p className="sub narrow">
                {fa
                  ? 'حجم معاملات را نمی‌سنجیم، دفتر سفارش واقعی را می‌پیماییم. حجم را یک نهنگ می‌سازد؛ عمق چیزی است که سفارش شما با آن روبه‌رو می‌شود.'
                  : 'This walks the real order book rather than trusting turnover. Turnover is a number one whale can manufacture; depth is what your order actually meets.'}
              </p>
            </div>
            <button className="btn primary" onClick={() => run('capacity')} disabled={!!busy}>
              {busy === 'capacity' ? t('running_', lang) : fa ? 'اسکن کن' : 'Scan'}
            </button>
          </div>

          {uni && (
            <>
              <div className="grid g4 stats">
                <Stat label={fa ? 'اسکن‌شده' : 'Scanned'} value={digits(uni.scanned, lang)} />
                <Stat label={fa ? 'قابل معامله' : 'Tradeable'}
                      value={digits(uni.tradeable_count, lang)} tone="gain" />
                <Stat label={fa ? 'کنار گذاشته' : 'Excluded'}
                      value={digits(uni.scanned - uni.tradeable_count, lang)} tone="loss" />
                <Stat label={fa ? 'اندازهٔ موقعیت' : 'Position size'}
                      value={`$${money(uni.position_notional, lang, 0)}`} />
              </div>

              <div className="tablewrap">
                <table>
                  <thead>
                    <tr>
                      <th>{t('symbol', lang)}</th>
                      <th className="num">{fa ? 'گردش ۲۴س' : '24h turnover'}</th>
                      <th className="num">{fa ? 'دامنه' : 'range'}</th>
                      <th className="num">{fa ? 'لغزش' : 'slippage'}</th>
                      <th className="num">{fa ? 'رفت‌وبرگشت' : 'round trip'}</th>
                      <th className="num">{fa ? 'پوشش' : 'cover'}</th>
                    </tr>
                  </thead>
                  <tbody>
                    {uni.markets.filter((m) => m.tradeable).map((m) => (
                      <tr key={m.symbol}>
                        <td><b>{m.symbol}</b></td>
                        <td className="num">{money(m.turnover, lang, 0)}</td>
                        <td className="num">{pct(m.daily_range, lang, 2).replace('+', '')}</td>
                        <td className="num">
                          {m.slippage === null ? '—' : pct(m.slippage, lang, 3).replace('+', '')}
                        </td>
                        <td className="num">{pct(m.round_trip, lang, 3).replace('+', '')}</td>
                        <td className={`num ${m.edge_ratio > 20 ? 'gain' : ''}`}>
                          {num(m.edge_ratio, lang, 1)}×
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>

              <div className="rejects">
                <div className="label">{fa ? 'چرا بقیه کنار رفتند' : 'Why the rest were excluded'}</div>
                <div className="chips">
                  {Object.entries(uni.rejection_summary).map(([k, v]) => (
                    <span key={k} className="chip">{k.replace(/_/g, ' ')} · {digits(v, lang)}</span>
                  ))}
                </div>
                <ul className="examples">
                  {uni.markets
                    .filter((m) => !m.tradeable && m.verdict === 'too_expensive')
                    .slice(0, 4)
                    .map((m) => (
                      <li key={m.symbol}>
                        <b>{m.symbol}</b> — {m.reason}
                      </li>
                    ))}
                </ul>
              </div>
            </>
          )}
        </section>
      )}

      {/* ── 2. Correlation ──────────────────────────────────────────────── */}
      {tab === 'correlation' && (
        <section className="panel fade">
          <div className="between">
            <div>
              <div className="label">{fa ? 'چند شرط مستقل؟' : 'How many independent bets?'}</div>
              <p className="sub narrow">
                {fa
                  ? 'بیست ارز که همه دنبال بیت‌کوین حرکت می‌کنند، یک موقعیت است با بیست برابر کارمزد.'
                  : 'Twenty coins that all follow BTC are one position paying twenty sets of fees.'}
              </p>
            </div>
            <button className="btn primary" onClick={() => run('correlation')} disabled={!!busy}>
              {busy === 'correlation' ? t('running_', lang) : fa ? 'اندازه بگیر' : 'Measure'}
            </button>
          </div>

          {pf && (
            <>
              <div className="grid g3 stats">
                <Stat label={fa ? 'بازارها' : 'Markets'} value={digits(pf.symbols.length, lang)} />
                <Stat label={fa ? 'همبستگی میانگین' : 'Avg correlation'}
                      value={num(pf.average_correlation, lang, 2)}
                      tone={pf.average_correlation > 0.6 ? 'loss' : 'warn'} />
                <Stat label={fa ? 'شرط‌های مستقل واقعی' : 'Independent bets'}
                      value={num(pf.effective_breadth, lang, 1)}
                      tone="loss"
                      sub={`${num(pf.concentration_multiplier, lang, 1)}× ${
                        fa ? 'ضربهٔ بیشتر در حرکت همبسته' : 'worse in a correlated move'
                      }`} />
              </div>

              <p className="verdict">{pf.summary}</p>

              <div className="label" style={{ marginTop: 18 }}>
                {fa ? 'خوشه‌ها' : 'Clusters'}
              </div>
              <div className="clusters">
                {pf.clusters.map((c) => (
                  <div key={c.representative} className="cluster">
                    <b>{c.representative}</b>
                    {c.members.length > 1 ? (
                      <span className="carries">
                        {fa ? 'به‌همراه' : 'carries'} {c.members.filter((m) => m !== c.representative).join(', ')}
                        <em> ρ {num(c.cohesion, lang, 2)}</em>
                      </span>
                    ) : (
                      <span className="alone">{fa ? 'تنها' : 'alone'}</span>
                    )}
                  </div>
                ))}
              </div>

              <div className="selected">
                <span className="label">{fa ? 'اینها را معامله کن' : 'Trade these'}</span>
                <div className="chips">
                  {pf.selected.map((s) => <span key={s} className="chip live">{s}</span>)}
                </div>
              </div>
            </>
          )}
        </section>
      )}

      {/* ── 3. Significance ─────────────────────────────────────────────── */}
      {tab === 'significance' && (
        <section className="panel fade">
          <div className="between">
            <div>
              <div className="label">
                {fa ? 'آیا این برتری است یا شانس؟' : 'Is this edge, or is it luck?'}
              </div>
              <p className="sub narrow">
                {fa
                  ? 'جست‌وجو در بیست بازار و انتخاب بهترین، همان کاری است که نویز را شبیه کشف می‌کند. بیشترینِ چند قرعه، ذاتاً بزرگ است. این ستون «DSR» می‌گوید با در نظر گرفتن تعداد چیزهایی که آزمودیم، چقدر می‌توان باور کرد.'
                  : 'Searching twenty markets and taking the best is exactly how noise starts looking like a discovery — the maximum of many draws is large by construction. The DSR column is how much to believe it, given how many things were tried.'}
              </p>
            </div>
            <button className="btn primary" onClick={() => run('significance')} disabled={!!busy}>
              {busy === 'significance' ? t('running_', lang) : fa ? 'غربال کن' : 'Screen'}
            </button>
          </div>
          {busy === 'significance' && (
            <p className="sub">{fa ? 'چند دقیقه طول می‌کشد…' : 'This takes a few minutes…'}</p>
          )}

          {scr && (
            <>
              <div className={`verdictbox ${scr.survivors ? 'ok' : 'bad'}`}>{scr.verdict}</div>

              <div className="grid g3 stats">
                <Stat label={fa ? 'آزمون‌ها' : 'Trials'} value={digits(scr.trials, lang)} />
                <Stat label={fa ? 'بازمانده' : 'Survivors'} value={digits(scr.survivors, lang)}
                      tone={scr.survivors ? 'gain' : 'loss'} />
                <Stat label={fa ? 'شارپ روی دادهٔ بی‌ساختار' : 'Null Sharpe p95'}
                      value={num(scr.null_sharpe_p95, lang, 3)}
                      sub={fa ? 'چیزی که استراتژی روی هیچ به دست می‌آورد' : 'what it scores on nothing'} />
              </div>

              <div className="tablewrap">
                <table>
                  <thead>
                    <tr>
                      <th>{t('symbol', lang)}</th>
                      <th className="num">{t('trades', lang)}</th>
                      <th className="num">SR/{fa ? 'معامله' : 'trade'}</th>
                      <th className="num">{t('totalReturn', lang)}</th>
                      <th className="num">DSR</th>
                      <th className="num">&gt;null</th>
                      <th>{fa ? 'حکم' : 'verdict'}</th>
                    </tr>
                  </thead>
                  <tbody>
                    {scr.results.map((r) => (
                      <tr key={r.symbol} className={r.survived ? 'passrow' : ''}>
                        <td><b>{r.symbol}</b></td>
                        <td className="num">{digits(r.trades, lang)}</td>
                        <td className={`num ${r.trade_sharpe >= 0 ? '' : 'loss'}`}>
                          {num(r.trade_sharpe, lang, 3)}
                        </td>
                        <td className={`num ${r.total_return >= 0 ? 'gain' : 'loss'}`}>
                          {pct(r.total_return, lang, 1)}
                        </td>
                        <td className={`num ${r.dsr > 0.95 ? 'gain' : 'dim'}`}>
                          {num(r.dsr, lang, 2)}
                        </td>
                        <td className="num dim">{pct(r.null_percentile, lang, 0).replace('+', '')}</td>
                        <td className={r.survived ? 'gain' : 'dim'}>
                          {r.survived
                            ? (fa ? 'باقی ماند' : 'survived')
                            : <span title={r.failed_because}>{r.failed_because.split(';')[0].slice(0, 46)}</span>}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>

              <p className="footnote">
                {fa
                  ? 'ستون بازدهی عمداً معیار مرتب‌سازی نیست. اگر بالاترین بازدهی را انتخاب کنید، دقیقاً همان اشتباهی را کرده‌اید که این صفحه برای جلوگیری از آن ساخته شده.'
                  : 'The return column is deliberately not the sort key. Picking the highest return is precisely the mistake this page exists to prevent.'}
              </p>
            </>
          )}
        </section>
      )}

      <style jsx>{`
        .head { margin-bottom: 18px; }
        h1 { font-size: 26px; }
        .form { margin-bottom: 14px; }
        .fields {
          display: grid; grid-template-columns: repeat(auto-fit, minmax(150px, 1fr));
          gap: 14px; align-items: end;
        }
        .fields label { display: flex; flex-direction: column; }
        .fields :global(.label) { margin-bottom: 6px; }
        .fields input, .fields select { width: 100%; }
        .hint { font-size: 12px; color: var(--silver-3); margin: 14px 0 0; line-height: 1.65; }

        .tabs { display: flex; gap: 6px; margin-bottom: 14px; flex-wrap: wrap; }
        .tab {
          padding: 9px 16px; border-radius: var(--r-sm); font-size: 13px; font-weight: 600;
          border: 1px solid var(--rule); color: var(--silver-3);
          transition: all 0.15s var(--ease);
        }
        .tab:hover { color: var(--silver); }
        .tab.on { background: var(--accent-soft); color: var(--accent); border-color: var(--accent-line); }

        .between {
          display: flex; justify-content: space-between; align-items: flex-start;
          gap: 18px; margin-bottom: 16px; flex-wrap: wrap;
        }
        .narrow { max-width: 62ch; line-height: 1.65; margin: 4px 0 0; }
        .errbox { color: var(--loss); font-size: 13px; margin-bottom: 14px; }
        .stats { margin-bottom: 18px; }
        .tablewrap { overflow-x: auto; }

        .rejects { margin-top: 20px; border-top: 1px solid var(--rule); padding-top: 16px; }
        .chips { display: flex; flex-wrap: wrap; gap: 7px; }
        .examples { margin: 12px 0 0; padding-inline-start: 18px; }
        .examples li { font-size: 12px; color: var(--silver-3); margin-bottom: 5px; line-height: 1.6; }

        .verdict {
          font-size: 13.5px; color: var(--silver-2); line-height: 1.7;
          border-inline-start: 2px solid var(--accent-line); padding-inline-start: 14px;
          margin: 4px 0 0;
        }
        .clusters { display: flex; flex-direction: column; gap: 8px; }
        .cluster {
          display: flex; gap: 10px; align-items: baseline; flex-wrap: wrap;
          font-size: 12.5px; padding: 8px 0; border-bottom: 1px solid var(--rule);
        }
        .cluster b { min-width: 100px; }
        .carries { color: var(--silver-3); }
        .carries em { font-style: normal; color: var(--warn); margin-inline-start: 8px; }
        .alone { color: var(--silver-4); }
        .selected { margin-top: 18px; }
        .selected :global(.label) { display: block; margin-bottom: 8px; }

        .verdictbox {
          padding: 14px 16px; border-radius: var(--r); font-size: 13.5px;
          line-height: 1.65; margin-bottom: 18px; border: 1px solid;
        }
        .verdictbox.ok { border-color: rgba(52,211,153,0.35); background: rgba(52,211,153,0.06); }
        .verdictbox.bad { border-color: rgba(251,191,36,0.35); background: rgba(251,191,36,0.05); color: var(--warn); }
        .passrow { background: rgba(52,211,153,0.05); }
        .footnote {
          font-size: 11.5px; color: var(--silver-4); margin-top: 14px; line-height: 1.7;
          border-inline-start: 2px solid var(--rule-2); padding-inline-start: 12px;
        }
      `}</style>
    </div>
  );
}

function Stat({ label, value, sub, tone }: {
  label: string; value: string; sub?: string; tone?: string;
}) {
  return (
    <div className="panel flat">
      <div className="label">{label}</div>
      <div className={`value sm tab ${tone ?? ''}`}>{value}</div>
      {sub && <div className="sub">{sub}</div>}
    </div>
  );
}
