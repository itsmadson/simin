'use client';

/*
  The app shell: rail, language/direction switch, and the `--heat` driver.

  Language state lives here and is pushed onto <html lang dir>, so RTL is a real
  document direction rather than a per-component override. Everything in the
  stylesheet uses logical properties (inset-inline, border-inline) so the whole
  layout mirrors from that one attribute.
*/

import { createContext, useContext, useEffect, useState, type ReactNode } from 'react';
import { usePathname } from 'next/navigation';
import Link from 'next/link';
import { type Lang, t } from '@/lib/i18n';

interface Ctx {
  lang: Lang;
  setLang: (l: Lang) => void;
  heat: number;
  setHeat: (h: number) => void;
}

const AppCtx = createContext<Ctx>({
  lang: 'fa',
  setLang: () => {},
  heat: 0.3,
  setHeat: () => {},
});

export const useApp = () => useContext(AppCtx);

const NAV = [
  { href: '/', key: 'dial' as const, glyph: '◉' },
  { href: '/live', key: 'live' as const, glyph: '◈' },
  { href: '/lab', key: 'lab' as const, glyph: '⬡' },
  { href: '/markets', key: 'markets' as const, glyph: '◇' },
  { href: '/settings', key: 'settings' as const, glyph: '⚙' },
];

export function Shell({ children }: { children: ReactNode }) {
  const [lang, setLangState] = useState<Lang>('fa');
  const [heat, setHeat] = useState(0.3);
  const pathname = usePathname();

  useEffect(() => {
    const saved = window.localStorage.getItem('simin.lang');
    if (saved === 'en' || saved === 'fa') setLangState(saved);
  }, []);

  useEffect(() => {
    const root = document.documentElement;
    root.lang = lang;
    root.dir = lang === 'fa' ? 'rtl' : 'ltr';
    window.localStorage.setItem('simin.lang', lang);
  }, [lang]);

  useEffect(() => {
    // One variable drives every temperature-linked colour in the stylesheet.
    document.documentElement.style.setProperty('--heat', String(heat));
    const hue = 178 + (348 - 178) * heat;
    document.documentElement.style.setProperty('--accent', `hsl(${hue} 82% 56%)`);
    document.documentElement.style.setProperty('--accent-soft', `hsl(${hue} 82% 56% / 0.14)`);
    document.documentElement.style.setProperty('--accent-line', `hsl(${hue} 82% 56% / 0.36)`);
    document.documentElement.style.setProperty('--cool', `${hue} 82% 56%`);
  }, [heat]);

  const setLang = (l: Lang) => setLangState(l);

  return (
    <AppCtx.Provider value={{ lang, setLang, heat, setHeat }}>
      <div className="shell">
        <nav className="rail">
          <div className="brand">
            <span className="mark">◉</span>
            <div>
              <div className="name">{lang === 'fa' ? 'سیمین' : 'Simin'}</div>
              <div className="tag">{lang === 'fa' ? 'Simin' : 'سیمین'}</div>
            </div>
          </div>

          <div className="links">
            {NAV.map((item) => (
              <Link
                key={item.href}
                href={item.href}
                className={`navitem ${pathname === item.href ? 'on' : ''}`}
              >
                <span className="glyph">{item.glyph}</span>
                {t(item.key, lang)}
              </Link>
            ))}
          </div>

          <div className="foot">
            <div className="langs">
              <button
                className={`lb ${lang === 'fa' ? 'on' : ''}`}
                onClick={() => setLang('fa')}
                aria-pressed={lang === 'fa'}
              >
                فارسی
              </button>
              <button
                className={`lb ${lang === 'en' ? 'on' : ''}`}
                onClick={() => setLang('en')}
                aria-pressed={lang === 'en'}
              >
                EN
              </button>
            </div>
          </div>
        </nav>

        <main className="main">{children}</main>
      </div>

      <style jsx>{`
        .brand { display: flex; align-items: center; gap: 11px; padding: 4px 8px 22px; }
        .mark {
          font-size: 22px; color: var(--accent);
          filter: drop-shadow(0 0 10px var(--accent));
          transition: color 0.4s var(--ease), filter 0.4s var(--ease);
        }
        .name {
          font-family: var(--font-display); font-size: 21px; font-weight: 700;
          line-height: 1.1; letter-spacing: 0.01em;
        }
        .tag { font-size: 10.5px; color: var(--silver-4); letter-spacing: 0.1em; }
        .links { display: flex; flex-direction: column; gap: 3px; flex: 1; }
        .glyph { font-size: 13px; width: 15px; text-align: center; opacity: 0.85; }
        .foot { padding-top: 16px; border-top: 1px solid var(--rule); }
        .langs { display: flex; gap: 5px; }
        .lb {
          flex: 1; padding: 7px; border-radius: var(--r-sm); font-size: 12px;
          font-weight: 600; color: var(--silver-3); border: 1px solid var(--rule);
          transition: all 0.15s var(--ease);
        }
        .lb:hover { color: var(--silver); }
        .lb.on { background: var(--accent-soft); color: var(--accent); border-color: var(--accent-line); }

        @media (max-width: 860px) {
          .brand { padding: 0 10px 0 0; }
          .links { flex-direction: row; }
          .foot { border-top: none; padding-top: 0; padding-inline-start: 10px; }
        }
      `}</style>
    </AppCtx.Provider>
  );
}
