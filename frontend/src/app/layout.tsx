import type { Metadata } from 'next';
import { Suspense } from 'react';
import './globals.css';
import Sidebar from '@/components/Sidebar';
import TopBar from '@/components/TopBar';
import StockDetailsProvider from '@/components/StockDetailsProvider';

export const metadata: Metadata = {
  title: 'Algo.Analyst',
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
        <StockDetailsProvider>
          <div className="app-layout">
            <Suspense fallback={<aside className="sidebar" aria-hidden="true" />}>
              <Sidebar />
            </Suspense>
            <TopBar />
            <main className="main-content">
              <div className="page-container">{children}</div>
            </main>
          </div>
        </StockDetailsProvider>
      </body>
    </html>
  );
}
