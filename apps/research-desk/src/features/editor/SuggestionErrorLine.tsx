import { AlertTriangle } from '@/components/icons'

/** Der Fehlschlag einer Vorschlagsaktion, an der Karte, die den Knopf traegt.
 *
 * Eine Fassung fuer beide Panel-Flaechen (Dokument-Aenderungen und die
 * kommentarverankerte Pruefung) statt zweier Varianten. Bewusst KEIN Toast
 * und kein globales Banner: der Fehlerkanal ist `suggestionErrors`, und ein
 * zweiter Kanal daneben waere genau die Parallelstruktur, die hier
 * ausgeschlossen bleiben soll. */
export function SuggestionErrorLine({ message }: { message: string }) {
  return (
    <p
      className="t-meta-sm mt-1.5 flex items-start gap-1.5 rounded-md border border-destructive/25 bg-destructive/5 px-2 py-1.5 text-destructive"
      data-suggestion-error="true"
      role="alert"
    >
      <AlertTriangle aria-hidden="true" className="size-3 shrink-0 translate-y-0.5" />
      <span className="min-w-0">{message}</span>
    </p>
  )
}
