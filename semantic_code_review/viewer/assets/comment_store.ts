// CommentStore — persistence strategy for [[reviewer-comment]]s.
//
// One live backend: the review server's `/comments` routes, which
// round-trip per mutation. `makeNoopStore` is an in-memory stand-in
// used only for the pre-init window (see comments.ts), so stray
// click-handlers before `Comments.init` don't crash.
//
// Optimistic updates: `save` and `delete` mutate the in-memory dict
// synchronously, then return a Promise that resolves when the
// backend has persisted (or failed). Renders that consult `getAll()`
// after a sync mutation see the new state immediately. The server's
// answer replaces the optimistic copy, since only the server knows a
// comment's lifecycle fields (ADR 0009).
//
// `apply` / `remove` are the other direction: a `comment` /
// `comment-deleted` SSE frame — another tab's edit, a Send landing as
// delivered, Claude's reply — lands in the dict without a round trip.

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

  /** Send one draft to the counterpart. Resolves with the comment as
   *  the server now holds it (`delivery: "sent"`), or null on failure. */
  send(id: string): Promise<ReviewerComment | null>;

  /** Send every draft as one batch. Resolves with the ids sent. */
  sendAll(): Promise<string[]>;

  /** Take a comment as the server states it (an SSE frame). */
  apply(c: ReviewerComment): void;

  /** Drop a comment the server says is gone (an SSE frame). */
  remove(id: string): void;
}


export function makeServerStore(endpoint: string): CommentStore {
  const dict: Record<string, ReviewerComment> = Object.create(null);

  const post = (path: string, body: unknown): Promise<Response> =>
    fetch(`${endpoint}${path}`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });

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
      return post("/comments", c)
        .then((r) => (r.ok ? r.json() as Promise<ReviewerComment> : null))
        .then((saved) => {
          if (saved) dict[saved.id] = saved;
          return saved;
        })
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

    send(id: string): Promise<ReviewerComment | null> {
      return post(`/comments/${encodeURIComponent(id)}/send`, {})
        .then((r) => (r.ok ? r.json() as Promise<{ comment_ids: string[] }> : null))
        .then((sent) => {
          if (!sent) return null;
          const c = dict[id];
          if (!c) return null;
          dict[id] = { ...c, delivery: "sent" };
          return dict[id];
        })
        .catch(() => null);
    },

    sendAll(): Promise<string[]> {
      return post("/comments/send-all", {})
        .then((r) => (r.ok ? r.json() as Promise<{ comment_ids: string[] }> : { comment_ids: [] }))
        .then((sent) => {
          for (const id of sent.comment_ids) {
            const c = dict[id];
            if (c) dict[id] = { ...c, delivery: "sent" };
          }
          return sent.comment_ids;
        })
        .catch(() => []);
    },

    apply(c: ReviewerComment): void {
      dict[c.id] = c;
    },

    remove(id: string): void {
      delete dict[id];
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

    send(id: string): Promise<ReviewerComment | null> {
      const c = dict[id];
      if (!c) return Promise.resolve(null);
      dict[id] = { ...c, delivery: "sent" };
      return Promise.resolve(dict[id]);
    },

    sendAll(): Promise<string[]> {
      const ids: string[] = [];
      for (const c of Object.values(dict)) {
        if ((c.delivery ?? "draft") === "draft") {
          dict[c.id] = { ...c, delivery: "sent" };
          ids.push(c.id);
        }
      }
      return Promise.resolve(ids);
    },

    apply(c: ReviewerComment): void {
      dict[c.id] = c;
    },

    remove(id: string): void {
      delete dict[id];
    },
  };
}
