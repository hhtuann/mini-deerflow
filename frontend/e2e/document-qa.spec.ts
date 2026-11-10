import { expect, test } from '@playwright/test';

// D4 is an API surface; D6 owns document UI. Exercise production transport
// through the same real local FastAPI launcher as the existing React workflow.
test('document answers verify citations and replace source versions without network providers', async ({ request }) => {
  const created = await request.post('/api/offline/workspaces', { data: { name: 'Synthetic D4 browser fixture' } });
  expect(created.status()).toBe(201);
  const ws = (await created.json()).workspace_id;
  const root = `/api/offline/workspaces/${ws}`;
  const uploaded = await request.post(`${root}/documents?filename=fixture.txt`, { data: Buffer.from('Synthetic project budget is 10 credits.'), headers: { 'Content-Type': 'text/plain' } });
  expect(uploaded.status()).toBe(201);
  const doc = (await uploaded.json()).document_id;
  const extract = async () => {
    const submitted = await request.post(`${root}/documents/${doc}/extraction`);
    expect(submitted.status()).toBe(202);
    const job = (await submitted.json()).job_id;
    await expect.poll(async () => (await (await request.get(`/api/offline/jobs/${job}`)).json()).status).toBe('completed');
  };
  await extract();
  const first = await request.post(`${root}/questions`, { data: { question: 'What is the project budget?', document_ids: [doc], skill_id: 'source-comparison' } });
  expect(first.status()).toBe(200);
  const answer = await first.json();
  expect(answer.status).toBe('answered');
  expect(answer.synthesis_status).toBe('extractive');
  const citation = answer.claims[0].citations[0];
  const source = await request.get(`${root}/documents/${doc}/chunks/${citation.chunk_id}?version=${citation.version}`);
  expect(source.status()).toBe(200);
  expect((await source.json()).text).toContain(citation.quote);
  const revised = await request.post(`${root}/documents/${doc}/versions?filename=fixture.txt`, { data: Buffer.from('Synthetic project budget is 20 credits.'), headers: { 'Content-Type': 'text/plain' } });
  expect(revised.status()).toBe(200);
  await extract();
  const next = await request.post(`${root}/questions`, { data: { question: 'What is the project budget now?', document_ids: [doc], previous_answer: answer.continuation } });
  expect(next.status()).toBe(200);
  const current = await next.json();
  expect(current.source_changes.length).toBeGreaterThan(0);
  expect(current.claims.flatMap((claim: { citations: { version: number }[] }) => claim.citations).every((item: { version: number }) => item.version === 2)).toBe(true);
  const stale = await request.get(`${root}/documents/${doc}/chunks/${citation.chunk_id}?version=1`);
  expect(stale.status()).toBe(409);
  const unsupported = await request.post(`${root}/questions`, { data: { question: 'quasar taxonomy', document_ids: [doc] } });
  expect(unsupported.status()).toBe(200);
  expect((await unsupported.json()).status).toBe('abstained');
});
