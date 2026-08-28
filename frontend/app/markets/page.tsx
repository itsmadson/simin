'use client';

/*
  Markets: the chart, with the indicators the bot actually reads.

  Drawn as inline SVG rather than pulled from a charting library, for one
  substantive reason beyond bundle size: the overlays have to be the *same*
  series the strategies see, warm-up gaps and all. A charting library will
  happily interpolate across a null, which paints an EMA that starts before it
  exists — and then the chart and the bot disagree about what the market looked
  like.
*/

import { useCallback, useEffect, useState } from 'react';
import { useApp } from '@/components/Shell';
import { ApiError, api } from '@/lib/api';
import { digits, money, num, t } from '@/lib/i18n';

type Candles = Awaited<ReturnType<typeof api.candles>>;

const TIMEFRAMES = ['5m', '15m', '1h', '2h', '4h', '1d'];

export default function MarketsPage() {
  const { lang } = useApp();
  const [symbol, setSymbol] = useState('BTCUSDT');
  const [tf, setTf] = useState('2h');
  const [data, setData] = useState<Candles | null>(null);
  const [err, setErr] = useState('');
  const [busy, setBusy] = useState(false);

  const load = useCallback(async () => {
    setBusy(true);
    setErr('');
    try {
      setData(await api.candles(symbol, tf, 240));
    } catch (e) {
      setErr(e instanceof ApiError ? e.message : String(e));
      setData(null);
    } finally {
      setBusy(false);
    }
  }, [symbol, tf]);

  useEffect(() => { load(); }, [load]);

  return (
    <div className="rise">
      <header className="head">
        <div>
          <h1>{t('markets', lang)}</h1>
          <p className="sub">
            {lang === 'fa'
              ? 'همان اندیکاتورهایی که ربات می‌بیند — با همان فاصلهٔ گرم‌شدن.'
              : 'The same indicators the bot reads, with the same warm-up gaps.'}
          </p>
        </div>
        <div className="pickers">
          <input value={symbol} onChange={(e) => setSymbol(e.target.value.toUpperCase())}
                 onKeyDown={(e) => e.key === 'Enter' && load()} style={{ width: 130 }} />
          <div className="tfs">
            {TIMEFRAMES.map((x) => (
              <button key={x} className={`tfb ${tf === x ? 'on' : ''}`} onClick={() => setTf(x)}>{x}</button>
            ))}
          </div>
        </div>
      </header>

      {err && <div className="panel err fade">{err}</div>}
      {busy && !data && <div className="panel loading">{t('loading', lang)}</div>}

      {data && data.candles.length > 0 && (
        <>
          <Chart data={data} lang={lang} />

          <section className="grid g4 readouts fade">
            <Read label="RSI" series={data.indicators.rsi} lang={lang} places={1} />
            <Read label="ADX" series={data.indicators.adx} lang={lang} places={1} />
            <Read label="MACD hist" series={data.indicators.macd_hist} lang={lang} places={4} />
            <Read label="ATR" series={data.indicators.atr} lang={lang} places={2} />
            <Read label="Stoch %K" series={data.indicators.stoch_k} lang={lang} places={1} />
            <Read label="EMA 200" series={data.indicators.ema_trend} lang={lang} places={2} />
            <Read label="VWAP" series={data.indicators.vwap} lang={lang} places={2} />
            <div className="panel flat">
              <div className="label">{lang === 'fa' ? 'ساختار بازار' : 'Structure'}</div>
              <div className="value sm">{data.structure}</div>
            </div>
          </section>

          {data.levels.length > 0 && (
            <section className="panel fade" style={{ marginTop: 16 }}>
              <div className="label">{lang === 'fa' ? 'سطوح حمایت و مقاومت' : 'Support & resistance'}</div>
              <div className="lvls">
                {data.levels.map((lv, i) => (
                  <div key={i} className="lvl">
                    <span className={`kind ${lv.kind}`}>{lv.kind === 'high' ? '▲' : '▼'}</span>
                    <b className="tab">{money(lv.price, lang, 2)}</b>
                    <span className="sub">
                      {digits(lv.touches, lang)} {lang === 'fa' ? 'برخورد' : 'touches'}
                    </span>
                    <div className="strength"><div style={{ width: `${lv.strength * 100}%` }} /></div>
                  </div>
                ))}
              </div>
            </section>
          )}
        </>
      )}

      <style jsx>{`
        .head {
          display: flex; justify-content: space-between; align-items: flex-start;
          gap: 16px; margin-bottom: 18px; flex-wrap: wrap;
        }
        h1 { font-size: 26px; }
        .pickers { display: flex; gap: 10px; align-items: center; flex-wrap: wrap; }
        .tfs { display: flex; border: 1px solid var(--rule-2); border-radius: var(--r-sm); overflow: hidden; }
        .tfb {
          padding: 8px 12px; font-size: 12px; font-weight: 600; color: var(--silver-3);
          transition: all 0.15s var(--ease); font-family: var(--font-mono);
        }
        .tfb:hover { color: var(--silver); }
        .tfb.on { background: var(--accent-soft); color: var(--accent); }
        .err { color: var(--loss); font-size: 13px; margin-bottom: 16px; }
        .loading { text-align: center; padding: 60px; color: var(--silver-3); }
        .readouts { margin-top: 16px; }
        .lvls { display: flex; flex-direction: column; gap: 9px; }
        .lvl { display: grid; grid-template-columns: 18px 110px 1fr 90px; gap: 10px; align-items: center; font-size: 12.5px; }
        .kind.high { color: var(--loss); }
        .kind.low { color: var(--gain); }
        .strength { height: 4px; background: var(--rule-2); border-radius: 2px; overflow: hidden; }
        .strength div { height: 100%; background: var(--accent); border-radius: 2px; }
      `}</style>
    </div>
  );
}

function Read({ label, series, lang, places }: {
  label: string; series?: Array<number | null>; lang: import('@/lib/i18n').Lang; places: number;
}) {
  const last = series?.length ? series[series.length - 1] : null;
  return (
    <div className="panel flat">
      <div className="label">{label}</div>
      <div className="value sm tab">{last == null ? '—' : num(last, lang, places)}</div>
    </div>
  );
}

function Chart({ data, lang }: { data: Candles; lang: import('@/lib/i18n').Lang }) {
  const W = 1100;
  const H = 420;
  const PAD = { top: 16, right: 62, bottom: 22, left: 8 };
  const candles = data.candles;
  const n = candles.length;

  const lo = Math.min(...candles.map((c) => c.l));
  const hi = Math.max(...candles.map((c) => c.h));
  const pad = (hi - lo) * 0.06;
  const min = lo - pad;
  const max = hi + pad;

  const iw = W - PAD.left - PAD.right;
  const ih = H - PAD.top - PAD.bottom;
  const x = (i: number) => PAD.left + (i / Math.max(n - 1, 1)) * iw;
  const y = (p: number) => PAD.top + (1 - (p - min) / (max - min)) * ih;
  const cw = Math.max(1.2, (iw / Math.max(n, 1)) * 0.62);

  /**
   * Build a path that BREAKS at every null rather than bridging it. This is the
   * whole reason the chart is hand-drawn: a line that spans a warm-up gap is a
   * line claiming the indicator existed when it did not.
   */
  const line = (series?: Array<number | null>) => {
    if (!series) return '';
    let d = '';
    let open = false;
    for (let i = 0; i < Math.min(series.length, n); i++) {
      const v = series[i];
      if (v == null || !Number.isFinite(v)) { open = false; continue; }
      d += `${open ? 'L' : 'M'} ${x(i).toFixed(1)} ${y(v).toFixed(1)} `;
      open = true;
    }
    return d;
  };

  const gridLines = 5;

  return (
    <div className="panel chart fade">
      <div className="chead">
        <b>{data.symbol}</b>
        <span className="tab">{data.timeframe}</span>
        <span className="tab last">{money(candles[n - 1].c, lang, 2)}</span>
        <span className="legend">
          <i style={{ background: '#60a5fa' }} /> EMA20
          <i style={{ background: '#fbbf24' }} /> EMA50
          <i style={{ background: '#f4436c' }} /> EMA200
          <i style={{ background: 'rgba(216,221,230,0.4)' }} /> Bollinger
        </span>
      </div>

      <svg viewBox={`0 0 ${W} ${H}`} role="img" aria-label={`${data.symbol} ${data.timeframe} chart`}>
        {Array.from({ length: gridLines + 1 }, (_, i) => {
          const p = min + ((max - min) * i) / gridLines;
          return (
            <g key={i}>
              <line x1={PAD.left} y1={y(p)} x2={W - PAD.right} y2={y(p)} stroke="var(--rule)" strokeWidth="1" />
              <text x={W - PAD.right + 7} y={y(p) + 3.5} fontSize="10" fill="var(--silver-4)"
                    fontFamily="var(--font-mono)">
                {p >= 1000 ? p.toFixed(0) : p.toFixed(2)}
              </text>
            </g>
          );
        })}

        {/* Bollinger envelope */}
        {data.indicators.bb_upper && data.indicators.bb_lower && (
          <>
            <path d={line(data.indicators.bb_upper)} fill="none" stroke="rgba(216,221,230,0.28)" strokeWidth="1" />
            <path d={line(data.indicators.bb_lower)} fill="none" stroke="rgba(216,221,230,0.28)" strokeWidth="1" />
          </>
        )}

        {/* Candles */}
        {candles.map((c, i) => {
          const up = c.c >= c.o;
          const col = up ? '#34d399' : '#f4436c';
          const top = y(Math.max(c.o, c.c));
          const bot = y(Math.min(c.o, c.c));
          return (
            <g key={i}>
              <line x1={x(i)} y1={y(c.h)} x2={x(i)} y2={y(c.l)} stroke={col} strokeWidth="1" opacity="0.75" />
              <rect x={x(i) - cw / 2} y={top} width={cw} height={Math.max(bot - top, 0.8)}
                    fill={col} opacity="0.88" />
            </g>
          );
        })}

        <path d={line(data.indicators.ema_fast)} fill="none" stroke="#60a5fa" strokeWidth="1.5" />
        <path d={line(data.indicators.ema_slow)} fill="none" stroke="#fbbf24" strokeWidth="1.5" />
        <path d={line(data.indicators.ema_trend)} fill="none" stroke="#f4436c" strokeWidth="1.6" opacity="0.85" />

        {/* Support/resistance, drawn where they sit on the price axis */}
        {data.levels.slice(0, 5).map((lv, i) =>
          lv.price >= min && lv.price <= max ? (
            <line key={i} x1={PAD.left} y1={y(lv.price)} x2={W - PAD.right} y2={y(lv.price)}
                  stroke={lv.kind === 'high' ? 'rgba(244,67,108,0.34)' : 'rgba(52,211,153,0.34)'}
                  strokeWidth="1" strokeDasharray="5 6" />
          ) : null,
        )}
      </svg>

      <style jsx>{`
        .chart { padding: 14px 16px 8px; }
        .chead {
          display: flex; align-items: center; gap: 12px; margin-bottom: 8px;
          flex-wrap: wrap; font-size: 13px;
        }
        .chead b { font-size: 15px; }
        .chead .tab { color: var(--silver-3); font-size: 12px; }
        .chead .last { color: var(--silver); font-size: 14px; font-weight: 600; }
        .legend {
          margin-inline-start: auto; display: flex; align-items: center; gap: 8px;
          font-size: 10.5px; color: var(--silver-4); flex-wrap: wrap;
        }
        .legend i { width: 12px; height: 2px; border-radius: 1px; margin-inline-end: -4px; }
        svg { width: 100%; height: auto; display: block; }
      `}</style>
    </div>
  );
}
