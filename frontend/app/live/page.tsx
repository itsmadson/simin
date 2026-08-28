'use client';

/*
  The live view: what the bot is doing right now, and why.

  The "why" is the part most bots omit. A row of positions tells you what
  happened; the decision panel tells you what the ensemble thought on the last
  bar for every symbol — including the ones it decided *not* to trade and the
  reason. A bot that only shows its trades is a bot you cannot supervise.
*/

import { useCallback, useEffect, useRef, useState } from 'react';
import Link from 'next/link';
import { PositionCard } from '@/components/PositionCard';
import { useApp } from '@/components/Shell';
import { API, ApiError, api, type BotStatus } from '@/lib/api';
import { digits, money, num, pct, t } from '@/lib/i18n';

export default function LivePage() {
  const { lang, setHeat } = useApp();
  const [bot, setBot] = useState<BotStatus | null>(null);
  const [err, setErr] = useState('');
  const [busy, setBusy] = useState('');
  const wsRef = useRef<WebSocket | null>(null);

  const refresh = useCallback(async () => {
    try {
      const b = await api.bot();
      setBot(b);
      if (b.risk_level) setHeat((b.risk_level - 1) / 9);
      setErr('');
    } catch (e) {
      setErr(e instanceof ApiError ? e.message : String(e));
    }
  }, [setHeat]);

  useEffect(() => {
    refresh();
    // The socket is the primary channel; polling is the fallback for when it
    // drops. Losing the connection must not mean losing sight of a live
    // leveraged position.
    const poll = setInterval(refresh, 5000);
    try {
      const ws = new WebSocket(API.replace(/^http/, 'ws') + '/ws');
      ws.onmessage = (ev) => {
        try {
          const payload = JSON.parse(ev.data);
          if (payload.status?.state && payload.status.state !== 'stopped') {
            setBot((prev) => ({ ...(prev ?? {}), ...payload.status, positions: payload.positions, events: payload.events }));
          }
        } catch { /* malformed frame: the poll will catch up */ }
      };
      wsRef.current = ws;
    } catch { /* no socket: polling covers it */ }
    return () => {
      clearInterval(poll);
      wsRef.current?.close();
    };
  }, [refresh]);

  const act = async (name: string, fn: () => Promise<unknown>) => {
    setBusy(name);
    setErr('');
    try {
      await fn();
      await refresh();
    } catch (e) {
      setErr(e instanceof ApiError ? e.message : String(e));
    } finally {
      setBusy('');
    }
  };

  if (!bot || bot.state === 'stopped') {
    return (
      <div className="empty rise">
        <div className="glyph">◈</div>
        <h2>{t('botNotRunning', lang)}</h2>
        <p className="sub">{t('botNotRunningHint', lang)}</p>
        <Link href="/" className="btn primary" style={{ marginTop: 18 }}>
          {t('dial', lang)} →
        </Link>
        {err && <p className="err">{err}</p>}
        <style jsx>{`
          .empty { text-align: center; padding: 90px 20px; }
          .glyph { font-size: 46px; color: var(--silver-4); margin-bottom: 14px; }
          h2 { font-size: 21px; margin-bottom: 6px; }
          .err { color: var(--loss); font-size: 13px; margin-top: 18px; }
        `}</style>
      </div>
    );
  }

  const positions = bot.positions ?? [];
  const events = bot.events ?? [];
  const decisions = Object.entries(bot.decisions ?? {});
  const ret = bot.return_pct ?? 0;
  const halted = bot.halt && bot.halt !== 'none';

  return (
    <div className="rise">
      <header className="head">
        <div>
          <h1>{t('live', lang)}</h1>
          <p className="sub">
            {bot.venue} · {t('dial', lang)} {digits(bot.risk_level ?? 0, lang)} ·{' '}
            {bot.profile_name}
          </p>
        </div>
        <div className="controls">
          <span className={`chip ${bot.mode === 'real' ? 'real' : 'lab'}`}>
            {bot.mode === 'real' ? t('modeReal', lang) : t('modeLab', lang)}
          </span>
          <span className={`chip ${bot.state === 'running' ? 'live' : ''}`}>
            {bot.state === 'running' && <span className="dot pulse" />}
            {t(bot.state === 'running' ? 'running' : bot.state === 'paused' ? 'paused' : 'halted', lang)}
          </span>
          {bot.state === 'running' ? (
            <button className="btn" onClick={() => act('pause', api.pause)} disabled={!!busy}>
              {t('pause', lang)}
            </button>
          ) : bot.state === 'paused' ? (
            <button className="btn primary" onClick={() => act('resume', api.resume)} disabled={!!busy}>
              {t('resume', lang)}
            </button>
          ) : null}
          <button className="btn" onClick={() => act('flatten', api.flatten)} disabled={!!busy || !positions.length}>
            {t('flatten', lang)}
          </button>
          <button className="btn danger" onClick={() => act('kill', api.kill)} disabled={!!busy}>
            {t('kill', lang)}
          </button>
          <button className="btn ghost" onClick={() => act('stop', () => api.stop(false))} disabled={!!busy}>
            {t('stop', lang)}
          </button>
        </div>
      </header>

      {halted && (
        <div className="panel hot halt fade">
          <strong>{t('halted', lang)}: {bot.halt}</strong>
          <span className="sub">{bot.halt_note}</span>
        </div>
      )}
      {bot.profile_clamped && <div className="panel warnsoft fade">{t('spotOnlyWarning', lang)}</div>}
      {err && <div className="panel warnsoft fade" style={{ color: 'var(--loss)' }}>{err}</div>}

      <section className="grid g4 stats">
        <Stat label={t('equity', lang)} value={money(bot.equity, lang, 2)}
              sub={`${t('startingEquity', lang)} ${money(bot.starting_equity, lang, 0)}`} />
        <Stat label={t('totalReturn', lang)} value={pct(ret, lang, 2)} tone={ret >= 0 ? 'gain' : 'loss'}
              sub={`${t('maxDrawdown', lang)} ${pct(bot.drawdown ?? 0, lang, 1).replace('+', '')}`} />
        <Stat label={t('todayPnl', lang)} value={money(bot.day_realised, lang, 2)}
              tone={(bot.day_realised ?? 0) >= 0 ? 'gain' : 'loss'}
              sub={`${digits(bot.trades_today ?? 0, lang)} ${t('trades', lang)}`} />
        <Stat label={t('openPositions', lang)} value={digits(positions.length, lang)}
              sub={`${digits(bot.total_trades ?? 0, lang)} ${t('totalTrades', lang)}`} />
      </section>

      <section className="cols">
        <div className="left">
          <h2 className="section">{t('openPositions', lang)}</h2>
          {positions.length === 0 ? (
            <div className="panel flat none">{t('noPositions', lang)}</div>
          ) : (
            <div className="poslist">
              {positions.map((p) => (
                <PositionCard key={p.symbol} p={p} lang={lang} />
              ))}
            </div>
          )}

          {decisions.length > 0 && (
            <>
              <h2 className="section">
                {lang === 'fa' ? 'تصمیم آخرین کندل' : 'Last bar decision'}
              </h2>
              <div className="panel decisions">
                {decisions.map(([sym, d]) => (
                  <div key={sym} className="drow">
                    <div className="dsym">
                      <b>{sym}</b>
                      <span className={`regime ${d.regime}`}>{d.regime}</span>
                    </div>
                    <div className="dbar">
                      <div className="dtrack">
                        <div
                          className="dfill"
                          style={{
                            width: `${Math.min(d.score * 100, 100)}%`,
                            background: d.accepted ? 'var(--gain)' : 'var(--silver-4)',
                          }}
                        />
                        <div className="dthresh" style={{ insetInlineStart: `${d.threshold * 100}%` }} />
                      </div>
                      <span className="dscore tab">{num(d.score, lang, 2)}</span>
                    </div>
                    <div className="dwhy">
                      {d.accepted ? (
                        <span className="gain">{d.agreeing.join(' + ')}</span>
                      ) : (
                        <span className="dim">{d.reason}</span>
                      )}
                    </div>
                  </div>
                ))}
              </div>
            </>
          )}
        </div>

        <div className="right">
          <h2 className="section">{t('activity', lang)}</h2>
          <div className="panel tape">
            {events.length === 0 ? (
              <div className="none">{t('noActivity', lang)}</div>
            ) : (
              events.map((e, i) => (
                <div key={`${e.ts}-${i}`} className={`ev ${kindTone(e.kind)}`}>
                  <span className="time tab">{new Date(e.ts).toLocaleTimeString(lang === 'fa' ? 'fa-IR' : 'en-GB')}</span>
                  <span className="kind">{e.kind}</span>
                  <span className="msg">{(lang === 'fa' && e.message_fa) || e.message}</span>
                </div>
              ))
            )}
          </div>
        </div>
      </section>

      <style jsx>{`
        .head {
          display: flex; justify-content: space-between; align-items: flex-start;
          gap: 16px; margin-bottom: 20px; flex-wrap: wrap;
        }
        h1 { font-size: 26px; }
        .controls { display: flex; gap: 8px; align-items: center; flex-wrap: wrap; }
        .halt { margin-bottom: 16px; display: flex; flex-direction: column; gap: 4px; }
        .halt strong { color: var(--loss); }
        .warnsoft {
          margin-bottom: 16px; border-color: rgba(251,191,36,0.35);
          color: var(--warn); font-size: 13px; padding: 12px 16px;
        }
        .stats { margin-bottom: 22px; }
        .section {
          font-size: 12px; text-transform: uppercase; letter-spacing: 0.11em;
          color: var(--silver-3); margin: 0 0 11px; font-weight: 600;
        }
        :global([lang='fa']) .section { text-transform: none; letter-spacing: 0; font-size: 13px; }

        .cols { display: grid; grid-template-columns: 1fr 380px; gap: 18px; align-items: start; }
        @media (max-width: 1100px) { .cols { grid-template-columns: 1fr; } }
        .poslist { display: flex; flex-direction: column; gap: 14px; }
        .none { color: var(--silver-4); text-align: center; padding: 34px 10px; font-size: 13px; }

        .decisions { display: flex; flex-direction: column; gap: 14px; }
        .drow { display: grid; grid-template-columns: 130px 1fr; gap: 10px 14px; align-items: center; }
        .dsym { display: flex; align-items: center; gap: 8px; }
        .dsym b { font-size: 13px; }
        .regime {
          font-size: 9.5px; padding: 1px 6px; border-radius: 3px;
          text-transform: uppercase; letter-spacing: 0.05em; font-weight: 700;
          background: var(--rule-2); color: var(--silver-3);
        }
        .regime.trend { background: rgba(96,165,250,0.15); color: var(--info); }
        .regime.range { background: rgba(251,191,36,0.13); color: var(--warn); }
        .dbar { display: flex; align-items: center; gap: 10px; }
        .dtrack {
          position: relative; flex: 1; height: 6px;
          background: var(--rule-2); border-radius: 3px; overflow: visible;
        }
        .dfill { height: 100%; border-radius: 3px; transition: width 0.4s var(--ease); }
        .dthresh {
          position: absolute; top: -3px; width: 2px; height: 12px;
          background: var(--silver-2); border-radius: 1px;
        }
        .dscore { font-size: 11.5px; color: var(--silver-2); min-width: 34px; text-align: end; }
        .dwhy { grid-column: 2; font-size: 11.5px; margin-top: -6px; }

        .tape { max-height: 620px; overflow-y: auto; padding: 8px; }
        .ev {
          display: grid; grid-template-columns: 62px 1fr; gap: 4px 9px;
          padding: 8px 9px; border-radius: var(--r-sm); font-size: 12px;
          border-inline-start: 2px solid transparent;
        }
        .ev:hover { background: rgba(255,255,255,0.02); }
        .ev.good { border-inline-start-color: var(--gain); }
        .ev.bad { border-inline-start-color: var(--loss); }
        .ev.warn { border-inline-start-color: var(--warn); }
        .time { color: var(--silver-4); font-size: 10.5px; }
        .kind {
          font-size: 9.5px; text-transform: uppercase; letter-spacing: 0.06em;
          color: var(--silver-4); font-weight: 700;
        }
        .msg { grid-column: 2; color: var(--silver-2); line-height: 1.5; }
      `}</style>
    </div>
  );
}

function Stat({ label, value, sub, tone }: { label: string; value: string; sub?: string; tone?: string }) {
  return (
    <div className="panel">
      <div className="label">{label}</div>
      <div className={`value tab ${tone ?? ''}`}>{value}</div>
      {sub && <div className="sub">{sub}</div>}
    </div>
  );
}

function kindTone(kind: string): string {
  if (kind === 'entry' || kind === 'resume' || kind === 'start') return 'good';
  if (kind.includes('fail') || kind === 'halt' || kind === 'kill' || kind === 'error' || kind === 'fatal') return 'bad';
  if (kind === 'exit' || kind === 'signal_rejected' || kind === 'stop_moved') return 'warn';
  return '';
}
