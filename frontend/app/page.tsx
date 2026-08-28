'use client';

/*
  The dial page: pick a risk level, see exactly what it means, start the bot.

  Layout logic — the order is an argument:

    1. The dial itself, with the honesty gap drawn on it.
    2. Target vs measured, as two large readouts side by side.
    3. The ruin meter.
    4. Only then, the parameters and the start button.

  The user asked for a bot that returns 40% at risk 5 and 200% at risk 10. The
  dial delivers exactly those levels, and puts the measured number next to the
  promise every single time it shows the promise. That pairing is the entire
  design brief of this page.
*/

import { useCallback, useEffect, useState } from 'react';
import { useRouter } from 'next/navigation';
import { HonestyLegend, RiskDial } from '@/components/RiskDial';
import { useApp } from '@/components/Shell';
import { ApiError, api, type BotStatus, type DialLevel, type DialResponse, type Venue } from '@/lib/api';
import { digits, num, pct, t } from '@/lib/i18n';

export default function DialPage() {
  const { lang, setHeat } = useApp();
  const router = useRouter();

  const [dial, setDial] = useState<DialResponse | null>(null);
  const [venues, setVenues] = useState<Venue[]>([]);
  const [bot, setBot] = useState<BotStatus | null>(null);
  const [level, setLevel] = useState(4);
  const [venue, setVenue] = useState('offline');
  const [symbols, setSymbols] = useState('BTCUSDT,ETHUSDT');
  const [equity, setEquity] = useState(10000);
  const [mode, setMode] = useState<'lab' | 'real'>('lab');
  const [phrase, setPhrase] = useState('');
  const [busy, setBusy] = useState(false);
  const [calibrating, setCalibrating] = useState(false);
  const [err, setErr] = useState('');
  const [notice, setNotice] = useState('');

  const load = useCallback(async () => {
    try {
      const [d, v, b] = await Promise.all([api.dial(), api.venues(), api.bot()]);
      setDial(d);
      setVenues(v.venues);
      setBot(b);
      if (b.risk_level) setLevel(b.risk_level);
      // Mirror the running bot rather than leaving the form on its defaults.
      // A selector showing "Offline demo" while the bot trades CoinEx is not a
      // cosmetic mismatch — it misreports where the money would go.
      if (b.venue_key) setVenue(b.venue_key);
      if (b.mode === 'lab' || b.mode === 'real') setMode(b.mode);
      setErr('');
    } catch (e) {
      setErr(e instanceof ApiError ? e.message : String(e));
    }
  }, []);

  useEffect(() => {
    load();
    const id = setInterval(load, 6000);
    return () => clearInterval(id);
  }, [load]);

  useEffect(() => {
    setHeat((level - 1) / 9);
  }, [level, setHeat]);

  const data: DialLevel | undefined = dial?.levels.find((l) => l.level === level);
  const chosenVenue = venues.find((v) => v.name === venue);
  const spotClamped = data && data.leverage > 1 && chosenVenue && !chosenVenue.supports_futures;
  const running = bot?.running ?? false;
  const warnings = lang === 'fa' ? data?.warnings_fa : data?.warnings_en;

  const start = async () => {
    setBusy(true);
    setErr('');
    try {
      const res = await api.start({
        risk_level: level,
        mode,
        venue,
        symbols: symbols.split(',').map((s) => s.trim().toUpperCase()).filter(Boolean),
        starting_equity: equity,
        confirmation: mode === 'real' ? phrase : undefined,
      });
      setNotice(res.clamped ? t('spotOnlyWarning', lang) : '');
      router.push('/live');
    } catch (e) {
      setErr(e instanceof ApiError ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  };

  const changeRunningLevel = async (next: number) => {
    setLevel(next);
    if (running) {
      try {
        await api.setRisk(next);
      } catch (e) {
        setErr(e instanceof ApiError ? e.message : String(e));
      }
    }
  };

  const calibrate = async () => {
    setCalibrating(true);
    setErr('');
    try {
      await api.calibrate({
        risk_level: level,
        symbols: symbols.split(',').map((s) => s.trim().toUpperCase()).filter(Boolean),
        venue,
        bars: 6000,
        starting_equity: equity,
      });
      await load();
    } catch (e) {
      setErr(e instanceof ApiError ? e.message : String(e));
    } finally {
      setCalibrating(false);
    }
  };

  if (err && !dial) {
    return (
      <div className="panel rise" style={{ maxWidth: 560, marginTop: 60 }}>
        <div className="label">{t('error', lang)}</div>
        <p style={{ margin: '6px 0 0' }}>{err}</p>
        <p className="sub">
          {lang === 'fa'
            ? 'مطمئن شوید سرویس پشتیبان در حال اجراست: docker compose up'
            : 'Make sure the backend is running: docker compose up'}
        </p>
      </div>
    );
  }

  return (
    <div className="rise">
      <header className="head">
        <div>
          <h1>{t('chooseRisk', lang)}</h1>
          <p className="sub">{t('dragDial', lang)}</p>
        </div>
        <div className="badges">
          {running && (
            <span className="chip live">
              <span className="dot pulse" />
              {t('running', lang)}
            </span>
          )}
          <span className={`chip ${mode === 'real' ? 'real' : 'lab'}`}>
            {mode === 'real' ? t('modeReal', lang) : t('modeLab', lang)}
          </span>
        </div>
      </header>

      <div className="top">
        {/* ── The instrument ────────────────────────────────────────────── */}
        <section className="panel dialpanel">
          <RiskDial level={level} onChange={changeRunningLevel} data={data} lang={lang} />
          <HonestyLegend data={data} lang={lang} />
          <p className="desc">{lang === 'fa' ? data?.description_fa : data?.description_en}</p>
        </section>

        {/* ── Target vs measured ────────────────────────────────────────── */}
        <section className="readouts">
          <div className="panel ro">
            <div className="label">{t('target', lang)}</div>
            <div className="value lg ghost tab">
              {data ? pct(data.target_monthly_return, lang, 0) : '—'}
            </div>
            <div className="sub">{t('perMonth', lang)}</div>
          </div>

          <div className={`panel ro ${data?.calibrated ? '' : 'empty'}`}>
            <div className="label">{t('measured', lang)}</div>
            {data?.measured_monthly_return != null ? (
              <>
                <div
                  className={`value lg tab ${data.measured_monthly_return >= 0 ? 'gain' : 'loss'}`}
                >
                  {pct(data.measured_monthly_return, lang, 2)}
                </div>
                <div className="sub">
                  {t('worstCase', lang)} {pct(data.measured_monthly_p05, lang, 1)} ·{' '}
                  {t('bestCase', lang)} {pct(data.measured_monthly_p95, lang, 1)}
                </div>
              </>
            ) : (
              <>
                <div className="value lg dim notmeasured">{t('notMeasured', lang)}</div>
                <button className="btn ghost small" onClick={calibrate} disabled={calibrating}>
                  {calibrating ? t('calibrating', lang) : t('runCalibration', lang)}
                </button>
              </>
            )}
          </div>

          {/* ── Ruin meter ──────────────────────────────────────────────── */}
          <div className="panel ro ruin">
            <div className="label">{t('ruinRisk', lang)}</div>
            {data?.ruin_probability != null ? (
              <>
                <div className="value tab" style={{ color: ruinColor(data.ruin_probability) }}>
                  {pct(data.ruin_probability, lang, 1).replace('+', '')}
                </div>
                <div className="meter">
                  <div
                    className="fill"
                    style={{
                      width: `${Math.min(data.ruin_probability * 100, 100)}%`,
                      background: ruinColor(data.ruin_probability),
                    }}
                  />
                </div>
              </>
            ) : (
              <div className="value sm dim">{t('notMeasured', lang)}</div>
            )}
            <p className="sub tiny">{t('ruinExplain', lang)}</p>
          </div>

          <div className="panel ro">
            <div className="label">{t('worstDay', lang)}</div>
            <div className="value tab warn">
              {data ? pct(-data.worst_case_day, lang, 1) : '—'}
            </div>
            <div className="sub">
              {t('drawdownHalt', lang)} {data ? pct(data.max_drawdown_halt, lang, 0).replace('+', '') : '—'}
            </div>
          </div>
        </section>
      </div>

      {/* ── Warnings ──────────────────────────────────────────────────────── */}
      {warnings && warnings.length > 0 && (
        <section className="panel hot warnbox fade">
          <div className="label" style={{ color: 'var(--loss)' }}>{t('warnings', lang)}</div>
          <ul>
            {warnings.map((w, i) => (
              <li key={i}>{w}</li>
            ))}
          </ul>
        </section>
      )}

      {spotClamped && (
        <section className="panel warnbox soft fade">
          <p>{t('spotOnlyWarning', lang)}</p>
        </section>
      )}

      {/* ── Parameters ────────────────────────────────────────────────────── */}
      <section className="grid g4 params">
        <Param label={t('riskPerTrade', lang)} value={data ? `${digits((data.risk_per_trade * 100).toFixed(2), lang)}%` : '—'} />
        <Param label={t('leverage', lang)} value={data ? `${digits(data.leverage, lang)}×` : '—'} />
        <Param label={t('timeframe', lang)} value={data?.signal_timeframe ?? '—'} />
        <Param label={t('maxPositions', lang)} value={data ? digits(data.max_positions, lang) : '—'} />
        <Param label={t('tradesPerDay', lang)} value={data ? digits(data.max_trades_per_day, lang) : '—'} />
        <Param label={t('selectivity', lang)} value={data ? num(data.min_confluence, lang, 2) : '—'} />
        <Param label={t('shorts', lang)} value={data ? (data.allow_shorts ? '✓' : '✕') : '—'} />
        <Param label={t('dailyLossHalt', lang)} value={data ? pct(data.daily_loss_halt, lang, 1).replace('+', '') : '—'} />
      </section>

      {data && (
        <section className="panel strategies">
          <div className="label">{t('strategies', lang)}</div>
          <div className="strat-list">
            {data.strategies.map((s) => (
              <span key={s} className="chip">{s}</span>
            ))}
          </div>
        </section>
      )}

      {/* ── Launch ────────────────────────────────────────────────────────── */}
      <section className="panel launch">
        <div className="fields">
          <label>
            <span className="label">{t('venue', lang)}</span>
            <select value={venue} onChange={(e) => setVenue(e.target.value)} disabled={running}>
              {venues.map((v) => (
                <option key={v.name} value={v.name}>
                  {v.display_name}
                  {!v.supports_futures ? ' · spot' : ''}
                </option>
              ))}
            </select>
          </label>
          <label>
            <span className="label">{t('symbols', lang)}</span>
            <input value={symbols} onChange={(e) => setSymbols(e.target.value)} disabled={running} />
          </label>
          <label>
            <span className="label">{t('startingEquity', lang)}</span>
            <input
              type="number"
              value={equity}
              onChange={(e) => setEquity(Number(e.target.value))}
              disabled={running}
            />
          </label>
          <div className="modeswitch">
            <span className="label">{lang === 'fa' ? 'حالت' : 'Mode'}</span>
            <div className="seg">
              <button
                className={mode === 'lab' ? 'on' : ''}
                onClick={() => setMode('lab')}
                disabled={running}
              >
                {t('modeLab', lang)}
              </button>
              <button
                className={`danger ${mode === 'real' ? 'on' : ''}`}
                onClick={() => setMode('real')}
                disabled={running}
              >
                {t('modeReal', lang)}
              </button>
            </div>
          </div>
        </div>

        <p className="modehint">{mode === 'real' ? t('realExplain', lang) : t('labExplain', lang)}</p>

        {mode === 'real' && !running && (
          <div className="confirm">
            <p className="label" style={{ color: 'var(--loss)' }}>{t('realConfirm', lang)}</p>
            <code>{t('realPhrase', lang)}</code>
            <input
              value={phrase}
              onChange={(e) => setPhrase(e.target.value)}
              placeholder={t('realPhrase', lang)}
              dir="ltr"
            />
          </div>
        )}

        {chosenVenue && (
          <p className="venuenote sub">{lang === 'fa' ? chosenVenue.notes_fa : chosenVenue.notes_en}</p>
        )}

        {err && <p className="err">{err}</p>}
        {notice && <p className="notice">{notice}</p>}

        <div className="actions">
          {running ? (
            <button className="btn primary" onClick={() => router.push('/live')}>
              {t('live', lang)} →
            </button>
          ) : (
            <button
              className={`btn ${mode === 'real' ? 'danger' : 'primary'}`}
              onClick={start}
              disabled={busy || (mode === 'real' && phrase !== t('realPhrase', lang))}
            >
              {busy ? t('loading', lang) : t('start', lang)}
            </button>
          )}
        </div>
      </section>

      {dial && (
        <p className="disclaimer">{lang === 'fa' ? dial.disclaimer_fa : dial.disclaimer_en}</p>
      )}

      <style jsx>{`
        .head {
          display: flex; justify-content: space-between; align-items: flex-start;
          gap: 16px; margin-bottom: 22px; flex-wrap: wrap;
        }
        h1 { font-size: 27px; }
        .badges { display: flex; gap: 8px; align-items: center; }

        .top { display: grid; grid-template-columns: minmax(340px, 460px) 1fr; gap: 16px; }
        @media (max-width: 1020px) { .top { grid-template-columns: 1fr; } }

        .dialpanel { padding: 26px 22px 20px; }
        .desc {
          margin: 16px 0 0; font-size: 13.5px; color: var(--silver-2);
          line-height: 1.65; text-align: center;
        }

        .readouts { display: grid; grid-template-columns: 1fr 1fr; gap: 16px; align-content: start; }
        @media (max-width: 560px) { .readouts { grid-template-columns: 1fr; } }
        .ro { display: flex; flex-direction: column; justify-content: flex-start; min-height: 128px; }

        /* The target is drawn as hollow outline type — it is a claim, not a
           result, and it should not look as solid as one. */
        .ghost {
          color: transparent;
          -webkit-text-stroke: 1.5px var(--silver-3);
          opacity: 0.85;
        }
        .ro.empty { border-style: dashed; border-color: var(--rule-2); }
        .notmeasured { font-size: 17px; font-weight: 500; }

        .meter {
          height: 7px; background: var(--rule-2); border-radius: 999px;
          overflow: hidden; margin-top: 9px;
        }
        .fill { height: 100%; border-radius: 999px; transition: width 0.5s var(--ease); }
        .tiny { font-size: 11px; line-height: 1.5; }

        .warnbox { margin-top: 16px; }
        .warnbox ul { margin: 0; padding-inline-start: 18px; }
        .warnbox li { font-size: 13px; color: var(--silver-2); margin-bottom: 5px; line-height: 1.6; }
        .warnbox.soft { border-color: rgba(251,191,36,0.35); }
        .warnbox.soft p { margin: 0; font-size: 13px; color: var(--warn); }

        .params { margin-top: 16px; }
        .strategies { margin-top: 16px; }
        .strat-list { display: flex; flex-wrap: wrap; gap: 7px; }

        .launch { margin-top: 16px; }
        .fields {
          display: grid; grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
          gap: 14px; align-items: end;
        }
        .fields label { display: flex; flex-direction: column; }
        .fields :global(.label) { margin-bottom: 6px; }
        .fields input, .fields select { width: 100%; }

        .modeswitch { display: flex; flex-direction: column; }
        .seg { display: flex; gap: 0; border: 1px solid var(--rule-2); border-radius: var(--r-sm); overflow: hidden; }
        .seg button {
          flex: 1; padding: 9px 10px; font-size: 12.5px; font-weight: 700;
          color: var(--silver-3); transition: all 0.15s var(--ease);
        }
        .seg button.on { background: var(--accent-soft); color: var(--accent); }
        .seg button.danger.on { background: rgba(244,67,108,0.16); color: var(--loss); }
        .seg button:disabled { opacity: 0.4; }

        .modehint { font-size: 12.5px; color: var(--silver-3); margin: 12px 0 0; }
        .venuenote { margin-top: 10px; line-height: 1.6; }

        .confirm {
          margin-top: 16px; padding: 14px;
          border: 1px solid rgba(244,67,108,0.32); border-radius: var(--r);
          background: rgba(244,67,108,0.05);
        }
        .confirm code {
          display: block; font-family: var(--font-mono); font-size: 12.5px;
          color: var(--silver-2); margin: 8px 0; direction: ltr;
        }
        .confirm input { width: 100%; }

        .err { color: var(--loss); font-size: 13px; margin: 14px 0 0; }
        .notice { color: var(--warn); font-size: 13px; margin: 14px 0 0; }
        .actions { margin-top: 18px; display: flex; gap: 10px; }
        .btn.small { padding: 6px 12px; font-size: 12px; margin-top: 10px; align-self: flex-start; }

        .disclaimer {
          margin: 26px 0 0; font-size: 12px; line-height: 1.75;
          color: var(--silver-4); max-width: 78ch;
          border-inline-start: 2px solid var(--rule-2); padding-inline-start: 14px;
        }
      `}</style>
    </div>
  );
}

function Param({ label, value }: { label: string; value: string }) {
  return (
    <div className="panel flat p">
      <div className="label">{label}</div>
      <div className="value sm tab">{value}</div>
      <style jsx>{`
        .p { padding: 13px 15px; }
        .p :global(.label) { margin-bottom: 4px; }
      `}</style>
    </div>
  );
}

function ruinColor(p: number): string {
  if (p < 0.05) return 'var(--gain)';
  if (p < 0.2) return 'var(--warn)';
  return 'var(--loss)';
}
