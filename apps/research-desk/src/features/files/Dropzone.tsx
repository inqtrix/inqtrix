import { useRef, useState, type DragEvent, type ReactNode } from 'react'
import { Upload } from '@/components/icons'
import { cn } from '@/lib/utils'

/**
 * Reusable file drag-and-drop surface. Used identically by the chat composer,
 * the editor surface and the library view — each call site differs only in what
 * it does with the dropped files. The overlay is contained within the wrapped
 * area (no full-viewport fixed layer) and only appears while files are dragged.
 */
export function Dropzone({
  children,
  className,
  disabled = false,
  label,
  onFiles,
}: {
  children: ReactNode
  className?: string
  disabled?: boolean
  label: string
  onFiles: (files: File[]) => void
}) {
  const [isDragging, setIsDragging] = useState(false)
  const depth = useRef(0)

  function hasFiles(event: DragEvent): boolean {
    return Array.from(event.dataTransfer?.types ?? []).includes('Files')
  }

  return (
    <div
      className={cn('relative', className)}
      onDragEnter={(event) => {
        if (disabled || !hasFiles(event)) return
        event.preventDefault()
        depth.current += 1
        setIsDragging(true)
      }}
      onDragLeave={() => {
        if (disabled) return
        depth.current = Math.max(0, depth.current - 1)
        if (depth.current === 0) setIsDragging(false)
      }}
      onDragOver={(event) => {
        if (disabled || !hasFiles(event)) return
        event.preventDefault()
        event.dataTransfer.dropEffect = 'copy'
      }}
      onDrop={(event) => {
        if (disabled || !hasFiles(event)) return
        event.preventDefault()
        depth.current = 0
        setIsDragging(false)
        const files = Array.from(event.dataTransfer.files)
        if (files.length > 0) onFiles(files)
      }}
    >
      {children}
      {isDragging && (
        <div className="pointer-events-none absolute inset-0 z-40 grid place-items-center rounded-lg border-2 border-dashed border-file bg-file-subtle/70 backdrop-blur-sm">
          <div className="flex items-center gap-2 rounded-md bg-background/90 px-3 py-1.5 text-sm font-semibold text-file shadow">
            <Upload className="size-4" />
            {label}
          </div>
        </div>
      )}
    </div>
  )
}
