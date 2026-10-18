import { readFileSync, readdirSync } from 'node:fs'
import path from 'node:path'

import type { Root, RootContent } from 'mdast'
import remarkGfm from 'remark-gfm'
import remarkMath from 'remark-math'
import remarkParse from 'remark-parse'
import { unified } from 'unified'
import { describe, expect, it } from 'vitest'

import { parseChatRule, parseChatThread, parseResearchRun } from '@/features/project/markdown'

import { sourceCopy } from './shippedCopy'

// Reviewed German misspellings in shipped copy. Exact words deliberately avoid
// guessing from ae/oe/ue/ss: Quelle, neue, Koeffizienten and English remain valid.
// This lexicon never rewrites model output, user text or imported documents.
const misspellings = new Set(`
abgeloest abhaengig abhaengigkeit abhaengigkeitsverhaeltnis abkuendigung abloesung abschliessend
adaequanzentscheidung aehnliche aehnlichen aendern aendert aenderungen aerztezeitung aerztlichen aesthetische
aeussern aeussert aktivitaeten aktualitaet allgemeinzahnaerzte anfaenglich angehaengte angekuendigt
angekuendigte angekuendigten angriffsverstaerker ankuendigung ankuendigungen anwendungsfaelle
anwendungsfaellen arbeitsplaetze aufgefuehrten aufgeschluesselt aufsichtsbehoerden aufzuloesen ausdruecklich
ausfuehrliche ausgefuehrt ausgeloest ausgewaehlte ausgewaehlten ausloesen ausschliesslich ausschliesst
ausschoepfen aussen ausser aussergewoehnliche ausserhalb befuerchten befuerwortet begruendet begrüssen
beguenstigt behoerden behoerdenmarkt behoerdlichen beruecksichtigen beruecksichtigung beschaedigen
beschaeftigten beschaeftigtendaten beschaeftigung beschraenkt bestaetigen bestaetigt bestaetigte betriebsraete
bezueglich blogbeitraege blogbeitraegen boerse branchenloesungen branchenverbaende buendelt buerojobs
buerotaetigkeiten bundeszahnaerztekammer bundeszuschuesse bussgeldrahmen cybersicherheitsmassnahmen dafuer
datenqualitaet datenschuetzer demgegenueber draengen dualitaet duenne duerfen duerfte durchgaengig
durchsetzungsmassnahmen durchzufuehren eigenanteilserhoehung eigenstaendig eigenstaendiger eigenstaendiges
einfuehrung eingeschraenkt einkommensabhaengig einsaetzen einschaetzung einschliessen einschliesslich
einschraenken einschraenkend einschraenkungen einstellungsankuendigung empfaenger endgueltigen engpaessen
entfaellt enthaelt entscheidungsunterstuetzung entwicklungskapazitaet entwuerfe erfuellen ergaenzen ergaenzend
ergaenzende ergaenzt erhoehe erhoehen erhoeht erhoehten erhoehung erklaeren erklaert erklaerung erklaerungen
erloesbasis ermoeglichen ermoeglicht ertraegen erwaehnt europaeische europaeischen europaeischer
fachzahnaerzte fachzahnaerzten faehigem faehiges faehigkeit faehigkeiten faellt festzuschuesse festzuschuessen
festzuschusskuerzungen festzuschusssaetzen flaechenkontrast flexibilitaetszugestaendnissen fliessen foerderten
forschungsaktivitaeten forschungsveroeffentlichung fortfuehrung fruehe fruehere fruehes fruehjahr fruehling
fuehre fuehren fuehrt fuehrte fuellungen fuenf fuer funktionalitaet funktionsfaehige geaendert
gebaeudesanierung gefaehrden gegenueber gehoere gehoert geklaert geldbussen geloest gemaess gepraegt geraete
geraeteintegration geraetekategorien geraetespezifikationen geruechte geruechten gesamtqualitaetsscore
gesamtverguetung geschaeftsjahr geschaetzten geschaetzter gestuetzt gestuetzte gestuetztes groessenordnung
groesseren grosse grossen grosser grossunternehmen gruendungen gruenem grundsaetzen grundsaetzlich
grundsaetzliche grundsicherungsempfaenger grösseren haelt haengt haertefallregelung haetten haeufig
heliumfuellung herausloesung hochkapazitaetslaufwerke hoehe hoeher hoehere hoeheren hoeherwertige hoerbar
informationsqualitaet inhaerent innovationsraeume jaehrlich jaehrlichen kapazitaet kapitalmaerkte
kassenaerztliche kassenverguetung kassenzahnaerztliche kieferorthopaedie kieferorthopaedisch
kieferorthopaedische klaeren koeln koenne koennen koennte koennten koerperschaften kohaerente kohaerenter
komplementaer komplementaere konformitaetsbewertung kontinuitaet kostendaempfung kostengruenden
kostenstabilitaet kuehl kuehlung kuehlungsspezialisten kuendigt kuendigte kuenftig kuenftige kuenstliche
kuenstlicher kuerzung kuerzungen kursruecksetzer kurzzeitgedaechtnis laendern laendlichen laesst laeuft
leistungsfaehige leistungsueberwachung leitfaeden loehne loese loest loesung loesungen luecke luecken
lueckenlose maerkte markenfuehrung marktaufsichtsbehoerden markteinfuehrung markteinschaetzung
marktverfuegbarkeit massgeblich massnahme massnahmen missverstaendnis missverstaendnisses mittelstaendische
modellankuendigungen moeglich moegliche moeglicher moeglicherweise muessen naechste naeher naehert
niederlaendische niederlaendischen noetig nutzerbestaetigung oberflaeche oberflaechen oeffentlich oeffentliche
oeffentlichem oeffentlichen oeffentliches oeffnen oekonomen oekonomischen oekosystem oesterreichische
oesterreichs parallelitaet pauschalverguetung pauschalverguetungen plaene praeferenz praegen praesentation
praesentiert praesenz praeventiv praezise primaer primaerbeleg primaere primaeren primaerquelle primaerquellen
primaerquellenluecke produktabloesung produktaktivitaeten produktankuendigungen produktivitaetseffekt
produktivitaetseffekten produktivitaetsgewinne produktivitaetsgewinnen produktivitaetspotenzial
produktivitaetspotenziale produktivitaetsschaetzungen produktivitaetswirkung proportionalitaet
proportionalitaetsprinzip proprietaer proprietaere pruefen pruefende pruefkriterien pruefungsgesellschaften
punktwertabschlaege qualitaet qualitaetsmanagement qualitaetsscore qualitaetssicherung quellenqualitaet
quellenqualitaetsscore realitaet rechtsanwaelte regulierungsbehoerden regulierungsmassnahmen repraesentationen
risikoerhoehend rueckfuehrung rueckgang rueckhalt rueckkehr ruecknahme rueckt saemtliche saetzen schaerfere
schaetzung schaetzungen schliesst schraenkt schwaechten sicherheitsluecken sicherheitspruefungen singulaeres
skalierungsplaene solidaritaetsgrad souveraene sparmassnahmen speicherkapazitaeten spuerbar spuerbaren
staerken staerker staerkere staerkt standardmaessig standortattraktivitaet strukturphaenomen stueckzahlen
stuenden stuendigen stuetzen stuetzender stuetzt stuetzte taetige taetigkeitsprofile tatsaechlich
tatsaechlichen traeger traegt transparenzloesungen truebt truebte ueber ueberbewertet ueberdelegation
uebergangsfrist uebergangsfristen uebergreifenden ueberhitzungsgefahr ueberkapazitaeten uebermassige
uebernahmen uebernehmen uebernommene ueberproportional ueberregulierung ueberschaetzt ueberschreiten
ueberwachen ueberwachung ueberwachungs ueberwiegend uebrigen unabhaengig unabhaengige unberuehrt unbestaetigt
unmissverstaendlich unterrepraesentiert unterstuetzt unterstuetzung unverhältnismässige urspruenglich
urspruenglichen veraendert verdraengt verfuegbar verfuegbaren verfuegbarkeit vergleichsmassstab verguetung
verguetungsabschlaege verguetungsabschlaegen verguetungsdeckelung verguetungsdynamik verguetungsentwicklung
verguetungsfortschreibungen verhaelt verhaeltnis verlaengern verlaengert verlaesslichere verlaeuft
veroeffentlichen veroeffentlichte veroeffentlichten veroeffentlichung verschaerfen verschaerft verschaerften
versorgungsengpaesse versorgungsengpaessen versorgungsqualitaet verstaerkt verstaerktem verstoesse verstoessen
vertragsaerztliche vertragsaerztlichen vertrauenswuerdigen vollstaendig vorfaelle vorgaenger vorschlaege
waechst waehle waehrend waere waeren weiterfuehrende wertschoepfung wertschoepfungskette westfaelische
wettbewerbsbehoerde wettbewerbsfaehigkeit widersprueche woertlich wuenschenswerten wuerde wuerden zaehlen
zahnaerzte zahnaerztliche zahnaerztlichen zahnaerztlicher zahnaeztlichen zahnersatzzuschuesse zeitplaenen
zitierfaehige zugaenglichen zunaechst zurueck zurueckfuehren zurueckgegeben zurueckgeht zurueckhaltend
zurueckhaltung zurueckzufuehren zusaetzlich zusaetzliche zusammenfuehrung übermässige
`.trim().split(/\s+/))

function spellingErrors(text: string): string[] {
  const prose = text
    .replace(/https?:\/\/\S+/g, '')
    .replace(/\{\{[^}]+\}\}/g, '')
  return [...new Set((prose.match(/[\p{L}]+/gu) ?? [])
    .filter((word) => misspellings.has(word.toLowerCase())))].sort()
}

const markdownParser = unified().use(remarkParse).use(remarkGfm).use(remarkMath)
function markdownText(markdown: string): string {
  const chunks: string[] = []
  function visit(node: Root | RootContent) {
    if (node.type === 'text') chunks.push(node.value)
    if ('children' in node) node.children.forEach(visit)
  }
  visit(markdownParser.parse(markdown))
  return chunks.join('\n')
}

const sourceFiles = [
  'i18n/translations.ts',
  'features/editor/editorCopy.ts',
  'features/editor/CollaborationNoticeBars.tsx',
  'features/editor/GuestEditorPage.tsx',
  'features/editor/inspector/model.ts',
  'features/agent/demo.ts',
  'features/knowledge/demo.ts',
  'features/project/seedProject.ts',
  'features/skills/useSkillsApi.ts',
]
const demoRoot = path.resolve(import.meta.dirname, '../features/project/demoContent')
const demoFiles = readdirSync(demoRoot, { recursive: true })
  .filter((file): file is string => typeof file === 'string' && file.endsWith('.md'))
  .sort()

function demoCopy(file: string): string {
  const source = readFileSync(path.join(demoRoot, file), 'utf8')
  if (file.startsWith('search-history/')) {
    const run = parseResearchRun(source)
    if (!run.result) throw new Error(`Demo report has no result: ${file}`)
    return [
      run.summary.title,
      ...run.events.map((event) => event.title),
      run.snapshot?.last_message,
      ...run.result.topClaims.flatMap((claim) => [claim.text, claim.status_reason]),
      markdownText(run.result.markdown),
    ].join('\n')
  }
  if (file.startsWith('chat-history/')) {
    const thread = parseChatThread(source)
    return [thread.title, thread.preview, ...thread.messages.map((message) => markdownText(message.contentMarkdown))].join('\n')
  }
  if (file.startsWith('rules/')) {
    const rule = parseChatRule(source)
    return [rule.title, markdownText(rule.contentMarkdown)].join('\n')
  }
  throw new Error(`Unclassified demo copy: ${file}`)
}

describe('German orthography in shipped copy', () => {
  it.each(sourceFiles)('uses real umlauts in %s', (file) => {
    const source = readFileSync(path.resolve(import.meta.dirname, '..', file), 'utf8')
    expect(spellingErrors(sourceCopy(source, file))).toEqual([])
  })

  it.each(demoFiles)('uses real umlauts in demo prose and visible metadata: %s', (file) => {
    expect(spellingErrors(demoCopy(file))).toEqual([])
  })

  it('detects complete words without treating legitimate spelling as an error', () => {
    expect(spellingErrors('Fuer neue Quellen: woertlich, Erhoeht, grossen.')).toEqual(['Erhoeht', 'Fuer', 'grossen', 'woertlich'])
    expect(spellingErrors('Für neue Quellen, Koeffizienten, Steuerung, OECD, Queue und Schluss.')).toEqual([])
  })

  it('keeps links, code, math and template parameters out of prose checks', () => {
    const markdown = [
      '[Neue Quelle](https://example.org/fuer/grossen)',
      '`reference_id: fuer` und {{empfaenger}}',
      '$\\mathrm{fuer}$',
      '```mermaid\nerDiagram\n  EMPFAENGER {\n    string massnahme\n  }\n```',
    ].join('\n\n')
    expect(spellingErrors(markdownText(markdown))).toEqual([])
    expect(spellingErrors(markdownText('[Fuer Leser](https://example.org/fuer)'))).toEqual(['Fuer'])
  })

  it('checks visible filenames while preserving seed labels and skill parameter names', () => {
    expect(spellingErrors(sourceCopy("const data = {label: 'konformitaetsbewertung', fileName: 'Konformitaetsbewertung.pdf'}", 'seedProject.ts')))
      .toEqual(['Konformitaetsbewertung'])
    expect(spellingErrors(sourceCopy("const data = {name: 'empfaenger', question: 'Fuer wen?', instructions: '{{empfaenger}}'}", 'useSkillsApi.ts')))
      .toEqual(['Fuer'])
  })
})
