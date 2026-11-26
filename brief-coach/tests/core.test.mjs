// Unit tests for the analysis core of brief-coach/index.html.
// Run: node --test brief-coach/tests/core.test.mjs
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';

const here = dirname(fileURLToPath(import.meta.url));
const html = readFileSync(join(here, '..', 'index.html'), 'utf8');
const core = html.split('// CORE-START')[1].split('// CORE-END')[0];
const BC = new Function(core + '\nreturn BC;')();

/* ---- synthetic voice: harmonic pulse train with per-syllable pitch and loudness ---- */
function rng(seed) { let s = seed >>> 0; return () => ((s = (s * 1664525 + 1013904223) >>> 0) / 4294967296); }
function synth(sr, sentences, { noise = 0.002, lead = 1.0 } = {}) {
  const rand = rng(7);
  const out = [];
  const push = (v) => out.push(v + (rand() * 2 - 1) * noise);
  for (let i = 0; i < lead * sr; i++) push(0);
  let phase = 0;
  const marks = [];
  for (const sen of sentences) {
    const t0 = out.length / sr;
    sen.syl.forEach(([fA, fB, amp]) => {
      const n = Math.round(0.17 * sr);
      for (let i = 0; i < n; i++) {
        const f = fA + (fB - fA) * (i / n);
        phase += 2 * Math.PI * f / sr;
        const env = Math.sqrt(Math.sin(Math.PI * i / n));
        let v = 0; for (let k = 1; k <= 8; k++) v += Math.sin(k * phase) / k;
        push(v * amp * env * 0.6);
      }
      const g = Math.round(0.05 * sr);
      for (let i = 0; i < g; i++) push(0);
    });
    marks.push({ start: t0, end: out.length / sr });
    for (let i = 0; i < sen.pause * sr; i++) push(0);
  }
  return { data: Float32Array.from(out), marks };
}
const flat = (n, f, amp = 0.3) => Array.from({ length: n }, () => [f, f, amp]);
function feed(sr, data) {
  const tr = new BC.Tracker(sr, {});
  for (let i = 0; i + 2048 <= data.length; i += 2048) tr.push(data.subarray(i, i + 2048));
  tr.finish();
  return tr;
}

test('YIN pitch detector finds f0 within 2% at 44.1 and 48 kHz', () => {
  for (const sr of [44100, 48000]) {
    for (const f of [90, 130, 200, 290]) {
      const buf = new Float32Array(2048);
      for (let i = 0; i < buf.length; i++) { const p = 2 * Math.PI * f * i / sr; let v = 0; for (let k = 1; k <= 6; k++) v += Math.sin(k * p) / k; buf[i] = 0.3 * v; }
      const { f0, conf } = BC.detectPitch(buf, sr);
      assert.ok(Math.abs(f0 - f) / f < 0.02, `sr=${sr} f=${f} got ${f0}`);
      assert.ok(conf > 0.8);
    }
  }
  const silent = BC.detectPitch(new Float32Array(2048), 48000);
  assert.equal(silent.f0, 0);
});

function threeSentences() {
  const s1 = flat(10, 140); s1[3] = [175, 178, 0.42]; s1[8] = [118, 108, 0.3]; s1[9] = [106, 98, 0.3];
  const s2 = flat(10, 135); s2[8] = [150, 165, 0.3]; s2[9] = [168, 190, 0.3];
  const s3 = flat(10, 135); s3[8] = [125, 115, 0.04]; s3[9] = [112, 105, 0.03];
  return [{ syl: s1, pause: 0.9 }, { syl: s2, pause: 0.9 }, { syl: s3, pause: 1.0 }];
}

test('phrase contours: falling, rising and trailing endings are told apart', () => {
  const sr = 48000;
  const { data } = synth(sr, threeSentences());
  const tr = feed(sr, data);
  assert.equal(tr.phrases.length, 3, 'one breath group per sentence');
  const [a, b, c] = tr.phrases.map((p) => p.m);
  assert.equal(a.contour, 'fall');
  assert.equal(b.contour, 'rise');
  assert.equal(a.trailing, false);
  assert.equal(c.trailing, true, `tail drop ${c.tailDrop}`);
  assert.ok(a.peaks.length >= 1, 'stressed syllable in sentence 1 is a peak');
  assert.ok(a.range > 3, `range ${a.range}`);
});

test('uploaded files decoded at 16 kHz give the same contours with scaled blocks', () => {
  const sr = 16000, N = Math.round(2048 * sr / 48000);
  const { data } = synth(sr, threeSentences());
  const tr = new BC.Tracker(sr, {});
  for (let i = 0; i < Math.round(sr / N); i++) tr.calibrate(data.subarray(i * N, (i + 1) * N));
  for (let i = 0; i + N <= data.length; i += N) tr.push(data.subarray(i, i + N));
  tr.finish();
  assert.equal(tr.phrases.length, 3);
  assert.deepEqual(tr.phrases.map((p) => p.m.contour), ['fall', 'rise', tr.phrases[2].m.contour]);
  assert.deepEqual(tr.phrases.map((p) => p.m.trailing), [false, false, true]);
  const pros = BC.prosodyStage(tr, [], tr.t);
  assert.equal(pros.units.length, 3);
  assert.ok(Math.abs(pros.acousticSyl - 30) <= 3, `syllables ${pros.acousticSyl}`);
});

test('full pipeline: prosody + transcript -> findings and scores', () => {
  const sr = 44100;
  const { data, marks } = synth(sr, threeSentences());
  const tr = feed(sr, data);
  const texts = [
    '결론부터 말씀드리면 물류 거점을 통합하면 연 18억 원을 절감합니다',
    '이유는 세 가지인데요',
    '아마 비용이 좀 줄어들 것 같습니다',
  ];
  const segments = marks.map((m, i) => ({ text: texts[i], tStart: m.start - 0.3, tEnd: m.end + 0.7 }));
  const pros = BC.prosodyStage(tr, segments, tr.t);
  assert.equal(pros.units.length, 3);
  assert.deepEqual(pros.units.map((u) => u.contour), ['fall', 'rise', pros.units[2].contour]);
  assert.equal(pros.units[2].trailing, true);
  assert.ok(pros.contour && pros.contour.st.length > 50);
  const ctx = { audience: 'exec', purpose: 'decision', target: 60, lang: 'ko-KR', key: '물류 거점 통합으로 18억 원 절감', ask: '' };
  const ev = BC.evaluate(pros, pros.units.map((u) => u.text), ctx, 'ko-KR');
  const ids = ev.findings.map((f) => f.id);
  for (const id of ['uptalk', 'trailing', 'hedges', 'no_ask']) assert.ok(ids.includes(id), `missing ${id} in ${ids}`);
  assert.equal(ev.m.bluf, true);
  assert.equal(ev.m.numbers, 1);
  assert.ok(ev.units[0].stressedIdx.length >= 1, 'a stressed word was mapped');
  assert.equal(ev.focusIds.length, 3);
  for (const k of ['structure', 'delivery', 'pace', 'language']) assert.equal(typeof ev.scores[k], 'number', k);
  assert.ok(ev.overall > 0 && ev.overall < 100);
  // the same take without prosody: delivery cannot be scored
  const ev2 = BC.evaluate(BC.prosodyStage(null, segments, tr.t), texts, ctx, 'ko-KR');
  assert.equal(ev2.scores.delivery, null);
  assert.ok(!ev2.findings.some((f) => f.axis === 'delivery'));
});

test('comparison marks fixed focus items as improved', () => {
  const ctx = { purpose: 'decision', target: 120, key: '', ask: '' };
  const before = BC.evaluate(null, ['음 그러니까 매출이 좀 줄어든 것 같습니다', '아마 다음 달에는 회복될 것 같아요'], ctx, 'ko-KR', { kind: 'script' });
  const after = BC.evaluate(null, ['결론부터 말씀드리면 매출이 12% 줄었습니다', '첫째 원인은 단가 하락입니다', '다음 주까지 가격 조정을 승인해 주십시오'], ctx, 'ko-KR', { kind: 'script' });
  assert.ok(before.focusIds.includes('no_bluf'));
  const cmp = BC.compare(before, after);
  const byId = Object.fromEntries(cmp.items.map((i) => [i.id, i.status]));
  assert.equal(byId.no_bluf, 'improved');
  assert.ok(cmp.delta > 0, `delta ${cmp.delta}`);
});

test('Korean text analysis: fillers, hedges, endings, numbers', () => {
  const t = BC.analyzeText('어 이제 매출이 15% 정도 늘어날 것 같은데', 'ko');
  assert.deepEqual(t.fillerIdx, [0, 1]);
  assert.ok(t.hedges.some((h) => h.includes('것 같')));
  assert.equal(t.ending, 'open');
  assert.deepEqual(t.numIdx, [3]);
  assert.equal(BC.analyzeText('승인해 주십시오', 'ko').ending, 'formal');
  assert.equal(BC.analyzeText('진행했어요', 'ko').ending, 'yo');
});

test('English lexicon works for global briefs', () => {
  const ctx = { purpose: 'decision', target: 60, key: '', ask: '' };
  const ev = BC.evaluate(null, ['Bottom line, I recommend we consolidate the two warehouses.', 'It saves 2 million dollars a year.', 'I think the risk is maybe the transition.', 'I need your approval by Friday.'], ctx, 'en-US', { kind: 'script' });
  assert.equal(ev.m.bluf, true);
  assert.equal(ev.m.ask, true);
  assert.ok(ev.m.hedgeN >= 2);
  assert.ok(!ev.findings.some((f) => f.id === 'yo_endings' || f.id === 'open_endings'));
});

test('auto markup adds stress, pauses and falling endings', () => {
  const out = BC.autoMark('결론부터 말씀드리면, 연간 18억 원을 절감합니다. 다음 주까지 승인해 주십시오.', ['절감'], 'ko-KR');
  const lines = out.split('\n');
  assert.equal(lines.length, 2);
  assert.match(lines[0], /\*\*18억 원을\*\*/);
  assert.match(lines[0], /\/ \*\*18억/);
  assert.match(lines[0], /↘ \/\/$/);
  const chunks = BC.speechChunks(out);
  assert.ok(chunks.length >= 3 && chunks.every((c) => !c.text.includes('*')));
});

test('transcript sentences without timestamps are placed on the right pauses', () => {
  const sr = 16000, N = Math.round(2048 * sr / 48000);
  const { data } = synth(sr, threeSentences());
  const tr = new BC.Tracker(sr, {});
  for (let i = 0; i + N <= data.length; i += N) tr.push(data.subarray(i, i + N));
  tr.finish();
  BC.prosodyStage(tr, [], tr.t);
  const sentences = BC.splitSentences('결론부터 말씀드리면 연 18억 원을 절감합니다. 이유는 세 가지입니다. 비용이 줄어듭니다.');
  assert.equal(sentences.length, 3);
  const segs = BC.alignSentences(sentences, tr.phrases, 'ko-KR');
  assert.deepEqual(segs.map((s) => s.tEnd), tr.phrases.map((p) => Math.round((p.end + 0.05) * 100) / 100));
  const pros = BC.prosodyStage(tr, segs, tr.t);
  assert.deepEqual(pros.units.map((u) => u.text), sentences);
  assert.deepEqual(pros.units.map((u) => u.contour), ['fall', 'rise', pros.units[2].contour]);
  // more sentences than breath groups still yields one segment per sentence, in order
  const many = BC.alignSentences(['가.', '나.', '다.', '라.', '마.'], tr.phrases, 'ko-KR');
  assert.equal(many.length, 5);
  assert.ok(many.every((s, i) => i === 0 || s.tEnd >= many[i - 1].tEnd));
});

test('markup becomes speakable text with stress words for TTS', () => {
  const { text, stress } = BC.toSpeechText('결론부터 말씀드리면, / 연 **18억 원**을 절감합니다 ↘ //\n이유는 / **세 가지**입니다 ↘ //');
  assert.equal(text, '결론부터 말씀드리면, 연 18억 원을 절감합니다.\n이유는, 세 가지입니다.');
  assert.deepEqual(stress, ['18억 원', '세 가지']);
});

test('final score blends device measurement with the AI score per axis and keeps the reasons', () => {
  const ctx = { purpose: 'decision', target: 120, key: '', ask: '' };
  const ev = BC.evaluate(null, ['결론부터 말씀드리면 매출이 12% 줄었습니다', '다음 주까지 가격 조정을 승인해 주십시오'], ctx, 'ko-KR', { kind: 'script' });
  assert.match(ev.reasons.structure, /결론 먼저 O/);
  assert.match(ev.reasons.structure, /마지막 요청 O/);
  assert.match(ev.reasons.delivery, /말한 뒤/);
  const ai = BC.normalizeAI({ scores: { structure: { score: 90, reason: '결론과 요청이 분명' }, delivery: { score: 80, reason: 'x' }, pace: { score: null, reason: '' }, language: { score: '70', reason: '단정형' } }, overall_reason: '구조가 탄탄함' });
  const fs = BC.finalScores(ev, ai);
  assert.equal(fs.axes.structure.final, Math.round(0.4 * ev.scores.structure + 0.6 * 90));
  assert.equal(fs.axes.structure.aiReason, '결론과 요청이 분명');
  assert.equal(fs.axes.delivery.final, null, 'no measurement: the AI score is not used');
  assert.equal(fs.axes.language.ai, 70);
  assert.equal(fs.overallReason, '구조가 탄탄함');
  assert.ok(fs.blended);
  const plain = BC.finalScores(ev, null);
  assert.equal(plain.overall, ev.overall);
  assert.equal(plain.blended, false);
  // out-of-range AI scores are clamped
  assert.equal(BC.normalizeAI({ scores: { structure: { score: 140, reason: '' } } }).scores.structure.score, 100);
  // comparison deltas follow the final scores
  const cmp = BC.compare(ev, ev, plain, fs);
  assert.equal(cmp.delta, fs.overall - plain.overall);
});

test('AI prompt and schema are well formed', () => {
  const walk = (s, path = 'root') => {
    if (s.type === 'object') {
      assert.equal(s.additionalProperties, false, path);
      assert.deepEqual([...s.required].sort(), Object.keys(s.properties).sort(), path);
      for (const [k, v] of Object.entries(s.properties)) walk(v, path + '.' + k);
    }
    if (s.type === 'array') walk(s.items, path + '[]');
  };
  walk(BC.AI_SCHEMA);
  const ctx = { audience: 'ceo', purpose: 'decision', target: 120, key: '18억 절감', ask: '착수 승인', script: '' };
  const ev = BC.evaluate(null, ['결론부터 말씀드리면 18억 원을 절감합니다'], ctx, 'ko-KR', { kind: 'script' });
  const { system, user } = BC.buildAIPrompt(ctx, ev, ev, null);
  assert.match(system, /BLUF/);
  assert.match(user, /\[보고 상황\]/);
  assert.match(user, /\[지난 시도 요약\]/);
  assert.match(user, /model_script/);
  const n = BC.normalizeAI({ headline: 'x', focus: [{ title: 'a', why: 'b', how: 'c', drill: 'd', evidence: 'e' }], previous_check: [{ title: 't', status: 'weird', note: '' }] });
  assert.equal(n.previous_check[0].status, 'not_yet');
  assert.deepEqual(n.exec_questions, []);
});
