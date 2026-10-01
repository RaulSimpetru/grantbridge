import type { Concept, ConceptJob } from './types';

export const sections = {
  aims: 'Research question & aims',
  methods: 'Methods & evaluation',
  work_packages: 'Work packages & milestones',
  risks: 'Risks, ethics & dependencies',
  impact: 'Expected contribution & impact',
};
export type Section = keyof typeof sections;
export type Citation = { source_id: string; claim_supported: string };
export interface Content {
  text: string;
  citations: Citation[];
  missing_info: string[];
}
export interface Draft {
  id: string;
  title: string;
  job_id: string;
  concept_id: string;
  variant: string;
  topic_code: string;
  trial: boolean;
  revision: number;
  updated_at: string;
  notice: string;
  research_question: string;
  concept: Concept;
  sections: Record<Section, Content & { origin: string }>;
  evidence: ConceptJob['evidence'];
  archive_context: ConceptJob['archive_context'];
}

// The Markdown that "Export draft & sources" saves. Kept out of ProposalWorkspace.tsx so
// `npm test` can run it. A sub-heading appears only over a list that has entries.
export function draftMarkdown(draft: Draft): string {
  return [
    `# ${draft.title}`,
    draft.notice,
    `Call: ${draft.topic_code}`,
    `Concept: ${draft.job_id}/${draft.concept_id} (${draft.variant}) · revision ${draft.revision}`,
    draft.trial
      ? 'EXPLORATORY TRIAL — eligibility not established.'
      : 'Verify current eligibility before submission.',
    draft.research_question ? `Research question: ${draft.research_question}` : '',
    ...Object.entries(sections).flatMap(([key, label]) => {
      const content = draft.sections[key as Section];
      return [
        `## ${label}`,
        content.text || '[TO CONFIRM: section not drafted]',
        ...(content.missing_info.length
          ? ['### Unresolved information', ...content.missing_info.map((q) => `- ${q}`)]
          : []),
        ...(content.citations.length
          ? [
              '### Source claims (verify support)',
              ...content.citations.map((c) => `- [${c.source_id}] ${c.claim_supported}`),
            ]
          : []),
      ];
    }),
    '## Source snapshot',
    ...Object.entries(draft.evidence).map(([id, source]) => {
      const section = source.section.replaceAll('_', ' ');
      const archive = draft.archive_context?.passages.find((p) => p.source_id === id);
      // An archived application may be third-party-authored. Name where it came
      // from so the claim can be checked, but never copy the text out — the
      // same rule briefing.py applies when it composes a briefing.
      if (archive || id.toUpperCase().startsWith('ARCHIVE')) {
        const where = archive
          ? `${archive.document_name}, text lines ${archive.line_start}–${archive.line_end}`
          : 'an archived application';
        return `### ${id} · ${section}\n\nFrom ${where}. Text withheld: it belongs to the original applicant. Open it in Past applications to read it.`;
      }
      return `### ${id} · ${section}\n\n${source.text}`;
    }),
  ]
    .filter(Boolean) // the research-question line is empty when there is none
    .join('\n\n');
}
