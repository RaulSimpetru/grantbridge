import { useEffect, useState } from 'react';
import { archiveDocumentUrl, request } from './api';
import type { Call, ConceptJob, GenerationConnection } from './types';

export interface ArchivePassage {
  source_id: string;
  document_id: string;
  document_name: string;
  application_title: string;
  applicant: string;
  programme: string;
  year: string;
  ownership: string;
  kind: string;
  text: string;
  line_start: number;
  line_end: number;
  warnings: string[];
  score?: number;
  duplicate_sources?: { name: string; document_id: string }[];
}
interface Preview {
  id: string;
  query: string;
  topic_code: string;
  trial: boolean;
  passages: ArchivePassage[];
  connection: GenerationConnection;
}
interface IndexStatus {
  ready: boolean;
  stale: boolean;
  passages: number;
  documents_indexed?: number;
  documents_without_text?: number;
}
interface Application {
  id: string;
  title: string;
  ownership: string;
}

export function ArchiveSource({ passage }: { passage: ArchivePassage }) {
  return (
    <>
      <p className="archive-muted">
        {passage.application_title} · Applicant: {passage.applicant}
        <br />
        {passage.document_name} · {passage.kind === 'ocr' ? 'Unverified OCR' : 'Native text'} lines{' '}
        {passage.line_start}–{passage.line_end}
      </p>
      {passage.warnings.map((w, i) => (
        <p className="archive-warning" key={i}>
          {w}
        </p>
      ))}
      <blockquote className="archive-passage-text">{passage.text}</blockquote>
      <a href={archiveDocumentUrl(passage.document_id)}>Download source original ↗</a>
      {!!passage.duplicate_sources?.length && (
        <details>
          <summary>{passage.duplicate_sources.length} identical passage copies</summary>
          {passage.duplicate_sources.map((d, i) => (
            <p key={i}>
              <a href={archiveDocumentUrl(d.document_id)}>{d.name}</a>
            </p>
          ))}
        </details>
      )}
    </>
  );
}

export default function ArchiveMining({
  applications,
  calls,
  onStarted,
  onConnection,
}: {
  applications: Application[];
  calls: Call[];
  onStarted: (job: ConceptJob) => void;
  onConnection: () => void;
}) {
  const [status, setStatus] = useState<IndexStatus | null>(null);
  const [selectedApps, setSelectedApps] = useState<string[]>([]);
  const [query, setQuery] = useState('');
  const [attachments, setAttachments] = useState(false);
  const [ocr, setOcr] = useState(false);
  const [preferred, setPreferred] = useState(false);
  const [hits, setHits] = useState<ArchivePassage[] | null>(null);
  const [picked, setPicked] = useState<string[]>([]);
  const [call, setCall] = useState('');
  const [trial, setTrial] = useState(true);
  const [preview, setPreview] = useState<Preview | null>(null);
  const [busy, setBusy] = useState('');
  const [error, setError] = useState('');
  const [approved, setApproved] = useState(false);
  const loadStatus = async () => {
    try {
      setStatus(await request<IndexStatus>('/archive-rag/status'));
    } catch (err) {
      setError((err as Error).message);
    }
  };
  useEffect(() => {
    // The loader updates state only after the status request resolves.
    // oxlint-disable-next-line react/set-state-in-effect
    void loadStatus();
  }, []);
  const invalidate = () => {
    setPreview(null);
    setApproved(false);
  };
  const resetSearch = () => {
    invalidate();
    setHits(null);
    setPicked([]);
  };
  const run = async (action: 'index' | 'search' | 'preview' | 'generate') => {
    setBusy(action);
    setError('');
    try {
      if (action === 'index') {
        setStatus(await request<IndexStatus>('/archive-rag/index', { method: 'POST' }));
        resetSearch();
      }
      if (action === 'search') {
        const result = await request<{ hits: ArchivePassage[] }>('/archive-rag/search', {
          method: 'POST',
          body: JSON.stringify({
            query,
            application_ids: selectedApps,
            include_attachments: attachments,
            include_ocr: ocr,
            preferred_only: preferred,
          }),
        });
        setHits(result.hits);
        setPicked([]);
        invalidate();
      }
      if (action === 'preview') {
        setPreview(
          await request<Preview>('/archive-rag/preview', {
            method: 'POST',
            body: JSON.stringify({ query, chunk_ids: picked, topic_code: call, trial }),
          }),
        );
        setApproved(false);
      }
      if (action === 'generate' && preview && approved) {
        const job = await request<ConceptJob>('/archive-rag/generate', {
          method: 'POST',
          body: JSON.stringify({ preview_id: preview.id, approved: true }),
        });
        onStarted(job);
      }
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setBusy('');
    }
  };
  return (
    <div className="archive-mining">
      <aside className="archive-mining-controls">
        <h2>Explore ideas from past work</h2>
        <p className="archive-muted">
          Retrieve passages locally, then choose what to send for new concepts. Search uses keyword
          ranking, not semantic embeddings; try the wording used in the documents, including German
          terms.
        </p>
        <div className="archive-index-status">
          <strong>
            {busy === 'index'
              ? 'Indexing on this computer…'
              : status?.ready
                ? `${status.passages} searchable passages`
                : status?.stale
                  ? 'Index needs rebuilding'
                  : 'Build your local search index'}
          </strong>
          <p className="archive-muted">
            {status?.documents_indexed !== undefined &&
              `${status.documents_indexed} documents indexed. ${status.documents_without_text ?? 0} without readable text. `}
            No model contact or profile changes.
          </p>
          <button disabled={!!busy} onClick={() => void run('index')}>
            {status?.ready ? 'Rebuild index' : 'Build local index'}
          </button>
        </div>
        <form
          onSubmit={(e) => {
            e.preventDefault();
            void run('search');
          }}
        >
          <fieldset disabled={!!busy}>
            <legend>Choose applications to search</legend>
            {applications.map((a) => (
              <label className="check-label" key={a.id}>
                <input
                  type="checkbox"
                  checked={selectedApps.includes(a.id)}
                  onChange={(e) => {
                    setSelectedApps(
                      e.target.checked
                        ? [...selectedApps, a.id]
                        : selectedApps.filter((id) => id !== a.id),
                    );
                    resetSearch();
                  }}
                />
                {a.title} <small>({a.ownership})</small>
              </label>
            ))}
            <label className="archive-question">
              Research question or keywords
              <textarea
                required
                minLength={2}
                maxLength={2000}
                value={query}
                onChange={(e) => {
                  setQuery(e.target.value);
                  resetSearch();
                }}
                placeholder="e.g. a method, a population or an outcome"
              />
            </label>
            <label className="check-label">
              <input
                type="checkbox"
                checked={preferred}
                onChange={(e) => {
                  setPreferred(e.target.checked);
                  resetSearch();
                }}
              />
              Only versions I marked preferred
            </label>
            <label className="check-label">
              <input
                type="checkbox"
                checked={attachments}
                onChange={(e) => {
                  setAttachments(e.target.checked);
                  resetSearch();
                }}
              />
              Include CVs and consent forms
            </label>
            <label className="check-label">
              <input
                type="checkbox"
                checked={ocr}
                onChange={(e) => {
                  setOcr(e.target.checked);
                  resetSearch();
                }}
              />
              Include unverified OCR
            </label>
            <button
              className="btn-primary"
              disabled={!status?.ready || !selectedApps.length || query.trim().length < 2}
            >
              {busy === 'search' ? 'Searching…' : 'Find relevant passages'}
            </button>
          </fieldset>
        </form>
      </aside>
      <main className="archive-mining-results">
        {error && (
          <p className="archive-warning" role="alert">
            {error}
          </p>
        )}
        <h2>From sources to new concepts</h2>
        {hits === null && (
          <p>
            Select applications and search for a problem, method or research direction. Past
            applications are for ideas. They do not show what you can do now.
          </p>
        )}
        {hits?.length === 0 && (
          <p>
            No matching passages within these filters. Try other terms, more applications, or
            include other documents. An empty result does not mean the archive lacks evidence.
          </p>
        )}
        {!!hits?.length && (
          <>
            <p className="archive-muted">
              {hits.length} matches · select up to 8 passages. Exact copies are linked and
              near-identical results are reduced. Ranking is not a relevance guarantee.
            </p>
            <div className="archive-hit-list">
              {hits.map((p) => (
                <article
                  className={`archive-hit ${picked.includes(p.source_id) ? 'is-picked' : ''}`}
                  key={p.source_id}
                >
                  <label className="check-label">
                    <input
                      type="checkbox"
                      disabled={!!busy || (!picked.includes(p.source_id) && picked.length >= 8)}
                      checked={picked.includes(p.source_id)}
                      onChange={(e) => {
                        setPicked(
                          e.target.checked
                            ? [...picked, p.source_id]
                            : picked.filter((id) => id !== p.source_id),
                        );
                        invalidate();
                      }}
                    />
                    <strong>Use as inspiration</strong>
                    <span className="archive-muted">Keyword score {p.score}</span>
                  </label>
                  <ArchiveSource passage={p} />
                </article>
              ))}
            </div>
            <section className="archive-generation">
              <h3>Develop concepts with selected evidence</h3>
              <p>
                Uses {picked.length} selected archive passages, the funding call and your confirmed
                profile. Proposed work stays separate from demonstrated capability.
              </p>
              <label>
                Funding call
                <select
                  disabled={!!busy}
                  value={call}
                  onChange={(e) => {
                    setCall(e.target.value);
                    invalidate();
                  }}
                >
                  <option value="">Choose a call</option>
                  {calls.map((c) => (
                    <option value={c.topic_code} key={c.topic_code}>
                      {c.title ?? c.topic_code}
                    </option>
                  ))}
                </select>
              </label>
              <label className="check-label">
                <input
                  type="checkbox"
                  disabled={!!busy}
                  checked={trial}
                  onChange={(e) => {
                    setTrial(e.target.checked);
                    invalidate();
                  }}
                />
                Exploratory run: this does not check whether I can apply
              </label>
              <button
                disabled={!!busy || !call || !picked.length}
                onClick={() => void run('preview')}
              >
                {busy === 'preview' ? 'Preparing preview…' : 'Preview exactly what will be sent'}
              </button>
              {preview && (
                <div className="archive-send-preview">
                  <h3>Review before sending</h3>
                  <p>
                    <strong>Model:</strong> {preview.connection.model}
                    <br />
                    <strong>Destination:</strong> {preview.connection.base_url}
                    {preview.connection.tunnel_host && (
                      <>
                        <br />
                        SSH tunnel to {preview.connection.tunnel_host}
                      </>
                    )}
                  </p>
                  <button
                    disabled={!!busy}
                    className="btn-ghost"
                    onClick={() => {
                      invalidate();
                      onConnection();
                    }}
                  >
                    Change model connection
                  </button>
                  <p>
                    The following {preview.passages.length} passages and their source metadata—not
                    the whole archive—will be sent, along with this call and your confirmed profile.
                    Drafting, critique and revision use the same destination. New hypotheses and
                    claimed novelty require review.
                  </p>
                  {preview.passages.map((p) => (
                    <details key={p.source_id}>
                      <summary>
                        {p.document_name} · lines {p.line_start}–{p.line_end}
                      </summary>
                      <ArchiveSource passage={p} />
                    </details>
                  ))}
                  <label className="check-label">
                    <input
                      type="checkbox"
                      disabled={!!busy}
                      checked={approved}
                      onChange={(e) => setApproved(e.target.checked)}
                    />
                    I approve sending this evidence to the displayed server.
                  </label>
                  <button
                    className="btn-primary"
                    disabled={!!busy || !approved}
                    onClick={() => void run('generate')}
                  >
                    {busy === 'generate'
                      ? 'Starting concept run…'
                      : 'Send selected evidence & generate 3 concepts'}
                  </button>
                </div>
              )}
            </section>
          </>
        )}
      </main>
    </div>
  );
}
