/**
 * The desk's one plural rule: exactly one takes the singular phrase, every
 * other count (0, 2, 12 …) the plural phrase. Both phrases are whole
 * dictionary entries (`xOne` / `x`), and `{count}` is filled in both, so a
 * singular may spell its number or carry the placeholder alike. `shown` is
 * the number as the surface writes it (a locale-grouped `1.234`); the form
 * is always chosen by `count`. The server counterpart is `plural_form` /
 * `t_count` in `inqtrix/i18n.py`; the two sides must agree, because a
 * server label and a desk label can stand in the same line.
 */
export function countLabel(
  count: number,
  one: string,
  other: string,
  shown: string = String(count),
): string {
  return (count === 1 ? one : other).replaceAll('{count}', shown)
}
