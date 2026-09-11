import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import test from 'node:test';
import ts from 'typescript';

const source = await readFile(new URL('../src/lib/client.ts', import.meta.url), 'utf8');
const { outputText } = ts.transpileModule(source, { compilerOptions: { module: ts.ModuleKind.ESNext, target: ts.ScriptTarget.ES2022 } });
const { extract, retryUploadRequest } = await import(`data:text/javascript;base64,${Buffer.from(outputText).toString('base64')}`);

for (const failure of ['network', 503, 408]) {
  test(`retries the same upload part after ${failure}`, async (t) => {
    const requests = [];
    t.mock.method(globalThis, 'setTimeout', (callback) => { callback(); return 0; });
    t.mock.method(globalThis, 'fetch', async (url, options) => {
      requests.push({ url, options });
      if (requests.length === 1) {
        if (failure === 'network') throw new TypeError('Failed to fetch');
        return new Response('', { status: failure });
      }
      return new Response('{}');
    });
    const body = new Blob(['%PDF']);
    await retryUploadRequest('/uploads/test/0', { method: 'PUT', body }, 'team');
    assert.equal(requests.length, 2);
    assert.equal(requests[0].url, requests[1].url);
    assert.equal(requests[1].options.body, body);
    assert.equal(requests[1].options.headers.get('x-team-id'), 'team');
  });
}

test('does not retry invalid uploads', async (t) => {
  const fetch = t.mock.method(globalThis, 'fetch', async () => new Response('{}', { status: 413 }));
  await assert.rejects(retryUploadRequest('/uploads/test/0', { method: 'PUT' }, 'team'), { status: 413 });
  assert.equal(fetch.mock.callCount(), 1);
});

test('stops after three failed attempts', async (t) => {
  t.mock.method(globalThis, 'setTimeout', (callback) => { callback(); return 0; });
  const fetch = t.mock.method(globalThis, 'fetch', async () => { throw new TypeError('Failed to fetch'); });
  await assert.rejects(retryUploadRequest('/uploads/test/0', { method: 'PUT' }, 'team'));
  assert.equal(fetch.mock.callCount(), 3);
});

test('uploads at most three parts concurrently and processes only after all parts complete', async (t) => {
  const pending = new Map();
  const uploaded = new Map();
  const progress = [];
  const artifacts = [{ id: 'excel', kind: 'excel', filename: 'result.xlsx' }];
  let processCalls = 0;
  let maximumConcurrent = 0;
  t.mock.method(globalThis, 'setTimeout', () => { throw new Error('Completed jobs should be available immediately'); });
  t.mock.method(globalThis, 'fetch', async (url, options) => {
    assert.equal(options.headers.get('x-team-id'), 'team');
    if (url === '/api/uploads') return Response.json({ id: 'upload', chunk_size: 4 });
    if (options.method === 'PUT') {
      const part = Number(url.split('/').at(-1));
      uploaded.set(part, await options.body.text());
      await new Promise(resolve => pending.set(part, resolve));
      return Response.json({ ok: true });
    }
    if (url.endsWith('/process')) {
      processCalls++;
      assert.equal(pending.size, 0);
      assert.equal(uploaded.size, 5);
      return Response.json({ id: 'job' });
    }
    assert.equal(url, '/api/documents/job');
    return Response.json({ status: 'done', artifacts });
  });
  const result = extract(new File(['%PDFabcdefghijklm'], 'test.pdf'), 'cartao', 'team', (_, value) => progress.push(value));
  await new Promise(setImmediate);
  assert.deepEqual([...pending.keys()], [0, 1, 2]);
  for (const part of [2, 3, 4, 1, 0]) {
    maximumConcurrent = Math.max(maximumConcurrent, pending.size);
    assert.equal(processCalls, 0);
    const resolve = pending.get(part);
    assert.ok(resolve);
    pending.delete(part);
    resolve();
    await new Promise(setImmediate);
  }
  assert.deepEqual(await result, artifacts);
  assert.equal(maximumConcurrent, 3);
  assert.equal(processCalls, 1);
  assert.equal([...uploaded].sort(([a], [b]) => a - b).map(([, body]) => body).join(''), '%PDFabcdefghijklm');
  assert.deepEqual(progress, [...progress].sort((a, b) => a - b));
  assert.equal(progress.at(-1), 15);
});

test('waits for in-flight uploads before cleanup and stops scheduling after failure', async (t) => {
  const pending = new Map();
  const started = [];
  let deleted = false;
  t.mock.method(globalThis, 'fetch', async (url, options) => {
    if (url === '/api/uploads') return Response.json({ id: 'upload', chunk_size: 4 });
    if (options.method === 'PUT') {
      const part = Number(url.split('/').at(-1));
      started.push(part);
      return await new Promise(resolve => pending.set(part, resolve));
    }
    assert.equal(options.method, 'DELETE');
    assert.equal(pending.size, 0);
    deleted = true;
    return Response.json({ ok: true });
  });
  const result = extract(new File(['%PDFabcdefghijklm'], 'test.pdf'), 'cartao', 'team', () => {});
  const rejected = assert.rejects(result, { status: 413 });
  await new Promise(setImmediate);
  const failed = pending.get(1);
  pending.delete(1);
  failed(new Response('{}', { status: 413 }));
  await new Promise(setImmediate);
  assert.equal(deleted, false);
  for (const [part, resolve] of pending) {
    pending.delete(part);
    resolve(Response.json({ ok: true }));
  }
  await rejected;
  assert.deepEqual(started, [0, 1, 2]);
  assert.equal(deleted, true);
});
