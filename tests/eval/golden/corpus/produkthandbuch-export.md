# Produkthandbuch: Berichte exportieren

## Exportformate

Berichte koennen als PDF und als DOCX exportiert werden. Der Export
behaelt Zitate, Fussnoten und Quellenverzeichnis bei.

## Limits

Ein einzelner Export umfasst maximal 500 Seiten. Pro Stunde sind
hoechstens 10 Exporte pro Arbeitsbereich moeglich; darueber hinaus
antwortet die Schnittstelle mit dem Hinweis auf das Stundenlimit.

## Schnittstelle

Exporte koennen auch ueber den API-Endpunkt /v1/exports ausgeloest
werden. Der Endpunkt erwartet die Berichts-ID und das Zielformat und
liefert eine Job-ID zurueck, ueber die der Status abgefragt wird.
