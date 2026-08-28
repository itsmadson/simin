'use client';

/*
  The risk dial.

  This is the product, so it is a real instrument rather than a slider with a
  number next to it: a 240° arc with ten detents, draggable, keyboard-operable,
  and wired to the page's `--heat` variable so turning it up warms the entire
  interface at once.

  The one idea that makes it more than a pretty gauge is the **honesty gap**.
  Two arcs are drawn on the same track: the level's design *target* as a hollow
  dashed sweep, and its *measured* result as a solid one. On a well-behaved
  level the two are close. On level 10 the target sweeps the whole dial and the
  measurement is a stub near zero, and the space between them is filled with a
  hatched band — you can see the exaggeration rather than having to read it out
  of a table. When nothing has been measured, the solid arc is absent entirely
  and the gap area says so, which is different from and much better than
  drawing a zero.
*/

import { useCallback, useEffect, useRef } from 'react';
import type { DialLevel } from '@/lib/api';
import { type Lang, digits, pct, t } from '@/lib/i18n';

const START = 150; // degrees; 0 is East, sweeping clockwise
const SWEEP = 240;
const R = 132;
const CX = 170;
const CY = 168;

function polar(cx: number, cy: number, r: number, deg: number) {
  const rad = ((deg - 90) * Math.PI) / 180;
  return { x: cx + r * Math.cos(rad), y: cy + r * Math.sin(rad) };
}

function arcPath(cx: number, cy: number, r: number, from: number, to: number) {
  const a = polar(cx, cy, r, from);
  const b = polar(cx, cy, r, to);
  const large = Math.abs(to - from) > 180 ? 1 : 0;
  return `M ${a.x.toFixed(2)} ${a.y.toFixed(2)} A ${r} ${r} 0 ${large} 1 ${b.x.toFixed(2)} ${b.y.toFixed(2)}`;
}

/** Level 1..10 -> degrees on the arc. */
const angleFor = (level: number) => START + ((level - 1) / 9) * SWEEP;

/**
 * Map a monthly return onto the same 0..1 track the dial uses, so the target
 * and measured arcs are directly comparable. The scale is deliberately
 * compressive: +200%/month and +12%/month would be unreadable on a linear axis,
 * and a log-ish curve keeps the small honest numbers visible while still
 * showing the outrageous one as outrageous.
 */
function returnToFraction(monthly: number): number {
  if (monthly <= 0) return 0;
  return Math.min(Math.log10(1 + monthly * 9) / Math.log10(1 + 2.0 * 9), 1);
}

export interface RiskDialProps {
  level: number;
  onChange: (level: number) => void;
  data?: DialLevel;
  lang: Lang;
  disabled?: boolean;
}

export function RiskDial({ level, onChange, data, lang, disabled }: RiskDialProps) {
  const svgRef = useRef<SVGSVGElement>(null);
  const dragging = useRef(false);

  const heat = (level - 1) / 9;

  const levelFromPoint = useCallback((clientX: number, clientY: number) => {
    const svg = svgRef.current;
    if (!svg) return null;
    const rect = svg.getBoundingClientRect();
    // Work in the SVG's own coordinate space so the maths is independent of
    // however CSS has scaled the element.
    const x = ((clientX - rect.left) / rect.width) * 340 - CX;
    const y = ((clientY - rect.top) / rect.height) * 340 - CY;
    let deg = (Math.atan2(y, x) * 180) / Math.PI + 90;
    if (deg < 0) deg += 360;
    let offset = deg - START;
    if (offset < -60) offset += 360;
    const fraction = Math.max(0, Math.min(offset / SWEEP, 1));
    return Math.round(fraction * 9) + 1;
  }, []);

  const handlePointer = useCallback(
    (e: PointerEvent | React.PointerEvent) => {
      if (disabled) return;
      const next = levelFromPoint(e.clientX, e.clientY);
      if (next !== null && next !== level) onChange(next);
    },
    [disabled, level, levelFromPoint, onChange],
  );

  useEffect(() => {
    const move = (e: PointerEvent) => {
      if (dragging.current) handlePointer(e);
    };
    const up = () => {
      dragging.current = false;
    };
    window.addEventListener('pointermove', move);
    window.addEventListener('pointerup', up);
    window.addEventListener('pointercancel', up);
    return () => {
      window.removeEventListener('pointermove', move);
      window.removeEventListener('pointerup', up);
      window.removeEventListener('pointercancel', up);
    };
  }, [handlePointer]);

  const onKeyDown = (e: React.KeyboardEvent) => {
    if (disabled) return;
    const step = e.key === 'ArrowRight' || e.key === 'ArrowUp' ? 1
      : e.key === 'ArrowLeft' || e.key === 'ArrowDown' ? -1
      : 0;
    if (step) {
      e.preventDefault();
      onChange(Math.max(1, Math.min(10, level + step)));
    }
    if (e.key === 'Home') { e.preventDefault(); onChange(1); }
    if (e.key === 'End') { e.preventDefault(); onChange(10); }
  };

  const targetFrac = data ? returnToFraction(data.target_monthly_return) : 0;
  const measuredFrac =
    data?.measured_monthly_return != null
      ? returnToFraction(data.measured_monthly_return)
      : null;

  const handleAngle = angleFor(level);
  const handlePos = polar(CX, CY, R, handleAngle);
  const targetAngle = START + targetFrac * SWEEP;
  const measuredAngle = measuredFrac != null ? START + measuredFrac * SWEEP : null;

  const hue = 178 + (348 - 178) * heat;

  return (
    <div className="dialwrap">
      <svg
        ref={svgRef}
        viewBox="0 0 340 340"
        role="slider"
        aria-label={t('chooseRisk', lang)}
        aria-valuemin={1}
        aria-valuemax={10}
        aria-valuenow={level}
        aria-valuetext={data ? `${level} — ${lang === 'fa' ? data.name_fa : data.name_en}` : String(level)}
        tabIndex={disabled ? -1 : 0}
        onKeyDown={onKeyDown}
        onPointerDown={(e) => {
          if (disabled) return;
          dragging.current = true;
          (e.target as Element).setPointerCapture?.(e.pointerId);
          handlePointer(e);
        }}
        style={{ touchAction: 'none', cursor: disabled ? 'default' : 'grab' }}
      >
        <defs>
          <linearGradient id="heatTrack" x1="0" y1="1" x2="1" y2="0">
            <stop offset="0%" stopColor="hsl(178 82% 52%)" />
            <stop offset="45%" stopColor="hsl(38 96% 56%)" />
            <stop offset="100%" stopColor="hsl(348 92% 58%)" />
          </linearGradient>

          {/* The hatch that fills the honesty gap. Visually "this is empty". */}
          <pattern id="hatch" width="7" height="7" patternTransform="rotate(45)" patternUnits="userSpaceOnUse">
            <line x1="0" y1="0" x2="0" y2="7" stroke="hsl(348 92% 58% / 0.45)" strokeWidth="2.5" />
          </pattern>

          <filter id="dialGlow" x="-60%" y="-60%" width="220%" height="220%">
            <feGaussianBlur stdDeviation="6" result="b" />
            <feMerge>
              <feMergeNode in="b" />
              <feMergeNode in="SourceGraphic" />
            </feMerge>
          </filter>
        </defs>

        {/* Base track */}
        <path
          d={arcPath(CX, CY, R, START, START + SWEEP)}
          fill="none"
          stroke="var(--rule-2)"
          strokeWidth="15"
          strokeLinecap="round"
        />

        {/* Filled portion up to the selected level */}
        <path
          d={arcPath(CX, CY, R, START, handleAngle)}
          fill="none"
          stroke="url(#heatTrack)"
          strokeWidth="15"
          strokeLinecap="round"
          opacity="0.92"
          style={{ transition: 'd 0.3s var(--ease)' }}
        />

        {/* ── The honesty gap ──────────────────────────────────────────────
            Target arc (dashed, outer) vs measured arc (solid, inner). The
            hatched band between them is the exaggeration made visible. */}
        {data && (
          <g className="gap">
            {measuredAngle != null && targetAngle > measuredAngle && (
              <path
                d={arcPath(CX, CY, R + 26, measuredAngle, targetAngle)}
                fill="none"
                stroke="url(#hatch)"
                strokeWidth="13"
                opacity="0.85"
              />
            )}
            <path
              d={arcPath(CX, CY, R + 26, START, targetAngle)}
              fill="none"
              stroke="hsl(0 0% 100% / 0.3)"
              strokeWidth="2"
              strokeDasharray="3 5"
              strokeLinecap="round"
            />
            {measuredAngle != null && (
              <path
                d={arcPath(CX, CY, R + 26, START, measuredAngle)}
                fill="none"
                stroke="var(--gain)"
                strokeWidth="4.5"
                strokeLinecap="round"
              />
            )}
          </g>
        )}

        {/* Detents */}
        {Array.from({ length: 10 }, (_, i) => {
          const lv = i + 1;
          const a = angleFor(lv);
          const inner = polar(CX, CY, R - 24, a);
          const outer = polar(CX, CY, R - 13, a);
          const on = lv <= level;
          return (
            <g key={lv}>
              <line
                x1={inner.x} y1={inner.y} x2={outer.x} y2={outer.y}
                stroke={on ? `hsl(${178 + (348 - 178) * ((lv - 1) / 9)} 85% 58%)` : 'var(--silver-4)'}
                strokeWidth={lv === level ? 3.5 : 2}
                strokeLinecap="round"
                opacity={on ? 1 : 0.5}
              />
              <text
                {...polar(CX, CY, R - 38, a)}
                textAnchor="middle"
                dominantBaseline="central"
                fontSize="11"
                fontFamily="var(--font-mono)"
                fill={lv === level ? `hsl(${hue} 85% 62%)` : 'var(--silver-4)'}
                fontWeight={lv === level ? 700 : 500}
              >
                {digits(lv, lang)}
              </text>
            </g>
          );
        })}

        {/* Handle */}
        <g
          style={{ transition: 'transform 0.28s var(--ease-snap)' }}
          transform={`translate(${handlePos.x} ${handlePos.y})`}
          filter="url(#dialGlow)"
        >
          <circle r="15" fill="var(--pit)" stroke={`hsl(${hue} 85% 58%)`} strokeWidth="3" />
          <circle r="5" fill={`hsl(${hue} 85% 62%)`} />
        </g>

        {/* Centre readout */}
        <text
          x={CX} y={CY - 14}
          textAnchor="middle"
          fontSize="62"
          fontFamily="var(--font-display)"
          fontWeight="800"
          fill={`hsl(${hue} 82% 64%)`}
        >
          {digits(level, lang)}
        </text>
        <text
          x={CX} y={CY + 22}
          textAnchor="middle"
          fontSize="15"
          fontFamily="var(--font-display)"
          fontWeight="600"
          fill="var(--silver-2)"
        >
          {data ? (lang === 'fa' ? data.name_fa : data.name_en) : ''}
        </text>
        <text
          x={CX} y={CY + 46}
          textAnchor="middle"
          fontSize="11.5"
          fontFamily="var(--font-mono)"
          fill="var(--silver-3)"
        >
          {data
            ? `${digits((data.risk_per_trade * 100).toFixed(2), lang)}% · ${digits(data.leverage, lang)}× · ${data.signal_timeframe}`
            : ''}
        </text>
      </svg>

      <style jsx>{`
        .dialwrap {
          display: flex;
          justify-content: center;
          user-select: none;
        }
        .dialwrap svg {
          width: 100%;
          max-width: 380px;
          height: auto;
          overflow: visible;
        }
        .dialwrap svg:active { cursor: grabbing; }
      `}</style>
    </div>
  );
}

/** The legend that explains the two arcs. Without it the gap is just a shape. */
export function HonestyLegend({ data, lang }: { data?: DialLevel; lang: Lang }) {
  if (!data) return null;
  const measured = data.measured_monthly_return;
  return (
    <div className="legend">
      <div className="row">
        <span className="swatch dashed" />
        <span className="k">{t('target', lang)}</span>
        <span className="v tab dim">{pct(data.target_monthly_return, lang, 0)}</span>
      </div>
      <div className="row">
        <span className="swatch solid" />
        <span className="k">{t('measured', lang)}</span>
        <span className={`v tab ${measured == null ? 'dim' : measured >= 0 ? 'gain' : 'loss'}`}>
          {measured == null ? t('notMeasured', lang) : pct(measured, lang, 2)}
        </span>
      </div>
      <style jsx>{`
        .legend { display: flex; flex-direction: column; gap: 7px; margin-top: 14px; }
        .row { display: flex; align-items: center; gap: 10px; font-size: 12.5px; }
        .swatch { width: 24px; height: 3px; border-radius: 2px; flex: none; }
        .swatch.dashed {
          background: repeating-linear-gradient(90deg, rgba(255,255,255,0.45) 0 3px, transparent 3px 8px);
        }
        .swatch.solid { background: var(--gain); height: 4px; }
        .k { color: var(--silver-3); flex: 1; }
        .v { font-weight: 600; }
      `}</style>
    </div>
  );
}
