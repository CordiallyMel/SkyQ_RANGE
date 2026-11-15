import type {
  Book,
  BookKey,
  ControlStatus,
  FuturesRow,
  Overview,
  SystemInfo,
  TradePage,
  WindowKey,
} from "./types";

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(path, {
    ...init,
    headers: {
      Accept: "application/json",
      ...(init?.headers ?? {}),
    },
  });
  if (!response.ok) {
    let detail = `${response.status} ${response.statusText}`;
    try {
      const payload = (await response.json()) as { detail?: string };
      if (payload.detail) detail = payload.detail;
    } catch {
      // Keep the HTTP status when the body is not JSON.
    }
    throw new Error(detail);
  }
  return (await response.json()) as T;
}

export function getBooks(signal?: AbortSignal): Promise<Book[]> {
  return request<Book[]>("/api/books", { signal });
}

export function getOverview(
  symbol: BookKey,
  window: WindowKey,
  signal?: AbortSignal,
): Promise<Overview> {
  const params = new URLSearchParams({ symbol, window });
  return request<Overview>(`/api/overview?${params}`, { signal });
}

export function getTrades(
  symbol: BookKey,
  window: WindowKey,
  limit: number,
  offset: number,
  side: "" | "LONG" | "SHORT",
  signal?: AbortSignal,
): Promise<TradePage> {
  const params = new URLSearchParams({
    symbol,
    window,
    limit: String(limit),
    offset: String(offset),
  });
  if (side) params.set("side", side);
  return request<TradePage>(`/api/trades?${params}`, { signal });
}

export function getFutures(signal?: AbortSignal): Promise<FuturesRow[]> {
  return request<FuturesRow[]>("/api/research/futures", { signal });
}

export function getSystem(signal?: AbortSignal): Promise<SystemInfo> {
  return request<SystemInfo>("/api/system", { signal });
}

export function getControl(): Promise<ControlStatus> {
  return request<ControlStatus>("/api/control/status");
}

export async function control(
  action: "start" | "stop",
  symbol: BookKey = "SPY",
): Promise<ControlStatus> {
  return request<ControlStatus>(`/api/control/${action}`, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      "X-SkyQ-Control": "1",
    },
    body: JSON.stringify({ symbol }),
  });
}
