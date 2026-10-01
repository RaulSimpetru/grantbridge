import test from 'node:test';
import assert from 'node:assert/strict';
import { draftMarkdown } from '../src/proposalDraft.ts';
import type { Draft } from '../src/proposalDraft.ts';

const blank = { text: '', citations: [], missing_info: [], origin: 'outline' };
const draft = {
  id: 'draft',
  title: 'Glycaemic biomarkers',
  job_id: 'run',
  concept_id: '1',
  variant: 'original',
  topic_code: 'CALL-1',
  trial: true,
  revision: 2,
  updated_at: '2030-02-01T08:00:00+00:00',
  notice: 'A working draft.',
  research_question: '',
  sections: {
    aims: {
      ...blank,
      text: 'Aims from the concept',
      citations: [{ source_id: 'CALL_SCOPE', claim_supported: 'Within the scope' }],
    },
    methods: blank,
    work_packages: blank,
    risks: { ...blank, missing_info: ['Confirm ethics approval'] },
    impact: blank,
  },
  evidence: {
    CALL_SCOPE: { section: 'scope', text: 'The call scope.' },
    PROFILE_001: { section: 'topics_trajectory', text: 'Glucose monitoring since 2019.' },
  },
  archive_context: null,
} as unknown as Draft;

test('the export has no empty headings or blank lines, and sources read as words', () => {
  const md = draftMarkdown(draft);
  assert.equal(md.match(/### Source claims/g)?.length, 1, 'only the aims cite a source');
  assert.equal(md.match(/### Unresolved information/g)?.length, 1, 'only the risks ask');
  assert.ok(!md.includes('\n\n\n'), 'no empty paragraph for a missing research question');
  assert.ok(md.includes('### PROFILE_001 · topics trajectory\n\nGlucose monitoring since 2019.'));
});
