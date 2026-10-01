// Mirrors grant_recommender.api.FitScore.
export interface FitScore {
  keyword_overlap: number;
  bm25: number;
  rank: number;
  n_portfolio_claims: number;
}

// Eligibility is a source-versioned researcher review; deadline expiry overrides it.
export interface Eligibility {
  state: 'eligible' | 'ineligible' | 'uncertain';
  reason: string;
  basis?: string;
  source?: string;
  reviewed_at?: string;
}

export interface Passage {
  source_id: string;
  section: string;
  text: string;
}
export interface Dossier {
  passages: Passage[];
  evidence_pairs: {
    assertion_id: string;
    claim: string;
    component: string;
    source: string | null;
    passage: Passage;
    overlap: number;
  }[];
  capability: { label: string; explanation: string };
  strategic: { label: string; explanation: string };
  risk: { label: string; explanation: string };
  gaps: string[];
}
export interface GenerationConnection {
  base_url: string;
  model: string;
  source?: 'saved' | 'launcher';
  tunnel_host?: string | null;
}
export interface GenerationHealth {
  ready: boolean;
  model: string;
  detail: string;
  base_url?: string;
  models?: string[];
  checked_at?: string;
  latency_ms?: number;
  tunnel_host?: string | null;
}

// Mirrors grant_recommender.api.Call exactly -- keep both in sync by hand
// until the API ships an OpenAPI-generated client.
export interface Call {
  topic_conditions_document?: SourceNode[];
  provider?: string;
  country?: string | null;
  topic_code: string;
  title: string | null;
  call_identifier: string | null;
  call_title: string | null;
  deadlines: string[];
  status: string | null;
  types_of_action: string[];
  scope_text: string;
  topic_conditions: string;
  framework_programme: string | null;
  programme_period: string | null;
  source_url: string | null;
  fetched_at: string | null;
  scientific_fit: FitScore | null;
  eligibility: Eligibility;
  availability: string;
  dossier: Dossier;
  source_warning?: string;
}

export interface SourceNode {
  type: string;
  text?: string;
  href?: string;
  children?: SourceNode[];
  start?: number;
  value?: number;
  colspan?: number;
  rowspan?: number;
}

export const COMPONENTS = [
  'topics_trajectory',
  'methods',
  'software',
  'datasets',
  'infrastructure',
  'preliminary_findings',
  'populations_access',
  'collaborators',
  'career_eligibility',
  'prior_proposals',
] as const;
export type Component = (typeof COMPONENTS)[number];

export const CONFIDENCES = ['confirmed', 'inferred', 'needs_review', 'missing'] as const;
export type Confidence = (typeof CONFIDENCES)[number];

export type Visibility = 'public' | 'private';

// Mirrors grant_recommender.api.Assertion.
export interface Assertion {
  id: string;
  component: Component;
  claim: string;
  source: string | null;
  confidence: Confidence;
  visibility: Visibility;
  included: boolean;
  phd_award_date?: string | null;
}

// Mirrors grant_recommender.api.Stage.
export interface Stage {
  name: string;
  status: 'pending' | 'running' | 'succeeded' | 'failed';
  wall_time_seconds: number | null;
  detail: string | null;
  // Tail of the model's output while this stage streams; null once it ends.
  live?: string | null;
  // Server-stamped start of this stage, so elapsed time survives a reload.
  started_at?: string | null;
}

export interface EvidenceCitation {
  source_id: string;
  claim_supported: string;
}

export interface Concept {
  archive_evidence?: EvidenceCitation[];
  concept_id: string;
  title: string;
  problem: string;
  central_hypothesis: string;
  call_fit: string;
  researcher_fit: string;
  objectives: string[];
  required_assets_and_collaborators: string[];
  novelty: string;
  principal_feasibility_risk: string;
  call_evidence: EvidenceCitation[];
  researcher_evidence: EvidenceCitation[];
}

export interface ConceptCritique {
  concept_id: string;
  grounding_and_fit: string;
  novelty_and_feasibility: string;
  revision_action: string;
}

export interface Critique {
  concept_critiques: ConceptCritique[];
  unsupported_claims: string[];
  missing_requirements: string[];
  diversity_problems: string[];
  revision_priorities: string[];
}

export interface EvidenceItem {
  section: string;
  text: string;
}

// Mirrors grant_recommender.api.ConceptJob.
export interface ConceptJob {
  revision_audit?: { notice: string; restored_fields: string[]; policy: string } | null;
  archive_context?: {
    query: string;
    connection: GenerationConnection;
    passages: {
      source_id: string;
      document_id: string;
      document_name: string;
      line_start: number;
      line_end: number;
    }[];
  } | null;
  trial?: boolean;
  id: string;
  topic_code: string;
  status: 'running' | 'succeeded' | 'failed' | 'interrupted';
  created_at: string;
  retry_of: string | null;
  seed_index: number;
  model: string;
  model_digest: string | null;
  stages: Stage[];
  draft: { concepts: Concept[] } | null;
  critique: Critique | null;
  concepts: Concept[] | null;
  diversity: Record<string, Record<string, number>> | null;
  error: string | null;
  // source_id -> the actual call/portfolio passage a citation names.
  evidence: Record<string, EvidenceItem>;
}

// Mirrors grant_recommender.api.Decision.
export interface Decision {
  id: string;
  kind:
    | 'save'
    | 'unsave'
    | 'dismiss'
    | 'undo_dismiss'
    | 'develop'
    | 'undevelop'
    | 'choose'
    | 'unchoose';
  target_type: 'call' | 'concept';
  target_id: string;
  // For kind 'develop' this is the id of the concept_run it started, not a
  // human-written rationale -- treat as a cross-reference, not display text.
  reason: string | null;
  // 'watcher' when the background watch loop acted on its own; absent on rows
  // written before the loop existed.
  actor?: 'researcher' | 'watcher';
  created_at: string;
}

// Mirrors grant_recommender.api.ActivityItem.
export interface ActivityItem {
  type: 'decision' | 'concept_run';
  occurred_at: string;
  decision: Decision | null;
  concept_run: ConceptJob | null;
}
