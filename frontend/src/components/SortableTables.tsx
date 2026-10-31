'use client';

import { useEffect } from 'react';

type SortDirection = 'ascending' | 'descending';

interface SortGroup {
  rows: HTMLTableRowElement[];
  value: string;
  empty: boolean;
  originalIndex: number;
}

function cleanValue(value: string): string {
  return value.replace(/\s+/g, ' ').trim();
}

function numericValue(value: string): number | null {
  const cleaned = value
    .replace(/[₹$€£,%×]/g, '')
    .replace(/−/g, '-')
    .replace(/,/g, '')
    .replace(/\s*(?:cr|crores?|inr|days?|d|trades?|weeks?|w|months?|m|years?|y)$/i, '')
    .replace(/^\+/, '')
    .trim();
  if (!cleaned || !/^[-+]?(?:\d+\.?\d*|\.\d+)$/.test(cleaned)) return null;
  const parsed = Number(cleaned);
  return Number.isFinite(parsed) ? parsed : null;
}

function dateValue(value: string): number | null {
  if (!/(?:\d{1,2}[/-]\d{1,2}[/-]\d{2,4}|\b(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)\b|\d{4}-\d{2}-\d{2})/i.test(value)) return null;
  const parsed = Date.parse(value);
  return Number.isFinite(parsed) ? parsed : null;
}

function compareValues(left: string, right: string): number {
  const leftNumber = numericValue(left);
  const rightNumber = numericValue(right);
  if (leftNumber != null && rightNumber != null) return leftNumber - rightNumber;

  const leftDate = dateValue(left);
  const rightDate = dateValue(right);
  if (leftDate != null && rightDate != null) return leftDate - rightDate;

  return left.localeCompare(right, undefined, { numeric: true, sensitivity: 'base' });
}

function cellValue(row: HTMLTableRowElement, column: number): string {
  const cell = row.cells[column];
  if (!cell) return '';
  return cleanValue(cell.dataset.sortValue ?? cell.innerText ?? cell.textContent ?? '');
}

function collectGroups(body: HTMLTableSectionElement, column: number): SortGroup[] {
  const groups: SortGroup[] = [];
  Array.from(body.rows).forEach((row, originalIndex) => {
    const isFullWidthDetail = row.cells.length === 1 && row.cells[0].colSpan > 1;
    const lastGroup = groups[groups.length - 1];
    if (isFullWidthDetail && lastGroup) {
      lastGroup.rows.push(row);
      return;
    }
    const value = cellValue(row, column);
    const placeholder = row.cells.length === 1 && row.cells[0].colSpan > 1;
    groups.push({ rows: [row], value, empty: placeholder || !value || value === '—' || value === '-', originalIndex });
  });
  return groups;
}

function sortTable(table: HTMLTableElement, column: number, direction: SortDirection) {
  const multiplier = direction === 'ascending' ? 1 : -1;
  for (const body of Array.from(table.tBodies)) {
    const groups = collectGroups(body, column);
    const sorted = groups.filter((group) => !group.empty).sort((left, right) => (
      compareValues(left.value, right.value) * multiplier || left.originalIndex - right.originalIndex
    ));
    sorted.push(...groups.filter((group) => group.empty));

    const currentOrder = groups.flatMap((group) => group.rows);
    const nextOrder = sorted.flatMap((group) => group.rows);
    if (currentOrder.every((row, index) => row === nextOrder[index])) continue;
    for (const row of nextOrder) body.appendChild(row);
  }
}

function setupTableHeaders(root: ParentNode) {
  root.querySelectorAll<HTMLTableElement>('table').forEach((table) => {
    if (table.tBodies.length === 0) return;
    table.querySelectorAll<HTMLTableCellElement>('thead th').forEach((header) => {
      if (header.querySelector('button') || header.classList.contains('changes-th-sort') || header.colSpan > 1 || !cleanValue(header.innerText ?? header.textContent ?? '')) return;
      header.classList.add('auto-sortable-header');
      header.tabIndex = 0;
      header.title = `Sort by ${cleanValue(header.innerText ?? header.textContent ?? '')}`;
      header.dataset.autoSort = 'true';
    });
  });
}

function sortHeader(header: HTMLTableCellElement) {
  const table = header.closest('table');
  if (!table || header.dataset.autoSort !== 'true') return;
  const column = header.cellIndex;
  const currentColumn = Number(table.dataset.autoSortColumn);
  const currentDirection = header.dataset.sortDirection as SortDirection | undefined;
  const direction: SortDirection = currentColumn === column && currentDirection === 'ascending' ? 'descending' : 'ascending';

  table.querySelectorAll<HTMLTableCellElement>('thead th[data-auto-sort="true"]').forEach((candidate) => {
    delete candidate.dataset.sortDirection;
    candidate.removeAttribute('aria-sort');
  });
  header.dataset.sortDirection = direction;
  header.setAttribute('aria-sort', direction);
  header.title = `Sorted ${direction}; activate to reverse`;
  table.dataset.autoSortColumn = String(column);
  table.dataset.autoSortDirection = direction;
  sortTable(table, column, direction);
}

/** Adds sortable, keyboard-accessible column headers to every table in the app. */
export default function SortableTables() {
  useEffect(() => {
    setupTableHeaders(document);

    const onClick = (event: MouseEvent) => {
      const target = event.target;
      if (!(target instanceof Element)) return;
      const header = target.closest<HTMLTableCellElement>('th[data-auto-sort="true"]');
      if (header) sortHeader(header);
    };

    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key !== 'Enter' && event.key !== ' ') return;
      const target = event.target;
      if (!(target instanceof Element)) return;
      const header = target.closest<HTMLTableCellElement>('th[data-auto-sort="true"]');
      if (!header) return;
      event.preventDefault();
      sortHeader(header);
    };

    const observer = new MutationObserver((records) => {
      setupTableHeaders(document);
      const changedTables = new Set<HTMLTableElement>();
      records.forEach((record) => {
        const target = record.target instanceof Element ? record.target : record.target.parentElement;
        const table = target?.closest('table');
        if (table instanceof HTMLTableElement) changedTables.add(table);
      });
      changedTables.forEach((table) => {
        const column = Number(table.dataset.autoSortColumn);
        const direction = table.dataset.autoSortDirection as SortDirection | undefined;
        if (Number.isInteger(column) && direction) sortTable(table, column, direction);
      });
    });

    document.addEventListener('click', onClick);
    document.addEventListener('keydown', onKeyDown);
    observer.observe(document.body, { childList: true, subtree: true });
    return () => {
      document.removeEventListener('click', onClick);
      document.removeEventListener('keydown', onKeyDown);
      observer.disconnect();
    };
  }, []);

  return null;
}
