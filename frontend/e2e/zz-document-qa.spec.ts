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

for (const length of [1501, 1600, 2000]) {
  test(`complete ${length}-character D2 evidence survives question projection`, async ({ request }) => {
    const text = 'Revenue ' + 'x'.repeat(length - 8);
    const created = await request.post('/api/offline/workspaces', { data: { name: `Synthetic D2 ${length}` } });
    expect(created.status()).toBe(201);
    const root = `/api/offline/workspaces/${(await created.json()).workspace_id}`;
    const upload = await request.post(`${root}/documents?filename=fixture.txt`, { data: Buffer.from(text), headers: { 'Content-Type': 'text/plain' } });
    expect(upload.status()).toBe(201);
    const doc = (await upload.json()).document_id;
    const extraction = await request.post(`${root}/documents/${doc}/extraction`);
    expect(extraction.status()).toBe(202);
    const job = (await extraction.json()).job_id;
    await expect.poll(async () => (await (await request.get(`/api/offline/jobs/${job}`)).json()).status).toBe('completed');
    const retrieved = await request.post(`${root}/retrieval`, { data: { query: 'Revenue', document_ids: [doc] } });
    expect(retrieved.status()).toBe(200);
    const [hit] = (await retrieved.json()).hits;
    expect(hit.char_end - hit.char_start).toBe(length);
    const result = await request.post(`${root}/questions`, { data: { question: 'Revenue', document_ids: [doc] } });
    expect(result.status()).toBe(200);
    const answer = await result.json();
    expect(answer.status).toBe('answered');
    expect(answer.claims[0].text).toBe(text);
    const citation = answer.claims[0].citations[0];
    expect(citation.quote).toBe(text);
    expect(citation.document_id).toBe(doc);
    expect(citation.version).toBe(1);
    expect(citation.chunk_id).toBe(hit.chunk_id);
    const source = await request.get(`${root}/documents/${doc}/chunks/${citation.chunk_id}?version=1`);
    expect(source.status()).toBe(200);
    const persisted = await source.json();
    expect(persisted.text).toBe(text);
    expect(citation.locator).toEqual(persisted.locator);
    expect(answer.context_projection.omitted_items).toBe(0);
    expect(answer.context_projection.payload_chars).toBeLessThanOrEqual(60000);
  });
}
