import type { ProjectFile } from './markdown'

const textEncoder = new TextEncoder()
let crcTable: Uint32Array | null = null

export function buildZip(files: ProjectFile[]) {
  const localParts: Uint8Array[] = []
  const centralParts: Uint8Array[] = []
  let offset = 0

  for (const file of files) {
    const nameBytes = textEncoder.encode(file.path)
    const data = textEncoder.encode(file.contents)
    const crc = crc32(data)
    const localHeader = localFileHeader(nameBytes, data, crc)
    const centralHeader = centralDirectoryHeader(nameBytes, data, crc, offset)

    localParts.push(localHeader, data)
    centralParts.push(centralHeader)
    offset += localHeader.byteLength + data.byteLength
  }

  const centralSize = centralParts.reduce((sum, part) => sum + part.byteLength, 0)
  const end = endOfCentralDirectory(files.length, centralSize, offset)
  const blobParts = [...localParts, ...centralParts, end].map(toBlobPart)
  return new Blob(blobParts, { type: 'application/zip' })
}

function toBlobPart(bytes: Uint8Array): BlobPart {
  return bytes.buffer.slice(
    bytes.byteOffset,
    bytes.byteOffset + bytes.byteLength,
  ) as ArrayBuffer
}

function localFileHeader(name: Uint8Array, data: Uint8Array, crc: number) {
  const header = new Uint8Array(30 + name.byteLength)
  const view = new DataView(header.buffer)
  view.setUint32(0, 0x04034b50, true)
  view.setUint16(4, 20, true)
  view.setUint16(6, 0, true)
  view.setUint16(8, 0, true)
  view.setUint16(10, 0, true)
  view.setUint16(12, 0, true)
  view.setUint32(14, crc, true)
  view.setUint32(18, data.byteLength, true)
  view.setUint32(22, data.byteLength, true)
  view.setUint16(26, name.byteLength, true)
  view.setUint16(28, 0, true)
  header.set(name, 30)
  return header
}

function centralDirectoryHeader(
  name: Uint8Array,
  data: Uint8Array,
  crc: number,
  offset: number,
) {
  const header = new Uint8Array(46 + name.byteLength)
  const view = new DataView(header.buffer)
  view.setUint32(0, 0x02014b50, true)
  view.setUint16(4, 20, true)
  view.setUint16(6, 20, true)
  view.setUint16(8, 0, true)
  view.setUint16(10, 0, true)
  view.setUint16(12, 0, true)
  view.setUint16(14, 0, true)
  view.setUint32(16, crc, true)
  view.setUint32(20, data.byteLength, true)
  view.setUint32(24, data.byteLength, true)
  view.setUint16(28, name.byteLength, true)
  view.setUint16(30, 0, true)
  view.setUint16(32, 0, true)
  view.setUint16(34, 0, true)
  view.setUint16(36, 0, true)
  view.setUint32(38, 0, true)
  view.setUint32(42, offset, true)
  header.set(name, 46)
  return header
}

function endOfCentralDirectory(
  fileCount: number,
  centralSize: number,
  centralOffset: number,
) {
  const header = new Uint8Array(22)
  const view = new DataView(header.buffer)
  view.setUint32(0, 0x06054b50, true)
  view.setUint16(4, 0, true)
  view.setUint16(6, 0, true)
  view.setUint16(8, fileCount, true)
  view.setUint16(10, fileCount, true)
  view.setUint32(12, centralSize, true)
  view.setUint32(16, centralOffset, true)
  view.setUint16(20, 0, true)
  return header
}

function crc32(data: Uint8Array) {
  const table = getCrcTable()
  let crc = 0xffffffff
  for (const byte of data) {
    crc = table[(crc ^ byte) & 0xff] ^ (crc >>> 8)
  }
  return (crc ^ 0xffffffff) >>> 0
}

function getCrcTable() {
  if (crcTable) return crcTable
  const table = new Uint32Array(256)
  for (let index = 0; index < 256; index += 1) {
    let value = index
    for (let bit = 0; bit < 8; bit += 1) {
      value = value & 1 ? 0xedb88320 ^ (value >>> 1) : value >>> 1
    }
    table[index] = value >>> 0
  }
  crcTable = table
  return table
}
