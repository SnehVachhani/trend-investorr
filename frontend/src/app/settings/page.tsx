'use client';

import { useEffect, useState } from 'react';
import { getSystemConfig } from '@/lib/api';
import type { AppConfig } from '@/lib/types';

export default function SettingsPage() {
  const [config, setConfig] = useState<AppConfig[]>([]);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    getSystemConfig().then(setConfig).catch(()=>{}).finally(()=>setLoading(false));
  }, []);

  if (loading) return <div><h1 className="page-title">Settings</h1><div className="skeleton" style={{width:'100%',height:400}}/></div>;

  const categories = [...new Set(config.map(c=>c.category))];

  return (
    <div>
      <h1 className="page-title">Settings</h1>
      {categories.map(cat=>(
        <div key={cat} className="section">
          <div className="section-header">
            <h2 className="section-title" style={{textTransform:'capitalize'}}>{cat}</h2>
          </div>
          <div className="card">
            <table className="data-table">
              <thead><tr><th>Key</th><th>Value</th><th>Type</th><th>Description</th></tr></thead>
              <tbody>
                {config.filter(c=>c.category===cat).map(c=>(
                  <tr key={c.key} style={{cursor:'default'}}>
                    <td style={{fontWeight:600,fontSize:12,fontFamily:"'JetBrains Mono',monospace"}}>{c.key}</td>
                    <td style={{fontFamily:"'JetBrains Mono',monospace",color:'var(--accent-secondary)'}}>{c.value}</td>
                    <td style={{fontSize:11,color:'var(--text-tertiary)'}}>{c.data_type}</td>
                    <td style={{fontSize:12,color:'var(--text-secondary)',maxWidth:300}}>{c.description}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      ))}
    </div>
  );
}
