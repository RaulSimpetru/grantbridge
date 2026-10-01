import { useEffect, useRef, useState } from 'react';
import { archiveDocumentUrl, request } from './api';
import ArchiveMining from './ArchiveMining';
import type { Call, ConceptJob } from './types';
import './past-applications.css';

interface Application {
  id: string;
  family: string;
  title: string;
  funder: string;
  programme: string;
  year: string;
  applicant: string;
  summary: string;
  source_key: string;
  ownership: string;
  submission: string;
  outcome: string;
  note: string;
  preferred: Record<string, string>;
  reviewed_at: string | null;
}
interface Document {
  id: string;
  key: string;
  application_id: string;
  name: string;
  group: string;
  status: string;
  sha256: string;
  bytes: number;
  warnings: string[];
  source_url: string;
  exact_duplicate_of: string | null;
  same_extracted_text_as: string | null;
  identity_warning: string | null;
  has_text: boolean;
  has_ocr: boolean;
}
interface Library {
  applications: Application[];
  documents: Document[];
  unique_files: number;
  imported_at: string | null;
  limitations: string[];
}
const ownership: Record<string, string> = {
  unclassified: 'Unclassified',
  mine: 'My application',
  lab: 'Lab application',
  reference: 'Reference example',
  template: 'Template',
};
const extraction = (d: Document) =>
  d.has_ocr
    ? 'OCR · unverified'
    : d.has_text
      ? 'Native text'
      : d.status === 'empty_text_needs_review'
        ? 'OCR pending · original review needed'
        : 'Extraction failed · original review needed';

function TextPreview({ doc }: { doc: Document }) {
  const [kind, setKind] = useState<'text' | 'ocr'>(doc.has_text ? 'text' : 'ocr');
  const [text, setText] = useState<string | null>(null);
  const [error, setError] = useState('');
  const [retry, setRetry] = useState(0);
  useEffect(() => {
    let active = true;
    request<{ text: string }>(`/past-application-documents/${doc.id}/${kind}`)
      .then((value) => {
        if (active) {
          setText(value.text);
          setError('');
        }
      })
      .catch((err) => {
        if (active) setError((err as Error).message);
      });
    return () => {
      active = false;
    };
  }, [doc.id, kind, retry]);
  return (
    <section className="archive-preview" aria-label={`Text of ${doc.name}`}>
      <h3>{doc.name}</h3>
      <div className="archive-actions">
        <a href={archiveDocumentUrl(doc.id)}>Download original ↗</a>
        {doc.has_text && doc.has_ocr && (
          <select
            aria-label="Text extraction"
            value={kind}
            onChange={(e) => {
              setText(null);
              setKind(e.target.value as typeof kind);
            }}
          >
            <option value="text">Native text</option>
            <option value="ocr">Unverified OCR</option>
          </select>
        )}
      </div>
      {kind === 'ocr' && (
        <p className="archive-warning">
          Unverified OCR. Check the original before relying on wording.
        </p>
      )}
      <p className="archive-muted">
        Text comparison omits page layout, figures and some annotations. File dates do not establish
        version order.
      </p>
      {error ? (
        <p role="alert">
          {error}{' '}
          <button
            onClick={() => {
              setError('');
              setRetry((n) => n + 1);
            }}
          >
            Retry preview
          </button>
        </p>
      ) : (
        <pre>
          {text === null
            ? 'Loading text…'
            : text || 'No readable text. Download the original for review.'}
        </pre>
      )}
    </section>
  );
}

function ApplicationDetail({
  application,
  docs,
  allDocs,
  onSaved,
  onBack,
}: {
  application: Application;
  docs: Document[];
  allDocs: Document[];
  onSaved: (a: Application) => void;
  onBack: () => void;
}) {
  const [tab, setTab] = useState('Overview');
  const [draft, setDraft] = useState(application);
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState('');
  const [error, setError] = useState('');
  const [first, setFirst] = useState('');
  const [second, setSecond] = useState('');
  const reader = useRef<HTMLElement>(null);
  useEffect(() => {
    if (first) reader.current?.scrollIntoView({ block: 'start' });
  }, [first, tab]);
  const firstDoc = docs.find((d) => d.id === first);
  const secondDoc = docs.find((d) => d.id === second);
  const source = docs.find((d) => d.key === application.source_key);
  const groups = [...new Set(docs.map((d) => d.group))];
  const save = async (body: object) => {
    setBusy(true);
    setMessage('');
    setError('');
    try {
      const updated = await request<Application>(`/past-applications/${application.id}`, {
        method: 'PATCH',
        body: JSON.stringify(body),
      });
      onSaved(updated);
      setDraft(updated);
      setMessage('Review saved. Your researcher profile is unchanged.');
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setBusy(false);
    }
  };
  return (
    <main className="archive-detail">
      <button className="archive-back btn-ghost" onClick={onBack}>
        ← All applications
      </button>
      <header>
        <p className="archive-kicker">
          {application.family} / {application.funder}
        </p>
        <h2>{application.title}</h2>
        <p>
          {application.programme} · {application.year}
        </p>
        <p className="archive-muted">Applicant: {application.applicant}</p>
      </header>
      <nav className="archive-tabs" aria-label="Application sections">
        {['Overview', 'Documents', 'Evidence review'].map((t) => (
          <button aria-current={tab === t ? 'page' : undefined} key={t} onClick={() => setTab(t)}>
            {t}
            {t === 'Documents' ? ` (${docs.length})` : ''}
          </button>
        ))}
      </nav>
      <div className="archive-detail-body">
        {message && (
          <p className="archive-success" role="status">
            {message}
          </p>
        )}
        {error && (
          <p className="archive-warning" role="alert">
            {error}
          </p>
        )}
        {tab === 'Overview' && (
          <>
            <h3>What this application proposed</h3>
            <p>{application.summary}</p>
            {source && (
              <button
                className="btn-ghost"
                onClick={() => {
                  setFirst(source.id);
                  setTab('Documents');
                }}
              >
                Read source: {source.name} ↗
              </button>
            )}
            <p className="archive-warning">
              Initial grouping is provisional. “Final” filenames do not establish submission or
              funding.
            </p>
            {docs
              .filter((d) => d.identity_warning)
              .map((d) => (
                <p className="archive-warning" key={d.id}>
                  <strong>{d.name}</strong>
                  <br />
                  {d.identity_warning}
                </p>
              ))}
            <form
              className="archive-review"
              onSubmit={(e) => {
                e.preventDefault();
                void save({
                  ownership: draft.ownership,
                  submission: draft.submission,
                  outcome: draft.outcome,
                  note: draft.note,
                });
              }}
            >
              <h3>Your review</h3>
              <label>
                Relationship to you
                <select
                  value={draft.ownership}
                  onChange={(e) => setDraft({ ...draft, ownership: e.target.value })}
                >
                  {Object.entries(ownership).map(([value, label]) => (
                    <option key={value} value={value}>
                      {label}
                    </option>
                  ))}
                </select>
              </label>
              <div className="archive-review-row">
                <label>
                  Submission status
                  <select
                    value={draft.submission}
                    onChange={(e) => setDraft({ ...draft, submission: e.target.value })}
                  >
                    {['unknown', 'draft', 'submitted'].map((s) => (
                      <option key={s} value={s}>
                        {s[0].toUpperCase() + s.slice(1)}
                      </option>
                    ))}
                  </select>
                </label>
                <label>
                  Funding outcome
                  <select
                    value={draft.outcome}
                    onChange={(e) => setDraft({ ...draft, outcome: e.target.value })}
                  >
                    {['unknown', 'pending', 'funded', 'rejected'].map((s) => (
                      <option key={s} value={s}>
                        {s[0].toUpperCase() + s.slice(1)}
                      </option>
                    ))}
                  </select>
                </label>
              </div>
              <label>
                Review notes / supporting evidence
                <textarea
                  maxLength={4000}
                  value={draft.note}
                  onChange={(e) => setDraft({ ...draft, note: e.target.value })}
                  placeholder="Record your involvement, status evidence or an association that needs correcting."
                />
              </label>
              <div>
                <button className="btn-primary" disabled={busy}>
                  {busy ? 'Saving…' : 'Save review'}
                </button>
              </div>
              {application.reviewed_at && (
                <small>Last saved {new Date(application.reviewed_at).toLocaleString()}</small>
              )}
            </form>
          </>
        )}
        {tab === 'Documents' && (
          <>
            <p className="archive-muted">
              Groups are reading aids, not verified version sequences. Every original is preserved.
            </p>
            {groups.map((group) => (
              <section className="archive-document-group" key={group}>
                <h3>{group}</h3>
                {docs
                  .filter((d) => d.group === group)
                  .map((d) => (
                    <article className="archive-document" key={d.id}>
                      <div>
                        <h4>{d.name}</h4>
                        <p
                          className={
                            d.has_ocr || !d.has_text ? 'archive-warning-inline' : 'archive-muted'
                          }
                        >
                          {extraction(d)} · {(d.bytes / 1024).toFixed(0)} KB
                          {application.preferred[group] === d.id && ' · Preferred version'}
                        </p>
                      </div>
                      {d.identity_warning && (
                        <p className="archive-warning">{d.identity_warning}</p>
                      )}
                      {d.warnings.map((w, i) => (
                        <p className="archive-warning-inline" key={i}>
                          {w}
                        </p>
                      ))}
                      {d.exact_duplicate_of && (
                        <p className="archive-muted">
                          Exact duplicate of{' '}
                          {allDocs.find((v) => v.key === d.exact_duplicate_of)?.name ??
                            d.exact_duplicate_of}
                          . Both originals are retained.
                        </p>
                      )}
                      {d.same_extracted_text_as && (
                        <p className="archive-muted">
                          Matching extracted text:{' '}
                          {allDocs.find((v) => v.key === d.same_extracted_text_as)?.name ??
                            d.same_extracted_text_as}
                          . Other file contents may differ.
                        </p>
                      )}
                      <div className="archive-actions">
                        <button
                          disabled={!d.has_text && !d.has_ocr}
                          onClick={() => {
                            setFirst(d.id);
                            setSecond('');
                          }}
                        >
                          Read text
                        </button>
                        <a href={archiveDocumentUrl(d.id)}>Original ↗</a>
                        <a href={d.source_url} target="_blank" rel="noreferrer">
                          Source ↗
                        </a>
                        <button
                          disabled={busy || application.preferred[group] === d.id}
                          onClick={() =>
                            void save({ preferred: { ...application.preferred, [group]: d.id } })
                          }
                        >
                          Prefer this version
                        </button>
                      </div>
                    </article>
                  ))}
              </section>
            ))}
            <section className="archive-reader" ref={reader}>
              <h3>Read & compare</h3>
              <div className="archive-review-row">
                <label>
                  First document
                  <select value={first} onChange={(e) => setFirst(e.target.value)}>
                    <option value="">Choose document</option>
                    {docs
                      .filter((d) => d.has_text || d.has_ocr)
                      .map((d) => (
                        <option key={d.id} value={d.id}>
                          {d.name}
                        </option>
                      ))}
                  </select>
                </label>
                <label>
                  Compare with
                  <select value={second} onChange={(e) => setSecond(e.target.value)}>
                    <option value="">No comparison</option>
                    {docs
                      .filter((d) => (d.has_text || d.has_ocr) && d.id !== first)
                      .map((d) => (
                        <option key={d.id} value={d.id}>
                          {d.name}
                        </option>
                      ))}
                  </select>
                </label>
              </div>
              <div className={`archive-previews ${firstDoc && secondDoc ? 'is-comparing' : ''}`}>
                {firstDoc && <TextPreview key={`first-${firstDoc.id}`} doc={firstDoc} />}
                {secondDoc && <TextPreview key={`second-${secondDoc.id}`} doc={secondDoc} />}
              </div>
            </section>
          </>
        )}
        {tab === 'Evidence review' && (
          <section className="archive-evidence">
            <h3>No automatic profile additions</h3>
            <p>Nothing you do here is added to your profile.</p>
            <p>
              Use Explore ideas to find passages and choose evidence for a new concept run. Profile
              suggestions with per-claim approval remain a later step.
            </p>
            <p className="archive-warning">
              Someone else’s CV, a planned purchase or a maybe from a collaborator is not something
              you have.
            </p>
            <button onClick={() => setTab('Overview')}>Review application labels</button>
          </section>
        )}
      </div>
    </main>
  );
}

export default function PastApplications({
  calls,
  onStarted,
  onConnection,
}: {
  calls: Call[];
  onStarted: (job: ConceptJob) => void;
  onConnection: () => void;
}) {
  const [mode, setMode] = useState<'browse' | 'mine'>('browse');
  const [library, setLibrary] = useState<Library | null>(null);
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);
  const [selected, setSelected] = useState('');
  const [query, setQuery] = useState('');
  const [filter, setFilter] = useState('all');
  const [mobileDetail, setMobileDetail] = useState(false);
  const load = async () => {
    try {
      setLibrary(await request<Library>('/past-applications'));
      setError('');
    } catch (err) {
      setError((err as Error).message);
    }
  };
  useEffect(() => {
    // The loader updates state only after the library request resolves.
    // oxlint-disable-next-line react/set-state-in-effect
    void load();
  }, []);
  const importArchive = async () => {
    setBusy(true);
    setError('');
    try {
      setLibrary(await request<Library>('/past-applications/import', { method: 'POST' }));
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setBusy(false);
    }
  };
  const filtered =
    library?.applications.filter(
      (a) =>
        (filter === 'all' || a.ownership === filter) &&
        `${a.title} ${a.family} ${a.programme} ${a.applicant} ${library.documents
          .filter((d) => d.application_id === a.id)
          .map((d) => d.name)
          .join(' ')}`
          .toLowerCase()
          .includes(query.toLowerCase()),
    ) ?? [];
  const active = filtered.find((a) => a.id === selected) ?? filtered[0];
  return (
    <section className="archive-page">
      <div className="workspace-heading">
        <div>
          <p className="archive-kicker">PRIVATE RESEARCH LIBRARY</p>
          <h1>Past applications</h1>
          <p>
            {library?.imported_at
              ? `${library.applications.length} application records · ${library.documents.length} documents · ${library.unique_files} unique files`
              : 'Preserved sources, separate from your confirmed profile.'}
          </p>
        </div>
        {library?.imported_at && (
          <nav className="archive-mode" aria-label="Archive workflow">
            <button
              aria-current={mode === 'browse' ? 'page' : undefined}
              onClick={() => setMode('browse')}
            >
              Browse documents
            </button>
            <button
              aria-current={mode === 'mine' ? 'page' : undefined}
              onClick={() => setMode('mine')}
            >
              Explore ideas
            </button>
          </nav>
        )}
      </div>
      {error && (
        <p className="error-note" role="alert">
          {error} <button onClick={() => void load()}>Retry loading</button>
        </p>
      )}
      {!library && !error && (
        <p className="empty-note" role="status">
          Loading your library…
        </p>
      )}
      {library && !library.imported_at && (
        <div className="archive-empty">
          <h2>Bring your archived applications into view</h2>
          <p>
            Import the prepared local archive. Originals are validated and kept on this computer.
            Importing does not contact Ollama or change your profile.
          </p>
          <button className="btn-primary" disabled={busy} onClick={() => void importArchive()}>
            {busy ? 'Validating and importing…' : 'Import prepared archive'}
          </button>
        </div>
      )}
      {library?.imported_at && mode === 'mine' && (
        <ArchiveMining
          applications={library.applications}
          calls={calls}
          onStarted={onStarted}
          onConnection={onConnection}
        />
      )}
      {library?.imported_at && mode === 'browse' && (
        <div className={`archive-workspace ${mobileDetail ? 'archive-show-detail' : ''}`}>
          <aside className="archive-list">
            <label>
              Find an application or document
              <input
                type="search"
                value={query}
                onChange={(e) => setQuery(e.target.value)}
                placeholder="Title, applicant, programme, filename…"
              />
            </label>
            <label>
              Relationship
              <select value={filter} onChange={(e) => setFilter(e.target.value)}>
                <option value="all">All applications</option>
                {Object.entries(ownership).map(([value, label]) => (
                  <option key={value} value={value}>
                    {label}
                  </option>
                ))}
              </select>
            </label>
            <p className="archive-muted">
              {filtered.length} application records · grouping needs review
            </p>
            {filtered.length === 0 && (
              <p>No matching applications. Try another search or relationship.</p>
            )}
            {filtered.map((a) => {
              const docs = library.documents.filter((d) => d.application_id === a.id);
              return (
                <button
                  className={`archive-card ${active?.id === a.id ? 'selected' : ''}`}
                  aria-current={active?.id === a.id ? 'true' : undefined}
                  key={a.id}
                  onClick={() => {
                    setSelected(a.id);
                    setMobileDetail(true);
                  }}
                >
                  <span className="archive-kicker">
                    {a.funder} · {a.year}
                  </span>
                  <strong>{a.title}</strong>
                  <span>{a.programme}</span>
                  <span>{a.applicant}</span>
                  <span className="archive-card-label">{ownership[a.ownership]}</span>
                  <span>
                    Submission: {a.submission} · Outcome: {a.outcome}
                  </span>
                  <small>
                    {docs.length} documents
                    {docs.some((d) => d.has_ocr || d.identity_warning || d.warnings.length) &&
                      ' · Source review needed'}
                  </small>
                </button>
              );
            })}
            <details className="archive-limitations">
              <summary>Archive coverage</summary>
              {library.limitations.map((l) => (
                <p key={l}>{l}</p>
              ))}
            </details>
          </aside>
          {active ? (
            <ApplicationDetail
              key={active.id}
              application={active}
              docs={library.documents.filter((d) => d.application_id === active.id)}
              allDocs={library.documents}
              onBack={() => setMobileDetail(false)}
              onSaved={(a) =>
                setLibrary({
                  ...library,
                  applications: library.applications.map((old) => (old.id === a.id ? a : old)),
                })
              }
            />
          ) : (
            <main className="archive-detail">
              <button className="archive-back" onClick={() => setMobileDetail(false)}>
                ← All applications
              </button>
              <p>No application selected.</p>
            </main>
          )}
        </div>
      )}
    </section>
  );
}
