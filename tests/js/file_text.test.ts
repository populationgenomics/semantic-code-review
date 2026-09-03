// The shared /file-text cache: one round trip per file per session,
// shared by whoever asks (rendered mode, the expand chips), with a
// failure left uncached so the next ask retries.

import { describe, test, expect, vi, beforeEach } from "vitest";
import { FileTextCache } from "../../semantic_code_review/viewer/assets/file_text";

function file(id: string, path = "a.py"): FileBlock {
  return { id, path } as FileBlock;
}

function ok(body: unknown): Response {
  return new Response(JSON.stringify(body), {
    status: 200, headers: { "Content-Type": "application/json" },
  });
}

beforeEach(() => {
  FileTextCache.init("");
});

describe("FileTextCache.load", () => {
  test("fetches once and serves the cache after", async () => {
    const spy = vi.spyOn(globalThis, "fetch")
      .mockResolvedValue(ok({ file_idx: 4, path: "a.py", base: "x\n", head: "y\n" }));
    const first = await FileTextCache.load(file("F4"));
    const second = await FileTextCache.load(file("F4"));
    expect(spy).toHaveBeenCalledTimes(1);
    expect(spy).toHaveBeenCalledWith("/file-text?file_idx=4", { cache: "no-store" });
    expect(second).toBe(first);
    expect(FileTextCache.cached("F4")).toBe(first);
  });

  test("two askers during one flight share the request", async () => {
    let release: (r: Response) => void = () => {};
    const spy = vi.spyOn(globalThis, "fetch")
      .mockReturnValue(new Promise<Response>((r) => { release = r; }));
    const a = FileTextCache.load(file("F1"));
    const b = FileTextCache.load(file("F1"));
    expect(spy).toHaveBeenCalledTimes(1);
    release(ok({ file_idx: 1, path: "a.py", base: null, head: "h\n" }));
    const [ta, tb] = await Promise.all([a, b]);
    expect(tb).toBe(ta);
    expect(ta.head).toBe("h\n");
  });

  test("a failed fetch rejects, caches nothing, and the next ask retries", async () => {
    const spy = vi.spyOn(globalThis, "fetch")
      .mockResolvedValueOnce(new Response("nope", { status: 500 }))
      .mockResolvedValueOnce(ok({ file_idx: 2, path: "a.py", base: "b\n", head: "h\n" }));
    await expect(FileTextCache.load(file("F2"))).rejects.toThrow("GET /file-text -> 500");
    expect(FileTextCache.cached("F2")).toBeUndefined();
    const text = await FileTextCache.load(file("F2"));
    expect(text.base).toBe("b\n");
    expect(spy).toHaveBeenCalledTimes(2);
  });

  test("the endpoint prefixes the route", async () => {
    FileTextCache.init("http://127.0.0.1:9/s1");
    const spy = vi.spyOn(globalThis, "fetch")
      .mockResolvedValue(ok({ file_idx: 0, path: "a.py", base: null, head: null }));
    await FileTextCache.load(file("F0"));
    expect(spy.mock.calls[0][0]).toBe("http://127.0.0.1:9/s1/file-text?file_idx=0");
  });

  test("init empties the cache", async () => {
    vi.spyOn(globalThis, "fetch")
      .mockResolvedValue(ok({ file_idx: 0, path: "a.py", base: null, head: "h\n" }));
    await FileTextCache.load(file("F0"));
    expect(FileTextCache.cached("F0")).toBeDefined();
    FileTextCache.init("");
    expect(FileTextCache.cached("F0")).toBeUndefined();
  });

  test("an id that is not F<idx> is a bug, not a request", async () => {
    const spy = vi.spyOn(globalThis, "fetch");
    await expect(FileTextCache.load(file("H0_1"))).rejects.toThrow("unexpected file id");
    expect(spy).not.toHaveBeenCalled();
  });
});

describe("FileTextCache.splitLines", () => {
  test("numbers lines as the diff does: a trailing newline closes the last line", () => {
    expect(FileTextCache.splitLines("a\nb\n")).toEqual(["a", "b"]);
    expect(FileTextCache.splitLines("a\nb")).toEqual(["a", "b"]);
    expect(FileTextCache.splitLines("")).toEqual([]);
    expect(FileTextCache.splitLines("\n")).toEqual([""]);
    expect(FileTextCache.splitLines("a\n\nb\n")).toEqual(["a", "", "b"]);
  });
});
