import { useEffect, useState } from 'react';
import { importFundingNotice, listFundingSources, refreshFundingSource } from './api';
import type { FundingSource } from './api';
import type { Call } from './types';

export default function FundingSources({ onImported }: { onImported: (call: Call) => void }) {
  const [sources, setSources] = useState<FundingSource[]>([]);
  const [busy, setBusy] = useState('');
  const [message, setMessage] = useState('');
  const [error, setError] = useState('');
  const [query, setQuery] = useState('');
  const load = () =>
    listFundingSources()
      .then(setSources)
      .catch((err) => setError((err as Error).message));
  useEffect(() => {
    void listFundingSources()
      .then(setSources)
      .catch((err) => setError((err as Error).message));
  }, []);
  const refresh = async (id: string) => {
    setBusy(id);
    setMessage('Fetching official notices…');
    setError('');
    try {
      const updated = await refreshFundingSource(id);
      setSources((previous) => previous.map((s) => (s.id === id ? updated : s)));
      setMessage(
        updated.error
          ? 'Refresh failed; retained notices remain available.'
          : `${updated.items.length} notices loaded from ${updated.name}.`,
      );
    } catch (err) {
      setError((err as Error).message);
      setMessage('');
    } finally {
      setBusy('');
    }
  };
  const add = async (source: string, id: string) => {
    setBusy(id);
    setMessage('Fetching the full official text…');
    setError('');
    try {
      const call = await importFundingNotice(source, id);
      setMessage('Added to Opportunities with eligibility and deadlines awaiting review.');
      onImported(call);
    } catch (err) {
      setError((err as Error).message);
      setMessage('');
    } finally {
      setBusy('');
    }
  };
  return (
    <main className="sources-scroll">
      <h1>Funding sources</h1>
      <p>
        German discovery connections alongside the EU watch-list. Notices can include programme
        updates and funding news; read the official source before treating an entry as an open
        opportunity.
      </p>
      <label>
        Search loaded German notices
        <input
          value={query}
          onChange={(e) => setQuery(e.target.value)}
          placeholder="e.g. Medizin, Klima, data…"
        />
      </label>
      <p role="status">{message}</p>
      {error && (
        <p role="alert">
          {error} <button onClick={() => void load()}>Retry loading</button>
        </p>
      )}
      {!sources.length && !error && <p>Loading connections…</p>}
      {sources.map((s) => (
        <section className="source-card" key={s.id}>
          <header>
            <h2>{s.name}</h2>
            <button
              className="btn-primary"
              disabled={Boolean(busy)}
              onClick={() => void refresh(s.id)}
            >
              {busy === s.id ? 'Refreshing…' : 'Refresh official source'}
            </button>
          </header>
          <p>{s.coverage}</p>
          <p>
            <a href={s.url} target="_blank" rel="noreferrer">
              Official source ↗
            </a>{' '}
            ·{' '}
            {s.fetched_at
              ? `Last fetched ${new Date(s.fetched_at).toLocaleString()}`
              : 'Not fetched yet'}
          </p>
          {s.error && <p role="alert">{s.error}</p>}
          {s.items
            .filter((item) => item.title.toLowerCase().includes(query.toLowerCase()))
            .map((item) => (
              <article className="source-notice" key={item.id}>
                <h3>
                  <a href={item.url} target="_blank" rel="noreferrer">
                    {item.title} ↗
                  </a>
                </h3>
                {item.published && <p>Published: {item.published} — not a deadline</p>}
                <button
                  className="btn-sm"
                  disabled={Boolean(busy)}
                  onClick={() => void add(s.id, item.id)}
                >
                  {busy === item.id ? 'Importing…' : 'Import / refresh in Opportunities'}
                </button>
              </article>
            ))}
          {!s.items.length && <p>Refresh this source to load available notices.</p>}
        </section>
      ))}
      <p>
        Coverage is intentionally explicit: DFG and BMFTR health research are connected. Other BMFTR
        areas, federal ministries, Länder, DAAD and foundations are not yet connected.
      </p>
    </main>
  );
}
