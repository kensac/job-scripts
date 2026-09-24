export type OperationState = "idle" | "running" | "paused" | "stopped";

/** One lease owns a fill, including time spent waiting inside an ATS widget. */
export class Operation {
  private controller = new AbortController();
  private wake = new Set<() => void>();
  private phase: OperationState = "idle";
  private sourceUrl = "";
  private leased = false;
  private observers = new Set<(state: OperationState) => void>();

  constructor(
    private readonly location: () => string,
    private readonly changed: (state: OperationState) => void,
  ) {}

  get state(): OperationState { return this.phase; }
  get signal(): AbortSignal { return this.controller.signal; }
  get active(): boolean { return this.phase === "running" || this.phase === "paused"; }
  get busy(): boolean { return this.leased; }

  subscribe(observer: (state: OperationState) => void): () => void {
    this.observers.add(observer);
    return () => this.observers.delete(observer);
  }

  start(): void {
    if (this.leased) throw new Error("An autofill operation is already running");
    this.leased = true;
    this.controller = new AbortController();
    this.sourceUrl = this.location();
    this.setState("running");
  }

  pause(): void {
    if (this.phase === "running") this.setState("paused");
  }

  resume(): void {
    if (this.phase !== "paused") return;
    this.assertCurrent();
    this.setState("running");
    this.releaseWaiters();
  }

  stop(): void {
    if (!this.active) return;
    this.controller.abort(new DOMException("Autofill stopped", "AbortError"));
    this.setState("stopped");
    this.releaseWaiters();
  }

  finish(): void {
    this.leased = false;
    this.controller = new AbortController();
    this.setState("idle");
    this.releaseWaiters();
  }

  assertCurrent(): void {
    this.controller.signal.throwIfAborted();
    if (this.active && this.location() !== this.sourceUrl) {
      this.stop();
      this.controller.signal.throwIfAborted();
    }
  }

  async checkpoint(): Promise<void> {
    this.assertCurrent();
    while (this.phase === "paused") {
      await new Promise<void>((resolve) => this.wake.add(resolve));
      this.assertCurrent();
    }
  }

  async wait<T>(pending: Promise<T>): Promise<T> {
    const signal = this.signal;
    let cancel: () => void = () => {};
    try {
      const cancelled = new Promise<never>((_, reject) => {
        cancel = () => reject(signal.reason);
        signal.addEventListener("abort", cancel, { once: true });
        if (signal.aborted) cancel();
      });
      const result = await Promise.race([pending, cancelled]);
      await this.checkpoint();
      return result;
    } finally {
      signal.removeEventListener("abort", cancel);
    }
  }

  async sleep(ms: number): Promise<void> {
    await this.checkpoint();
    const signal = this.controller.signal;
    await new Promise<void>((resolve, reject) => {
      const cancel = () => { clearTimeout(timer); reject(signal.reason); };
      const timer = setTimeout(() => {
        signal.removeEventListener("abort", cancel);
        resolve();
      }, ms);
      signal.addEventListener("abort", cancel, { once: true });
      if (signal.aborted) cancel();
    });
    await this.checkpoint();
  }

  private setState(state: OperationState): void {
    this.phase = state;
    this.changed(state);
    for (const observer of this.observers) observer(state);
  }

  private releaseWaiters(): void {
    for (const resolve of this.wake) resolve();
    this.wake.clear();
  }
}

export function isStopped(error: unknown): boolean {
  return error instanceof Error && error.name === "AbortError";
}
