import { useCallback, useEffect, useRef, useState } from 'react';
import { api } from '../../api/client';
import { ApiError, getErrorMessage, isAbortError } from '../../api/errors';
import type { DocumentUploadResponse } from '../../api/types';

export type SubmissionPhase = 'idle' | 'uploading' | 'analyzing' | 'submitting' | 'timeout' | 'failed' | 'cancelled' | 'submission-error' | 'complete';
interface SubmissionState { phase: SubmissionPhase; message: string; canRetrySubmission?: boolean }
interface Submission { document: DocumentUploadResponse; languages: string[]; key: string }
interface Run { controller: AbortController; timedOut: boolean }
interface Options { pollIntervalMs?: number; analysisTimeoutMs?: number }

function pause(ms: number, signal: AbortSignal): Promise<void> {
  return new Promise((resolve, reject) => {
    const abort = () => { clearTimeout(timer); reject(new DOMException('Cancelled', 'AbortError')); };
    const timer = setTimeout(() => { signal.removeEventListener('abort', abort); resolve(); }, ms);
    signal.addEventListener('abort', abort, { once: true });
    if (signal.aborted) abort();
  });
}

/** Requests start only from user actions, so StrictMode cannot duplicate submissions. */
export function useTranslationSubmission(onComplete: (batchId: string) => void, options: Options = {}) {
  const pollIntervalMs = options.pollIntervalMs ?? 1500;
  const analysisTimeoutMs = options.analysisTimeoutMs ?? 60_000;
  const [state, setState] = useState<SubmissionState>({ phase: 'idle', message: '' });
  const active = useRef<Run | null>(null);
  const submission = useRef<Submission | null>(null);
  const mounted = useRef(true);
  const completion = useRef(onComplete);
  completion.current = onComplete;

  useEffect(() => {
    mounted.current = true;
    return () => { mounted.current = false; active.current?.controller.abort(); active.current = null; };
  }, []);

  const isCurrent = (run: Run) => mounted.current && active.current === run;
  const update = (run: Run, next: SubmissionState) => { if (isCurrent(run)) setState(next); };
  const begin = (): Run | null => {
    if (active.current) return null;
    const run = { controller: new AbortController(), timedOut: false };
    active.current = run;
    return run;
  };

  async function submit(run: Run, current: Submission) {
    if (!isCurrent(run) || run.controller.signal.aborted) return;
    update(run, { phase: 'submitting', message: 'Starting your translations…' });
    try {
      const batch = await api.createJobs({ document_id: current.document.id, target_languages: current.languages, idempotency_key: current.key }, run.controller.signal);
      if (!isCurrent(run) || run.controller.signal.aborted) return;
      update(run, { phase: 'complete', message: 'Translations started.' });
      completion.current(batch.batch_id);
    } catch (error) {
      if (!isCurrent(run) || isAbortError(error)) return;
      // A readiness conflict must never trigger an automatic POST retry.
      const pending = error instanceof ApiError && error.error_code === 'analysis_pending';
      update(run, { phase: 'submission-error', message: getErrorMessage(error), canRetrySubmission: !pending });
    }
  }

  async function awaitReadiness(run: Run, current: Submission, retryTriage = false) {
    update(run, { phase: 'analyzing', message: 'Analyzing your document…' });
    let timer: ReturnType<typeof setTimeout> | undefined;
    let onAbort: (() => void) | undefined;
    try {
      const readiness = async () => {
        if (retryTriage) current.document = await api.retryTriage(current.document.id, run.controller.signal);
        while (isCurrent(run) && !run.controller.signal.aborted) {
          if (current.document.status === 'extracted' || current.document.status === 'failed') return current.document;
          await pause(pollIntervalMs, run.controller.signal);
          const document = await api.getDocument(current.document.id, run.controller.signal);
          if (!isCurrent(run) || run.controller.signal.aborted) return undefined;
          current.document = document;
        }
        return undefined;
      };
      // The deadline bounds network requests as well as the interval sleeps.
      const deadline = new Promise<never>((_, reject) => {
        timer = setTimeout(() => {
          run.timedOut = true;
          run.controller.abort();
          reject(new DOMException('Analysis timeout', 'TimeoutError'));
        }, analysisTimeoutMs);
      });
      const cancellation = new Promise<never>((_, reject) => {
        onAbort = () => reject(new DOMException('Cancelled', 'AbortError'));
        run.controller.signal.addEventListener('abort', onAbort, { once: true });
      });
      const document = await Promise.race([readiness(), deadline, cancellation]);
      clearTimeout(timer);
      if (!isCurrent(run) || !document) return;
      if (document.status === 'failed') {
        update(run, { phase: 'failed', message: 'Document analysis failed. Choose another readable PDF, DOCX, or Markdown document.' });
      } else {
        await submit(run, current);
      }
    } catch (error) {
      if (!isCurrent(run)) return;
      if (run.timedOut) update(run, { phase: 'timeout', message: 'Document analysis took too long. Retry analysis or choose another document.' });
      else if (!isAbortError(error)) update(run, { phase: 'failed', message: getErrorMessage(error) });
    } finally {
      clearTimeout(timer);
      if (onAbort) run.controller.signal.removeEventListener('abort', onAbort);
    }
  }

  async function start(file: File, languages: string[]) {
    const run = begin();
    if (!run) return;
    submission.current = null;
    update(run, { phase: 'uploading', message: 'Uploading your document…' });
    try {
      const document = await api.uploadDocument(file, run.controller.signal);
      if (!isCurrent(run) || run.controller.signal.aborted) return;
      const current = { document, languages: [...languages], key: crypto.randomUUID() };
      submission.current = current;
      await awaitReadiness(run, current);
    } catch (error) {
      if (isCurrent(run) && !isAbortError(error)) update(run, { phase: 'failed', message: getErrorMessage(error) });
    } finally { if (active.current === run) active.current = null; }
  }

  async function retryAnalysis() {
    const current = submission.current;
    if (!current || state.phase !== 'timeout') return;
    const run = begin();
    if (!run) return;
    try { await awaitReadiness(run, current, true); }
    finally { if (active.current === run) active.current = null; }
  }

  async function retrySubmission() {
    const current = submission.current;
    if (!current || state.phase !== 'submission-error' || !state.canRetrySubmission) return;
    const run = begin();
    if (!run) return;
    try { await submit(run, current); }
    finally { if (active.current === run) active.current = null; }
  }

  const cancel = useCallback(() => {
    active.current?.controller.abort();
    active.current = null;
    submission.current = null;
    setState({ phase: 'cancelled', message: 'Request cancelled. You can choose a document and start again.' });
  }, []);
  const reset = useCallback(() => {
    active.current?.controller.abort();
    active.current = null;
    submission.current = null;
    setState({ phase: 'idle', message: '' });
  }, []);

  return { ...state, busy: ['uploading', 'analyzing', 'submitting'].includes(state.phase), start, cancel, reset, retryAnalysis, retrySubmission };
}
