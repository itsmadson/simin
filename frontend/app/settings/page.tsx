'use client';

/*
  Settings: read-only, deliberately.

  Credentials are set through environment variables and never through the UI,
  because a browser form that accepts an API secret means that secret travels
  through the page, sits in React state, and lands in whatever the browser
  decided to cache. The backend never sends key material either — this page can
  only learn *whether* a venue is configured, which is all it needs to tell you
  what is missing.
*/

import { useEffect, useState } from 'react';
import { useApp } from '@/components/Shell';
import { ApiError, api, type Venue } from '@/lib/api';
import { digits, money, pct, t } from '@/lib/i18n';

export default function SettingsPage() {
  const { lang } = useApp();
  const [settings, setSettings] = useState<Awaited<ReturnType<typeof api.settings>> | null>(null);
  const [venues, setVenues] = useState<Venue[]>([]);
  const [strategies, setStrategies] = useState<Awaited<ReturnType<typeof api.strategies>> | null>(null);
  const [err, setErr] = useState('');

  useEffect(() => {
    Promise.all([api.settings(), api.venues(), api.strategies()])
      .then(([s, v, st]) => { setSettings(s); setVenues(v.venues); setStrategies(st); })
      .catch((e) => setErr(e instanceof ApiError ? e.message : String(e)));
  }, []);

  return (
    <div className="rise">
      <header className="head">
        <h1>{t('settings', lang)}</h1>
        <p className="sub">
          {lang === 'fa'
            ? 'همه‌چیز از طریق متغیرهای محیطی تنظیم می‌شود. کلیدهای API هرگز از مرورگر عبور نمی‌کنند.'
            : 'Everything is configured through environment variables. API keys never pass through the browser.'}
        </p>
      </header>

      {err && <div className="panel errbox">{err}</div>}

      {settings && (
        <>
          {settings.start_problems.length > 0 && (
            <section className="panel hot fade">
              <div className="label" style={{ color: 'var(--loss)' }}>
                {lang === 'fa' ? 'موانع شروع' : 'Blocking start'}
              </div>
              <ul className="probs">
                {settings.start_problems.map((p, i) => <li key={i}>{p}</li>)}
              </ul>
            </section>
          )}

          <section className="grid g3 fade" style={{ marginTop: 16 }}>
            <Row label={lang === 'fa' ? 'حالت' : 'Mode'} value={settings.mode} />
            <Row label={t('venue', lang)} value={settings.venue} />
            <Row label={t('dial', lang)} value={digits(settings.risk_level, lang)} />
            <Row label={t('symbols', lang)} value={settings.symbols.join(', ')} />
            <Row label={t('startingEquity', lang)} value={money(settings.starting_equity, lang, 0)} />
            <Row label={lang === 'fa' ? 'انجماد معاملات' : 'Trading frozen'}
                 value={settings.trading_frozen ? 'yes' : 'no'} />
          </section>

          <section className="panel fade" style={{ marginTop: 16 }}>
            <div className="label">{t('venue', lang)}</div>
            <div className="tablewrap">
              <table>
                <thead>
                  <tr>
                    <th>{t('venue', lang)}</th>
                    <th>{lang === 'fa' ? 'فیوچرز' : 'Futures'}</th>
                    <th className="num">{t('leverage', lang)}</th>
                    <th className="num">{lang === 'fa' ? 'کارمزد رفت‌وبرگشت' : 'Round trip'}</th>
                    <th>{lang === 'fa' ? 'کلید API' : 'API key'}</th>
                  </tr>
                </thead>
                <tbody>
                  {venues.map((v) => (
                    <tr key={v.name}>
                      <td>
                        <b>{v.display_name}</b>
                        <div className="note">{lang === 'fa' ? v.notes_fa : v.notes_en}</div>
                      </td>
                      <td>{v.supports_futures ? <span className="gain">✓</span> : <span className="dim">✕</span>}</td>
                      <td className="num">{digits(v.max_leverage, lang)}×</td>
                      <td className="num">{pct(v.round_trip_cost, lang, 2).replace('+', '')}</td>
                      <td>
                        {v.credentials_configured
                          ? <span className="gain">{lang === 'fa' ? 'تنظیم شده' : 'configured'}</span>
                          : <span className="dim">{lang === 'fa' ? 'تنظیم نشده' : 'not set'}</span>}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
            <p className="sub envhint">
              {lang === 'fa'
                ? 'برای فعال‌سازی حالت واقعی: SIMIN_COINEX_KEY، SIMIN_COINEX_SECRET، SIMIN_REAL_MODE_ACKNOWLEDGED=1 و SIMIN_MAX_CAPITAL را تنظیم کنید.'
                : 'To enable real mode set SIMIN_COINEX_KEY, SIMIN_COINEX_SECRET, SIMIN_REAL_MODE_ACKNOWLEDGED=1 and SIMIN_MAX_CAPITAL.'}
            </p>
          </section>

          {strategies && (
            <section className="panel fade" style={{ marginTop: 16 }}>
              <div className="label">{t('strategies', lang)}</div>
              <div className="strats">
                {strategies.strategies.map((s) => (
                  <div key={s.name} className="strat">
                    <div className="sname">
                      <b>{lang === 'fa' && s.name_fa ? s.name_fa : s.name}</b>
                      <span className={`regime ${s.regime}`}>{s.regime}</span>
                    </div>
                    <p>{lang === 'fa' && s.description_fa ? s.description_fa : s.description}</p>
                  </div>
                ))}
              </div>
            </section>
          )}
        </>
      )}

      <style jsx>{`
        .head { margin-bottom: 20px; }
        h1 { font-size: 26px; }
        .errbox { color: var(--loss); font-size: 13px; }
        .probs { margin: 0; padding-inline-start: 18px; }
        .probs li { font-size: 12.5px; color: var(--silver-2); margin-bottom: 5px; line-height: 1.6; }
        .tablewrap { overflow-x: auto; }
        .note { font-size: 11px; color: var(--silver-4); margin-top: 3px; max-width: 46ch; line-height: 1.5; }
        .envhint { margin-top: 12px; font-family: var(--font-mono); font-size: 11px; line-height: 1.7; }
        .strats { display: grid; grid-template-columns: repeat(auto-fit, minmax(250px, 1fr)); gap: 14px; }
        .strat { padding: 12px; border: 1px solid var(--rule); border-radius: var(--r-sm); background: var(--pit); }
        .sname { display: flex; align-items: center; gap: 8px; margin-bottom: 5px; }
        .sname b { font-size: 13.5px; }
        .regime {
          font-size: 9.5px; padding: 1px 6px; border-radius: 3px; font-weight: 700;
          text-transform: uppercase; background: var(--rule-2); color: var(--silver-3);
        }
        .regime.trend { background: rgba(96,165,250,0.15); color: var(--info); }
        .regime.range { background: rgba(251,191,36,0.13); color: var(--warn); }
        .strat p { margin: 0; font-size: 12px; color: var(--silver-3); line-height: 1.6; }
      `}</style>
    </div>
  );
}

function Row({ label, value }: { label: string; value: string }) {
  return (
    <div className="panel flat">
      <div className="label">{label}</div>
      <div className="value sm">{value}</div>
    </div>
  );
}
