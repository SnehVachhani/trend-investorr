'use client';

import Link from 'next/link';
import { usePathname, useSearchParams } from 'next/navigation';
import { useState } from 'react';

const NAV_ITEMS = [
  {
    href: '/',
    label: 'Dashboard',
    icon: (
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
        <rect x="3" y="3" width="7" height="7" rx="1" /><rect x="14" y="3" width="7" height="7" rx="1" />
        <rect x="3" y="14" width="7" height="7" rx="1" /><rect x="14" y="14" width="7" height="7" rx="1" />
      </svg>
    ),
  },
  {
    href: '/watchlists',
    label: 'Watchlists',
    icon: (
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
        <line x1="8" y1="6" x2="21" y2="6" /><line x1="8" y1="12" x2="21" y2="12" /><line x1="8" y1="18" x2="21" y2="18" />
        <line x1="3" y1="6" x2="3.01" y2="6" /><line x1="3" y1="12" x2="3.01" y2="12" /><line x1="3" y1="18" x2="3.01" y2="18" />
      </svg>
    ),
  },
  {
    href: '/sectors',
    label: 'Sector pulse',
    icon: (
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
        <path d="M3 19h18" /><path d="M6 16V9" /><path d="M12 16V5" /><path d="M18 16v-4" /><path d="m4 7 6-3 5 4 5-3" />
      </svg>
    ),
  },
  {
    href: '/signals',
    label: 'Signals',
    icon: (
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
        <polygon points="13 2 3 14 12 14 11 22 21 10 12 10 13 2" />
      </svg>
    ),
  },
  {
    href: '/screener',
    label: 'Screener',
    icon: (
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
        <path d="M4 5h16" /><path d="M7 12h10" /><path d="M10 19h4" /><path d="M6 5l6 7v7" />
      </svg>
    ),
  },
  {
    href: '/momentum',
    label: 'Momentum scanner',
    icon: (
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
        <polyline points="3 17 9 11 13 15 21 6" /><polyline points="17 6 21 6 21 10" />
      </svg>
    ),
  },
  {
    href: '/enhanced-momentum',
    label: 'Enhanced momentum',
    icon: (
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
        <path d="M4 18 9 13l3 3 8-9" /><path d="M15 7h5v5" /><path d="M4 4h5" />
      </svg>
    ),
  },
  {
    href: '/master',
    label: 'Master',
    icon: (
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
        <rect x="3" y="3" width="8" height="8" rx="1" /><rect x="13" y="3" width="8" height="5" rx="1" />
        <rect x="13" y="10" width="8" height="11" rx="1" /><rect x="3" y="13" width="8" height="8" rx="1" />
      </svg>
    ),
  },
  {
    href: '/orders',
    label: 'Orders',
    icon: (
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
        <path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z" />
        <polyline points="14 2 14 8 20 8" /><line x1="16" y1="13" x2="8" y2="13" /><line x1="16" y1="17" x2="8" y2="17" />
      </svg>
    ),
  },
  {
    href: '/portfolio',
    label: 'Portfolio',
    icon: (
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
        <rect x="2" y="7" width="20" height="14" rx="2" ry="2" /><path d="M16 21V5a2 2 0 0 0-2-2h-4a2 2 0 0 0-2 2v16" />
      </svg>
    ),
  },
  {
    href: '/backtest',
    label: 'Backtest',
    icon: (
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
        <path d="M9 3h6l2 4H7l2-4z" /><path d="M7 7v10a3 3 0 0 0 3 3h4a3 3 0 0 0 3-3V7" />
        <line x1="12" y1="11" x2="12" y2="15" /><line x1="10" y1="13" x2="14" y2="13" />
      </svg>
    ),
  },
  {
    href: '/settings',
    label: 'Settings',
    icon: (
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
        <circle cx="12" cy="12" r="3" />
        <path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 0 1 0 2.83 2 2 0 0 1-2.83 0l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 0 1-2 2 2 2 0 0 1-2-2v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 0 1-2.83 0 2 2 0 0 1 0-2.83l.06-.06A1.65 1.65 0 0 0 4.68 15a1.65 1.65 0 0 0-1.51-1H3a2 2 0 0 1-2-2 2 2 0 0 1 2-2h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 0 1 0-2.83 2 2 0 0 1 2.83 0l.06.06A1.65 1.65 0 0 0 9 4.68a1.65 1.65 0 0 0 1-1.51V3a2 2 0 0 1 2-2 2 2 0 0 1 2 2v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 0 1 2.83 0 2 2 0 0 1 0 2.83l-.06.06A1.65 1.65 0 0 0 19.4 9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 0 1 2 2 2 2 0 0 1-2 2h-.09a1.65 1.65 0 0 0-1.51 1z" />
      </svg>
    ),
  },
  {
    href: '/charges',
    label: 'Brokerage charges',
    icon: (
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
        <path d="M12 2v20" /><path d="M17 5H9.5a3.5 3.5 0 0 0 0 7h5a3.5 3.5 0 0 1 0 7H7" />
      </svg>
    ),
  },
];

export default function Sidebar() {
  const pathname = usePathname();
  const searchParams = useSearchParams();
  const isBacktestActive = pathname.startsWith('/backtest');
  const requestedBacktestSection = searchParams.get('section');
  const activeBacktestSection = requestedBacktestSection === 'filters' || requestedBacktestSection === 'watchlist' || requestedBacktestSection === 'fundamentals' || requestedBacktestSection === 'enhanced-momentum'
    ? requestedBacktestSection
    : 'algorithms';
  const [isBacktestExpanded, setIsBacktestExpanded] = useState(true);

  const renderNavItem = (item: (typeof NAV_ITEMS)[number]) => {
    const isActive =
      item.href === '/'
        ? pathname === '/'
        : pathname.startsWith(item.href);

    if (item.href === '/backtest') {
      return (
        <div key={item.href} className="sidebar-group">
          <div className="sidebar-parent-row">
            <Link
              href="/backtest?section=algorithms"
              className={`sidebar-link sidebar-parent-link ${isActive ? 'active' : ''}`}
              aria-current={isActive ? 'page' : undefined}
            >
              {item.icon}
              <span>{item.label}</span>
            </Link>
            <button
              type="button"
              className={`sidebar-expand-button ${isBacktestExpanded ? 'is-expanded' : ''}`}
              aria-expanded={isBacktestExpanded}
              aria-controls="backtest-subnav"
              aria-label={`${isBacktestExpanded ? 'Collapse' : 'Expand'} Backtest navigation`}
              onClick={() => setIsBacktestExpanded((expanded) => !expanded)}
            >
              <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                <polyline points="6 9 12 15 18 9" />
              </svg>
            </button>
          </div>

          <div id="backtest-subnav" className={`sidebar-subnav ${isBacktestExpanded ? 'is-open' : ''}`}>
            <Link
              href="/backtest?section=algorithms"
              className={`sidebar-sublink ${isBacktestActive && activeBacktestSection === 'algorithms' ? 'active' : ''}`}
              aria-current={isBacktestActive && activeBacktestSection === 'algorithms' ? 'page' : undefined}
            >
              <span className="sidebar-subdot" />
              <span>Algorithms</span>
            </Link>
            <Link
              href="/backtest?section=filters"
              className={`sidebar-sublink ${isBacktestActive && activeBacktestSection === 'filters' ? 'active' : ''}`}
              aria-current={isBacktestActive && activeBacktestSection === 'filters' ? 'page' : undefined}
            >
              <span className="sidebar-subdot" />
              <span>Filter combinations</span>
            </Link>
            <Link
              href="/backtest?section=watchlist"
              className={`sidebar-sublink ${isBacktestActive && activeBacktestSection === 'watchlist' ? 'active' : ''}`}
              aria-current={isBacktestActive && activeBacktestSection === 'watchlist' ? 'page' : undefined}
            >
              <span className="sidebar-subdot" />
              <span>Watchlist analysis</span>
            </Link>
            <Link
              href="/backtest?section=fundamentals"
              className={`sidebar-sublink ${isBacktestActive && activeBacktestSection === 'fundamentals' ? 'active' : ''}`}
              aria-current={isBacktestActive && activeBacktestSection === 'fundamentals' ? 'page' : undefined}
            >
              <span className="sidebar-subdot" />
              <span>Fundamental setups</span>
            </Link>
            <Link
              href="/backtest?section=enhanced-momentum"
              className={`sidebar-sublink ${isBacktestActive && activeBacktestSection === 'enhanced-momentum' ? 'active' : ''}`}
              aria-current={isBacktestActive && activeBacktestSection === 'enhanced-momentum' ? 'page' : undefined}
            >
              <span className="sidebar-subdot" />
              <span>Enhanced momentum</span>
            </Link>
          </div>
        </div>
      );
    }

    return (
      <Link
        key={item.href}
        href={item.href}
        className={`sidebar-link ${isActive ? 'active' : ''}`}
        aria-current={isActive ? 'page' : undefined}
      >
        {item.icon}
        <span>{item.label}</span>
      </Link>
    );
  };

  return (
    <aside className="sidebar">
      <div className="sidebar-logo">
        <div className="sidebar-logo-icon" aria-hidden="true">
          <svg viewBox="0 0 32 32" fill="none" xmlns="http://www.w3.org/2000/svg">
            <rect x="5" y="18" width="4" height="9" rx="1.2" fill="white" fillOpacity="0.48" />
            <rect x="12" y="14" width="4" height="13" rx="1.2" fill="white" fillOpacity="0.68" />
            <rect x="19" y="10" width="4" height="17" rx="1.2" fill="white" fillOpacity="0.88" />
            <path d="M4 17L10 14L14 16L24 7H28M24 7H28V11" stroke="white" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" />
          </svg>
        </div>
        <span className="sidebar-logo-text">Algo.Analyst</span>
      </div>

      <nav className="sidebar-nav">
        {NAV_ITEMS.map(renderNavItem)}
      </nav>

      <div className="sidebar-status">
        <span className="sidebar-status-dot online" />
        <span style={{ fontSize: '12px', color: 'var(--text-secondary)' }}>
          System Running
        </span>
      </div>
    </aside>
  );
}
