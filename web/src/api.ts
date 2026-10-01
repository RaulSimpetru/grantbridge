import type {
  ActivityItem,
  Assertion,
  Call,
  Component,
  Confidence,
  ConceptJob,
  Decision,
  Visibility,
  Eligibility,
  GenerationHealth,
  GenerationConnection,
} from './types';

// 127.0.0.1, like the dev server: localhost would make the API a different
// site, and the browser would then send archive download links without an Origin.
const API_BASE = import.meta.env.VITE_API_BASE_URL ?? 'http://127.0.0.1:8010';

export async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(`${API_BASE}${path}`, {
    ...init,
    headers: { 'Content-Type': 'application/json', ...init?.headers },
  });
  if (!res.ok) {
    const detail = await res.json().catch(() => null);
    const message = Array.isArray(detail?.detail)
      ? detail.detail.map((e: { msg: string }) => e.msg).join('; ')
      : detail?.detail;
    throw new Error(message ?? `${path} failed: ${res.status} ${res.statusText}`);
  }
  return res.json() as Promise<T>;
}

export function archiveDocumentUrl(id: string): string {
  return `${API_BASE}/past-application-documents/${encodeURIComponent(id)}/original`;
}

export function listCalls(): Promise<Call[]> {
  return request<Call[]>('/calls');
}

export interface FundingNotice {
  id: string;
  provider: string;
  url: string;
  title: string;
  published: string | null;
}
export interface FundingSource {
  id: string;
  name: string;
  url: string;
  coverage: string;
  fetched_at: string | null;
  error: string | null;
  items: FundingNotice[];
}
export function listFundingSources(): Promise<FundingSource[]> {
  return request('/funding-sources');
}
export function refreshFundingSource(id: string): Promise<FundingSource> {
  return request(`/funding-sources/${id}/refresh`, {
    method: 'POST',
    signal: AbortSignal.timeout(90000),
  });
}
export function importFundingNotice(source: string, id: string): Promise<Call> {
  return request(`/funding-sources/${source}/notices/${id}/import`, {
    method: 'POST',
    signal: AbortSignal.timeout(90000),
  });
}

export function reviewEligibility(
  code: string,
  body: {
    state: 'eligible' | 'ineligible';
    reason: string;
    source: string;
    conditions_checked: true;
  },
): Promise<Eligibility> {
  return request(`/calls/${encodeURIComponent(code)}/eligibility`, {
    method: 'POST',
    body: JSON.stringify(body),
  });
}

export function generationHealth(): Promise<GenerationHealth> {
  return request('/generation/health', { signal: AbortSignal.timeout(15000) });
}
export function getGenerationSettings(): Promise<GenerationConnection> {
  return request('/generation/settings', { signal: AbortSignal.timeout(15000) });
}
export function testGenerationConnection(body: GenerationConnection): Promise<GenerationHealth> {
  return request('/generation/test', {
    method: 'POST',
    body: JSON.stringify(body),
    signal: AbortSignal.timeout(15000),
  });
}
export function saveGenerationSettings(body: GenerationConnection): Promise<GenerationHealth> {
  return request('/generation/settings', {
    method: 'PUT',
    body: JSON.stringify(body),
    signal: AbortSignal.timeout(15000),
  });
}
export function resetGenerationSettings(): Promise<GenerationHealth> {
  return request('/generation/settings', { method: 'DELETE', signal: AbortSignal.timeout(15000) });
}

export function listPortfolio(): Promise<Assertion[]> {
  return request<Assertion[]>('/portfolio');
}

export interface ResearcherProfile {
  assertions: Assertion[];
  evidence: { source_id: string; section: string; text: string }[];
  context: string;
  used_count: number;
  not_used_count: number;
  connection: GenerationConnection;
}
export function getResearcherProfile(): Promise<ResearcherProfile> {
  return request('/researcher-profile');
}

export function createAssertion(body: {
  component: Component;
  claim: string;
  source?: string;
  confidence?: Confidence;
  visibility?: Visibility;
  phd_award_date?: string;
}): Promise<Assertion> {
  return request<Assertion>('/portfolio', { method: 'POST', body: JSON.stringify(body) });
}

export function updateAssertion(
  id: string,
  fields: Partial<
    Pick<
      Assertion,
      'claim' | 'source' | 'confidence' | 'visibility' | 'included' | 'phd_award_date'
    >
  >,
): Promise<Assertion> {
  return request<Assertion>(`/portfolio/${id}`, {
    method: 'PATCH',
    body: JSON.stringify(fields),
  });
}

export function importFromOpenAlex(authorId: string): Promise<Assertion[]> {
  return request<Assertion[]>('/portfolio/import/openalex', {
    method: 'POST',
    body: JSON.stringify({ author_id: authorId }),
  });
}

export function createConceptJob(
  topicCode: string,
  retryOf?: string,
  trial = false,
): Promise<ConceptJob> {
  return request<ConceptJob>('/concepts/jobs', {
    method: 'POST',
    body: JSON.stringify({ topic_code: topicCode, retry_of: retryOf ?? null, trial }),
  });
}

export function getConceptJob(jobId: string): Promise<ConceptJob> {
  return request<ConceptJob>(`/concepts/jobs/${jobId}`);
}

export function listConceptJobs(topicCode?: string): Promise<ConceptJob[]> {
  const query = topicCode ? `?topic_code=${encodeURIComponent(topicCode)}` : '';
  return request<ConceptJob[]>(`/concepts/jobs${query}`);
}

export function createDecision(body: {
  kind: Decision['kind'];
  target_type: Decision['target_type'];
  target_id: string;
  reason?: string;
}): Promise<Decision> {
  return request<Decision>('/decisions', { method: 'POST', body: JSON.stringify(body) });
}

export function listDecisions(): Promise<Decision[]> {
  return request<Decision[]>('/decisions');
}

export function listActivity(): Promise<ActivityItem[]> {
  return request<ActivityItem[]>('/activity');
}
