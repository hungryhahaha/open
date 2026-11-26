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
  const before = BC.evaluate(null, ['지난달 거래처 세 곳과 미팅을 했는데요', '첫 미팅에서는 가격 이야기를 했고요', '두 번째 미팅에서는 물량 이야기를 했습니다', '아마 다음 달에는 매출이 회복될 것 같아요'], ctx, 'ko-KR', { kind: 'script' });
  const after = BC.evaluate(null, ['결론부터 말씀드리면 매출이 12% 줄었습니다', '첫째 원인은 단가 하락입니다', '다음 주까지 가격 조정을 승인해 주십시오'], ctx, 'ko-KR', { kind: 'script' });
  assert.ok(before.focusIds.includes('first30'));
  const cmp = BC.compare(before, after);
  const byId = Object.fromEntries(cmp.items.map((i) => [i.id, i.status]));
  assert.equal(byId.first30, 'improved');
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
  assert.match(ev.reasons.structure, /첫 30초 결론 O/);
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

const F30_CASES = {
  contextThenConclusion: '지난주 말씀하신 ○○사 AX 점검 건입니다. 점검해 보니 AI 성과로 보고된 효과의 대부분은 기존 설비 개선 효과였습니다. 근거는 두 가지입니다. 첫째, 생산성 향상 12% 가운데 9%는 설비 교체 시점과 겹칩니다. 둘째, AI를 쓰지 않은 라인도 같은 기간 8% 개선됐습니다. 다음 달 실사에서 이 부분을 확인하도록 승인해 주십시오.',
  conclusionFirst: '결론부터 말씀드리면, 물류 거점을 평택으로 통합하면 연간 18억 원을 절감할 수 있습니다. 이유는 세 가지입니다. 첫째, 운송비가 연 9억 원 줄어듭니다. 둘째, 재고 회전이 2주 빨라집니다. 셋째, 인력 재배치로 6억 원을 아낍니다. 다음 주 금요일까지 착수를 승인해 주십시오.',
  narrativeFirst: '○○사와 세 차례 미팅을 했는데요, 첫 미팅에서는 현황 자료를 받았고요. 두 번째 미팅에서는 공장 현장을 둘러봤습니다. 세 번째 미팅에서는 담당 임원 인터뷰를 진행했습니다. 그 결과, AI 성과로 보고된 효과의 대부분은 기존 설비 개선 효과였습니다. 다음 달 실사를 승인해 주십시오.',
  demoWithPreview: '오늘은 새 상담 시스템을 화면으로 직접 보여 드리겠습니다. 결론은 상담 한 건당 처리 시간이 40% 줄었다는 것이고, 어떻게 줄었는지 순서대로 보여 드리겠습니다. 먼저 기존 상담 화면입니다. 고객 문의가 들어오면 상담원이 세 개의 시스템을 오가며 주문 내역을 찾았습니다. 새 화면에서는 문의 내용과 주문 내역이 한 화면에 뜹니다. 다음 달부터 전 센터로 확대하도록 승인해 주십시오.',
};
const first30Flag = (text, opts = {}) => {
  const ctx = { purpose: 'decision', target: 120, key: opts.key || '', ask: '' };
  const units = opts.spoken ? BC.splitSentences(text).map((x) => x.replace(/[.,]/g, '')) : BC.splitSentences(text);
  const ev = BC.evaluate(null, units, ctx, 'ko-KR', { kind: opts.spoken ? 'speech' : 'script', duration: 40 });
  return ev.findings.find((f) => f.id === 'first30') || null;
};

test('first 30 seconds: review cases (script and unpunctuated speech)', () => {
  for (const spoken of [false, true]) {
    assert.equal(first30Flag(F30_CASES.contextThenConclusion, { spoken }), null, 'context sentence + conclusion passes');
    assert.equal(first30Flag(F30_CASES.conclusionFirst, { spoken }), null, 'conclusion first passes');
    const f = first30Flag(F30_CASES.narrativeFirst, { spoken });
    assert.ok(f, 'narrated events pushing the conclusion out of scope are flagged');
    assert.match(f.evidence, /^30초 안에 전달되는 내용: .+ \/ 실제 결론: .+기존 설비 개선 효과/);
    assert.equal(first30Flag(F30_CASES.demoWithPreview, { spoken }), null, 'demo with a preview in scope passes');
  }
});

test('first 30 seconds: context only, demo without preview, empty promise, different conclusion', () => {
  const contextOnly = first30Flag('요즘 업계에서 AI 도입 사례가 많습니다. 저희 팀도 작년부터 관심을 가져 왔습니다. 여러 회사의 사례를 살펴보고 있습니다. 오늘은 그 내용을 공유드립니다.');
  assert.ok(contextOnly); assert.match(contextOnly.evidence, /맥락만 있고 결론이 없음/);
  assert.ok(first30Flag('오늘은 새 상담 시스템을 화면으로 보여 드리겠습니다. 먼저 기존 상담 화면입니다. 고객 문의가 들어오면 상담원이 세 개의 시스템을 오갑니다. 결론적으로 처리 시간이 40% 줄었습니다.'), 'long demo without a preview is flagged');
  assert.ok(first30Flag('결론은 마지막에 말씀드리겠습니다. 먼저 경과를 설명드리면 지난달 회의가 있었습니다. 그 회의에서 일정이 정해졌습니다. 결과적으로 비용이 10% 줄었습니다.'), 'a promise without the conclusion does not count');
  const diff = first30Flag('지난주 말씀하신 ○○사 점검 건입니다. AI 도입 후 생산성이 12% 늘었습니다. 현장 반응도 좋았습니다. 하지만 그 대부분은 기존 설비 개선 효과였습니다.', { key: 'AI 성과의 대부분은 기존 설비 개선 효과' });
  assert.ok(diff, 'a different conclusion in the first 30 seconds is flagged when the prepared conclusion comes later');
  assert.match(diff.evidence, /실제 결론: "하지만 그 대부분은 기존 설비 개선 효과였습니다/);
  // never flagged only because the first sentence is not the conclusion
  assert.equal(first30Flag('이번 달 정례 보고입니다. 신규 고객이 지난달보다 20% 늘었습니다. 이유는 두 가지입니다.'), null);
});

test('review prompt lists the first-30-seconds criterion without numbering and drops conclusion-first', () => {
  const ctx = { audience: 'ceo', purpose: 'decision', target: 120, key: '', ask: '' };
  const ev = BC.evaluate(null, BC.splitSentences(F30_CASES.narrativeFirst), ctx, 'ko-KR', { kind: 'script' });
  const { system, user } = BC.buildAIPrompt(ctx, ev, null, null);
  assert.match(system, /\[점검 기준\]/);
  assert.match(system, /- 첫 30초: 임원이 첫 30초 안에 무슨 건이고 결론이 무엇인지 알 수 있는가\?/);
  assert.match(system, /결론이 첫 문장에 없다는 이유만으로는 지적하지 않습니다/);
  assert.match(system, /30초 안에 전달되는 내용: ○○ \/ 실제 결론: ○○/);
  assert.doesNotMatch(system + user, /BLUF|결론 먼저|6번|\n\s*6\./);
  assert.match(user, /첫 30초 범위\(첫 3문장\)/);
  for (const f of Object.values(BC.F)) assert.doesNotMatch(f.title, /^\d+[.)]|\d+번/);
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
  assert.match(system, /\[점검 기준\]/);
  assert.match(user, /\[보고 상황\]/);
  assert.match(user, /\[지난 시도 요약\]/);
  assert.match(user, /model_script/);
  const n = BC.normalizeAI({ headline: 'x', focus: [{ title: 'a', why: 'b', how: 'c', drill: 'd', evidence: 'e' }], previous_check: [{ title: 't', status: 'weird', note: '' }] });
  assert.equal(n.previous_check[0].status, 'not_yet');
  assert.deepEqual(n.exec_questions, []);
});
