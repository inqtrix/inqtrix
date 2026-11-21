import '@fontsource-variable/inter/index.css'
import '@/styles/globals.css'

import { useCallback, useEffect, useState } from 'react'
import { createRoot } from 'react-dom/client'

import { AppProviders } from '@/app/AppProviders'

import { editorCopy } from '../editorCopy'
import { MarkdownSourceEditor } from '../MarkdownSourceEditor'

declare global {
  interface Window {
    /** Every text the source view handed its host, with the text it was
     * typed on, in order. */
    __sourceChanges?: { base: string; text: string }[]
    /** Another writer's text reaches the record the view shows. */
    __takeRecordText?: (text: string) => void
    /** The interface language switches between German and English. */
    __switchLanguage?: () => void
  }
}

/** The record as stored when the view opens. */
const STORED = 'Absatz eins.\n\nAbsatz zwei.'

function sourceLabels(locale: 'de' | 'en') {
  const copy = editorCopy[locale]
  return {
    addColumn: copy.addColumn,
    addRow: copy.addRow,
    closeTableEditor: copy.closeTableEditor,
    columnLabel: copy.columnLabel,
    deleteColumn: copy.deleteColumn,
    deleteRow: copy.deleteRow,
    editor: copy.sourceEditor,
    formatTables: copy.formatTables,
    insertOrEditTable: copy.insertOrEditTable,
    lineWrap: copy.sourceLineWrap,
    tableAlignmentCenter: copy.tableAlignmentCenter,
    tableAlignmentLeft: copy.tableAlignmentLeft,
    tableAlignmentRight: copy.tableAlignmentRight,
    tableColumn: copy.tableColumn,
    tableEditor: copy.tableEditor,
    tableLines: copy.tableLines,
    tableRows: copy.tableRows,
  }
}

/** The host takes every text the view hands over as the record's text, the
 * way the desk's record does when nobody else writes. */
function MarkdownSourceFixture() {
  const [record, setRecord] = useState(STORED)
  const [locale, setLocale] = useState<'de' | 'en'>('de')
  const onChange = useCallback((text: string, base: string) => {
    window.__sourceChanges = [...(window.__sourceChanges ?? []), { base, text }]
    setRecord(text)
  }, [])
  useEffect(() => {
    window.__takeRecordText = (text) => setRecord(text)
    window.__switchLanguage = () => setLocale((current) => (current === 'de' ? 'en' : 'de'))
  }, [])
  return (
    <div className="h-[480px] w-[720px] p-4">
      <MarkdownSourceEditor labels={sourceLabels(locale)} onChange={onChange} value={record} />
    </div>
  )
}

createRoot(document.getElementById('root')!).render(
  <AppProviders>
    <MarkdownSourceFixture />
  </AppProviders>,
)
