const money0 = new Intl.NumberFormat("en-US", {
  style: "currency",
  currency: "USD",
  maximumFractionDigits: 0,
});

const money2 = new Intl.NumberFormat("en-US", {
  style: "currency",
  currency: "USD",
  minimumFractionDigits: 2,
  maximumFractionDigits: 2,
});

const number0 = new Intl.NumberFormat("en-US", { maximumFractionDigits: 0 });
const number1 = new Intl.NumberFormat("en-US", {
  minimumFractionDigits: 1,
  maximumFractionDigits: 1,
});

const number2 = new Intl.NumberFormat("en-US", {
  minimumFractionDigits: 2,
  maximumFractionDigits: 2,
});

export function currency(value: number, digits: 0 | 2 = 0): string {
  return digits === 2 ? money2.format(value) : money0.format(value);
}

export function signedCurrency(value: number, digits: 0 | 2 = 0): string {
  const formatted = currency(Math.abs(value), digits);
  return value < 0 ? `-${formatted}` : `+${formatted}`;
}

export function compactCurrency(value: number): string {
  const abs = Math.abs(value);
  const sign = value < 0 ? "-" : "";
  if (abs >= 1_000_000) return `${sign}$${(abs / 1_000_000).toFixed(2)}M`;
  if (abs >= 1_000) return `${sign}$${(abs / 1_000).toFixed(0)}K`;
  return `${sign}$${abs.toFixed(0)}`;
}

export function percentFraction(value: number, digits = 1): string {
  return `${(value * 100).toFixed(digits)}%`;
}

export function percentValue(value: number, digits = 1): string {
  return `${value.toFixed(digits)}%`;
}

export function number(value: number, digits: 0 | 1 | 2 = 0): string {
  if (digits === 2) return number2.format(value);
  if (digits === 1) return number1.format(value);
  return number0.format(value);
}

export function shortDate(value: string | null): string {
  if (!value) return "—";
  return new Intl.DateTimeFormat("en-US", {
    month: "short",
    day: "2-digit",
    year: "numeric",
    timeZone: "America/New_York",
  }).format(new Date(value));
}

export function compactDate(value: string): string {
  return new Intl.DateTimeFormat("en-US", {
    month: "short",
    year: "2-digit",
    timeZone: "America/New_York",
  }).format(new Date(value));
}

export function dateTime(value: string | null): string {
  if (!value) return "—";
  return new Intl.DateTimeFormat("en-US", {
    month: "short",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    hour12: false,
    timeZone: "America/New_York",
  }).format(new Date(value));
}

export function titleCase(value: string): string {
  return value
    .toLowerCase()
    .split("_")
    .map((part) => part.charAt(0).toUpperCase() + part.slice(1))
    .join(" ");
}
