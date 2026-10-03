import { StrictMode, type ReactNode } from 'react';
import { act, renderHook } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { api } from '../../api/client';
import { ApiError } from '../../api/errors';
import type { DocumentUploadResponse } from '../../api/types';
import { useTranslationSubmission } from './useTranslationSubmission';

vi.mock('../../api/client', () => ({ api: { uploadDocument: vi.fn(), getDocument: vi.fn(), retryTriage: vi.fn(), createJobs: vi.fn() } }));
const document = (status: DocumentUploadResponse['status'], id = 'doc'): DocumentUploadResponse => ({ id, status, filename: 'file.pdf', format: 'pdf', block_count: 1, analysis_cost_usd: 0 });
const file = new File(['pdf'], 'file.pdf', { type: 'application/pdf' });
const wrapper = ({ children }: { children: ReactNode }) => <StrictMode>{children}</StrictMode>;

describe('translation submission', () => {
  beforeEach(() => {
    vi.useFakeTimers();
    vi.mocked(api.uploadDocument).mockReset().mockResolvedValue(document('analyzing'));
    vi.mocked(api.getDocument).mockReset().mockResolvedValue(document('extracted'));
    vi.mocked(api.retryTriage).mockReset().mockResolvedValue(document('analyzing'));
    vi.mocked(api.createJobs).mockReset().mockResolvedValue({ batch_id: 'batch', jobs: [] });
  });
  afterEach(() => vi.useRealTimers());
  const setup = () => {
    const onComplete = vi.fn();
    const hook = renderHook(() => useTranslationSubmission(onComplete, { pollIntervalMs: 10, analysisTimeoutMs: 100 }), { wrapper });
    return { ...hook, onComplete };
  };

  it('shows analysis and submits exactly once after extraction, including double clicks and StrictMode', async () => {
    vi.mocked(api.getDocument).mockResolvedValueOnce(document('analyzing')).mockResolvedValueOnce(document('extracted'));
    const { result, onComplete } = setup();
    await act(async () => { void result.current.start(file, ['de', 'fr']); void result.current.start(file, ['de']); });
    expect(result.current.phase).toBe('analyzing');
    expect(api.uploadDocument).toHaveBeenCalledTimes(1);
    expect(api.createJobs).not.toHaveBeenCalled();
    await act(() => vi.advanceTimersByTimeAsync(10));
    expect(api.createJobs).not.toHaveBeenCalled();
    await act(() => vi.advanceTimersByTimeAsync(10));
    expect(api.createJobs).toHaveBeenCalledTimes(1);
    expect(api.createJobs).toHaveBeenCalledWith({ document_id: 'doc', target_languages: ['de', 'fr'], idempotency_key: expect.any(String) }, expect.any(AbortSignal));
    expect(onComplete).toHaveBeenCalledWith('batch');
    await act(() => vi.advanceTimersByTimeAsync(100));
    expect(api.createJobs).toHaveBeenCalledTimes(1);
  });

  it('offers recovery after failed analysis without creating a job', async () => {
    vi.mocked(api.getDocument).mockResolvedValue(document('failed'));
    const { result } = setup();
    await act(async () => { void result.current.start(file, ['de']); });
    await act(() => vi.advanceTimersByTimeAsync(10));
    expect(result.current.phase).toBe('failed');
    expect(result.current.message).toContain('Choose another');
    expect(api.createJobs).not.toHaveBeenCalled();
  });

  it('aborts cancellation and ignores stale results when a new upload starts', async () => {
    let resolveOld!: (value: DocumentUploadResponse) => void;
    vi.mocked(api.getDocument).mockReturnValueOnce(new Promise((resolve) => { resolveOld = resolve; }));
    const { result, onComplete } = setup();
    await act(async () => { void result.current.start(file, ['de']); });
    await act(() => vi.advanceTimersByTimeAsync(10));
    const signal = vi.mocked(api.getDocument).mock.calls[0][1];
    act(() => result.current.cancel());
    expect(signal?.aborted).toBe(true);
    expect(result.current.phase).toBe('cancelled');
    vi.mocked(api.uploadDocument).mockResolvedValue(document('extracted', 'new-doc'));
    await act(async () => { await result.current.start(file, ['fr']); resolveOld(document('extracted')); });
    expect(api.createJobs).toHaveBeenCalledTimes(1);
    expect(vi.mocked(api.createJobs).mock.calls[0][0].document_id).toBe('new-doc');
    expect(onComplete).toHaveBeenCalledTimes(1);
  });

  it('bounds a stalled readiness request and retries triage only on explicit action', async () => {
    vi.mocked(api.getDocument).mockReturnValueOnce(new Promise(() => {}));
    const { result } = setup();
    await act(async () => { void result.current.start(file, ['de']); });
    await act(() => vi.advanceTimersByTimeAsync(100));
    expect(result.current.phase).toBe('timeout');
    expect(vi.mocked(api.getDocument).mock.calls[0][1]?.aborted).toBe(true);
    expect(api.retryTriage).not.toHaveBeenCalled();
    expect(api.createJobs).not.toHaveBeenCalled();
    await act(async () => { void result.current.retryAnalysis(); void result.current.retryAnalysis(); });
    expect(api.retryTriage).toHaveBeenCalledTimes(1);
    await act(() => vi.advanceTimersByTimeAsync(10));
    expect(api.createJobs).toHaveBeenCalledTimes(1);
  });

  it('preserves the exact request and key across an explicit submission retry', async () => {
    vi.mocked(api.uploadDocument).mockResolvedValue(document('extracted'));
    vi.mocked(api.createJobs).mockRejectedValueOnce(new ApiError('internal_error', undefined, 'network'));
    const { result } = setup();
    await act(async () => { await result.current.start(file, ['de', 'fr']); });
    expect(result.current.phase).toBe('submission-error');
    const firstRequest = vi.mocked(api.createJobs).mock.calls[0][0];
    await act(() => vi.advanceTimersByTimeAsync(100));
    expect(api.createJobs).toHaveBeenCalledTimes(1);
    await act(async () => { await result.current.retrySubmission(); });
    expect(api.createJobs).toHaveBeenCalledTimes(2);
    expect(vi.mocked(api.createJobs).mock.calls[1][0]).toEqual(firstRequest);
    expect(api.uploadDocument).toHaveBeenCalledTimes(1);
  });

  it('never retries a job POST returning analysis_pending', async () => {
    vi.mocked(api.uploadDocument).mockResolvedValue(document('extracted'));
    vi.mocked(api.createJobs).mockRejectedValue(new ApiError('analysis_pending'));
    const { result } = setup();
    await act(async () => { await result.current.start(file, ['de']); });
    expect(result.current.canRetrySubmission).toBe(false);
    await act(async () => { await result.current.retrySubmission(); await vi.advanceTimersByTimeAsync(100); });
    expect(api.createJobs).toHaveBeenCalledTimes(1);
  });

  it('aborts in-flight requests and ignores late completion on unmount', async () => {
    let resolveUpload!: (value: DocumentUploadResponse) => void;
    vi.mocked(api.uploadDocument).mockReturnValue(new Promise((resolve) => { resolveUpload = resolve; }));
    const { result, unmount, onComplete } = setup();
    await act(async () => { void result.current.start(file, ['de']); });
    const signal = vi.mocked(api.uploadDocument).mock.calls[0][1];
    unmount();
    expect(signal?.aborted).toBe(true);
    await act(async () => resolveUpload(document('extracted')));
    expect(api.createJobs).not.toHaveBeenCalled();
    expect(onComplete).not.toHaveBeenCalled();
  });
});
