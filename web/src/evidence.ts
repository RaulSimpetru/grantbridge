// The model sometimes cites evidence inside its prose, "(PROFILE_001, CALL_SCOPE)", although
// only call_evidence and researcher_evidence are meant to carry IDs. The cards drop such
// bracketed groups. A bare ID mid-sentence stays: removing it would break the sentence.
const ID = '(?:CALL|PROFILE|ARCHIVE)_[A-Za-z0-9_]+'; // ARCHIVE ids end in lowercase hex
const CITED = new RegExp(`\\s*[([]${ID}(?:[\\s,;]+(?:and\\s+)?${ID})*[)\\]]`, 'g');

export function stripEvidenceIds(text: string): string {
  return text.replace(CITED, '');
}
