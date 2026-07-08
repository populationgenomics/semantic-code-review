// CommentStore — persistence strategy for [[reviewer-comment]]s.
//
// One live backend: the review server's `/comments` HTTP route, which
// round-trips per mutation. `makeNoopStore` is an in-memory stand-in
// used only for the pre-init window (see comments.ts), so stray
// click-handlers before `Comments.init` don't crash.
//
// Optimistic updates: `save` and `delete` mutate the in-memory dict
// synchronously, then return a Promise that resolves when the
// backend has persisted (or failed). Renders that consult `getAll()`
// after a sync mutation see the new state immediately.

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


export function makeNoopStore(): CommentStore {
  const dict: Record<string, ReviewerComment> = Object.create(null);

  return {
    load(): Promise<void> {
      return Promise.resolve();
    },

    getAll(): ReviewerComment[] {
      return Object.values(dict);
    },

    save(c: ReviewerComment): Promise<ReviewerComment | null> {
      dict[c.id] = c;
      return Promise.resolve(c);
    },

    delete(id: string): Promise<void> {
      delete dict[id];
      return Promise.resolve();
    },
  };
}
