// Four persistent streams leave HTTP/1.1 connections available for API requests.
const streamLimit = 4;
interface Subscription { start: () => void; active: boolean }
const active = new Set<Subscription>();
const waiting: Subscription[] = [];

function promote() {
  while (active.size < streamLimit && waiting.length > 0) {
    const subscription = waiting.shift()!;
    subscription.active = true;
    active.add(subscription);
    subscription.start();
  }
}

/** Reserve a stream or wait in FIFO order. The returned function cancels either. */
export function requestJobStream(start: () => void): () => void {
  const subscription: Subscription = { start, active: false };
  waiting.push(subscription);
  promote();
  let cancelled = false;
  return () => {
    if (cancelled) return;
    cancelled = true;
    if (subscription.active) active.delete(subscription);
    else {
      const index = waiting.indexOf(subscription);
      if (index >= 0) waiting.splice(index, 1);
    }
    promote();
  };
}
