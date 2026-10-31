'use client';

import Link from 'next/link';
import { useEffect, useMemo, useState } from 'react';
import FilterExpressionTree from '@/components/filter-builder/FilterExpressionTree';
import {
  createFilterProfile,
  getFilterCatalog,
  getFilterProfiles,
  updateFilterProfileDraft,
  validateFilterExpression,
} from '@/lib/filter-api';
import type {
  FilterCondition,
  FilterDefinition,
  FilterExpression,
  FilterGroup,
  FilterMode,
  FilterNode,
  FilterProfile,
  Timeframe,
} from '@/lib/filter-types';
import { emptyExpression, isCondition, newCondition } from '@/lib/filter-types';
import { usePersistentState } from '@/lib/persistence';

type CatalogTab = 'available' | 'all' | 'planned';

function nodeAtPath(root: FilterGroup, path: number[]): FilterNode | null {
  let current: FilterNode = root;
  for (const index of path) {
    if (current.type !== 'group' || !current.children[index]) return null;
    current = current.children[index];
  }
  return current;
}

function updateNode(root: FilterGroup, path: number[], update: (node: FilterNode) => FilterNode | null): FilterGroup {
  if (path.length === 0) return update(root) as FilterGroup;
  const [index, ...rest] = path;
  return {
    ...root,
    children: root.children.flatMap((child, childIndex) => {
      if (childIndex !== index) return [child];
      const next = rest.length ? updateNode(child as FilterGroup, rest, update) : update(child);
      return next ? [next] : [];
    }),
  };
}

function asExpression(root: FilterGroup): FilterExpression {
  return { ...emptyExpression(), operator: root.operator, children: root.children };
}

const inputStyle = { marginTop: 6 };

export default function ScreenerPage() {
  const [catalog, setCatalog] = useState<FilterDefinition[]>([]);
  const [catalogCounts, setCatalogCounts] = useState({ available: 0, planned: 0 });
  const [profiles, setProfiles] = useState<FilterProfile[]>([]);
  const [root, setRoot] = usePersistentState<FilterGroup>('workspace.screener.root', { type: 'group', operator: 'ALL', children: [] });
  const [selectedPath, setSelectedPath] = usePersistentState<number[] | null>('workspace.screener.selectedPath', null);
  const [profileId, setProfileId] = usePersistentState<number | null>('workspace.screener.profileId', null);
  const [versionId, setVersionId] = usePersistentState<number | undefined>('workspace.screener.versionId', undefined);
  const [name, setName] = usePersistentState('workspace.screener.name', '');
  const [description, setDescription] = usePersistentState('workspace.screener.description', '');
  const [mode, setMode] = usePersistentState<FilterMode>('workspace.screener.mode', 'screen');
  const [catalogTab, setCatalogTab] = usePersistentState<CatalogTab>('workspace.screener.catalogTab', 'all');
  const [catalogSearch, setCatalogSearch] = usePersistentState('workspace.screener.catalogSearch', '');
  const [category, setCategory] = usePersistentState('workspace.screener.category', 'all');
  const [requirements, setRequirements] = usePersistentState<Record<string, { minimum_candles: number; warmup: number; required_fields: string[]; requires_live: boolean }>>('workspace.screener.requirements', {});
  const [message, setMessage] = usePersistentState('workspace.screener.message', '');
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    Promise.all([getFilterCatalog(), getFilterProfiles()])
      .then(([catalogPayload, savedProfiles]) => {
        setCatalog(catalogPayload.filters);
        setCatalogCounts({ available: catalogPayload.available_count, planned: catalogPayload.planned_count });
        setProfiles(savedProfiles);
      })
      .catch((error: Error) => setMessage(error.message))
      .finally(() => setLoading(false));
  }, []);

  const availableCatalog = useMemo(() => catalog.filter((item) => item.availability === 'available'), [catalog]);
  const categories = useMemo(() => ['all', ...Array.from(new Set(catalog.map((item) => item.category)))], [catalog]);
  const visibleCatalog = useMemo(() => catalog.filter((item) => {
    const matchesTab = catalogTab === 'all' || item.availability === catalogTab;
    const matchesSearch = `${item.name} ${item.description} ${item.filter_id}`.toLowerCase().includes(catalogSearch.toLowerCase());
    const matchesCategory = category === 'all' || item.category === category;
    return matchesTab && matchesSearch && matchesCategory;
  }), [catalog, catalogSearch, catalogTab, category]);
  const selected = useMemo(() => selectedPath ? nodeAtPath(root, selectedPath) : null, [root, selectedPath]);
  const selectedDefinition = selected && isCondition(selected) ? catalog.find((item) => item.filter_id === selected.filter_id) : null;

  const validate = async () => {
    setBusy(true);
    try {
      const result = await validateFilterExpression(mode, asExpression(root));
      setRequirements(result.requirements as typeof requirements);
      setMessage(result.valid ? 'Rule is valid. Save the draft, then use the separate Backtest page when you want historical results.' : result.errors.join(' '));
    } catch (error) {
      setMessage((error as Error).message);
    } finally {
      setBusy(false);
    }
  };

  const save = async () => {
    setBusy(true);
    try {
      const expression = asExpression(root);
      if (profileId) {
        const result = await updateFilterProfileDraft(profileId, { mode, expression, expected_version_id: versionId });
        setVersionId(result.version.id);
        setRequirements(result.requirements as typeof requirements);
        setMessage(`Draft version ${result.version.version_number} saved. It is not live.`);
      } else {
        const result = await createFilterProfile({ name: name || 'Untitled profile', description, mode, expression });
        setProfileId(result.profile.id);
        setVersionId(result.profile.latest_version?.id);
        setRequirements(result.requirements as typeof requirements);
        setProfiles(await getFilterProfiles());
        setMessage('Draft saved. Open Backtest when you want to test this saved rule historically.');
      }
    } catch (error) {
      setMessage((error as Error).message);
    } finally {
      setBusy(false);
    }
  };

  const addCondition = (definition: FilterDefinition) => {
    if (definition.availability !== 'available') {
      setMessage(`${definition.name} is catalogued but not implemented yet; it cannot be added to a runnable profile.`);
      return;
    }
    addConditionNode(newCondition(definition));
  };

  const addConditionNode = (condition: FilterCondition) => {
    setRoot((current) => ({ ...current, children: [...current.children, condition] }));
    setSelectedPath([root.children.length]);
  };

  const updateSelected = (next: FilterNode | null) => {
    if (!selectedPath) return;
    setRoot((current) => updateNode(current, selectedPath, () => next));
  };

  const selectProfile = (profile: FilterProfile) => {
    const version = profile.latest_version || profile.versions?.[0];
    if (!version) return;
    setProfileId(profile.id);
    setName(profile.name);
    setDescription(profile.description);
    setMode(profile.mode);
    setVersionId(version.id);
    setRoot({ type: 'group', operator: version.expression_json.operator, children: version.expression_json.children });
    setRequirements(version.requirements_json);
    setSelectedPath(null);
    setMessage(`Loaded ${profile.name} v${version.version_number}.`);
  };

  if (loading) return <div><h1 className="page-title">Screener</h1><div className="skeleton" style={{ width: '100%', height: 420 }} /></div>;

  return (
    <div className="screener-page">
      <div className="screener-hero" style={{ marginBottom: 16 }}>
        <div className="screener-eyebrow"><span className="screener-eyebrow-dot" /> Filter workspace</div>
        <h1 className="page-title screener-title" style={{ marginBottom: 4 }}>Screener</h1>
        <p style={{ color: 'var(--text-secondary)', margin: 0 }}>Build and save reusable filter profiles here. Historical testing is intentionally on the separate Backtest page.</p>
      </div>

      <div className="card screener-intro" style={{ marginBottom: 16, display: 'flex', justifyContent: 'space-between', alignItems: 'center', gap: 16, flexWrap: 'wrap' }}>
        <div><strong>One job on this page</strong><p style={{ color: 'var(--text-secondary)', fontSize: 13, margin: '6px 0 0' }}>Choose runnable filters, compose the rule, validate it, and save a draft. Planned filters are shown for reference but cannot be executed.</p></div>
        <Link href="/backtest" className="btn btn-primary">Open Backtest</Link>
      </div>

      <div className="screener-actions" style={{ display: 'flex', gap: 8, marginBottom: 16, flexWrap: 'wrap' }}>
        <button className="btn btn-secondary" onClick={validate} disabled={busy}>Validate rule</button>
        <button className="btn btn-primary" onClick={save} disabled={busy || root.children.length === 0}>Save draft</button>
        <div style={{ flex: 1 }} />
        <span className="badge">{catalog.length} catalogue entries</span>
        <span className="badge">{catalogCounts.available} runnable</span>
        <span className="badge">{catalogCounts.planned} planned</span>
      </div>

      {message && <div className="card" style={{ marginBottom: 16, color: message.toLowerCase().includes('error') ? 'var(--danger)' : 'var(--text-secondary)' }}>{message}</div>}

      <div className="card screener-profile-card" style={{ marginBottom: 16 }}>
        <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 12 }}>
          <label className="form-label">Profile name<input className="input" style={inputStyle} placeholder="e.g. Trend breakout" value={name} onChange={(event) => setName(event.target.value)} /></label>
          <label className="form-label">Profile mode<select className="input" style={inputStyle} value={mode} onChange={(event) => setMode(event.target.value as FilterMode)}><option value="screen">Screen</option><option value="signal">Signal / paper</option><option value="backtest">Backtest-compatible</option></select></label>
        </div>
        <label className="form-label" style={{ marginTop: 10 }}>Description<textarea className="input" style={{ ...inputStyle, minHeight: 48 }} placeholder="What market setup does this profile identify?" value={description} onChange={(event) => setDescription(event.target.value)} /></label>
      </div>

      <div className="card screener-builder" style={{ display: 'grid', gridTemplateColumns: 'minmax(250px, .9fr) minmax(360px, 1.5fr) minmax(250px, .9fr)', gap: 16, alignItems: 'start' }}>
        <section className="screener-panel screener-catalog-panel">
          <h2 className="section-title">Filter catalogue</h2>
          <p style={{ color: 'var(--text-secondary)', fontSize: 12, marginTop: -4 }}>All {catalog.length} registered definitions are listed below.</p>
          <input className="input" placeholder="Search filters" value={catalogSearch} onChange={(event) => setCatalogSearch(event.target.value)} />
          <div style={{ display: 'flex', flexWrap: 'wrap', gap: 6, margin: '10px 0' }}>{(['all', 'available', 'planned'] as CatalogTab[]).map((item) => <button key={item} className={`btn ${catalogTab === item ? 'btn-primary' : 'btn-secondary'}`} style={{ fontSize: 12 }} onClick={() => setCatalogTab(item)}>{item === 'available' ? 'Runnable' : item === 'all' ? 'All filters' : 'Roadmap'}</button>)}</div>
          <select className="input" value={category} onChange={(event) => setCategory(event.target.value)}>{categories.map((item) => <option key={item} value={item}>{item === 'all' ? 'All categories' : item}</option>)}</select>
          <p style={{ color: 'var(--text-tertiary)', fontSize: 12 }}>{visibleCatalog.length} matching entries</p>
          <div style={{ display: 'grid', gap: 8, maxHeight: 520, overflowY: 'auto', marginTop: 10 }}>
            {visibleCatalog.map((definition) => <button key={definition.filter_id} className="btn btn-secondary" style={{ textAlign: 'left', opacity: definition.availability === 'planned' ? 0.65 : 1 }} onClick={() => addCondition(definition)}><strong>{definition.name}</strong><small style={{ display: 'block', color: 'var(--text-tertiary)' }}>Tier {definition.tier} · {definition.category} · {definition.availability === 'planned' ? 'roadmap — not runnable' : 'runnable'}</small></button>)}
            {visibleCatalog.length === 0 && <p style={{ color: 'var(--text-tertiary)', fontSize: 13 }}>No filters match this search.</p>}
          </div>
        </section>

        <section className="screener-panel screener-rule-panel">
          <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 10 }}><div><h2 className="section-title" style={{ marginBottom: 2 }}>Rule tree</h2><span style={{ color: 'var(--text-tertiary)', fontSize: 12 }}>Conditions in this group follow the selected operator.</span></div><select value={root.operator} onChange={(event) => setRoot({ ...root, operator: event.target.value as FilterGroup['operator'] })} className="input" style={{ width: 150 }}><option>ALL</option><option>ANY</option><option>AT_LEAST_N</option><option>WEIGHTED_SCORE</option><option>NOT</option></select></div>
          <FilterExpressionTree definitions={availableCatalog} root={root} selectedPath={selectedPath} onAdd={addConditionNode} onSelect={setSelectedPath} />
          <div style={{ marginTop: 16, display: 'flex', gap: 8 }}><button className="btn btn-secondary" onClick={() => availableCatalog[0] && setRoot({ ...root, children: [...root.children, { type: 'group', operator: 'ANY', children: [{ ...newCondition(availableCatalog[0]), required: true }] }] })} disabled={availableCatalog.length === 0}>+ Nested group</button>{selectedPath && <button className="btn btn-danger" onClick={() => { updateSelected(null); setSelectedPath(null); }}>Remove selected</button>}</div>
        </section>

        <section className="screener-panel screener-inspector-panel">
          <h2 className="section-title">Selected condition</h2>
          {selected && isCondition(selected) && selectedDefinition ? <div style={{ display: 'grid', gap: 10 }}>
            <strong>{selectedDefinition.name}</strong><p style={{ color: 'var(--text-secondary)', fontSize: 12, margin: 0 }}>{selectedDefinition.description}</p>
            <label className="form-label">Timeframe<select className="input" style={inputStyle} value={selected.timeframe} onChange={(event) => updateSelected({ ...selected, timeframe: event.target.value as Timeframe })}>{selectedDefinition.supported_timeframes.map((item) => <option key={item}>{item}</option>)}</select></label>
            {selectedDefinition.parameter_schema.map((parameter) => <label key={parameter.name} className="form-label">{parameter.label}{parameter.type === 'enum' ? <select className="input" style={inputStyle} value={String(selected.params[parameter.name] ?? parameter.default ?? '')} onChange={(event) => updateSelected({ ...selected, params: { ...selected.params, [parameter.name]: event.target.value } })}>{parameter.enum.map((item) => <option key={item}>{item}</option>)}</select> : <input className="input" style={inputStyle} type={parameter.type === 'number' || parameter.type === 'integer' ? 'number' : 'text'} value={String(selected.params[parameter.name] ?? parameter.default ?? '')} onChange={(event) => updateSelected({ ...selected, params: { ...selected.params, [parameter.name]: parameter.type === 'number' ? Number(event.target.value) : parameter.type === 'integer' ? Number.parseInt(event.target.value, 10) : event.target.value } })} />}</label>)}
            <label className="form-label" style={{ display: 'flex', alignItems: 'center', gap: 8 }}><input type="checkbox" checked={selected.required} onChange={(event) => updateSelected({ ...selected, required: event.target.checked })} /> Required hard gate</label>
          </div> : <p style={{ color: 'var(--text-tertiary)', fontSize: 13 }}>Choose a condition in the rule tree to edit it.</p>}
          <div style={{ borderTop: '1px solid var(--border)', marginTop: 18, paddingTop: 14 }}><h2 className="section-title">Saved profiles</h2>{profiles.length === 0 ? <p style={{ color: 'var(--text-tertiary)', fontSize: 13 }}>No saved profiles yet.</p> : profiles.map((profile) => <button key={profile.id} className="btn btn-secondary" style={{ width: '100%', textAlign: 'left', marginBottom: 6 }} onClick={() => selectProfile(profile)}><strong>{profile.name}</strong><small style={{ display: 'block', color: 'var(--text-tertiary)' }}>{profile.mode} · {profile.is_active ? 'active' : 'archived'}</small></button>)}</div>
        </section>
      </div>

      {Object.keys(requirements).length > 0 && <div className="card" style={{ marginTop: 16 }}><h2 className="section-title">Data needed by this rule</h2>{Object.entries(requirements).map(([interval, requirement]) => <div key={interval} style={{ padding: '8px 0', borderBottom: '1px solid var(--border)' }}><strong>{interval}</strong><span style={{ marginLeft: 12, color: 'var(--text-secondary)' }}>{requirement.minimum_candles} candles + {requirement.warmup} warmup · {requirement.required_fields.join(', ')}</span></div>)}</div>}
    </div>
  );
}
