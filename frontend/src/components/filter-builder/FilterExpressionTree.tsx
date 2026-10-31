"use client";

import { useMemo } from 'react';
import type { FilterDefinition, FilterNode, FilterCondition, FilterGroup, FilterEvaluationResult } from '@/lib/filter-types';
import { isCondition, newCondition } from '@/lib/filter-types';

type Props = {
  definitions: FilterDefinition[];
  root: FilterGroup;
  selectedPath: number[] | null;
  onAdd: (condition: FilterCondition) => void;
  onSelect: (path: number[]) => void;
};

export default function FilterExpressionTree({ definitions, root, selectedPath, onAdd, onSelect }: Props) {
  const definitionMap = useMemo(() => new Map(definitions.map((item) => [item.filter_id, item])), [definitions]);

  const renderNode = (node: FilterNode, path: number[], depth: number) => {
    if (isCondition(node)) {
      const definition = definitionMap.get(node.filter_id);
      const selected = JSON.stringify(selectedPath) === JSON.stringify(path);
      return (
        <button
          key={path.join('-')}
          type="button"
          onClick={() => onSelect(path)}
          className="card"
          style={{ display: 'block', width: '100%', textAlign: 'left', marginBottom: 8, borderColor: selected ? 'var(--accent)' : undefined, cursor: 'pointer' }}
        >
          <div style={{ display: 'flex', justifyContent: 'space-between', gap: 12 }}>
            <strong>{definition?.name || node.filter_id}</strong>
            <span className="badge">{node.timeframe}</span>
          </div>
          <div style={{ color: 'var(--text-secondary)', fontSize: 12, marginTop: 6 }}>
            {node.required ? 'Required gate' : 'Optional score'} · {Object.entries(node.params).map(([key, value]) => `${key}=${String(value)}`).join(' · ')}
          </div>
        </button>
      );
    }
    return (
      <div key={path.join('-') || 'root'} className="card" style={{ marginBottom: 10, marginLeft: depth * 8 }}>
        <div style={{ display: 'flex', justifyContent: 'space-between', marginBottom: 10 }}>
          <strong>{node.operator}</strong>
          <span style={{ color: 'var(--text-tertiary)', fontSize: 12 }}>{node.children.length} conditions</span>
        </div>
        {node.children.length === 0 ? (
          <div style={{ color: 'var(--text-tertiary)', fontSize: 13 }}>Add a filter from the palette.</div>
        ) : node.children.map((child, index) => renderNode(child, [...path, index], depth + 1))}
      </div>
    );
  };

  return <div>{renderNode(root, [], 0)}
    <div style={{ display: 'flex', flexWrap: 'wrap', gap: 6 }}>
      {definitions.slice(0, 8).map((definition) => (
        <button key={definition.filter_id} type="button" className="btn btn-secondary" onClick={() => onAdd(newCondition(definition))}>
          + {definition.name}
        </button>
      ))}
    </div>
  </div>;
}

export function EvaluationSummary({ result }: { result: FilterEvaluationResult }) {
  const color = result.status === 'pass' ? 'var(--success)' : result.status === 'fail' ? 'var(--danger)' : 'var(--warning)';
  return <div style={{ borderLeft: `3px solid ${color}`, paddingLeft: 10 }}><strong style={{ color }}>{result.status.toUpperCase()}</strong><div style={{ color: 'var(--text-secondary)', fontSize: 12 }}>{result.reason}</div></div>;
}
