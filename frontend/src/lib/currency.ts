export function formatCurrency(value: number, currency = "BRL"): string {
  return value.toLocaleString("pt-BR", {
    style: "currency",
    currency,
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  });
}
