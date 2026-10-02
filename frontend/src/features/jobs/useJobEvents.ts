import { useEffect, useRef, useState } from 'react';
import { api, parseServerSentEvent } from '../../api/client';
import { getErrorMessage, isAbortError } from '../../api/errors';
import type { JobStatus, JobSummaryResponse } from '../../api/types';
import { requestJobStream } from './jobStreamPool';

export const isTerminal = (status: JobStatus) => ['done', 'completed_with_errors', 'failed'].includes(status);

export function useJobEvents(jobId: string, initialJob?: JobSummaryResponse, live = true) {
  const initial = useRef(initialJob);
  initial.current = initialJob;
  const [reloadCount, setReloadCount] = useState(0);
  const [state, setState] = useState<{ id: string; job?: JobSummaryResponse; loading: boolean; error: string | null; reconnecting: boolean }>({ id: jobId, job: initialJob, loading: !initialJob, error: null, reconnecting: false });

  useEffect(() => {
    const controller = new AbortController();
    let source: EventSource | null = null;
    let cancelSubscription: (() => void) | null = null;
    let pollTimer: ReturnType<typeof setTimeout> | null = null;
    let requestController: AbortController | null = null;
    let latest = initial.current?.id === jobId ? initial.current : undefined;
    let eventRevision = 0;
    let requestRevision = 0;
    const alive = () => !controller.signal.aborted;
    const stopPolling = () => {
      if (pollTimer !== null) clearTimeout(pollTimer);
      pollTimer = null;
    };
    const cancelRequest = () => {
      requestController?.abort();
      requestController = null;
      requestRevision++;
    };
    const close = () => {
      stopPolling();
      source?.close(); source = null;
      const release = cancelSubscription;
      cancelSubscription = null;
      release?.();
    };
    const schedulePoll = () => {
      if (!alive() || !live || source || !cancelSubscription || !latest || isTerminal(latest.status) || pollTimer !== null) return;
      pollTimer = setTimeout(() => {
        pollTimer = null;
        void fetchJob();
      }, 1500);
    };
    const update = (job: JobSummaryResponse) => {
      latest = job;
      setState(current => ({ ...current, id: jobId, job, loading: false, error: null }));
      if (isTerminal(job.status)) {
        close();
        cancelRequest();
        setState(current => ({ ...current, reconnecting: false }));
      }
    };
    const fetchJob = async () => {
      requestController?.abort();
      const currentRequest = new AbortController();
      requestController = currentRequest;
      const revision = eventRevision;
      const request = ++requestRevision;
      try {
        const job = await api.getJob(jobId, currentRequest.signal);
        if (!alive() || currentRequest.signal.aborted || revision !== eventRevision || request !== requestRevision) return;
        update(job);
        connect();
      } catch (error) {
        if (alive() && !currentRequest.signal.aborted && !isAbortError(error) && revision === eventRevision && request === requestRevision) setState(current => ({ ...current, loading: false, error: getErrorMessage(error) }));
      } finally {
        if (requestController === currentRequest) requestController = null;
        // Schedule after completion so slow requests never overlap polls.
        schedulePoll();
      }
    };
    const startStream = () => {
      stopPolling();
      cancelRequest();
      source = new EventSource(`/api/jobs/${encodeURIComponent(jobId)}/events`);
      const connection = source;
      source.addEventListener('open', () => {
        if (!alive() || source !== connection) return;
        setState(current => ({ ...current, reconnecting: false }));
        void fetchJob();
      });
      for (const name of ['status', 'progress', 'error', 'done']) {
        source.addEventListener(name, (event) => {
          if (!alive() || source !== connection) return;
          // EventSource transport errors have no data; named backend errors do.
          if (!(event instanceof MessageEvent)) {
            if (name === 'error') setState(current => ({ ...current, reconnecting: true }));
            return;
          }
          const payload = parseServerSentEvent(event.data);
          if (!payload || payload.job_id !== jobId || payload.event !== name || !latest) return;
          eventRevision++;
          update({ ...latest, status: payload.status, done_chunks: payload.done_chunks, total_chunks: payload.total_chunks, cost_usd: payload.cost_usd, error: payload.error });
        });
      }
    };
    const connect = () => {
      if (!live || cancelSubscription || !latest || isTerminal(latest.status) || !alive()) return;
      cancelSubscription = requestJobStream(startStream);
    };
    setState({ id: jobId, job: latest, loading: !latest, error: null, reconnecting: false });
    if (live || !latest || reloadCount > 0) void fetchJob();
    return () => { controller.abort(); cancelRequest(); close(); };
  }, [jobId, live, reloadCount]);

  const visible = state.id === jobId ? state : { id: jobId, loading: true, error: null, reconnecting: false, job: undefined };
  return { ...visible, reload: () => setReloadCount(value => value + 1) };
}
