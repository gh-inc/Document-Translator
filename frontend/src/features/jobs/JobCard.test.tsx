import { StrictMode } from 'react';
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { MemoryRouter, Route, Routes } from 'react-router-dom';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { api } from '../../api/client';
import { ApiError } from '../../api/errors';
import type { JobSummaryResponse } from '../../api/types';
import JobCard from './JobCard';
import JobPage from './JobPage';
import BatchPage from './BatchPage';

vi.mock('../../api/client', async (original) => ({ ...await original<typeof import('../../api/client')>(), api: { getJob: vi.fn(), retryJob: vi.fn(), download: vi.fn(), getDocument: vi.fn(), getBatch: vi.fn() } }));

const job: JobSummaryResponse = { id: 'job-1', document_id: 'doc-1', batch_id: 'batch-1', target_language: 'de', status: 'queued', total_chunks: 4, done_chunks: 0, cache_hit_blocks: 0, cache_miss_blocks: 0, cost_usd: 0, error: null };
class Events {
  static all: Events[] = [];
  static maxLive = 0;
  static maxPerJob = 0;
  closed = false;
  handlers = new Map<string, ((event: Event) => void)[]>();
  constructor(public url: string) {
    Events.all.push(this);
    Events.maxLive = Math.max(Events.maxLive, Events.all.filter(e => !e.closed).length);
    Events.maxPerJob = Math.max(Events.maxPerJob, Events.all.filter(e => !e.closed && e.url === url).length);
  }
  addEventListener(name: string, handler: EventListener) { this.handlers.set(name, [...this.handlers.get(name) ?? [], handler]); }
  close() { this.closed = true; }
  emit(name: string, changes?: Partial<JobSummaryResponse>) {
    const event = changes ? new MessageEvent(name, { data: JSON.stringify({ job_id: changes.id ?? job.id, event: name, ...job, ...changes }) }) : new Event(name);
    this.handlers.get(name)?.forEach(handler => handler(event));
  }
}
const currentSource = () => Events.all.filter(e => !e.closed).at(-1)!;
afterEach(() => { cleanup(); vi.useRealTimers(); vi.unstubAllGlobals(); });

beforeEach(() => {
  vi.clearAllMocks(); Events.all = []; Events.maxLive = 0; Events.maxPerJob = 0;
  vi.stubGlobal('EventSource', Events);
  vi.mocked(api.getJob).mockResolvedValue(job);
  vi.mocked(api.getDocument).mockResolvedValue({ id: 'doc-1', filename: 'report.pdf', format: 'pdf', status: 'extracted', block_count: 4, analysis_cost_usd: 0 });
});

describe('job progress', () => {
  const manyJobs = Array.from({ length: 8 }, (_, index) => ({ ...job, id: `job-${index + 1}`, target_language: `language-${index + 1}` }));
  const renderMany = () => render(<StrictMode>{manyJobs.map(item => <JobCard key={item.id} jobId={item.id} />)}</StrictMode>);
  const flush = async () => { await act(async () => { await Promise.resolve(); }); };

  it('caps eight active cards at four streams and polls overflow jobs without overlap', async () => {
    vi.useFakeTimers();
    vi.mocked(api.getJob).mockImplementation(async id => manyJobs.find(item => item.id === id)!);
    const view = renderMany();
    await flush();
    expect(Events.maxLive).toBe(4);
    expect(Events.maxPerJob).toBe(1);
    expect(Events.all.filter(item => !item.closed)).toHaveLength(4);
    vi.mocked(api.getJob).mockImplementation(async id => ({ ...manyJobs.find(item => item.id === id)!, status: 'running', done_chunks: 2, cost_usd: 0.25 }));
    await act(async () => { await vi.advanceTimersByTimeAsync(1500); });
    expect(screen.getAllByText('2 / 4 chunks')).toHaveLength(4);
    expect(screen.getAllByText('$0.2500')).toHaveLength(4);
    act(() => Events.all.filter(item => !item.closed).forEach(item => item.emit('progress', { id: item.url.split('/')[3], status: 'running', done_chunks: 2, cost_usd: 0.25 })));
    expect(screen.getAllByText('2 / 4 chunks')).toHaveLength(8);
    let resolve!: (value: JobSummaryResponse) => void;
    vi.mocked(api.getJob).mockImplementation(id => new Promise(r => { if (id === 'job-5') resolve = r; }));
    await act(async () => { await vi.advanceTimersByTimeAsync(1500); });
    const calls = vi.mocked(api.getJob).mock.calls.length;
    await act(async () => { await vi.advanceTimersByTimeAsync(4500); });
    expect(api.getJob).toHaveBeenCalledTimes(calls);
    view.unmount();
    expect(vi.getTimerCount()).toBe(0);
    expect(Events.all.every(item => item.closed)).toBe(true);
    expect(vi.mocked(api.getJob).mock.calls.slice(-4).every(([, signal]) => signal?.aborted)).toBe(true);
    await act(async () => resolve(manyJobs[4]));
  });

  it('promotes waiting jobs after terminal and unmount, aborting their pending poll', async () => {
    vi.useFakeTimers();
    vi.mocked(api.getJob).mockImplementation(async id => manyJobs.find(item => item.id === id)!);
    const view = renderMany();
    await flush();
    let resolve!: (value: JobSummaryResponse) => void;
    vi.mocked(api.getJob).mockImplementation(id => id === 'job-5' ? new Promise(r => { resolve = r; }) : Promise.resolve(manyJobs.find(item => item.id === id)!));
    await act(async () => { await vi.advanceTimersByTimeAsync(1500); });
    const pollSignal = vi.mocked(api.getJob).mock.calls.filter(([id]) => id === 'job-5').at(-1)![1];
    act(() => Events.all.find(item => item.url.includes('/job-1/'))!.emit('done', { id: 'job-1', status: 'done', done_chunks: 4 }));
    expect(pollSignal?.aborted).toBe(true);
    expect(Events.all.filter(item => !item.closed).map(item => item.url)).toContain('/api/jobs/job-5/events');
    const promoted = Events.all.find(item => item.url.includes('/job-5/'))!;
    act(() => promoted.emit('progress', { id: 'job-5', status: 'running', done_chunks: 3 }));
    await act(async () => resolve(manyJobs[4]));
    expect(screen.getByText('3 / 4 chunks')).toBeInTheDocument();
    view.rerender(<StrictMode>{manyJobs.slice(1).map(item => <JobCard key={item.id} jobId={item.id} />)}</StrictMode>);
    view.rerender(<StrictMode>{manyJobs.slice(2).map(item => <JobCard key={item.id} jobId={item.id} />)}</StrictMode>);
    expect(Events.all.filter(item => !item.closed).map(item => item.url)).toContain('/api/jobs/job-6/events');
    expect(Events.maxLive).toBe(4);
    view.unmount();
    expect(vi.getTimerCount()).toBe(0);
    const single = render(<JobCard jobId="job-8" />);
    await flush();
    expect(Events.all.filter(item => !item.closed)).toHaveLength(1);
    single.unmount();
  });

  it('stops polling terminal overflow jobs and leaves history snapshots idle', async () => {
    vi.useFakeTimers();
    vi.mocked(api.getJob).mockImplementation(async id => manyJobs.find(item => item.id === id)!);
    const view = renderMany();
    await flush();
    vi.mocked(api.getJob).mockImplementation(async id => ({ ...manyJobs.find(item => item.id === id)!, status: 'done', done_chunks: 4 }));
    await act(async () => { await vi.advanceTimersByTimeAsync(1500); });
    expect(screen.getAllByText('Completed')).toHaveLength(4);
    expect(vi.getTimerCount()).toBe(0);
    view.unmount();
    vi.mocked(api.getJob).mockClear();
    render(<>{manyJobs.map(item => <JobCard key={item.id} jobId={item.id} initialJob={item} live={false} />)}</>);
    await act(async () => { await vi.advanceTimersByTimeAsync(4500); });
    expect(api.getJob).not.toHaveBeenCalled();
    expect(Events.all.every(item => item.closed)).toBe(true);
    expect(vi.getTimerCount()).toBe(0);
  });
  it('fetches on entry and applies named status, progress, cost, and terminal events', async () => {
    render(<JobCard jobId={job.id} />);
    expect(await screen.findByText('Queued')).toBeInTheDocument();
    act(() => currentSource().emit('status', { status: 'running' }));
    expect(screen.getByText('Translating')).toBeInTheDocument();
    act(() => currentSource().emit('progress', { status: 'running', done_chunks: 2, cache_hit_blocks: 4, cache_miss_blocks: 1, cost_usd: 0.1234 }));
    expect(screen.getByText('2 / 4 chunks')).toBeInTheDocument();
    expect(screen.getByText('$0.1234')).toBeInTheDocument();
    expect(screen.getByText('80% cached')).toBeInTheDocument();
    expect(screen.getByLabelText('4 of 5 blocks served from the translation cache')).toBeInTheDocument();
    act(() => currentSource().emit('status', { status: 'assembling', done_chunks: 4 }));
    expect(screen.getByText('Preparing document')).toBeInTheDocument();
    act(() => currentSource().emit('done', { status: 'done', done_chunks: 4 }));
    expect(screen.getByRole('button', { name: 'Download translation' })).toBeInTheDocument();
    expect(Events.all.every(e => e.closed)).toBe(true);
  });

  it('hides the cache percentage before any block lookup', async () => {
    render(<JobCard jobId={job.id} />);
    await screen.findByText('Queued');
    expect(screen.queryByText(/% cached/)).not.toBeInTheDocument();
    expect(screen.queryByText(/NaN%/)).not.toBeInTheDocument();
  });

  it('keeps at most one live connection in StrictMode and closes on unmount', async () => {
    const view = render(<StrictMode><JobCard jobId={job.id} /></StrictMode>);
    await screen.findByText('Queued');
    expect(Events.maxLive).toBe(1);
    view.unmount();
    expect(Events.all.every(e => e.closed)).toBe(true);
  });

  it('keeps a terminal result when an already queued event arrives after closing', async () => {
    render(<JobCard jobId={job.id} />);
    await screen.findByText('Queued');
    const source = currentSource();
    act(() => source.emit('done', { status: 'done', done_chunks: 4 }));
    act(() => source.emit('progress', { status: 'running', done_chunks: 1 }));
    expect(screen.getByText('Completed')).toBeInTheDocument();
    expect(screen.getByText('4 / 4 chunks')).toBeInTheDocument();
  });

  it('refetches on reconnect and prevents a stale GET from overwriting later SSE', async () => {
    render(<JobCard jobId={job.id} />);
    await screen.findByText('Queued');
    let resolve!: (value: JobSummaryResponse) => void;
    vi.mocked(api.getJob).mockReturnValueOnce(new Promise(r => { resolve = r; }));
    act(() => currentSource().emit('error'));
    expect(screen.getByText(/Reconnecting/)).toBeInTheDocument();
    act(() => currentSource().emit('open'));
    act(() => currentSource().emit('progress', { status: 'running', done_chunks: 3, cost_usd: 0.3 }));
    await act(async () => resolve(job));
    expect(screen.getByText('3 / 4 chunks')).toBeInTheDocument();
    expect(api.getJob).toHaveBeenCalledTimes(2);
  });

  it('shows partial source-text warning, safe named job error, and retry', async () => {
    render(<JobCard jobId={job.id} />);
    await screen.findByText('Queued');
    act(() => currentSource().emit('error', { status: 'completed_with_errors', error: { error_code: 'provider_timeout', message: 'raw secret', retryable: true } }));
    expect(screen.getByText(/Untranslated blocks remain in the source language/)).toBeInTheDocument();
    expect(screen.getByText('Translation provider timed out')).toBeInTheDocument();
    expect(screen.queryByText('raw secret')).not.toBeInTheDocument();
    vi.mocked(api.retryJob).mockResolvedValue(job);
    fireEvent.click(screen.getByRole('button', { name: 'Retry translation' }));
    await waitFor(() => expect(screen.getByText('Queued')).toBeInTheDocument());
    expect(api.retryJob).toHaveBeenCalledWith(job.id, expect.any(AbortSignal));
  });

  it('downloads DOCX with a useful filename and revokes its object URL', async () => {
    vi.mocked(api.getJob).mockResolvedValue({ ...job, status: 'done' });
    vi.mocked(api.download).mockResolvedValue(new Blob(['translated'], { type: 'application/vnd.openxmlformats-officedocument.wordprocessingml.document' }));
    const create = vi.fn(() => 'blob:translation'); const revoke = vi.fn();
    vi.stubGlobal('URL', { createObjectURL: create, revokeObjectURL: revoke });
    const click = vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(function (this: HTMLAnchorElement) { expect(this.download).toMatch(/de.*\.docx$/); });
    render(<JobCard jobId={job.id} />);
    fireEvent.click(await screen.findByRole('button', { name: 'Download translation' }));
    await waitFor(() => expect(revoke).toHaveBeenCalledWith('blob:translation'));
    expect(click).toHaveBeenCalledOnce(); expect(Events.all).toHaveLength(0);
  });

  it('supports reload after a safe load error', async () => {
    vi.mocked(api.getJob).mockRejectedValueOnce(new ApiError('not_found'));
    render(<JobCard jobId={job.id} />);
    expect(await screen.findByRole('alert')).toHaveTextContent('Requested resource was not found');
    fireEvent.click(screen.getByRole('button', { name: 'Reload job' }));
    expect(await screen.findByText('Queued')).toBeInTheDocument();
  });

  it('ignores old job fetches after the job ID changes', async () => {
    let resolve!: (value: JobSummaryResponse) => void;
    vi.mocked(api.getJob).mockReturnValueOnce(new Promise(r => { resolve = r; }));
    const view = render(<JobCard jobId={job.id} />);
    vi.mocked(api.getJob).mockResolvedValue({ ...job, id: 'job-2', target_language: 'sv' });
    view.rerender(<JobCard jobId="job-2" />);
    await screen.findByRole('heading', { name: 'sv' });
    await act(async () => resolve(job));
    expect(screen.queryByRole('heading', { name: 'de' })).not.toBeInTheDocument();
    expect(currentSource().url).toBe('/api/jobs/job-2/events');
  });

  it('history mode displays terminal actions without fetching or opening events', async () => {
    render(<JobCard jobId={job.id} initialJob={{ ...job, status: 'failed' }} live={false} />);
    expect(screen.getByText('Failed')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Retry translation' })).toBeInTheDocument();
    expect(api.getJob).not.toHaveBeenCalled(); expect(Events.all).toHaveLength(0);
  });

  it('ignores malformed and unrelated events while preserving progress', async () => {
    render(<JobCard jobId={job.id} />);
    await screen.findByText('Queued');
    act(() => {
      const handlers = currentSource().handlers.get('progress')!;
      for (const data of ['not json', JSON.stringify({ ...job, job_id: 'other', event: 'progress' }), JSON.stringify({ ...job, job_id: job.id, event: 'done' })]) handlers.forEach(handler => handler(new MessageEvent('progress', { data })));
    });
    expect(screen.getByText('0 / 4 chunks')).toBeInTheDocument();
    expect(screen.getByText('Queued')).toBeInTheDocument();
  });

  it('reports a safe retry error and leaves the action available', async () => {
    vi.mocked(api.getJob).mockResolvedValue({ ...job, status: 'failed' });
    vi.mocked(api.retryJob).mockRejectedValue(new ApiError('conflict'));
    render(<JobCard jobId={job.id} />);
    fireEvent.click(await screen.findByRole('button', { name: 'Retry translation' }));
    expect(await screen.findByRole('alert')).toHaveTextContent('Request conflicts with the resource state');
    expect(screen.getByRole('button', { name: 'Retry translation' })).toBeEnabled();
  });

  it('does not download an async result after unmount', async () => {
    vi.mocked(api.getJob).mockResolvedValue({ ...job, status: 'done' });
    let resolve!: (blob: Blob) => void;
    vi.mocked(api.download).mockReturnValueOnce(new Promise(r => { resolve = r; }));
    const create = vi.fn(); vi.stubGlobal('URL', { createObjectURL: create });
    const view = render(<JobCard jobId={job.id} />);
    fireEvent.click(await screen.findByRole('button', { name: 'Download translation' }));
    view.unmount();
    await act(async () => resolve(new Blob(['translated'], { type: 'application/pdf' })));
    expect(create).not.toHaveBeenCalled();
  });

  it('uses document format for binary downloads with an unspecified MIME type', async () => {
    vi.mocked(api.getJob).mockResolvedValue({ ...job, status: 'done' });
    vi.mocked(api.download).mockResolvedValue(new Blob(['translated']));
    vi.mocked(api.getDocument).mockResolvedValue({ id: 'doc-1', filename: 'report.docx', format: 'docx', status: 'extracted', block_count: 4, analysis_cost_usd: 0 });
    const revoke = vi.fn(); vi.stubGlobal('URL', { createObjectURL: () => 'blob:binary', revokeObjectURL: revoke });
    vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(function (this: HTMLAnchorElement) { expect(this.download).toMatch(/\.docx$/); });
    render(<JobCard jobId={job.id} />);
    fireEvent.click(await screen.findByRole('button', { name: 'Download translation' }));
    await waitFor(() => expect(revoke).toHaveBeenCalledWith('blob:binary'));
    expect(api.getDocument).toHaveBeenCalledWith('doc-1', expect.any(AbortSignal));
  });

  it('uses Markdown MIME and document format to name Markdown downloads', async () => {
    vi.mocked(api.getJob).mockResolvedValue({ ...job, status: 'done' });
    vi.mocked(api.download).mockResolvedValue(new Blob(['# Übersetzung'], { type: 'text/markdown' }));
    const create = vi.fn(() => 'blob:markdown'); const revoke = vi.fn();
    vi.stubGlobal('URL', { createObjectURL: create, revokeObjectURL: revoke });
    const click = vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(function (this: HTMLAnchorElement) { expect(this.download).toMatch(/\.md$/); });
    const view = render(<JobCard jobId={job.id} />);
    fireEvent.click(await screen.findByRole('button', { name: 'Download translation' }));
    await waitFor(() => expect(revoke).toHaveBeenCalledWith('blob:markdown'));
    expect(click).toHaveBeenCalledOnce();

    view.unmount();
    vi.mocked(api.download).mockResolvedValue(new Blob(['# Übersetzung']));
    vi.mocked(api.getDocument).mockResolvedValue({ id: 'doc-1', filename: 'report.md', format: 'md', status: 'extracted', block_count: 4, analysis_cost_usd: 0 });
    const fallback = render(<JobCard jobId={job.id} />);
    fireEvent.click(await screen.findByRole('button', { name: 'Download translation' }));
    await waitFor(() => expect(api.getDocument).toHaveBeenCalledWith('doc-1', expect.any(AbortSignal)));
    expect(click).toHaveBeenCalledTimes(2);
    fallback.unmount();
  });

  it('reloads a batch after a safe error', async () => {
    vi.mocked(api.getBatch).mockRejectedValueOnce(new ApiError('not_found')).mockResolvedValue({ batch_id: 'batch-1', jobs: [] });
    render(<MemoryRouter initialEntries={['/batches/batch-1']}><Routes><Route path="/batches/:batchId" element={<BatchPage />} /></Routes></MemoryRouter>);
    expect(await screen.findByRole('alert')).toHaveTextContent('Requested resource was not found');
    fireEvent.click(screen.getByRole('button', { name: 'Reload batch' }));
    expect(await screen.findByText('This batch has no translation jobs.')).toBeInTheDocument();
  });

  it('loads a direct job route and independent cards for a batch route', async () => {
    const view = render(<MemoryRouter initialEntries={['/jobs/job-1']}><Routes><Route path="/jobs/:jobId" element={<JobPage />} /></Routes></MemoryRouter>);
    expect(await screen.findByText('Queued')).toBeInTheDocument();
    view.unmount();
    vi.mocked(api.getBatch).mockResolvedValue({ batch_id: 'batch-1', jobs: [job, { ...job, id: 'job-2', target_language: 'sv' }] });
    vi.mocked(api.getJob).mockImplementation(async (id) => ({ ...job, id, target_language: id === job.id ? 'de' : 'sv' }));
    render(<MemoryRouter initialEntries={['/batches/batch-1']}><Routes><Route path="/batches/:batchId" element={<BatchPage />} /></Routes></MemoryRouter>);
    await screen.findByRole('heading', { name: 'sv' });
    await waitFor(() => expect(Events.all.filter(e => !e.closed)).toHaveLength(2));
  });
});
