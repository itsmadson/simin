'use client';

/*
  A position, drawn on a price axis instead of listed as numbers.

  Every trading UI shows entry / stop / liquidation as four numbers in a row,
  which requires the reader to do arithmetic to answer the only question that
  matters: *how close is this to being destroyed?* At 8× leverage the
  liquidation price is 12% away and the stop is 2% away — as text those look
  similar; placed on a shared axis, the difference is immediate.

  So the whole marker set is laid out to scale on one track: liquidation, stop,
  entry, current price and target. The distance you see is the distance that
  exists.
*/

import type { PositionView } from '@/lib/api';
import { type Lang, digits, money, num, t } from '@/lib/i18n';

export function PositionCard({ p, lang }: { p: PositionView; lang: Lang }) {
  const long = p.direction === 'long';
  const up = p.unrealized >= 0;

  // Axis bounds: every marker plus a little air, so nothing sits on the edge.
  const marks = [p.entry_price, p.current_price, p.stop_price];
  if (p.take_profit) marks.push(p.take_profit);
  // Null means unleveraged: there is no liquidation price to draw, and drawing
  // a zero there would put a phantom marker at the far end of the axis.
  const liq = p.liquidation_price;
  const liqLive = liq != null && liq > 0 && Number.isFinite(liq);
  if (liqLive) marks.push(liq);

  const lo = Math.min(...marks);
  const hi = Math.max(...marks);
  const pad = (hi - lo) * 0.12 || hi * 0.01 || 1;
  const min = lo - pad;
  const max = hi + pad;
  const at = (v: number) => ((v - min) / (max - min)) * 100;

  // The zone between entry and stop is the money at risk; between entry and
  // target is the money being played for. Drawn, not described.
  const riskFrom = Math.min(at(p.entry_price), at(p.stop_price));
  const riskTo = Math.max(at(p.entry_price), at(p.stop_price));
  const tpAt = p.take_profit ? at(p.take_profit) : null;
  const rewardFrom = tpAt != null ? Math.min(at(p.entry_price), tpAt) : null;
  const rewardTo = tpAt != null ? Math.max(at(p.entry_price), tpAt) : null;

  return (
    <div className="pos panel">
      <header>
        <div className="id">
          <strong>{p.symbol}</strong>
          <span className={`side ${long ? 'l' : 's'}`}>
            {long ? t('long', lang) : t('short', lang)}
          </span>
          {p.leverage > 1 && <span className="lev">{digits(p.leverage, lang)}×</span>}
          {p.breakeven_armed && <span className="be">BE</span>}
        </div>
        <div className={`pnl ${up ? 'gain' : 'loss'}`}>
          <span className="tab">{up ? '+' : ''}{money(p.unrealized, lang)}</span>
          <span className="r tab">{num(p.r_multiple, lang, 2)}R</span>
        </div>
      </header>

      <div className="axis" role="img"
           aria-label={`${p.symbol}: entry ${p.entry_price}, now ${p.current_price}, stop ${p.stop_price}`}>
        <div className="track" />
        {rewardFrom != null && rewardTo != null && (
          <div className="zone reward" style={{ insetInlineStart: `${rewardFrom}%`, width: `${rewardTo - rewardFrom}%` }} />
        )}
        <div className="zone risk" style={{ insetInlineStart: `${riskFrom}%`, width: `${riskTo - riskFrom}%` }} />

        {liqLive && (
          <Marker at={at(liq)} kind="liq" label={t('liquidation', lang)}
                  value={money(liq, lang, 2)} lang={lang} />
        )}
        <Marker at={at(p.stop_price)} kind="stop" label={t('stopLoss', lang)}
                value={money(p.stop_price, lang, 2)} lang={lang} />
        <Marker at={at(p.entry_price)} kind="entry" label={t('entry', lang)}
                value={money(p.entry_price, lang, 2)} lang={lang} />
        {tpAt != null && (
          <Marker at={tpAt} kind="tp" label={t('takeProfit', lang)}
                  value={money(p.take_profit!, lang, 2)} lang={lang} />
        )}
        <Marker at={at(p.current_price)} kind="now" label={t('current', lang)}
                value={money(p.current_price, lang, 2)} lang={lang} live />
      </div>

      <footer>
        <span>{t('size', lang)} <b className="tab">{num(p.qty, lang, 4)}</b></span>
        <span>{t('strategy', lang)} <b>{p.strategy}</b></span>
        <span className="dim tab">{digits(p.bars_held, lang)} bars</span>
      </footer>

      <style jsx>{`
        .pos { padding: 16px 18px 12px; }
        header { display: flex; justify-content: space-between; align-items: flex-start; gap: 12px; }
        .id { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }
        .id strong { font-size: 15px; font-weight: 700; letter-spacing: 0.01em; }
        .side {
          font-size: 10.5px; font-weight: 700; padding: 2px 8px; border-radius: 999px;
          text-transform: uppercase; letter-spacing: 0.06em;
        }
        .side.l { background: rgba(52,211,153,0.14); color: var(--gain); }
        .side.s { background: rgba(244,67,108,0.14); color: var(--loss); }
        .lev {
          font-size: 10.5px; font-weight: 700; padding: 2px 7px; border-radius: 999px;
          background: rgba(251,191,36,0.14); color: var(--warn);
          font-family: var(--font-mono);
        }
        .be {
          font-size: 10px; font-weight: 700; padding: 2px 6px; border-radius: 4px;
          border: 1px solid var(--rule-2); color: var(--silver-3);
        }
        .pnl { text-align: end; display: flex; flex-direction: column; gap: 1px; }
        .pnl span:first-child { font-size: 17px; font-weight: 600; }
        .r { font-size: 11.5px; opacity: 0.7; }

        .axis { position: relative; height: 76px; margin: 30px 6px 12px; }
        .track {
          position: absolute; inset-inline: 0; top: 26px; height: 4px;
          background: var(--rule-2); border-radius: 2px;
        }
        .zone { position: absolute; top: 26px; height: 4px; border-radius: 2px; }
        .zone.risk { background: rgba(244,67,108,0.55); }
        .zone.reward { background: rgba(52,211,153,0.35); }

        footer {
          display: flex; gap: 16px; flex-wrap: wrap;
          font-size: 11.5px; color: var(--silver-3);
          border-top: 1px solid var(--rule); padding-top: 9px; margin-top: 2px;
        }
        footer b { color: var(--silver-2); font-weight: 600; }
      `}</style>
    </div>
  );
}

function Marker({
  at, kind, label, value, live,
}: {
  at: number;
  kind: 'liq' | 'stop' | 'entry' | 'tp' | 'now';
  label: string;
  value: string;
  lang: Lang;
  live?: boolean;
}) {
  const clamped = Math.max(0, Math.min(at, 100));
  // Alternate label placement above/below so adjacent markers never collide.
  const below = kind === 'stop' || kind === 'liq';
  return (
    <div className={`m ${kind} ${below ? 'below' : 'above'}`} style={{ insetInlineStart: `${clamped}%` }}>
      <span className={`pin ${live ? 'live' : ''}`} />
      <span className="lab">
        <em>{label}</em>
        <b className="tab">{value}</b>
      </span>
      <style jsx>{`
        .m { position: absolute; top: 20px; transform: translateX(-50%); }
        :global([dir='rtl']) .m { transform: translateX(50%); }
        .pin {
          display: block; width: 3px; height: 16px; border-radius: 2px;
          margin: 0 auto; background: var(--silver-3);
        }
        .m.liq .pin { background: var(--loss); box-shadow: 0 0 9px var(--loss); height: 20px; margin-top: -2px; }
        .m.stop .pin { background: #f4436c; opacity: 0.8; }
        .m.entry .pin { background: var(--silver); }
        .m.tp .pin { background: var(--gain); }
        .m.now .pin { background: var(--info); width: 4px; height: 22px; margin-top: -3px; }
        .pin.live { animation: blip 2.2s ease-in-out infinite; }
        @keyframes blip { 0%,100% { opacity: 1; } 50% { opacity: 0.45; } }

        .lab {
          position: absolute; inset-inline-start: 50%; transform: translateX(-50%);
          display: flex; flex-direction: column; align-items: center;
          white-space: nowrap; line-height: 1.25;
        }
        :global([dir='rtl']) .lab { transform: translateX(50%); }
        .m.above .lab { bottom: 20px; }
        .m.below .lab { top: 20px; }
        .lab em {
          font-style: normal; font-size: 9px; text-transform: uppercase;
          letter-spacing: 0.07em; color: var(--silver-4);
        }
        .lab b { font-size: 11px; font-weight: 600; color: var(--silver-2); }
        .m.liq .lab em, .m.liq .lab b { color: var(--loss); }
        .m.now .lab b { color: var(--info); }
      `}</style>
    </div>
  );
}
