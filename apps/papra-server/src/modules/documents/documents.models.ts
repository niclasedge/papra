import type { PartialBy } from '@corentinth/chisels';
import type { DbSelectableDocument } from './documents.types';
import filenamify from 'filenamify';
import { getExtension } from '../shared/files/file-names';
import { omit } from '../shared/objects';
import { generateId } from '../shared/random/ids';
import { isDefined } from '../shared/utils';
import { ORIGINAL_DOCUMENTS_STORAGE_KEY } from './documents.constants';

export function joinStorageKeyParts(...parts: string[]) {
  return parts.join('/');
}

export function buildOriginalDocumentKey({
  documentId,
  organizationId,
  fileName,
}: {
  documentId: string;
  organizationId: string;
  fileName: string;
}) {
  const { extension } = getExtension({ fileName });

  const newFileName = isDefined(extension) ? `${documentId}.${extension}` : documentId;

  const originalDocumentStorageKey = joinStorageKeyParts(
    organizationId,
    ORIGINAL_DOCUMENTS_STORAGE_KEY,
    newFileName,
  );

  return { originalDocumentStorageKey };
}

export function generateDocumentId() {
  return generateId({ prefix: 'doc' });
}

export function isDocumentSizeLimitEnabled({ maxUploadSize }: { maxUploadSize: number }) {
  return maxUploadSize > 0;
}

export function formatDocumentForApi<T extends PartialBy<DbSelectableDocument, 'content'>>({
  document,
}: {
  document: T;
}) {
  return omit(document, [
    'fileEncryptionAlgorithm',
    'fileEncryptionKeyWrapped',
    'fileEncryptionKekVersion',
    'originalStorageKey',
  ]);
}

export function formatDocumentsForApi<T extends PartialBy<DbSelectableDocument, 'content'>>({
  documents,
}: {
  documents: T[];
}) {
  return documents.map((document) => formatDocumentForApi({ document }));
}

export function ensureSafeFileName(fileName: string) {
  return filenamify(fileName, { replacement: '_' });
}

// A leading YYYY-MM-DD in the file name (mail fetcher, scanner apps) is taken as the document date.
export function getDocumentDateFromFileName(fileName: string): Date | null {
  const match = /^(\d{4})-(\d{2})-(\d{2})(?!\d)/.exec(fileName);
  if (!match) {
    return null;
  }

  const [year, month, day] = match.slice(1).map(Number) as [number, number, number];
  // Local midnight, like the date picker of the client.
  const date = new Date(year, month - 1, day);

  return date.getMonth() === month - 1 && date.getDate() === day ? date : null;
}
