import type { Metadata } from 'next';
import './globals.css';
import Sidebar from '@/components/Sidebar';
import TopBar from '@/components/TopBar';

export const metadata: Metadata = {
  title: 'Trend-Investorr | Algorithmic Trading Dashboard',
  description:
    'Volume-first algorithmic trading system for NSE equities. Monitor signals, manage watchlists, and track portfolio performance.',
  keywords: 'algorithmic trading, NSE, stock market, trading signals, portfolio tracker',
};

export default function RootLayout({
  children,
}: {
  children: React.ReactNode;
}) {
  return (
    <html lang="en" suppressHydrationWarning>
      <body suppressHydrationWarning>
        <div className="app-layout">
          <Sidebar />
          <TopBar />
          <main className="main-content">
            <div className="page-container">{children}</div>
          </main>
        </div>
      </body>
    </html>
  );
}
