export function normalizeRuleLabel(value: string) {
  return value
    .normalize('NFKD')
    .toLowerCase()
    .replace(/[\u0300-\u036f]/g, '')
    .replace(/[^a-z0-9]+/g, '-')
    .replace(/^-+|-+$/g, '')
    .slice(0, 48)
}

export function createRuleId() {
  return `rule-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`
}
