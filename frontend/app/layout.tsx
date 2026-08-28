import type { Metadata, Viewport } from 'next';
import { Shell } from '@/components/Shell';
import './globals.css';

export const metadata: Metadata = {
  title: 'سیمین · Simin',
  description:
    'Adaptive crypto trading with an honest risk dial. معاملهٔ خودکار ارز دیجیتال با درجهٔ ریسک صادقانه.',
};

export const viewport: Viewport = {
  themeColor: '#07080b',
  width: 'device-width',
  initialScale: 1,
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  // `lang`/`dir` start Persian-first and are corrected on mount by Shell from
  // localStorage. Starting at the default the majority of users want means no
  // visible flip for them, and one imperceptible one for the rest.
  return (
    <html lang="fa" dir="rtl" suppressHydrationWarning>
      <body>
        <Shell>{children}</Shell>
      </body>
    </html>
  );
}
