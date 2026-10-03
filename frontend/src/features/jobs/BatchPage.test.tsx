import { act, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { MemoryRouter, Route, Routes, useNavigate } from 'react-router-dom';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { api } from '../../api/client';
import type { BatchResponse, DocumentUploadResponse, JobSummaryResponse } from '../../api/types';
import BatchPage from './BatchPage';

vi.mock('../../api/client', () => ({ api: { getBatch: vi.fn(), getDocument: vi.fn(), getJob: vi.fn(), retryJob: vi.fn(), download: vi.fn() } }));

const job = (id: string, documentId = 'doc-1'): JobSummaryResponse => ({
  id,
  document_id: documentId,
  batch_id: id.startsWith('batch-2') ? 'batch-2' : 'batch-1',
  target_language: id,
  status: 'done',
  total_chunks: 1,
  done_chunks: 1,
  cache_hit_blocks: 0,
  cache_miss_blocks: 1,
  cost_usd: 0.001,
  error: null,
});

const document = (analysisCost: number): DocumentUploadResponse => ({
  id: 'doc-1', filename: 'report.pdf', format: 'pdf', status: 'extracted', block_count: 4, analysis_cost_usd: analysisCost,
});

const batch = (jobs: JobSummaryResponse[]): BatchResponse => ({ batch_id: 'batch-1', jobs });

function renderBatchPage() {
  return render(<MemoryRouter initialEntries={['/batches/batch-1']}>
    <Routes><Route path="/batches/:batchId" element={<BatchPage />} /></Routes>
  </MemoryRouter>);
}

function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((fulfill) => { resolve = fulfill; });
  return { promise, resolve };
}

beforeEach(() => {
  vi.clearAllMocks();
  vi.stubGlobal('EventSource', vi.fn());
  vi.mocked(api.getJob).mockImplementation(async (id) => id === 'job-2' ? job('job-2') : job('job-1'));
});

describe('batch document analysis cost', () => {
  it('shows the document analysis cost once for the batch', async () => {
    vi.mocked(api.getBatch).mockResolvedValue(batch([job('job-1'), job('job-2')]));
    vi.mocked(api.getDocument).mockResolvedValue(document(0.0042));

    renderBatchPage();

    expect(await screen.findByText((_, element) => element?.tagName === 'P' && element.textContent === 'Document analysis: $0.0042')).toBeInTheDocument();
    expect(screen.getAllByText(/^Cost:/)).toHaveLength(2);
    expect(api.getDocument).toHaveBeenCalledTimes(1);
    expect(api.getDocument).toHaveBeenCalledWith('doc-1', expect.any(AbortSignal));
  });

  it('still renders the batch when the document request fails', async () => {
    vi.mocked(api.getBatch).mockResolvedValue(batch([job('job-1'), job('job-2')]));
    vi.mocked(api.getDocument).mockRejectedValue(new Error('offline'));

    renderBatchPage();

    expect(await screen.findByRole('heading', { name: 'job-1' })).toBeInTheDocument();
    expect(screen.getByRole('heading', { name: 'job-2' })).toBeInTheDocument();
    await waitFor(() => expect(api.getDocument).toHaveBeenCalledTimes(1));
    expect(screen.queryByText(/^Document analysis:/)).not.toBeInTheDocument();
  });

  it('ignores a document response from the previous batch after navigation', async () => {
    const firstDocument = deferred<DocumentUploadResponse>();
    const secondDocument = deferred<DocumentUploadResponse>();
    vi.mocked(api.getBatch).mockImplementation(async (id) => id === 'batch-1'
      ? batch([job('batch-1-job', 'doc-1')])
      : { batch_id: 'batch-2', jobs: [job('batch-2-job', 'doc-2')] });
    vi.mocked(api.getDocument).mockImplementation((id) => id === 'doc-1' ? firstDocument.promise : secondDocument.promise);
    vi.mocked(api.getJob).mockImplementation(async (id) => job(id, id === 'batch-2-job' ? 'doc-2' : 'doc-1'));

    function NavigableBatch() {
      const navigate = useNavigate();
      return <><button type="button" onClick={() => navigate('/batches/batch-2')}>Open second batch</button><BatchPage /></>;
    }

    render(<MemoryRouter initialEntries={['/batches/batch-1']}>
      <Routes><Route path="/batches/:batchId" element={<NavigableBatch />} /></Routes>
    </MemoryRouter>);

    await waitFor(() => expect(api.getDocument).toHaveBeenCalledTimes(1));
    const oldSignal = vi.mocked(api.getDocument).mock.calls[0][1];
    fireEvent.click(screen.getByRole('button', { name: 'Open second batch' }));
    await waitFor(() => expect(api.getDocument).toHaveBeenCalledTimes(2));
    expect(oldSignal?.aborted).toBe(true);

    await act(async () => secondDocument.resolve({ ...document(0.009), id: 'doc-2' }));
    expect(await screen.findByText((_, element) => element?.tagName === 'P' && element.textContent === 'Document analysis: $0.0090')).toBeInTheDocument();
    await act(async () => firstDocument.resolve(document(0.0042)));
    expect(screen.queryByText((_, element) => element?.tagName === 'P' && element.textContent === 'Document analysis: $0.0042')).not.toBeInTheDocument();
    expect(screen.getByText((_, element) => element?.tagName === 'P' && element.textContent === 'Document analysis: $0.0090')).toBeInTheDocument();
  });
});
