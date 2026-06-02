// CommentStore — persistence strategy for [[reviewer-comment]]s.
//
// Two backends: the live review server's `/comments` HTTP route
// (round-trips per mutation) and browser `localStorage` (scoped by
// PR head SHA, rewritten as a whole blob on every change). Before
// this module existed, `comments.ts` carried the in-memory dict
// directly and branched on `_sessionEndpoint()` inside every storage
// op — load, save, delete, flush each had its own
// "if server: fetch …; else: localStorage …" — and the branching
// was duplicated four times.
//
// Now the dict lives in a store; comments.ts picks one at init and
// the dispatch is uniform. A future third backend (IndexedDB,
// offline-with-sync, etc.) drops in as a third factory without
// touching comments.ts.
//
// Optimistic updates: `save` and `delete` mutate the in-memory dict
// synchronously, then return a Promise that resolves when the
// backend has persisted (or failed). Renders that consult `getAll()`
// after a sync mutation see the new state immediately, matching the
// behaviour of the pre-store code.

export interface CommentStore {
  /** Populate the in-memory dict from the backend. Resolves once the
   *  initial load is done so callers can render. */
  load(): Promise<void>;

  /** Snapshot of all currently-known comments. */
  getAll(): ReviewerComment[];

  /** Persist a comment. Synchronously updates in-memory state so
   *  subsequent `getAll` calls see it; returns a Promise that
   *  resolves with the persisted comment (or null on backend
   *  failure). */
  save(c: ReviewerComment): Promise<ReviewerComment | null>;

  /** Remove a comment. Synchronously updates in-memory state;
   *  returns a Promise that resolves once the backend has caught up
   *  (or silently fails). */
  delete(id: string): Promise<void>;
}


export function makeServerStore(endpoint: string): CommentStore {
  const dict: Record<string, ReviewerComment> = Object.create(null);

  return {
    load(): Promise<void> {
      return fetch(`${endpoint}/comments`)
        .then((r) => (r.ok ? r.json() : { comments: [] as ReviewerComment[] }))
        .then((d: { comments?: ReviewerComment[] }) => {
          for (const c of d.comments || []) dict[c.id] = c;
        })
        .catch(() => { /* server may have exited; ignore */ });
    },

    getAll(): ReviewerComment[] {
      return Object.values(dict);
    },

    save(c: ReviewerComment): Promise<ReviewerComment | null> {
      dict[c.id] = c;
      return fetch(`${endpoint}/comments`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(c),
      })
        .then((r) => (r.ok ? r.json() as Promise<ReviewerComment> : null))
        .catch(() => null);
    },

    delete(id: string): Promise<void> {
      delete dict[id];
      return fetch(`${endpoint}/comments/${encodeURIComponent(id)}`, {
        method: "DELETE",
      })
        .then(() => undefined)
        .catch(() => undefined);
    },
  };
}


export function makeLocalStore(lsKey: string): CommentStore {
  const dict: Record<string, ReviewerComment> = Object.create(null);

  function flush(): void {
    const payload = { comments: Object.values(dict) };
    try { localStorage.setItem(lsKey, JSON.stringify(payload)); }
    catch (_) { /* quota exceeded / private mode / etc. — ignore */ }
  }

  return {
    load(): Promise<void> {
      try {
        const raw = localStorage.getItem(lsKey);
        if (raw) {
          const data = JSON.parse(raw) as { comments?: ReviewerComment[] };
          for (const c of data.comments || []) dict[c.id] = c;
        }
      } catch (_) { /* ignore */ }
      return Promise.resolve();
    },

    getAll(): ReviewerComment[] {
      return Object.values(dict);
    },

    save(c: ReviewerComment): Promise<ReviewerComment | null> {
      dict[c.id] = c;
      flush();
      return Promise.resolve(c);
    },

    delete(id: string): Promise<void> {
      delete dict[id];
      flush();
      return Promise.resolve();
    },
  };
}
