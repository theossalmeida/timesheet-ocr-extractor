import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import test from 'node:test';
import ts from 'typescript';

const source = await readFile(new URL('../src/lib/client.ts', import.meta.url), 'utf8');
const { outputText } = ts.transpileModule(source, { compilerOptions: { module: ts.ModuleKind.ESNext, target: ts.ScriptTarget.ES2022 } });
const { retryUploadRequest } = await import(`data:text/javascript;base64,${Buffer.from(outputText).toString('base64')}`);

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
