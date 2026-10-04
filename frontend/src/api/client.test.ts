import { afterEach, describe, expect, it, vi } from 'vitest';
import { api, parseServerSentEvent } from './client';
import { ApiError, getErrorMessage, isAbortError } from './errors';

const documentResponse = { id: 'doc', filename: 'report.pdf', format: 'pdf', status: 'analyzing', block_count: 4, analysis_cost_usd: 0 };
const job = { id: 'job', document_id: 'doc', batch_id: 'batch', target_language: 'de', status: 'running', total_chunks: 4, done_chunks: 1, cache_hit_blocks: 3, cache_miss_blocks: 1, cost_usd: 0.01, analysis_cost_usd: 0.0014, error: null };
const batch = { batch_id: 'batch', jobs: [job] };
const json = (value: unknown, status = 200) => new Response(JSON.stringify(value), { status, headers: { 'Content-Type': 'application/json' } });

afterEach(() => vi.unstubAllGlobals());

describe('API client', () => {
  it('uploads multipart data without overriding its boundary and forwards cancellation', async () => {
    const signal = new AbortController().signal;
    const file = new File(['%PDF-'], 'report.pdf');
    vi.stubGlobal('fetch', async (url: string, init: RequestInit) => {
      expect(url).toBe('/api/documents');
      expect(init.method).toBe('POST');
      expect(init.signal).toBe(signal);
      expect(init.headers).toBeUndefined();
      expect((init.body as FormData).get('file')).toBe(file);
      return json(documentResponse);
    });
    expect(await api.uploadDocument(file, signal)).toEqual(documentResponse);
  });

  it('uses the existing contracts for document polling, retry and job operations', async () => {
    const signal = new AbortController().signal;
    const expected = [
      ['/api/documents/doc%2Fid', 'GET', undefined, documentResponse],
      ['/api/documents/doc/retry-triage', 'POST', undefined, documentResponse],
      ['/api/jobs', 'POST', JSON.stringify({ document_id: 'doc', target_languages: ['de'], idempotency_key: 'key' }), batch],
      ['/api/jobs/job', 'GET', undefined, job],
      ['/api/jobs/job/retry', 'POST', '{}', job],
      ['/api/batches/batch', 'GET', undefined, batch],
      ['/api/jobs?limit=10', 'GET', undefined, [job]],
    ];
    let index = 0;
    vi.stubGlobal('fetch', async (url: string, init: RequestInit) => {
      const [path, method, body, response] = expected[index++];
      expect([url, init.method, init.body]).toEqual([path, method, body]);
      expect(init.signal).toBe(signal);
      if (body) expect(new Headers(init.headers).get('Content-Type')).toBe('application/json');
      return json(response);
    });
    expect(await api.getDocument('doc/id', signal)).toEqual(documentResponse);
    expect(await api.retryTriage('doc', signal)).toEqual(documentResponse);
    expect(await api.createJobs({ document_id: 'doc', target_languages: ['de'], idempotency_key: 'key' }, signal)).toEqual(batch);
    expect(await api.getJob('job', signal)).toEqual(job);
    expect(await api.retryJob('job', signal)).toEqual(job);
    expect(await api.getBatch('batch', signal)).toEqual(batch);
    expect(await api.listRecentJobs(10, signal)).toEqual([job]);
  });

  it.each([
    { error_code: 'scanned_pdf', message: 'Traceback: /private/secret', retryable: false },
    { error_code: 'provider_timeout', message: 'raw provider body', retryable: true },
  ])('normalizes catalogued errors without exposing server text ($error_code)', async (envelope) => {
    vi.stubGlobal('fetch', async () => json(envelope, 422));
    const error = await api.getJob('job').catch((failure: unknown) => failure);
    expect(error).toBeInstanceOf(ApiError);
    expect(error).toMatchObject({ error_code: envelope.error_code, retryable: envelope.retryable, status: 422 });
    expect(getErrorMessage(error)).not.toContain(envelope.message);
    expect(getErrorMessage(error).length).toBeGreaterThan(10);
  });

  it.each([
    () => new Response('Traceback: private database path', { status: 500 }),
    () => json({ error_code: 'unknown_secret', message: 'private', retryable: true }, 500),
    () => json({ error_code: 'scanned_pdf', message: 8, retryable: false }, 422),
  ])('keeps malformed error responses safe', async (response) => {
    vi.stubGlobal('fetch', async () => response());
    await expect(api.getJob('job')).rejects.toMatchObject({ error_code: 'internal_error', message: 'Internal server error' });
  });

  it.each([null, {}, { ...job, status: 'mystery' }, { ...job, status: { toString: 'private' } }, { ...job, done_chunks: '1' }, { ...job, cache_hit_blocks: '12' }, { ...job, cache_miss_blocks: -1 }, { ...job, cache_hit_blocks: Number.MAX_SAFE_INTEGER + 1 }, { ...job, error: { error_code: 'scanned_pdf' } }, { ...job, analysis_cost_usd: '0.0014' }, { ...job, analysis_cost_usd: -1 }, { ...job, analysis_cost_usd: Number.POSITIVE_INFINITY }, { ...job, analysis_cost_usd: undefined }, { ...job, unexpected: 'private' }])('rejects malformed successful job payloads', async (payload) => {
    vi.stubGlobal('fetch', async () => json(payload));
    await expect(api.getJob('job')).rejects.toMatchObject({ error_code: 'internal_error', kind: 'invalid_response' });
  });

  it('rejects unknown job fields in batch and history responses', async () => {
    vi.stubGlobal('fetch', async () => json({ ...batch, jobs: [{ ...job, unexpected: true }] }));
    await expect(api.getBatch('batch')).rejects.toMatchObject({ kind: 'invalid_response' });
    vi.stubGlobal('fetch', async () => json([{ ...job, unexpected: true }]));
    await expect(api.listRecentJobs()).rejects.toMatchObject({ kind: 'invalid_response' });
  });

  it('rejects invalid JSON and malformed document and list responses', async () => {
    vi.stubGlobal('fetch', async () => new Response('raw private error'));
    await expect(api.getJob('job')).rejects.toBeInstanceOf(ApiError);
    vi.stubGlobal('fetch', async () => json({ ...documentResponse, block_count: -1 }));
    await expect(api.getDocument('doc')).rejects.toBeInstanceOf(ApiError);
    vi.stubGlobal('fetch', async () => json([job, {}]));
    await expect(api.listRecentJobs()).rejects.toBeInstanceOf(ApiError);
    vi.stubGlobal('fetch', async () => json({ batch_id: 'batch', jobs: [null] }));
    await expect(api.getBatch('batch')).rejects.toBeInstanceOf(ApiError);
  });

  it.each([
    { ...documentResponse, analysis_cost_usd: '0.01' },
    { ...documentResponse, analysis_cost_usd: -1 },
    { ...documentResponse, analysis_cost_usd: Number.POSITIVE_INFINITY },
    { ...documentResponse, analysis_cost_usd: undefined },
  ])('rejects malformed document analysis cost payloads', async (payload) => {
    vi.stubGlobal('fetch', async () => json(payload));
    await expect(api.getDocument('doc')).rejects.toMatchObject({ error_code: 'internal_error', kind: 'invalid_response' });
  });

  it('sanitizes persisted job errors as well as request errors', async () => {
    vi.stubGlobal('fetch', async () => json({ ...job, error: { error_code: 'cost_cap_exceeded', message: 'raw secret', retryable: false } }));
    expect((await api.getJob('job')).error).toEqual({ error_code: 'cost_cap_exceeded', message: 'Job translation cost limit reached', retryable: false });
  });

  it('replaces raw network exception messages with a safe retryable failure', async () => {
    vi.stubGlobal('fetch', async () => { throw new TypeError('secret URL with credentials'); });
    const error = await api.getJob('job').catch((failure: unknown) => failure);
    expect(error).toMatchObject({ kind: 'network', retryable: true });
    expect(getErrorMessage(error)).not.toContain('secret');
    expect(getErrorMessage(new Error('raw stack'))).toBe('Internal server error');
  });

  it('exposes cancellation separately from network failure', async () => {
    const controller = new AbortController();
    controller.abort('private abort reason');
    vi.stubGlobal('fetch', async () => { throw new Error('private abort reason'); });
    const error = await api.getJob('job', controller.signal).catch((failure: unknown) => failure);
    expect(isAbortError(error)).toBe(true);
    expect(getErrorMessage(error)).not.toContain('private');
    expect(isAbortError(new DOMException('cancelled', 'AbortError'))).toBe(true);
  });

  it('downloads binary data and rejects server errors before returning a blob', async () => {
    const signal = new AbortController().signal;
    vi.stubGlobal('fetch', async (url: string, init: RequestInit) => {
      expect(url).toBe('/api/jobs/job/download');
      expect(init.signal).toBe(signal);
      return new Response('%PDF-document', { headers: { 'Content-Type': 'application/pdf' } });
    });
    const blob = await api.download('job', signal);
    const content = await new Promise<string>((resolve, reject) => {
      const reader = new FileReader();
      reader.onload = () => resolve(String(reader.result));
      reader.onerror = () => reject(reader.error);
      reader.readAsText(blob);
    });
    expect(content).toBe('%PDF-document');
    vi.stubGlobal('fetch', async () => json({ error_code: 'conflict', message: 'private', retryable: false }, 409));
    await expect(api.download('job')).rejects.toMatchObject({ error_code: 'conflict', retryable: false });
  });

  it('validates SSE JSON and sanitizes its errors', () => {
    const event = { job_id: 'job', event: 'progress', status: 'running', done_chunks: 1, total_chunks: 4, cache_hit_blocks: 3, cache_miss_blocks: 1, cost_usd: 0.01, error: null };
    expect(parseServerSentEvent(JSON.stringify(event))).toEqual(event);
    expect(parseServerSentEvent(JSON.stringify({ ...event, analysis_cost_usd: 0.0014 }))).toEqual(event);
    expect(parseServerSentEvent('raw exception')).toBeNull();
    expect(parseServerSentEvent(JSON.stringify({ ...event, status: 'unknown' }))).toBeNull();
    expect(parseServerSentEvent(JSON.stringify({ ...event, cache_hit_blocks: '3' }))).toBeNull();
    expect(parseServerSentEvent(JSON.stringify({ ...event, cache_miss_blocks: -1 }))).toBeNull();
    expect(parseServerSentEvent(JSON.stringify({ ...event, error: { error_code: 'unknown', message: 'private', retryable: true } }))?.error?.message).toBe('Internal server error');
  });

  it('keeps cancellation recognizable when a download body finishes after abort', async () => {
    const controller = new AbortController();
    vi.stubGlobal('fetch', async () => {
      const response = new Response('%PDF-document');
      response.blob = async () => {
        controller.abort();
        return new Blob(['%PDF-document']);
      };
      return response;
    });
    const error = await api.download('job', controller.signal).catch((failure: unknown) => failure);
    expect(isAbortError(error)).toBe(true);
  });
});
